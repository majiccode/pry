"""Read-only macOS/Linux service browser using GitPanel's draw/event contract.

ServicesPanel(directory) discovers asynchronously; call poll(), draw(), event().
Start/Stop/Restart return ('service_command', shlex.join(argv)) for the parent
to place in its /run editor. The panel never executes service mutations.
"""
import json
import os
import queue
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

from git_panel import GitPanel


class ServicesPanel(GitPanel):
    list_title = 'Services / status'
    output_title = 'Service details / diagnostics'
    footer = 'Enter inspect | s start / x stop / R restart: prepare only | r refresh | Esc hide details / close'
    hints = {
        'start': 'Prepare a start command for review in the parent /run editor; nothing runs here.',
        'stop': 'Prepare a stop command. launchd KeepAlive jobs may restart automatically.',
        'restart': 'Prepare a restart command for review; nothing runs here.',
    }

    def __init__(self, directory):
        super().__init__(directory, auto_refresh=False)
        self.platform = sys.platform
        self.uid = os.getuid()
        self.tools = {}
        if self.platform == 'darwin':
            brew = shutil.which('brew')
            if not brew and os.path.isfile('/opt/homebrew/bin/brew') and os.access('/opt/homebrew/bin/brew', os.X_OK):
                brew = '/opt/homebrew/bin/brew'
            self.tools = {'launchctl': shutil.which('launchctl'), 'brew': brew}
            self.tabs = [('user', 'User'), ('homebrew', 'Homebrew'), ('all', 'All'), ('tools', 'Tools')]
        else:
            self.tools = {'systemctl': shutil.which('systemctl') if self.platform.startswith('linux') else None}
            self.tabs = [('user', 'User'), ('system', 'System'), ('all', 'All'), ('tools', 'Tools')]
        self.services = []
        self.output_visible = False
        self.stacked_output = True
        self.diagnostics = []
        self.tab = 'user'
        self.output = self.overview()
        self.start('refresh')

    def heading(self):
        return 'SERVICES | ' + self.platform, f'{len(self.services)} services | read-only discovery; actions prepare commands only'

    def overview(self):
        return ('Select a service and press Enter to inspect it asynchronously. Refresh rediscovers all scopes.\n'
                'Start / Stop / Restart only prepare a shell-quoted command for the parent /run editor.\n'
                'Review it there before executing; authentication and authorization belong to the terminal.\n'
                'Linux: loaded systemd service units, including inactive units (--all), not every installed unit file.\n'
                'System commands use sudo; User commands use systemctl --user.\n'
                'macOS: launchctl list for the current user, plus Homebrew services when available.\n'
                'Inspect a launchd job first to verify gui/<uid>/<label>; no system launchd mutations are offered.\n'
                'launchctl stop sends SIGTERM; KeepAlive may restart the job. Restart uses kickstart -k.\n'
                'Homebrew commands use the current user, not sudo. Listings from different managers may overlap.\n'
                'Tools shows missing tools, permission failures, and discovery diagnostics.\n'
                'Each read-only subprocess has a 10-second timeout and a 512 KiB output limit.')

    def entries(self):
        if self.tab == 'tools':
            return ([dict(label=f'{name}: {path or "unavailable (not installed / not on PATH)"}')
                     for name, path in self.tools.items()]
                    + [dict(label=note) for note in self.diagnostics])
        return [entry for entry in self.services if self.tab == 'all' or entry['scope'] == self.tab]

    def actions(self):
        return [('Hide details', 'hide_details')] * self.output_visible + [('Inspect', 'inspect'), ('Start', 'start'), ('Stop', 'stop'),
                ('Restart', 'restart'), ('Refresh', 'refresh'), ('Close', 'close')]

    def entry_action(self):
        return 'inspect'

    def _run(self, argv):
        """Drain a pipe incrementally so both retained output and runtime are bounded."""
        if self.cancel.is_set():
            raise ValueError('Service operation cancelled.')
        env = dict(os.environ, LC_ALL='C', SYSTEMD_COLORS='0', SYSTEMD_URLIFY='0', SYSTEMD_PAGER='cat')
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True, env=env)
        data = bytearray()
        reason = ''
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 10
                while True:
                    if self.cancel.is_set():
                        reason = 'Service operation cancelled.'
                        break
                    if time.monotonic() >= deadline:
                        reason = 'Timed out after 10 seconds.'
                        break
                    ready = selector.select(.05)
                    if not ready:
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    remaining = 512 * 1024 - len(data)
                    data.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        reason = 'Output exceeded 512 KiB; command stopped (output truncated).'
                        break
                if not reason:
                    while process.poll() is None:
                        if self.cancel.wait(.05):
                            reason = 'Service operation cancelled.'
                            break
                        if time.monotonic() >= deadline:
                            reason = 'Timed out after 10 seconds.'
                            break
        finally:
            # Also terminate descendants that might keep a pipe open after exit.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
        text = data.decode('utf-8', 'replace')
        if reason or process.returncode:
            raise ValueError(shlex.join(argv) + '\n' + (reason or f'Exited with status {process.returncode}.')
                             + '\n' + (text[:12000].strip() or 'No diagnostic output; check tool availability and permissions.'))
        return text

    def _discover(self):
        services, notes = [], []
        if self.platform == 'darwin':
            sources = [('user', 'launchctl'), ('homebrew', 'brew')]
        elif self.platform.startswith('linux'):
            sources = [('user', 'systemctl'), ('system', 'systemctl')]
        else:
            return [], ['Unsupported platform: only macOS and Linux are supported.']
        for scope, manager in sources:
            if self.cancel.is_set():
                raise ValueError('Service discovery cancelled.')
            tool = self.tools.get(manager)
            if not tool:
                notes.append(f'{scope}: {manager} unavailable; install it or check PATH.')
                continue
            try:
                found = []
                if manager == 'systemctl':
                    argv = [tool] + (['--user'] if scope == 'user' else [])
                    text = self._run(argv + ['list-units', '--all', '--type=service', '--no-pager', '--plain', '--no-legend'])
                    for line in text.splitlines():
                        parts = line.split(None, 4)
                        if len(parts) < 4 or not parts[0].endswith('.service'):
                            continue
                        name, loaded, active, sub = parts[:4]
                        found.append(dict(name=name, status=f'{loaded}/{active}/{sub}',
                                          description=parts[4] if len(parts) > 4 else ''))
                elif manager == 'brew':
                    records = json.loads(self._run([tool, 'services', 'list', '--json']))
                    if not isinstance(records, list):
                        raise ValueError('Unexpected Homebrew services JSON (expected a list).')
                    for record in records:
                        if not isinstance(record, dict) or not isinstance(record.get('name'), str):
                            raise ValueError('Unexpected Homebrew service record.')
                        found.append(dict(name=record['name'], status=str(record.get('status', 'unknown')),
                                          metadata=record))
                else:
                    text = self._run([tool, 'list'])
                    for line in text.splitlines():
                        parts = line.split(None, 2)
                        if len(parts) != 3 or parts[0] == 'PID':
                            continue
                        pid, exit_status, name = parts
                        if pid != '-' and not pid.isdigit():
                            continue
                        found.append(dict(name=name, status=f'{"running PID " + pid if pid != "-" else "not running"}; exit {exit_status}'))
                for entry in found:
                    entry.update(scope=scope, manager=manager, tool=tool)
                    entry['label'] = f'{entry["name"]} | {entry["status"]} | {scope}/{manager}'
                services.extend(found)
                notes.append(f'{scope}/{manager}: {len(found)} services discovered.')
            except (OSError, ValueError) as exc:
                if self.cancel.is_set():
                    raise ValueError('Service discovery cancelled.') from exc
                notes.append(f'{scope}/{manager}: unavailable or discovery failed\n{exc}')
        services.sort(key=lambda entry: (entry['name'].casefold(), entry['scope']))
        return services, notes

    def start(self, action, value='', entry=None):
        if self.busy or action not in ('refresh', 'inspect'):
            return
        if action == 'inspect' and not entry:
            return
        self.busy = True
        self.cancel.clear()
        self.message = 'Working | ' + action + ' | Ctrl+C cancels; Esc cancels and closes'

        def worker():
            try:
                if action == 'refresh':
                    payload = self._discover()
                else:
                    name, tool, manager = entry['name'], entry['tool'], entry['manager']
                    if manager == 'systemctl':
                        argv = [tool] + (['--user'] if entry['scope'] == 'user' else [])
                        argv += ['show', '--no-pager', '--', name]
                    elif manager == 'brew':
                        argv = [tool, 'services', 'info', '--json', name]
                    else:
                        argv = [tool, 'print', f'gui/{self.uid}/{name}']
                    payload = shlex.join(argv) + '\n\n' + self._run(argv)
                self.results.put((action, payload, entry, False))
            except (OSError, ValueError) as exc:
                self.results.put((action, str(exc), entry, True))

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def poll(self):
        updated = False
        while True:
            try:
                action, payload, entry, error = self.results.get_nowait()
            except queue.Empty:
                break
            self.busy = False
            if action == 'refresh' and not error:
                self.services, self.diagnostics = payload
                self.output = self.overview() + '\n\n' + '\n\n'.join(self.diagnostics)
            else:
                self.output = payload
                self.output_visible = True
                if entry and entry['manager'] == 'launchctl':
                    entry['gui_verified'] = not error
                    self.output += '\n\nOnly verified current GUI jobs support prepared launchctl commands. KeepAlive may restart stopped jobs.'
            self.index = max(0, min(self.index, len(self.entries()) - 1))
            self.offset = 0
            self.message = ('Failed | inspect diagnostics | ' if error else 'Finished | ') + action
            updated = True
        return updated

    def activate(self, action):
        if action == 'hide_details':
            self.output_visible = False
            self.focus = 'list'
            self.action_index = min(self.action_index, len(self.actions()) - 1)
            self.message = 'Details hidden. Select a service and Inspect to show them.'
            return None
        if action == 'close':
            self.cancel.set()
            return ('close', '')
        if self.busy:
            return None
        if action == 'refresh':
            self.start('refresh')
            return None
        entry = self.selected()
        if not entry or 'manager' not in entry:
            self.message = 'Select a service in User, System, Homebrew, or All first.'
            if entry:
                self.output, self.offset = entry['label'], 0
            return None
        name = entry['name']
        if not name or name.startswith('-') or any(not c.isprintable() for c in name):
            self.message = 'Cannot use an empty, option-like, or control-containing service name.'
            return None
        if action == 'inspect':
            self.output_title = 'Details: ' + name
            self.output_visible = True
            self.output = 'Loading service details...'
            self.offset = 0
            self.start(action, entry=entry)
        elif action in ('start', 'stop', 'restart'):
            manager, tool = entry['manager'], entry['tool']
            if manager == 'systemctl':
                argv = ([tool, '--user'] if entry['scope'] == 'user' else ['sudo', tool]) + [action, '--', name]
            elif manager == 'brew':
                argv = [tool, 'services', action, name]
            else:
                if not entry.get('gui_verified'):
                    self.message = 'Inspect this job first to verify current GUI access; no system launchd commands are offered.'
                    return None
                target = f'gui/{self.uid}/{name}'
                argv = ([tool, 'kill', 'SIGTERM', target] if action == 'stop' else
                        [tool, 'kickstart'] + (['-k'] if action == 'restart' else []) + [target])
            command = shlex.join(argv)
            self.output = 'Prepared only, NOT executed. Review in the parent /run editor:\n\n' + command
            if manager == 'launchctl' and action == 'stop':
                self.output += '\n\nKeepAlive may restart this job after SIGTERM.'
            self.offset = 0
            self.message = 'Command prepared for review; nothing executed.'
            return ('service_command', command)
        return None

    def event(self, event, value):
        if event == 'escape' and self.output_visible and not self.busy:
            return self.activate('hide_details')
        if event == 'pane' or event == 'key' and value == '\t':
            if not self.output_visible:
                self.focus = 'actions' if self.focus == 'list' else 'list'
                return None
        if event == 'wheel' and not self.output_visible:
            return super().event('scroll', value[0])
        if event == 'key':
            if value == '\x03':
                if self.busy:
                    self.cancel.set()
                    self.message = 'Cancelling service inspection/discovery...'
                return None
            if value in ('r', 's', 'x', 'R'):
                return self.activate({'r': 'refresh', 's': 'start', 'x': 'stop', 'R': 'restart'}[value])
            # Do not let GitPanel's Git-specific shortcuts or substring numeric guard leak through.
            if value not in ('1', '2', '3', '4', '\t', '\r'):
                return None
        return super().event(event, value)
