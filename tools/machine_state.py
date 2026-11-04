"""Explicit, read-only machine observations for the Changes dashboard.

capture_system(cancel, progress=None) returns independently comparable sources.
cancel is an Event-like object with is_set(); progress, if supplied, receives a
source ID before collection. No panel, thread, optional package or import-time
discovery is involved.

Compare only matching source IDs with complete=True on BOTH captures. Incomplete
records are observations, never evidence of removal. ``blocked`` names record
fields to exclude from comparison on either side (notably unavailable owners).
Display process removals and closed listeners as "No longer observed", not as
proof of exit/closure. Even complete captures are non-atomic, visibility-limited
observations, not an exact snapshot of the machine.
"""

import datetime
import ipaddress
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time


_OUTPUT_LIMIT = 4 * 1024 * 1024
_HELPERS = {'ps', 'lsof', 'ss', 'systemctl', 'brew', 'launchctl'}


def _run(argv, cancel, empty_exit_one=False):
    """Bound combined output/runtime; drain both pipes and reap the process group.

    Return stdout and diagnostic notes separately. Any note invalidates source
    completeness, including stderr from an otherwise successful invocation.
    """
    if cancel.is_set():
        return '', ['Capture cancelled.']
    env = dict(os.environ, LC_ALL='C', SYSTEMD_COLORS='0', SYSTEMD_URLIFY='0',
               SYSTEMD_PAGER='cat', HOMEBREW_NO_AUTO_UPDATE='1',
               HOMEBREW_NO_ANALYTICS='1', HOMEBREW_NO_ENV_HINTS='1')
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, env=env)
    except OSError as exc:
        return '', [f'{shlex.join(argv)}: {exc}']
    output, errors = bytearray(), bytearray()
    reason = ''
    deadline = time.monotonic() + 10
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, data in ((process.stdout, output), (process.stderr, errors)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, data)
            while selector.get_map() or process.poll() is None:
                if cancel.is_set():
                    reason = 'Capture cancelled.'
                    break
                if time.monotonic() >= deadline:
                    reason = 'Timed out after 10 seconds.'
                    break
                ready = selector.select(.05)
                if not selector.get_map():
                    time.sleep(.01)
                for key, _ in ready:
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = _OUTPUT_LIMIT - len(output) - len(errors)
                    key.data.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        reason = 'Output exceeded 4 MiB; command stopped (truncated).'
                        break
                if reason:
                    break
    except OSError as exc:
        reason = f'Could not read command output: {exc}'
    finally:
        # Descendants may retain pipes even after the immediate child exits.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            process.wait()
            process.stdout.close()
            process.stderr.close()
    notes = [reason] if reason else []
    diagnostic = errors.decode('utf-8', 'replace').strip()
    if diagnostic:
        notes.append('Command diagnostic: ' + diagnostic[:12000])
    empty_ok = empty_exit_one and process.returncode == 1 and not output and not errors
    if process.returncode and not reason and not empty_ok:
        notes.append(f'{shlex.join(argv)} exited with status {process.returncode}.')
    try:
        text = output.decode('utf-8')
    except UnicodeError:
        text = output.decode('utf-8', 'replace')
        notes.append('Output was not valid UTF-8; parsing may be incomplete.')
    return text, notes


def _source(category, notes=()):
    return dict(category=category, records={}, blocked=(), complete=False,
                notes=list(notes), captured_at=time.time())


def _collect(source, argv, cancel, parser, empty_exit_one=False):
    if not argv[0]:
        source['notes'].append('Required command unavailable (not installed or not on PATH).')
        return
    text, problems = _run(argv, cancel, empty_exit_one)
    source['notes'].extend(problems)
    try:
        parser(text, source)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        problems.append('Unrecognized or incomplete output: ' + str(exc))
        source['notes'].append(problems[-1])
    if cancel.is_set() and 'Capture cancelled.' not in problems:
        problems.append('Capture cancelled.')
        source['notes'].append(problems[-1])
    source['complete'] = not problems
    source['captured_at'] = time.time()


def _processes(text, source):
    if not text.strip():
        raise ValueError('ps returned no processes; this is not a valid empty process snapshot.')
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 7)
        if len(parts) != 8 or not parts[0].isdigit():
            raise ValueError('Malformed ps process row.')
        pid = int(parts[0])
        # C-locale lstart has five fields and second-resolution local wall time.
        started = datetime.datetime.strptime(' '.join(parts[1:6]), '%a %b %d %H:%M:%S %Y')
        command = parts[7]
        name = os.path.basename(command)
        if pid == os.getpid() or name in _HELPERS:
            continue
        record = dict(pid=pid, name=name, command=command,
                      started=started.isoformat(), user=parts[6])
        if os.path.isabs(command):
            record['executable'] = command
        key = f'{pid}@{record["started"]}'
        if key in source['records']:
            raise ValueError('Duplicate process identity.')
        source['records'][key] = record


