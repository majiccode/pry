"""Local-tool network discovery and reviewable SMB/NFS/SFTP operations."""
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, unquote, quote


class Network:
    def __init__(self, cancel=None):
        self.cancel = cancel or threading.Event()
        self.mac = __import__('sys').platform == 'darwin'

    def run(self, args, timeout=10, partial=False):
        if Path(args[0]).name == 'dns-sd':
            return self.run_discovery(args, timeout)
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            import time
            deadline = time.monotonic() + timeout
            stopped = False
            while process.poll() is None:
                if self.cancel.wait(.05) or time.monotonic() > deadline:
                    os.killpg(process.pid, signal.SIGKILL)
                    stopped = True
                    break
            process.wait()
            output.seek(0)
            text = output.read(512 * 1024).decode('utf-8', 'replace')
            if self.cancel.is_set():
                raise ValueError('Network operation cancelled.')
            if (stopped and not partial) or (process.returncode and not stopped):
                reason = f'Timed out after {timeout}s' if stopped else f'Exited with status {process.returncode}'
                raise ValueError(f'{shlex.join(args)}\n{reason}\n' + (text[:3000].strip() or 'The tool returned no diagnostic output.'))
            return text

    def run_discovery(self, args, timeout):
        """dns-sd buffers redirected stdout; a PTY makes each discovery line visible."""
        import pty
        import select
        import time
        master, slave = pty.openpty()
        process = None
        data = bytearray()
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=slave,
                                       stderr=slave, start_new_session=True)
            os.close(slave)
            slave = None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not self.cancel.is_set():
                if select.select([master], [], [], .05)[0]:
                    try:
                        chunk = os.read(master, 16384)
                    except OSError:
                        break
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) >= 512 * 1024:
                        break
                elif process.poll() is not None:
                    break
            if self.cancel.is_set():
                raise ValueError('Network operation cancelled.')
            if process.poll() not in (None, 0):
                raise ValueError(shlex.join(args) + '\n' + data.decode('utf-8', 'replace'))
            return data.decode('utf-8', 'replace')
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            if slave is not None:
                os.close(slave)
            os.close(master)

    def tools(self):
        names = ('dns-sd', 'avahi-browse', 'smbutil', 'smbclient', 'showmount', 'ssh', 'sftp', 'sshfs', 'mount_smbfs', 'mount.cifs', 'mount', 'umount')
        return [dict(label=f'{name}: {shutil.which(name) or "not installed"}') for name in names]

    def discover(self, emit):
        hosts = {}
        def add(host, protocol, detail=''):
            if host and not host.startswith('-'):
                key = (host, protocol)
                hosts[key] = dict(host=host, protocol=protocol, path='/', user='', port={'smb':445, 'nfs':2049, 'sftp':22}[protocol],
                                  discovery=detail or 'mDNS advertisement',
                                  label=f'{host} · {protocol.upper()} {detail}'.strip())
                emit(list(hosts.values()))
        notes = []
        # Advertisements are optional: also check hosts the OS already knows about.
        try:
            neighbors = self.neighbors()
            from concurrent.futures import ThreadPoolExecutor, as_completed
            def probe(host, protocol, port):
                if self.cancel.is_set():
                    return None
                try:
                    with socket.create_connection((host, port), timeout=.6):
                        return host, protocol, port
                except OSError:
                    return None
            with ThreadPoolExecutor(max_workers=16) as pool:
                jobs = [pool.submit(probe, host, protocol, port) for host in neighbors
                        for protocol, port in [('smb',445), ('nfs',2049), ('sftp',22)]]
                for job in as_completed(jobs):
                    result = job.result()
                    if result:
                        host, protocol, port = result
                        add(host, protocol, f'· known neighbor, TCP {port} open')
                        hosts[(host, protocol)]['port'] = port
            notes.append(f'Checked {len(neighbors)} known network neighbors for TCP 445/2049/22. An open port does not confirm login access or protocol support.')
        except (OSError, ValueError) as exc:
            notes.append('Network-neighbor discovery: ' + str(exc))
        if shutil.which('avahi-browse'):
            try:
                text = self.run(['avahi-browse', '-artp'], 6, partial=True)
                for line in text.splitlines():
                    parts = line.split(';')
                    if len(parts) >= 9 and parts[0] == '=':
                        service = parts[4]
                        protocol = {'_smb._tcp':'smb', '_nfs._tcp':'nfs', '_ssh._tcp':'sftp', '_sftp-ssh._tcp':'sftp'}.get(service)
                        if protocol:
                            add(parts[6], protocol)
            except (OSError, ValueError) as exc:
                notes.append(str(exc))
        elif shutil.which('dns-sd'):
            from concurrent.futures import ThreadPoolExecutor, as_completed
            services = [('_smb._tcp','smb'), ('_nfs._tcp','nfs'),
                        ('_ssh._tcp','sftp'), ('_sftp-ssh._tcp','sftp')]
            def browse(service, protocol):
                text = self.run(['dns-sd', '-B', service, 'local.'], 5, partial=True)
                instances = set()
                for line in text.splitlines():
                    match = re.search(r'\b' + re.escape(service) + r'\.?\s+(.+)$', line)
                    if match and ' Add ' in line:
                        instances.add(match[1].strip())
                found = []
                for instance in sorted(instances)[:32]:
                    if self.cancel.is_set():
                        raise ValueError('Network operation cancelled.')
                    resolved = self.run(['dns-sd', '-L', instance, service, 'local.'], 3, partial=True)
                    host = re.search(r'can be reached at (\S+):(\d+)', resolved)
                    if host:
                        found.append((host[1].rstrip('.'), protocol, int(host[2])))
                return found
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs = [pool.submit(browse, service, protocol) for service, protocol in services]
                for job in as_completed(jobs):
                    try:
                        for host, protocol, port in job.result():
                            add(host, protocol)
                            hosts[(host, protocol)]['port'] = port
                    except (OSError, ValueError) as exc:
                        notes.append(str(exc))

        else:
            notes.append('Install avahi-utils (Linux) for mDNS discovery. Manual hosts work without discovery.')
        return list(hosts.values()), '\n'.join(notes) or 'Discovery complete. Only advertised local-network services are shown. Add a host for unadvertised servers.'

    def neighbors(self):
        """Use existing IPv4 neighbor entries, never enumerate arbitrary subnets."""
        import ipaddress
        if shutil.which('ip') and not self.mac:
            text = self.run(['ip', '-4', 'neigh', 'show'])
            candidates = [line.split()[0] for line in text.splitlines() if line.split()]
        elif shutil.which('arp'):
            text = self.run(['arp', '-an'])
            candidates = re.findall(r'\((\d+\.\d+\.\d+\.\d+)\)', text)
        else:
            return []
        result = set()
        for candidate in candidates:
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if address.is_private and not (address.is_multicast or address.is_loopback or address.is_link_local or address.is_unspecified):
                result.add(str(address))
        return sorted(result)[:128]

    def location(self, value):
        if '://' not in value:
            value = 'smb://' + value
        parsed = urlsplit(value)
        if parsed.scheme not in ('smb', 'nfs', 'sftp', 'ssh') or not parsed.hostname:
            raise ValueError('Use smb://HOST/share, nfs://HOST/export, or sftp://USER@HOST:PORT/path.')
        if parsed.password is not None:
            raise ValueError('Enter credentials in the system authentication prompt, not in the address.')
        host = parsed.hostname
        user = unquote(parsed.username or '')
        path = unquote(parsed.path) or '/'
        if host.startswith('-') or any(c in host + user + path for c in '\n\r\0'):
            raise ValueError('Invalid network address.')
        if user.startswith('-') or any(c in host + user for c in ' ,/\\'):
            raise ValueError('Invalid hostname or username.')
        return dict(host=host, user=user, path=path, port=parsed.port or 22,
                    protocol='sftp' if parsed.scheme == 'ssh' else parsed.scheme,
                    label=value)

    def listing(self, item):
        host, protocol = item['host'], item['protocol']
        entries = []
        if protocol == 'smb':
            user = item.get('user', '')
            server = ('[' + host + ']' if ':' in host else host)
            login = (user + '@' if user else '') + server
            if shutil.which('smbutil'):
                args = ['smbutil', 'view', '-N', '//' + login]
            elif shutil.which('smbclient'):
                args = ['smbclient', '-g', '-L', host, '-N'] + (['-U', user] if user else [])
            else:
                raise ValueError('Share listing needs smbutil (macOS) or smbclient (Linux). You can enter a share address directly.')
            try:
                text = self.run(args)
            except ValueError as exc:
                raise ValueError(f'SMB share listing failed for {host}.\n{exc}\n\n'
                                 'This background query cannot prompt for a password. Discovery does not confirm share access.\n'
                                 'Choose Connect, enter your username if needed, then run the prepared command to authenticate (Finder on macOS).\n'
                                 'You can also Add server with smb://USER@HOST/share and Mount it directly.') from exc
            names = ([m[1] for line in text.splitlines() if (m := re.match(r'^\s*(.+?)\s+Disk\s', line))]
                     if Path(args[0]).name == 'smbutil' else
                     [line.split('|')[1] for line in text.splitlines() if line.startswith('Disk|')])
            for name in names:
                entries.append(dict(item, path='/' + name, label=name))
        elif protocol == 'nfs':
            if not shutil.which('showmount'):
                raise ValueError('Export listing needs showmount (nfs-common/nfs-utils). NFSv4-only servers may require a manually entered export.')
            text = self.run(['showmount', '-e', host])
            entries = [dict(item, path=line.split()[0], label=line) for line in text.splitlines() if line.startswith('/')]
        else:
            # Batch listing uses existing keys/agent and never prompts in a worker.
            args = ['sftp', '-oBatchMode=yes', '-oStrictHostKeyChecking=yes', '-P', str(item['port']),
                    (item['user'] + '@' if item['user'] else '') + host]
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as batch:
                escaped = item['path'].replace('\\', '\\\\').replace('"', '\\"')
                batch.write('ls -la "' + escaped + '"\n'); batch.flush()
                text = self.run(args[:1] + ['-b', batch.name] + args[1:])
            for line in text.splitlines():
                parts = line.split(None, 8)
                if len(parts) == 9 and parts[0].startswith('d') and parts[8] not in ('.','..'):
                    entries.append(dict(item, path=item['path'].rstrip('/') + '/' + parts[8], label=parts[8] + '/'))
        return entries, text

    def mounts(self):
        text = self.run(['mount'])
        entries = []
        for line in text.splitlines():
            if re.search(r'\b(smbfs|cifs|nfs\d*|fuse\.sshfs|osxfuse|macfuse)\b', line):
                match = re.match(r'(.+?) on (.+?) (?:type \S+ )?\(', line)
                if match:
                    path = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1],8)), match[2])
                    entries.append(dict(label=line, mountpoint=path, source=match[1]))
        return entries

    def mount_share(self, item):
        """Mount an SMB share using macOS authentication and return its real local path."""
        if item['protocol'] != 'smb' or item['path'] == '/':
            raise ValueError('Select an SMB share first.')
        if not self.mac:
            raise ValueError('Use Mount to choose a local directory on this platform.')
        host = item['host']
        for mounted in self.mounts():
            source = mounted.get('source', '')
            if source.startswith('//'):
                parsed = urlsplit('smb:' + source)
                if (parsed.hostname == host and unquote(parsed.path).rstrip('/') == item['path'].rstrip('/')
                        and Path(mounted['mountpoint']).is_dir()):
                    return mounted
        authority = (quote(item.get('user', ''), safe='') + '@' if item.get('user') else '')
        authority += '[' + host + ']' if ':' in host else host
        url = 'smb://' + authority + quote(item['path'], safe='/')
        # AppleScript receives the address as data; macOS owns passwords and Keychain.
        script = ('on run argv\n'
                  'set mountedVolume to mount volume (item 1 of argv)\n'
                  'return POSIX path of (mountedVolume as alias)\nend run')
        path = self.run(['osascript', '-e', script, url], timeout=180).strip()
        if not path or not Path(path).is_dir():
            raise ValueError('macOS did not return an accessible mounted folder. Refresh mounts to check the connection.')
        return dict(label=f'{url} → {path}', source='//' + authority + item['path'], mountpoint=path)

    def command(self, action, item, destination='', username=''):
        if action == 'unmount':
            args = ['umount', item['mountpoint']] if self.mac else ['sudo', 'umount', '--', item['mountpoint']]
            return shlex.join(args)
        host, protocol, path = item['host'], item['protocol'], item['path']
        user = username or item.get('user','')
        if user.startswith('-') or any(c in user for c in ' ,/\\\n\r\0:@'):
            raise ValueError('Use a plain username; system tools prompt separately for credentials.')
        remote_host = '[' + host + ']' if ':' in host else host
        login = (user + '@' if user else '') + remote_host
        if action == 'connect':
            if protocol == 'sftp':
                return shlex.join(['sftp', '-P', str(item['port']), login + ':' + path])
            if protocol == 'smb':
                if self.mac:
                    # Let Finder own credentials, Keychain and share selection.
                    # smbutil view is a share-enumeration query, not a connection UI.
                    authority = (quote(user, safe='') + '@' if user else '') + remote_host
                    return shlex.join(['open', 'smb://' + authority + quote(path, safe='/')])
                if shutil.which('smbutil'):
                    return shlex.join(['smbutil', 'view', '//' + login])
                if shutil.which('smbclient'):
                    return shlex.join((['smbclient', '-L', host] if path == '/' else ['smbclient', '//' + host + path])
                                      + (['-U',user] if user else []))
                raise ValueError('SMB Connect needs smbutil (macOS) or smbclient (Linux).')
            raise ValueError('Use Mount for this connection; or enter a full share address first.')
        if action == 'shell':
            return shlex.join(['ssh', '-p', str(item['port']), login])
        mountpoint = Path(destination).expanduser().absolute()
        if not mountpoint.is_dir():
            raise ValueError('Choose an existing local directory as the mount point.')
        if any(mountpoint.iterdir()):
            raise ValueError('Choose an empty local mount directory.')
        if protocol == 'smb':
            if path == '/':
                raise ValueError('Select a share or enter smb://HOST/share first.')
            if self.mac:
                args = ['mount_smbfs', '//' + login + path, str(mountpoint)]
            else:
                args = ['sudo','mount','-t','cifs','//' + host + path,str(mountpoint)]
                if user:
                    args += ['-o','username=' + user]
        elif protocol == 'nfs':
            args = ['sudo','mount','-t','nfs',remote_host + ':' + path,str(mountpoint)]
        else:
            if not shutil.which('sshfs'):
                raise ValueError('SFTP mounting requires SSHFS (and macFUSE on macOS). Connect works with the system sftp client.')
            args = ['sshfs','-p',str(item['port']), login + ':' + path,str(mountpoint)]
        return shlex.join(args)
