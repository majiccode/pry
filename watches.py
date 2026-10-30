"""Application-wide watches; call every public method on the creating thread.

WatchEngine(path) uses the supplied JSON filename (normally watches.json).
items is a read-only-by-convention list of dictionaries containing id, config,
status, observation, last_check (Unix seconds or None), and events. Events have
time and observation fields. add/update return the saved item; remove/pause/
resume return None. update replaces the complete configuration and restarts a
watch. resume starts a fresh timeout and baseline. poll returns whether visible
state changed; callers must poll regularly. No callbacks run on worker threads.

Disk thresholds mean percent used >= threshold. Process identity is captured
on the first asynchronous check. File watches scan existing content as well.
Loaded definitions are always paused. Evidence is bounded and control-escaped,
but is still untrusted text, not markup. Persistence errors propagate to callers.
"""

import copy
import errno
import json
import math
import os
from pathlib import Path
import queue
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid


def _evidence(value):
    text = str(value)
    text = ''.join(c if c in '\n\t' or ord(c) >= 32 and ord(c) != 127
                   else '\\x%02x' % ord(c) for c in text)
    return text.encode('utf-8', 'replace')[:4096].decode('utf-8', 'ignore')


def _number(value, name, minimum, maximum, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not minimum <= value <= maximum or not math.isfinite(value) or
            integer and not isinstance(value, int)):
        raise ValueError(f'{name} must be {"an integer" if integer else "a number"} '
                         f'from {minimum} to {maximum}.')
    return value


def _config(config):
    if not isinstance(config, dict):
        raise ValueError('Watch configuration must be a dictionary.')
    kind = config.get('kind')
    if not isinstance(kind, str) or kind not in ('port', 'process', 'disk', 'log'):
        raise ValueError('Watch kind must be port, process, disk, or log.')
    fields = {'port': {'host', 'port', 'condition'}, 'process': {'pid'},
              'disk': {'path', 'threshold'}, 'log': {'path', 'paths', 'text'}}[kind]
    if set(config) - (fields | {'kind', 'label', 'interval', 'timeout', 'repeat'}):
        raise ValueError('Unknown or inapplicable watch configuration field.')
    result = dict(kind=kind,
                  interval=_number(config.get('interval', 5), 'interval', 1, 3600),
                  timeout=_number(config.get('timeout', 3600), 'timeout', 1, 86400),
                  repeat=config.get('repeat', False))
    if not isinstance(result['repeat'], bool):
        raise ValueError('repeat must be a boolean.')
    for field in (fields & {'host', 'path', 'text'}) - ({'path'} if kind == 'log' else set()):
        value = config.get(field)
        limit = 253 if field == 'host' else 4096
        if (not isinstance(value, str) or not value or '\0' in value or
                len(value.encode('utf-8', 'replace')) > limit):
            raise ValueError(f'{field} must be nonempty text of at most {limit} bytes, without NUL.')
        if field == 'host' and (value != value.strip() or any(c.isspace() or ord(c) < 32 for c in value)):
            raise ValueError('host must not contain whitespace or control characters.')
        result[field] = value
    if kind == 'port':
        result['port'] = _number(config.get('port'), 'port', 1, 65535, True)
        condition = config.get('condition', 'reachable')
        if condition not in ('reachable', 'unreachable'):
            raise ValueError('Port condition must be reachable or unreachable.')
        result['condition'] = condition
        label = f'{result["host"]}:{result["port"]} {condition}'
    elif kind == 'process':
        result['pid'] = _number(config.get('pid'), 'pid', 1, 2147483647, True)
        label = f'PID {result["pid"]} exits'
    elif kind == 'disk':
        result['threshold'] = _number(config.get('threshold'), 'threshold', 0, 100)
        label = f'{result["path"]} used >= {result["threshold"]}%'
    else:
        # Existing saved log watches have a single path rather than paths.
        paths = config.get('paths', [config['path']] if 'path' in config else [])
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 32 or
                any(not isinstance(path, str) or not path.strip() or '\0' in path or
                    len(path.encode('utf-8')) > 4096 for path in paths)):
            raise ValueError('Choose between 1 and 32 explicit file paths.')
        result['paths'] = list(dict.fromkeys(str(Path(path).expanduser().absolute()) for path in paths))
        label = (result['paths'][0] if len(result['paths']) == 1 else f'{len(result["paths"])} files') + f' contains {result["text"]}'
    label = config.get('label', label)
    if not isinstance(label, str) or not label.strip():
        raise ValueError('label must be nonempty text.')
    result['label'] = _evidence(label)[:256]
    return result


