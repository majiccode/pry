"""Explicit machine baselines with independent coverage-aware comparisons."""
import difflib
import json
import os
import queue
import threading
import time
from pathlib import Path

from dashboard_panel import DashboardPanel
from git_panel import visible
from machine_files import capture_files, describe_file_policy
from machine_state import capture_system
from tui_theme import SUCCESS, WARNING, ERROR, SECONDARY, ACCENT


CATEGORIES = ('files', 'processes', 'ports', 'services')
COLORS = {'added': SUCCESS, 'modified': WARNING, 'removed': ERROR}


def blocked_path(path, prefixes):
    """Use components so /tmp/a never masks /tmp/another; cost is path depth."""
    while True:
        if path in prefixes:
            return True
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


def file_modified(old, new):
    if (old.kind, old.mode) != (new.kind, new.mode):
        return True
    if old.kind == 'directory':
        return False  # Child edits change directory times; don't duplicate them.
    if old.kind == 'symlink':
        return old.target != new.target
    return ((old.size, old.mtime_ns) != (new.size, new.mtime_ns)
            or old.digest is not None and new.digest is not None and old.digest != new.digest)


def observation_label(category, status, key, record):
    if category == 'files':
        path = key
        home = str(Path.home())
        if path == home or path.startswith(home + os.sep):
            path = '~' + path[len(home):]
        return f'{status.upper():8} FILE  {visible(path)}'
    if category == 'processes':
        verb = {'added': 'STARTED', 'removed': 'NOT SEEN', 'modified': 'CHANGED'}[status]
        return f'{verb:8} PROCESS  {visible(record["name"])}  PID {record["pid"]}'
    if category == 'ports':
        verb = {'added': 'OPENED', 'removed': 'NOT SEEN', 'modified': 'OWNER'}[status]
        address = record['address']
        endpoint = f'[{address}]:{record["port"]}' if record['family'] == 'ipv6' else f'{address}:{record["port"]}'
        owners = ', '.join(f'{owner["name"]} ({owner["pid"]})' for owner in record['owners']) or 'owner unavailable'
        return f'{verb:8} TCP  {endpoint}  {visible(owners)}'
    return f'{status.upper():8} SERVICE  {visible(record["name"])}  {record["scope"]}/{record["manager"]}'


def compare_machine(before, after):
    """Partial files compare only outside both blocked sets; other sources fail closed."""
    changes, coverage = [], []
    for source_id in sorted(before.keys() | after.keys()):
        old_source, new_source = before.get(source_id), after.get(source_id)
        source = new_source or old_source
        category = source['category']
        comparable = bool(old_source is not None and new_source is not None)
        if category != 'files':
            comparable = comparable and old_source['complete'] and new_source['complete']
        blocked = set()
        if old_source:
            blocked.update(old_source['blocked'])
        if new_source:
            blocked.update(new_source['blocked'])
        compared = suppressed = count = 0
        if comparable:
            old_records, new_records = old_source['records'], new_source['records']
            for key in sorted(old_records.keys() | new_records.keys()):
                if category == 'files' and blocked_path(key, blocked):
                    suppressed += 1
                    continue
                compared += 1
                old, new = old_records.get(key), new_records.get(key)
                status = 'added' if old is None else 'removed' if new is None else None
                if old is not None and new is not None:
                    if category == 'files':
                        modified = file_modified(old, new)
                    else:
                        modified = ({k: v for k, v in old.items() if k not in blocked}
                                    != {k: v for k, v in new.items() if k not in blocked})
                    if modified:
                        status = 'modified'
                if status:
                    count += 1
                    label = observation_label(category, status, key, new or old)
                    if category == 'services':
                        previous = (old or {}).get('status', (old or {}).get('enabled', 'not observed'))
                        current = (new or {}).get('status', (new or {}).get('enabled', 'not observed'))
                        label += '  ' + visible(previous) + ' -> ' + visible(current)
                    changes.append(dict(category=category, status=status, key=key, source=source_id,
                                        before=old, after=new, color=COLORS[status],
                                        label=label))
        coverage.append(dict(source=source_id, category=category, comparable=comparable,
                             compared=compared, suppressed=suppressed, changes=count,
                             before=old_source, after=new_source))
    # Put potentially actionable machine events ahead of filesystem churn.
    priority = {'ports': 0, 'services': 1, 'processes': 2, 'files': 3}
    changes.sort(key=lambda c: (priority[c['category']], ('added', 'modified', 'removed').index(c['status']), c['label']))
    return changes, coverage