def _endpoint(value, family=None):
    address, separator, port = value.rpartition(':')
    if not separator or not port.isdigit() or not 0 <= int(port) <= 65535:
        raise ValueError('Invalid TCP listening endpoint: ' + value)
    address = address.removeprefix('[').removesuffix(']')
    if address == '*':
        if family not in ('ipv4', 'ipv6'):
            raise ValueError('Ambiguous wildcard address without an address family.')
        address = '::' if family == 'ipv6' else '0.0.0.0'
    parsed = ipaddress.ip_address(address)
    actual_family = 'ipv6' if parsed.version == 6 else 'ipv4'
    if family and family != actual_family:
        raise ValueError('Conflicting listener address family.')
    return actual_family, str(parsed), int(port)


def _owner(pid, name, processes):
    record = processes.get(pid)
    if record:
        return dict(pid=pid, started=record['started'], name=record['name'])
    return dict(pid=pid, name=name)


def _listener(source, endpoint, owners):
    family, address, port = endpoint
    key = json.dumps(['tcp', family, address, port], separators=(',', ':'))
    record = source['records'].setdefault(
        key, dict(protocol='tcp', family=family, address=address, port=port, owners=[]))
    for owner in owners:
        if owner not in record['owners']:
            record['owners'].append(owner)
    record['owners'].sort(key=lambda item: (item['pid'], item.get('started', ''), item['name']))
    if not owners or any('started' not in owner for owner in owners):
        source['blocked'] = ('owners',)


def _ss(text, source, processes, family):
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 5)
        if len(parts) < 5 or parts[0] != 'LISTEN':
            raise ValueError('Malformed ss listener row.')
        endpoint = _endpoint(parts[3], family)
        detail = parts[5] if len(parts) == 6 else ''
        matches = re.findall(r'\("((?:[^"\\]|\\.)*)",pid=(\d+),fd=\d+\)', detail)
        owners = [_owner(int(pid), name, processes) for name, pid in matches]
        if detail and not matches:
            # Endpoint visibility is independent of permission to inspect owners.
            source['blocked'] = ('owners',)
        _listener(source, endpoint, owners)


def _lsof(text, source, processes):
    pid = name = family = endpoint = protocol = None
    have_file = False

    def finish():
        if not have_file:
            return
        if pid is None or not name or not endpoint or protocol != 'TCP' or family not in ('ipv4', 'ipv6'):
            raise ValueError('Incomplete lsof listener record.')
        _listener(source, _endpoint(endpoint, family), [_owner(pid, name, processes)])

    for line in text.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == 'p':
            finish()
            if not value.isdigit():
                raise ValueError('Invalid lsof PID.')
            pid, name = int(value), None
            have_file = False
        elif field == 'c':
            name = value
        elif field == 'f':
            finish()
            have_file = True
            family = endpoint = protocol = None
        elif field == 't':
            family = {'IPv4': 'ipv4', 'IPv6': 'ipv6'}.get(value)
        elif field == 'P':
            protocol = value
        elif field == 'n':
            endpoint = value
        else:
            raise ValueError('Unexpected lsof field: ' + field)
    finish()
    if text.strip() and not source['records']:
        raise ValueError('Nonempty lsof output contained no listening sockets.')


def _systemd(text, source, scope, enablement=False):
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 4)
        if not parts or not parts[0].endswith('.service'):
            raise ValueError('Malformed systemd service row.')
        name = parts[0]
        if name in source['records']:
            raise ValueError('Duplicate systemd service row.')
        record = dict(name=name, manager='systemd', scope=scope)
        if enablement:
            if len(parts) not in (2, 3):
                raise ValueError('Malformed systemd unit-file row.')
            # Preserve the actual state (static, masked, indirect, etc.), not a
            # guessed boolean. The optional third column is vendor preset.
            record['enabled'] = parts[1]
        else:
            if len(parts) < 4:
                raise ValueError('Malformed systemd loaded-unit row.')
            record.update(status='/'.join(parts[1:4]), running=parts[2] == 'active' and parts[3] == 'running')
        source['records'][name] = record