class WatchEngine:
    """One instance should be shared by all application views."""

    def __init__(self, path):
        self.path = Path(path)
        self._owner = threading.get_ident()
        self._closed = False
        self._results = queue.Queue()
        self._pending = []
        self._runtime = {}
        self._jobs = {}
        self.items = []
        try:
            with self.path.open('rb') as handle:
                raw = handle.read(64 * 1024 * 1024 + 1)
        except FileNotFoundError:
            return
        if len(raw) > 64 * 1024 * 1024:
            raise ValueError('Watch file is too large.')
        loaded = json.loads(raw)
        if not isinstance(loaded, list) or len(loaded) > 100:
            raise ValueError('Watch file must contain at most 100 watches.')
        seen = set()
        for item in loaded:
            if not isinstance(item, dict):
                raise ValueError('Invalid saved watch.')
            identifier = item.get('id')
            if (not isinstance(identifier, str) or not identifier or
                    len(identifier) > 128 or identifier in seen):
                raise ValueError('Invalid or duplicate watch id.')
            seen.add(identifier)
            last_check = item.get('last_check')
            if last_check is not None:
                _number(last_check, 'last_check', 0, 1e15)
            observation = item.get('observation', '')
            events = item.get('events', [])
            if not isinstance(observation, str) or not isinstance(events, list):
                raise ValueError('Invalid saved watch observations.')
            saved_events = []
            for event in events[-20:]:
                if not isinstance(event, dict) or not isinstance(event.get('observation'), str):
                    raise ValueError('Invalid saved watch event.')
                stamp = _number(event.get('time'), 'event time', 0, 1e15)
                saved_events.append(dict(time=stamp, observation=_evidence(event['observation'])))
            self.items.append(dict(id=identifier, config=_config(item.get('config')),
                                   status='paused', observation=_evidence(observation),
                                   last_check=last_check, events=saved_events))

    def _guard(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError('WatchEngine must be used on its creating thread.')
        if self._closed:
            raise RuntimeError('WatchEngine is closed.')

    def _index(self, identifier):
        for index, item in enumerate(self.items):
            if item['id'] == identifier:
                return index
        raise ValueError(f'No watch with id {identifier!r}.')

    def _save(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=self.path.parent, delete=False) as handle:
                temporary = handle.name
                os.fchmod(handle.fileno(), 0o600)
                json.dump(items, handle, ensure_ascii=True, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def _activate(self, item):
        identifier = item['id']
        if identifier in self._jobs:
            self._jobs[identifier]['cancel'].set()
        now = time.monotonic()
        self._runtime[identifier] = dict(token=uuid.uuid4().hex, state={}, matched=False,
                                         due=now, deadline=now + item['config']['timeout'])

    def add(self, config):
        self._guard()
        if len(self.items) >= 100:
            raise ValueError('At most 100 watches are allowed.')
        item = dict(id=uuid.uuid4().hex, config=_config(config), status='running',
                    observation='Awaiting first check.', last_check=None, events=[])
        items = self.items + [item]
        self._save(items)
        self.items = items
        self._activate(item)
        return item

    def update(self, identifier, config):
        self._guard()
        index = self._index(identifier)
        item = dict(id=identifier, config=_config(config), status='running',
                    observation='Awaiting first check.', last_check=None,
                    events=copy.deepcopy(self.items[index]['events']))
        items = self.items.copy()
        items[index] = item
        self._save(items)
        self.items = items
        self._activate(item)
        return item

    def remove(self, identifier):
        self._guard()
        index = self._index(identifier)
        items = self.items[:index] + self.items[index + 1:]
        self._save(items)
        self.items = items
        self._runtime.pop(identifier, None)
        if identifier in self._jobs:
            self._jobs[identifier]['cancel'].set()

    def pause(self, identifier):
        self._guard()
        index = self._index(identifier)
        items = copy.deepcopy(self.items)
        items[index]['status'] = 'paused'
        self._save(items)
        self.items = items
        self._runtime.pop(identifier, None)
        if identifier in self._jobs:
            self._jobs[identifier]['cancel'].set()

    def resume(self, identifier):
        self._guard()
        index = self._index(identifier)
        if self.items[index]['status'] == 'running':
            return
        items = copy.deepcopy(self.items)
        items[index]['status'] = 'running'
        items[index]['observation'] = 'Awaiting first check.'
        self._save(items)
        self.items = items
        self._activate(items[index])

    def poll(self):
        self._guard()
        while True:
            try:
                self._pending.append(self._results.get_nowait())
            except queue.Empty:
                break
        items = copy.deepcopy(self.items)
        by_id = {item['id']: item for item in items}
        runtime = {key: value.copy() for key, value in self._runtime.items()}
        now = time.monotonic()
        cancel = []
        timed_out = []
        for job, finished, stamp, result in self._pending:
            identifier = job['id']
            item, state = by_id.get(identifier), runtime.get(identifier)
            if (item is None or state is None or item['status'] != 'running' or
                    state['token'] != job['token'] or job['timed_out'] or
                    finished > state['deadline']):
                continue
            if finished > job['deadline']:
                result = dict(error='Check deadline exceeded; no condition inferred.')
            item['last_check'] = stamp
            if 'error' in result:
                item['observation'] = _evidence('Error: ' + result['error'])
            else:
                item['observation'] = result['observation']
                state['state'] = result['state']
                matched = result['matched']
                if matched and not state['matched']:
                    item['events'] = (item['events'] + [dict(
                        time=stamp, observation=result['observation'])])[-20:]
                    if not item['config']['repeat']:
                        item['status'] = 'triggered'
                state['matched'] = matched
            state['due'] = now + item['config']['interval']
        for identifier, state in runtime.items():
            item = by_id[identifier]
            if item['status'] == 'running' and now >= state['deadline']:
                item['status'] = 'expired'
                item['observation'] = 'Watch timeout expired.'
                if identifier in self._jobs:
                    cancel.append(self._jobs[identifier])
        handled = {id(entry[0]) for entry in self._pending}
        for identifier, job in self._jobs.items():
            item, state = by_id.get(identifier), runtime.get(identifier)
            if (item is not None and state is not None and item['status'] == 'running'
                    and state['token'] == job['token'] and not job['timed_out']
                    and not job['handled'] and id(job) not in handled
                    and now >= job['deadline']):
                item['observation'] = 'Error: Check deadline exceeded; no condition inferred.'
                item['last_check'] = time.time()
                state['due'] = now + item['config']['interval']
                timed_out.append(job)
                cancel.append(job)
        changed = items != self.items
        if changed:
            self._save(items)
            self.items = items
        self._runtime = runtime
        for job, _, _, _ in self._pending:
            job['handled'] = True
        self._pending.clear()
        for job in timed_out:
            job['timed_out'] = True
        for job in cancel:
            job['cancel'].set()
        for identifier, job in list(self._jobs.items()):
            state = runtime.get(identifier)
            if (not job['thread'].is_alive() and (job['handled'] or job['timed_out']
                    or state is None or state['token'] != job['token'])):
                del self._jobs[identifier]
        for item in self.items:
            identifier = item['id']
            state = runtime.get(identifier)
            if (item['status'] != 'running' or state is None or identifier in self._jobs
                    or now < state['due'] or len(self._jobs) >= 100):
                continue
            job = dict(id=identifier, token=state['token'], cancel=threading.Event(),
                       deadline=min(now + 10, state['deadline']),
                       timed_out=False, handled=False)
            thread = threading.Thread(target=self._worker,
                                      args=(self._results, job, copy.deepcopy(item['config']),
                                            copy.deepcopy(state['state'])), daemon=True,
                                      name='watch-' + identifier[:12])
            job['thread'] = thread
            self._jobs[identifier] = job
            try:
                thread.start()
            except RuntimeError:
                del self._jobs[identifier]
                raise
        return changed

    @staticmethod
    def _worker(results, job, config, state):
        try:
            result = WatchEngine._check(config, state, job['cancel'], job['deadline'])
        except Exception as exc:
            result = dict(error=_evidence(f'{type(exc).__name__}: {exc}'))
        results.put((job, time.monotonic(), time.time(), result))

    @staticmethod
    def _check(config, state, cancel, deadline):
        def remaining():
            if cancel.is_set():
                raise RuntimeError('Check cancelled.')
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('Check deadline exceeded.')
            return min(2, left)

        remaining()
        kind = config['kind']
        if kind == 'port':
            # getaddrinfo cannot be interrupted portably. The daemon worker is
            # quarantined on timeout, rather than replaced by overlapping work.
            addresses = socket.getaddrinfo(config['host'], config['port'],
                                           type=socket.SOCK_STREAM)
            remaining()
            if not addresses:
                raise OSError('DNS returned no addresses.')
            reachable = False
            failures = []
            seen = set()
            for family, socktype, protocol, _, address in addresses:
                if address in seen:
                    continue
                seen.add(address)
                if len(seen) > 4:
                    break
                wait = remaining()
                try:
                    with socket.socket(family, socktype, protocol) as connection:
                        connection.settimeout(wait)
                        connection.connect(address)
                    reachable = True
                    break
                except OSError as exc:
                    if not isinstance(exc, TimeoutError) and exc.errno not in (
                            errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTUNREACH,
                            errno.ENETUNREACH, errno.EHOSTDOWN):
                        raise
                    failures.append(str(exc))
            remaining()
            matched = reachable == (config['condition'] == 'reachable')
            observation = f'{config["host"]}:{config["port"]}: ' + (
                'reachable' if reachable else 'unreachable (up to 4 resolved addresses): ' + '; '.join(failures))
        elif kind == 'process':
            pid = config['pid']
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                matched = True
                observation = f'PID {pid} exited (no longer exists).'
            else:
                completed = subprocess.run(['ps', '-p', str(pid), '-o', 'stat=,lstart='],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, timeout=remaining(),
                                           env={**os.environ, 'LC_ALL': 'C'}, check=False)
                remaining()
                fields = completed.stdout.decode('utf-8', 'replace').strip().split(None, 1)
                status, identity = (fields[0], fields[1]) if len(fields) == 2 else ('', '')
                if completed.returncode != 0 or not identity:
                    # A disappearance between kill and ps is an exit; all other
                    # ps/permission failures are errors, never notifications.
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        matched = True
                        observation = f'PID {pid} exited during identity check.'
                    else:
                        raise RuntimeError('Cannot read PID start time: ' +
                                           completed.stderr.decode('utf-8', 'replace'))
                else:
                    matched = status.startswith('Z') or bool(state.get('identity') and state['identity'] != identity)
                    observation = (f'PID {pid} exited (zombie awaiting reaping).' if status.startswith('Z') else
                                   f'PID {pid} was reused; original process exited.' if matched
                                   else f'PID {pid} running; started {identity}.')
                    if not state.get('identity'):
                        state['identity'] = identity
        elif kind == 'disk':
            usage = shutil.disk_usage(config['path'])
            remaining()
            if usage.total <= 0:
                raise ValueError('Filesystem has no measurable capacity.')
            percent = usage.used / usage.total * 100
            matched = percent >= config['threshold']
            observation = f'{config["path"]}: {percent:.2f}% used ({usage.used}/{usage.total} bytes).'
        else:
            reports, errors = [], []
            matched = False
            needle = config['text'].encode('utf-8')
            for path in config['paths']:
                remaining()
                try:
                    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    try:
                        info = os.fstat(descriptor)
                        if not stat.S_ISREG(info.st_mode):
                            raise ValueError('Path must be a regular file.')
                        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                        previous = state.get(path, {})
                        if previous.get('identity') != identity:
                            previous = dict(identity=identity, offset=0, tail=b'', match='')
                        if not previous['match']:
                            os.lseek(descriptor, previous['offset'], os.SEEK_SET)
                            chunk = os.read(descriptor, 64 * 1024)
                            combined = previous['tail'] + chunk
                            location = combined.find(needle)
                            if location >= 0:
                                previous['match'] = combined[max(0, location-80):location+len(needle)+80].decode('utf-8', 'replace')
                            previous['offset'] += len(chunk)
                            previous['tail'] = combined[-(len(needle)-1):] if len(needle) > 1 else b''
                        state[path] = previous
                        if previous['match']:
                            matched = True
                            reports.append(f'File match: {path}\n{previous["match"]}')
                        elif previous['offset'] < info.st_size:
                            reports.append(f'{path}: scanning existing content ({previous["offset"]}/{info.st_size} bytes).')
                        else:
                            reports.append(f'{path}: text not found.')
                    finally:
                        os.close(descriptor)
                except (OSError, ValueError) as exc:
                    state.pop(path, None)
                    errors.append(f'{path}: {exc}')
            if errors and not matched:
                raise OSError('; '.join(errors))
            observation = '\n'.join(reports + errors)
        return dict(matched=matched, observation=_evidence(observation), state=state)

    def close(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError('WatchEngine must be used on its creating thread.')
        if self._closed:
            return
        self._closed = True
        for job in self._jobs.values():
            job['cancel'].set()
        items = copy.deepcopy(self.items)
        for item in items:
            if item['status'] == 'running':
                item['status'] = 'paused'
        self.items = items
        self._runtime.clear()
        try:
            self._save(items)
        finally:
            deadline = time.monotonic() + 0.25
            for job in self._jobs.values():
                job['thread'].join(max(0, deadline - time.monotonic()))