def change_details(change):
    category, key = change['category'], change['key']
    old, new = change['before'], change['after']
    lines = [change['label'], 'Source: ' + change['source'], '']
    if category != 'files':
        for label, record in [('Before', old), ('After', new)]:
            lines += [label + ':', json.dumps(record, indent=2, ensure_ascii=True) if record is not None else 'Not observed.']
        if category in ('ports', 'processes'):
            lines += ['', 'New / missing means observed / no longer observed, not proof of exact start, exit or closure.',
                      'Observations are visibility-limited and not atomic. See Coverage for source restrictions.']
        if category == 'ports':
            lines += ['Owners include captured process start identities where available.',
                      'If ownership is unavailable in either scan, owner differences are suppressed.']
        return '\n'.join(lines), False
    for label, record in [('Before', old), ('After', new)]:
        lines.append(label + ': ' + ('absent within observed scope' if record is None else
                     f'{record.kind} | {record.size:,} bytes | mode {record.mode:04o} | '
                     + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.mtime_ns / 1e9))))
        if record:
            if record.target is not None:
                lines.append('Link target: ' + visible(record.target))
            if record.kind == 'file':
                lines.append('SHA-256: ' + record.digest if record.digest else record.hash_note)
                if record.text_note:
                    lines.append(record.text_note)
    text_available = all(r is None or r.kind == 'file' and r.text is not None for r in (old, new))
    if text_available:
        a, b = json.dumps('a/' + key.lstrip('/')), json.dumps('b/' + key.lstrip('/'))
        lines += ['', 'diff --git ' + a + ' ' + b]
        lines.extend(difflib.unified_diff((old.text if old else '').splitlines(),
                                         (new.text if new else '').splitlines(),
                                         fromfile=a if old else '/dev/null',
                                         tofile=b if new else '/dev/null', lineterm=''))
        if old and new and old.text != new.text and old.text.splitlines() == new.text.splitlines():
            lines.append('Only line endings / final newline changed.')
    else:
        lines += ['', 'No captured text diff: binary, too large, budget-limited, directory, link or special file.']
    return '\n'.join(lines), text_available