def _launchctl(text, source):
    lines = [line.split(None, 2) for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != ['PID', 'Status', 'Label']:
        raise ValueError('Missing launchctl list header.')
    for parts in lines[1:]:
        if len(parts) != 3:
            raise ValueError('Malformed launchctl job row.')
        pid, status, name = parts
        if (pid != '-' and not pid.isdigit()) or not re.fullmatch(r'-?\d+', status):
            raise ValueError('Invalid launchctl PID or exit status.')
        if name in source['records']:
            raise ValueError('Duplicate launchctl job.')
        running = pid != '-'
        record = dict(name=name, manager='launchd', scope='user',
                      status='running' if running else 'not running', running=running)
        if not running:
            record['exit_status'] = int(status)
        source['records'][name] = record


def _brew(text, source):
    records = json.loads(text)
    if not isinstance(records, list):
        raise ValueError('Expected a Homebrew services JSON list.')
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name']:
            raise ValueError('Invalid Homebrew service record.')
        name, status = item['name'], item.get('status')
        if not isinstance(status, str) or not status or name in source['records']:
            raise ValueError('Missing Homebrew status or duplicate service.')
        source['records'][name] = dict(name=name, status=status, running=status == 'started',
                                       manager='homebrew', scope='user')


def capture_system(cancel, progress=None):
    """Collect processes, TCP listeners and services, with no side effects.

    Each command has its own 10-second/4-MiB budget; cancellation is checked
    between commands and every 50 ms while draining output. Failed sources stay
    present, with complete=False and explanatory notes. Service status and
    enablement are separate sources, never implicitly joined across failures.
    """
    sources = {}
    platform = sys.platform
    identity = f'{platform}:uid={os.getuid()}:euid={os.geteuid()}'

    def add(category, backend, notes=()):
        source_id = f'{category}:{backend}:{identity}'
        source = _source(category, notes)
        sources[source_id] = source
        if progress:
            progress(source_id)
        return source

    if platform != 'darwin' and not platform.startswith('linux'):
        for category in ('processes', 'ports', 'services'):
            add(category, 'unsupported', ['Only macOS and Linux collectors are supported.'])
        return sources

    processes = add('processes', 'ps', [
        'Visible processes only; not atomic. Missing records mean "No longer observed", not definite exit.',
        'Identity is PID plus C-locale lstart (local time, second resolution); no CPU or memory counters.',
        'Collector PID and discovery helper command names are excluded to avoid transient noise.',
    ])
    user_field = 'user=' if platform == 'darwin' else 'user:256='
    _collect(processes, [shutil.which('ps'), '-ww', '-axo', f'pid=,lstart=,{user_field},comm='], cancel, _processes)
    by_pid = {record['pid']: record for record in processes['records'].values()}

    ss = shutil.which('ss') if platform.startswith('linux') else None
    if ss:
        # Separate family queries avoid ambiguous '*' endpoints and IPv6 heuristics.
        for flag, family in (('-4', 'ipv4'), ('-6', 'ipv6')):
            ports = add('ports', 'ss:' + family, [
                'Kernel-visible TCP LISTEN endpoints; owner visibility may be restricted.',
                'Missing listeners mean "No longer observed". Blocked owners are observational only.',
            ])
            _collect(ports, [ss, flag, '-H', '-ltnp'], cancel,
                     lambda text, source, family=family: _ss(text, source, by_pid, family))
    else:
        ports = add('ports', 'lsof', [
            'Only TCP LISTEN sockets visible to this user via lsof; other users may be hidden.',
            'Missing listeners mean "No longer observed", not definite closure.',
            'Blocked owners are observational only; no ownership change may be inferred.',
        ])
        _collect(ports, [shutil.which('lsof'), '-nP', '-iTCP', '-sTCP:LISTEN', '-FpcftPn'],
                 cancel, lambda text, source: _lsof(text, source, by_pid), empty_exit_one=True)

    if platform == 'darwin':
        services = add('services', 'launchd:user:loaded', [
            'Current-user loaded launchd jobs only; not every installed job. Enablement is not collected.',
        ])
        _collect(services, [shutil.which('launchctl'), 'list'], cancel, _launchctl)
        add('services', 'launchd:system:loaded', [
            'System launchd discovery is unavailable in this collector; no system services comparison.',
        ])
        brew = shutil.which('brew')
        if not brew:
            for candidate in ('/opt/homebrew/bin/brew', '/usr/local/bin/brew'):
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    brew = candidate
                    break
        services = add('services', 'homebrew:user:status', [
            'Homebrew services for the current user, without sudo; may overlap launchd.',
            'Status does not establish enablement; enablement is not collected.',
        ])
        _collect(services, [brew, 'services', 'list', '--json'], cancel, _brew)
    else:
        tool = shutil.which('systemctl')
        for scope in ('user', 'system'):
            argv = [tool] + (['--user'] if scope == 'user' else [])
            services = add('services', f'systemd:{scope}:loaded', [
                'Loaded systemd service units including inactive units (--all), not every installed unit file.',
                'Enablement is collected independently; a failure there does not invalidate loaded status.',
            ])
            _collect(services, argv + ['list-units', '--all', '--type=service', '--no-pager', '--plain', '--no-legend'],
                     cancel, lambda text, source, scope=scope: _systemd(text, source, scope))
            enabled = add('services', f'systemd:{scope}:enablement', [
                'Installed service unit-file enablement states, not loaded/running status.',
            ])
            _collect(enabled, argv + ['list-unit-files', '--type=service', '--no-pager', '--no-legend'],
                     cancel, lambda text, source, scope=scope: _systemd(text, source, scope, True))
    return sources
