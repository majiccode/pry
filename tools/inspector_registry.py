"""Command-family inspectors. Pages contain data and explicit, reviewable actions."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any
from file_preview import preview_metadata

REGISTRY = {}


def register(*names):
    def decorate(function):
        for name in names:
            REGISTRY[name] = function
        return function
    return decorate


def words(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
    lexer.whitespace_split = True
    result = list(lexer)
    if not result or any(set(word) <= set(';&|<>()') for word in result):
        return []
    return result


def page(title, text='', entries=None, actions=None, **extra) -> dict[str, Any]:
    return dict(title=title, text=text, entries=entries or [], actions=actions or [],
                note='Captured snapshot; drill-down details may query live state. Refresh is explicit.', **extra)


def entry(label, target) -> dict[str, Any]:
    return dict(label=label, target=target)


def action(label, args, interactive=False) -> dict[str, Any]:
    return dict(label=label, target=dict(kind='inspector_command', command=shlex.join(args), interactive=interactive))


def captured(title, text):
    return dict(kind='inspector_text', title=title, text=text)


def query(args, cwd):
    # Bound output on disk rather than accumulate unbounded pipe buffers.
    import tempfile
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ValueError('Inspector query timed out.')
        output.seek(0)
        text = output.read(512 * 1024).decode('utf-8', 'replace')
        if output.read(1):
            text += '\n[Output truncated at 512 KiB]'
        if process.returncode:
            raise ValueError(text[:2000] or 'Item is unavailable.')
        return text


def recognize(command, stdout, cwd):
    args = words(command)
    handler = REGISTRY.get(Path(args[0]).name) if args else None
    if handler:
        try:
            return handler(args, stdout, cwd)
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
            return page('Inspector unavailable', str(exc))


@register('lsof', 'ss', 'netstat')
def ports(args, text, cwd):
    if Path(args[0]).name == 'lsof' and not any(arg.startswith('-i') for arg in args):
        return page('Open files · captured', '', [entry(row, captured('Open file · captured', row))
                    for row in text.splitlines()[1:]])
    rows = text.splitlines()
    entries = []
    for row in rows:
        if not re.search(r'LISTEN|ESTAB|TCP|UDP|tcp|udp', row):
            continue
        pid = re.search(r'pid=(\d+)', row)
        fields = row.split()
        if Path(args[0]).name == 'lsof' and len(fields) > 1 and fields[1].isdigit():
            pid = fields[1]
        else:
            pid = pid.group(1) if pid else None
        target = dict(kind='process', pid=int(pid), cwd=cwd) if pid else captured('Connection · captured', row)
        entries.append(entry(('Listening · ' if re.search(r'LISTEN', row) else 'Connection · ') + row, target))
    entries.sort(key=lambda e: (not e['label'].startswith('Listening'), e['label']))
    return page('Ports & connections', '\n'.join(rows[:1]), entries,
                [action('Refresh listeners', ['lsof', '-nP', '-iTCP', '-sTCP:LISTEN'])] if shutil.which('lsof') else [])


@register('tail', 'journalctl')
def logs(args, text, cwd):
    lines = text.splitlines()
    entries = [entry(f'{i + 1:>5}  {line}', captured(f'Log entry {i + 1} · captured',
               '\n'.join(lines[max(0, i - 3):i + 5]))) for i, line in enumerate(lines)]
    return page('Logs · captured', '', entries, log=True)


@register('df', 'du')
def disks(args, text, cwd):
    entries = []
    for row in text.splitlines():
        if Path(args[0]).name == 'du':
            fields = row.split(None, 1)
            path = fields[1] if len(fields) == 2 else ''
        else:
            match = re.match(r'^\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(?:\S+\s+\S+\s+\S+\s+)?(/.*)$', row)
            path = match.group(1) if match else ''
        if path:
            location = os.path.abspath(os.path.join(cwd, path))
            entries.append(entry(row, dict(kind='disk_directory', path=location, cwd=cwd)))
    def size_key(item):
        value = item['label'].split()[0]
        match = re.match(r'([\d.]+)([KMGTPE]?)', value, re.I)
        return float(match.group(1)) * 1024 ** ('KMGTPE'.find(match.group(2).upper()) + 1 if match.group(2) else 0) if match else 0
    if Path(args[0]).name == 'du':
        entries.sort(key=size_key, reverse=True)
    return page('Disk usage · captured', text.splitlines()[0] if Path(args[0]).name == 'df' and text else '', entries)


@register('docker', 'podman')
def containers(args, text, cwd):
    tool = Path(args[0]).name
    if 'logs' in args[1:3]:
        return logs(args, text, cwd)
    if 'ps' not in args[1:3]:
        return None
    entries = []
    for row in text.splitlines():
        try:
            record = json.loads(row)
            identity = record.get('ID') or record.get('Id') or record.get('Name') or record.get('Names')
        except (ValueError, AttributeError):
            identity = row.split()[0] if row.split() else ''
        if identity and identity not in ('CONTAINER', 'NAME') and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', str(identity)):
            entries.append(entry(row, dict(kind='container', tool=tool, identity=str(identity), cwd=cwd)))
    return page('Containers · captured', '', entries,
                [action('List all containers', [tool, 'ps', '-a', '--no-trunc'])])


@register('systemctl', 'launchctl')
def services(args, text, cwd):
    tool = Path(args[0]).name
    entries = []
    for row in text.splitlines():
        fields = row.lstrip('● ').split()
        identity = next((f for f in fields if f.endswith(('.service', '.socket', '.timer'))), '') if tool == 'systemctl' else fields[-1] if len(fields) >= 3 and fields[0] != 'PID' else ''
        if identity:
            entries.append(entry(row, dict(kind='service', tool=tool, identity=identity, cwd=cwd,
                                          user='--user' in args)))
    return page('Services · captured', '', entries)


@register('rg', 'grep', 'find')
def searches(args, text, cwd):
    entries = []
    records = text.split('\0') if '\0' in text else text.splitlines()
    for row in records:
        match = re.match(r'^(.*?):(\d+)(?::\d+)?:\s?(.*)$', row)
        name, number = (match.group(1), int(match.group(2))) if match else (row, 1)
        # grep/rg omit the filename when searching one explicit file.
        single = re.match(r'^(\d+):(.*)$', row)
        if not match and single:
            candidates = [arg for arg in args[1:] if not arg.startswith('-') and (Path(cwd) / arg).is_file()]
            if len(candidates) == 1:
                name, number = candidates[0], int(single.group(1))
        path = Path(cwd) / name
        if path.exists():
            entries.append(entry(row, dict(kind='search_match', path=str(path.absolute()), line=number)))
    entries.sort(key=lambda e: (e['target']['path'], e['target']['line']))
    return page('Search results · grouped by file', '', entries)


def json_page(value, path='$'):
    entries = []
    if isinstance(value, dict):
        children = value.items()
    elif isinstance(value, list):
        children = enumerate(value)
    else:
        return page(path, json.dumps(value, ensure_ascii=False))
    for key, child in children:
        child_path = path + '[' + json.dumps(key, ensure_ascii=False) + ']'
        preview = f'{len(child)} items' if isinstance(child, (dict, list)) else json.dumps(child, ensure_ascii=False)
        entries.append(entry(f'{key}: {preview}', dict(kind='json_value', value=child, path=child_path)))
    return page(path, '', entries)


@register('curl')
def http(args, text, cwd):
    headers = []
    body = text
    while re.match(r'^HTTP/\S+\s+\d+', body):
        match = re.search(r'\r?\n\r?\n', body)
        if not match:
            break
        headers.append(body[:match.start()])
        body = body[match.end():]
    entries = [entry('Headers · ' + h.splitlines()[0], captured('HTTP headers · captured', h)) for h in headers]
    try:
        value = json.loads(body)
        entries.append(entry('Body · JSON tree', dict(kind='json_value', value=value, path='$')))
    except ValueError:
        entries.append(entry('Body · text', captured('HTTP body · captured', body)))
    return page('HTTP response · captured', '\n'.join(h.splitlines()[0] for h in headers) or
                'Status/headers/timing available only when captured by curl (-i / -w).', entries)


@register('tar', 'unzip')
def archives(args, text, cwd):
    archive = None
    if Path(args[0]).name == 'unzip' and '-l' in args:
        index = args.index('-l') + 1
        archive = args[index] if index < len(args) else None
    elif Path(args[0]).name == 'tar':
        if '--file' in args and '--list' in args:
            index = args.index('--file') + 1
            archive = args[index] if index < len(args) else None
        for i, arg in enumerate(args[1:], 1):
            if arg in ('--file', '-f') or (arg.startswith('-') or i == 1) and 'f' in arg and 't' in ''.join(args[1:i + 1]):
                archive = args[i + 1] if i + 1 < len(args) else None
                break
    if archive:
        return archive_page(str((Path(cwd) / archive).absolute()))


def archive_page(path, prefix=''):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()[:10000]
    else:
        with tarfile.open(path) as archive:
            names = []
            for i, member in enumerate(archive):
                if i == 10000:
                    break
                names.append(member.name)
    children = {}
    for name in names:
        if not name.startswith(prefix):
            continue
        tail = name[len(prefix):]
        if not tail:
            continue
        child = tail.split('/')[0]
        full = prefix + child
        folder = '/' in tail
        children[full] = children.get(full, False) or folder
    return page(Path(path).name + ':' + (prefix or '/'), 'Archive listing · live from local archive (up to 10,000 entries).',
                [entry(name[len(prefix):] + ('/' if folder else ''),
                       dict(kind='archive_entry', path=path, name=name + '/' if folder else name, folder=folder))
                 for name, folder in sorted(children.items())],
                actions=[dict(label='Extract archive', target=dict(kind='inspector_command',
                    command=shlex.join([sys.executable,
                        str(Path(__file__).with_name('archive_extract.py')), path]) + ' {{destination}}',
                    interactive=False))], archive_path=path)


def is_archive(path):
    return str(path).lower().endswith(('.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz'))


def load(target, read_text):
    kind = target['kind']
    cwd = target.get('cwd', str(Path.cwd()))
    if kind == 'inspector_text':
        return page(target['title'], target['text'])
    if kind == 'json_value':
        return json_page(target['value'], target['path'])
    if kind == 'disk_directory':
        path = Path(target['path'])
        text = query(['du', '-k', '-d', '1', str(path)], cwd) if path.is_dir() else str(path)
        result = disks(['du'], text, cwd)
        result.update(title=str(path) + ' · live KiB', attach=str(path),
                      actions=[action('Open folder', ['ls', '-al', str(path)])])
        return result
    if kind == 'search_match':
        path = Path(target['path'])
        if path.is_dir():
            return None
        if path.is_symlink():
            raise ValueError('Symlink previews are unavailable.')
        text = read_text(path)
        lines = text.splitlines()
        number = target['line']
        start = max(0, number - 6)
        return page(f'{path}:{number} · live', '\n'.join(lines[start:number + 5]),
                    file_preview=preview_metadata(path, text, start=start + 1, highlight=number),
                    attach=str(path), actions=[action('Browse folder', ['ls', '-al', str(path.parent)])])
    if kind == 'container':
        tool, identity = target['tool'], target['identity']
        text = query([tool, 'inspect', identity], cwd)
        return page(identity + ' · live', text, actions=[
            action('Logs', [tool, 'logs', '--tail', '200', identity]),
            action('Live logs', [tool, 'logs', '-f', '--tail', '100', identity]),
            action('Resources', [tool, 'stats', '--no-stream', identity]),
            action('Shell', [tool, 'exec', '-it', identity, 'sh'], True)] +
            [action(verb.title(), [tool, verb, identity]) for verb in ('start', 'stop', 'restart')])
    if kind == 'service':
        tool, identity = target['tool'], target['identity']
        if tool == 'systemctl':
            base = [tool] + (['--user'] if target.get('user') else [])
            text = query(base + ['show', '--no-pager', identity], cwd)
            actions = [action(verb.title(), base + [verb, identity]) for verb in ('start', 'stop', 'restart')]
            actions.append(action('Logs', ['journalctl'] + (['--user'] if target.get('user') else []) + ['-u', identity, '-n', '100', '--no-pager']))
        else:
            domain = 'gui/' + str(os.getuid()) + '/' + identity
            text = query([tool, 'print', domain], cwd)
            actions = [action('Start', [tool, 'kickstart', domain]), action('Restart', [tool, 'kickstart', '-k', domain]),
                       action('Stop', [tool, 'kill', 'SIGTERM', domain])]
        return page(identity + ' · live', text, actions=actions)
    if kind == 'archive_entry':
        path, name = target['path'], target['name']
        if target['folder']:
            return archive_page(path, name)
        with zipfile.ZipFile(path) if zipfile.is_zipfile(path) else tarfile.open(path) as archive:
            if isinstance(archive, zipfile.ZipFile):
                info = archive.getinfo(name)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Symlink preview unavailable.')
                handle = archive.open(name)
            else:
                info = archive.getmember(name)
                if not info.isfile():
                    raise ValueError('Preview only supports regular archive files.')
                handle = archive.extractfile(info)
            if handle is None:
                raise ValueError('Archive entry cannot be previewed.')
            with handle:
                data = handle.read(65537)
        text = '[Binary file]' if b'\0' in data else data[:65536].decode('utf-8', 'replace')
        if len(data) > 65536:
            text += '\n[Preview truncated]'
        # The command editor reviews the extraction destination before execution.
        destination = '{{destination}}'
        safe = not Path(name).is_absolute() and '..' not in Path(name).parts and not name.startswith('-')
        actions = []
        if safe:
            if zipfile.is_zipfile(path):
                actions = [action('Extract entry…', ['unzip', '-n', path, name, '-d', destination])]
            else:
                actions = [action('Extract entry…', ['tar', '-xf', path, '-C', destination, '--', name])]
        return page(name + ' · archive preview', text, actions=actions)
    return None


def process_details(pid, cwd):
    text = query(['ps', '-p', str(pid), '-o', 'pid,ppid,user,etime,%cpu,%mem,args'], cwd)
    rows = query(['ps', '-axo', 'pid=,ppid=,%cpu=,%mem=,args='], cwd)
    entries = []
    parent = None
    for row in rows.splitlines():
        fields = row.split(None, 4)
        if len(fields) < 4:
            continue
        if fields[0] == str(pid):
            parent = fields[1]
        if fields[1] == str(pid):
            entries.append(entry('Child · ' + row.strip(), dict(kind='process', pid=int(fields[0]), cwd=cwd)))
    if parent and int(parent) > 0:
        entries.insert(0, entry('Parent · ' + parent, dict(kind='process', pid=int(parent), cwd=cwd)))
    if Path('/proc', str(pid), 'cwd').exists():
        text += '\nWorking directory: ' + os.readlink(f'/proc/{pid}/cwd')
        text += '\nExecutable: ' + os.readlink(f'/proc/{pid}/exe')
    elif shutil.which('lsof'):
        try:
            text += '\nExecutable / working directory (live):\n' + query(['lsof', '-a', '-p', str(pid), '-d', 'cwd,txt'], cwd)
        except ValueError as exc:
            text += '\nExecutable / working directory unavailable: ' + str(exc)
    actions = [action('Open files', ['lsof', '-p', str(pid)]),
               action('Ports', ['lsof', '-nP', '-a', '-p', str(pid), '-i']),
               action('Terminate (SIGTERM)', ['kill', '-TERM', str(pid)])]
    return page(f'Process {pid} · live', text, entries, actions)