class ChangesPanel(DashboardPanel):
    tabs = [('all', 'Overview'), ('files', 'Files'), ('processes', 'Processes'),
            ('ports', 'Ports'), ('services', 'Services'), ('coverage', 'Coverage')]
    list_title = 'Changes since machine baseline'
    output_title = 'Captured evidence'
    footer = 'b baseline | c compare | v coverage | 1-6 views | Ctrl+F find | Esc back/close'
    hints = {}

    def __init__(self, directory, auto_refresh=False):
        super().__init__(directory, auto_refresh=False)
        self.initial_directory = str(Path(directory).expanduser().resolve())
        self.scope, self.custom = 'machine', self.initial_directory
        self.baseline = self.comparison = None
        self.baseline_at = self.compared_at = None
        self.changes, self.coverage = [], []
        self.filter_status = 'all'
        self.progress = queue.Queue(maxsize=1)
        self.message = 'This Machine | choose Take baseline; Coverage lists roots and exclusions.'
        self.output = self.overview()
        self._update_cards()

    def _update_cards(self):
        self.cards = []
        for category in CATEGORIES:
            changes = [c for c in self.changes if c['category'] == category]
            counts = {status: sum(c['status'] == status for c in changes) for status in COLORS}
            sources = [c for c in self.coverage if c['category'] == category]
            usable = any(c['comparable'] and (c['compared'] or category != 'files') for c in sources)
            if self.baseline is None:
                value = 'Not captured'
            elif self.comparison is None:
                available = [s for s in self.baseline.values() if s['category'] == category]
                value = f'{sum(len(s["records"]) for s in available):,} observed'
                if any(not s['complete'] for s in available):
                    value += ' *'
            elif not usable:
                value = 'Not comparable'
            else:
                value = f'+{counts["added"]}  ~{counts["modified"]}  -{counts["removed"]}'
            self.cards.append((category.title(), value, WARNING if sources and not usable else
                               ERROR if counts['removed'] else WARNING if counts['modified'] else
                               SUCCESS if counts['added'] else SECONDARY))
        self.card_targets = list(CATEGORIES)
        self.empty_message = ('Take baseline first. Coverage shows what will be observed.' if self.baseline is None else
                              'Baseline captured. Choose Compare after making changes.' if self.comparison is None else
                              'No detected changes match this view. Check Coverage for observation gaps.')

    def heading(self):
        scope = 'This Machine' if self.scope == 'machine' else 'Custom files: ' + self.custom
        info = 'Read-only | files + processes + TCP listeners + services'
        if self.baseline_at:
            info = 'Baseline ' + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.baseline_at))
            if self.compared_at:
                info += ' | Compared ' + time.strftime('%H:%M:%S', time.localtime(self.compared_at))
            info += ' | + new  ~ changed  - no longer observed'
        if self.filter_status != 'all':
            info += ' | Filter: ' + self.filter_status
        return 'CHANGES | ' + scope, info

    def coverage_entries(self):
        if self.baseline is None:
            return [dict(label='FILE SCOPE  ' + note, detail=note, color=SECONDARY)
                    for note in describe_file_policy(self.scope, self.custom)] + [dict(
                        label='SYSTEM SCOPE  Processes, TCP LISTEN sockets, and services visible to this user.',
                        detail='No root or sudo. Unsupported / missing tools appear as unavailable sources after capture.', color=ACCENT)]
        rows = []
        for source_id in sorted(self.baseline.keys() | (self.comparison or {}).keys()):
            old, new = self.baseline.get(source_id), (self.comparison or {}).get(source_id)
            source = new or old
            detail = [source_id]
            for label, snapshot in [('Baseline', old), ('Comparison', new)]:
                if snapshot is None:
                    detail += [label + ': not captured']
                    continue
                detail += ['', label + ': ' + ('Observed within policy' if snapshot['complete'] else 'PARTIAL / UNAVAILABLE'),
                           f'{len(snapshot["records"]):,} observations | '
                           + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(snapshot['captured_at'])),
                           *snapshot['notes'], 'Blocked paths / fields:']
                detail.extend(snapshot['blocked'][:200])
                if len(snapshot['blocked']) > 200:
                    detail.append(f'{len(snapshot["blocked"]) - 200:,} additional blocked prefixes omitted from display.')
            coverage = next((c for c in self.coverage if c['source'] == source_id), None)
            if coverage:
                detail += ['', f'{coverage["compared"]:,} comparable observations; {coverage["suppressed"]:,} suppressed by blocked paths.',
                           'Comparison available.' if coverage['comparable'] else 'Not comparable; no changes inferred from this source.']
            partial = not source['complete'] or old is None or not old['complete']
            name = source_id.partition(':')[2]
            rows.append(dict(label=f'{"GAP" if partial else "OK":4} {source["category"].upper():9} '
                                   f'{len(source["records"]):6,} observed  {visible(name)}',
                             detail='\n'.join(str(line) for line in detail), color=WARNING if partial else SECONDARY))
        rows.append(dict(label='POLICY  File roots, exclusions and limits',
                         detail='\n'.join(describe_file_policy(self.scope, self.custom)), color=ACCENT))
        return rows

    def entries(self):
        if self.tab == 'coverage':
            return self.coverage_entries()
        return [c for c in self.changes if (self.tab == 'all' or c['category'] == self.tab)
                and (self.filter_status == 'all' or c['status'] == self.filter_status)]

    def actions(self):
        return [('Take baseline', 'baseline'), ('Compare', 'compare'), ('Details', 'details'),
                ('Coverage', 'coverage'), ('Status: ' + self.filter_status.title(), 'filter'),
                ('File scope', 'scope'), ('Close', 'close')]

    def entry_action(self):
        return 'details'

    def overview(self):
        return ('THIS MACHINE BASELINE\n\n'
                '1. Take baseline to capture independent file, process, TCP-listener and service observations.\n'
                '2. Make changes, then Compare. The baseline stays fixed until a confirmed replacement.\n'
                '3. Choose a category or Coverage; Enter shows before/after evidence.\n\n'
                'Machine file scope includes home and selected system configuration locations, NOT the whole disk.\n'
                'File scope can select a custom folder; system categories remain machine-wide.\n'
                'Known caches, dependency trees, credentials, mounts and protected paths are excluded or marked blocked.\n'
                'Partial files compare only outside blocked prefixes in BOTH scans. Failed system sources do not compare.\n'
                'New / missing processes and listeners mean newly observed / no longer observed, not proof of exact events.\n'
                'CPU/memory counters and directory timestamps are ignored. Port owners link to captured process identities.\n'
                'Only TCP listeners are captured, not every connection or UDP socket.\n'
                'No polling, restoration, deletion, sudo or automatic AI requests. Snapshots stay in memory.\n'
                'Captured text and commands may contain sensitive information. Nothing is automatically sent anywhere.\n'
                'Scans are not atomic; short-lived changes between captures are not recorded.')

    def activate(self, action):
        if action == 'close':
            if self.busy:
                self.message = 'Capture running. Ctrl+C cancels; close after it finishes.'
            else:
                return ('close', '')
        elif action == 'coverage':
            self.tab, self.index, self.top, self.focus = 'coverage', 0, 0, 'list'
        elif not self.busy:
            if action == 'details':
                entry = self.selected()
                if entry and 'detail' in entry:
                    self.output, self.output_is_diff = entry['detail'], False
                else:
                    self.output, self.output_is_diff = change_details(entry) if entry else (self.overview(), False)
                self.output_visible, self.offset, self.focus = True, 0, 'output'
            elif action == 'baseline' and self.baseline is not None:
                self.form = dict(action='reset_baseline', fields=[], index=0, value='', entry=None,
                                 summary='Replace ALL category baselines and clear results? Apply confirms.')
            elif action == 'filter':
                values = ['all', 'added', 'modified', 'removed']
                self.filter_status = values[(values.index(self.filter_status) + 1) % len(values)]
                self.index = self.top = 0
            elif action == 'scope':
                self.form = dict(action='scope', fields=[('File scope: machine OR folder path',
                                                         'machine' if self.scope == 'machine' else self.custom)],
                                 index=0, value='', entry=None,
                                 summary='Changes file scope; clears ALL baselines/results. System categories remain machine-wide.')
            elif action in ('baseline', 'compare', 'refresh'):
                self.start(action)
        return None

    def start(self, action, value='', entry=None):
        if self.busy:
            return
        if action == 'scope':
            if value.strip().lower() == 'machine':
                self.scope = 'machine'
            else:
                try:
                    path = (Path(self.initial_directory) / Path(value.strip()).expanduser()).resolve(strict=True)
                    if not path.is_dir():
                        raise ValueError('Choose an existing folder or type machine.')
                except (OSError, ValueError, RuntimeError) as exc:
                    self.message = 'Cannot change scope: ' + visible(exc)
                    return
                self.scope, self.custom = 'custom', str(path)
            self.baseline = self.comparison = self.baseline_at = self.compared_at = None
            self.changes, self.coverage = [], []
            self.tab, self.index, self.top, self.offset = 'coverage', 0, 0, 0
            self.output_visible = self.output_is_diff = False
            self.focus = 'list'
            self.output = self.overview()
            self.message = 'Scope changed. Review Coverage, then Take baseline.'
            self._update_cards()
            return
        if action not in ('baseline', 'reset_baseline', 'compare', 'refresh'):
            return
        if action == 'baseline' and self.baseline is not None:
            self.activate('baseline')
            return
        if action in ('compare', 'refresh') and self.baseline is None:
            self.message = 'Take baseline first; Compare never replaces a baseline implicitly.'
            return
        self.busy = True
        self.cancel.clear()
        self.message = 'Capturing machine observations | Ctrl+C cancels | previous results retained'
        scope, custom = self.scope, self.custom

        def progress(message):
            try:
                self.progress.get_nowait()
            except queue.Empty:
                pass
            self.progress.put_nowait(visible(message))

        def worker():
            try:
                sources = capture_system(self.cancel, progress)
                sources.update(capture_files(scope, custom, self.cancel, progress))
                if self.cancel.is_set():
                    raise ValueError('Capture cancelled; previous baseline/results retained.')
                self.results.put((action, sources, None))
            except Exception as exc:
                self.results.put((action, None, visible(exc)))
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def poll(self):
        updated = False
        try:
            self.message = 'Capturing | ' + self.progress.get_nowait() + ' | Ctrl+C cancels'
            updated = True
        except queue.Empty:
            pass
        try:
            action, snapshot, error = self.results.get_nowait()
        except queue.Empty:
            return updated
        self.busy = False
        if self.cancel.is_set() and error is None:
            error = 'Capture cancelled; previous baseline/results retained.'
        if error:
            self.message = 'Failed | ' + error
            self.output, self.output_visible, self.output_is_diff = self.message + '\n\n' + self.overview(), True, False
        else:
            if action in ('baseline', 'reset_baseline'):
                self.baseline, self.comparison = snapshot, None
                self.baseline_at, self.compared_at = time.time(), None
                self.changes, self.coverage = [], []
            else:
                self.changes, self.coverage = compare_machine(self.baseline, snapshot)
                self.comparison, self.compared_at = snapshot, time.time()
            gaps = sum(not s['complete'] for s in snapshot.values())
            self.index = self.top = self.offset = 0
            self.output, self.output_is_diff, self.output_visible = self.overview(), False, False
            self.focus = 'list'
            self.message = ('Baseline captured; choose Compare' if self.comparison is None else
                            f'{len(self.changes):,} changes | baseline unchanged') + f' | {gaps} source gaps; see Coverage'
            self._update_cards()
        return True

    def event(self, event, value):
        if not self.form and event == 'key' and value in ('b', 'c', 'v'):
            return self.activate({'b': 'baseline', 'c': 'compare', 'v': 'coverage'}[value])
        return super().event(event, value)
