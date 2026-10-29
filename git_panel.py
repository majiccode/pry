"""Independent terminal-native Git workspace: async jobs, views and labelled forms."""
import queue
import re
import threading
from typing import Any
from git_workspace import Repository


def visible(text):
    """Keep filenames and subprocess output from injecting terminal controls."""
    return ''.join(c if c == '\t' or c.isprintable() else f'\\x{ord(c):02x}' for c in str(text))


def diff_rows(patch, width, wrap, styles):
    """Render unified patches without altering the underlying operation output."""
    base, muted, text, accent, _ = styles
    added, removed = '\033[38;5;114m', '\033[38;5;203m'
    lines = patch.splitlines()
    if not any(line.startswith('diff --git ') for line in lines):
        return [row for line in lines for row in wrap(base + text + visible(line), width)]
    records = []
    old = new = None
    additions = deletions = files = 0
    for line in lines:
        if line.startswith('diff --git '):
            files += 1
            old = new = None
            records.extend([('', muted, '', ''), ('File: ' + line[11:], accent, '', '')])
        elif line.startswith('@@ '):
            match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)', line)
            if match:
                old, new = int(match[1]), int(match[2])
                records.append((f'Lines {old} -> {new}' + match[3], accent, '', ''))
            else:
                old = new = None
                records.append((line, muted, '', ''))
        elif old is not None and line.startswith(('+', '-', ' ')):
            marker, content = line[0], line[1:]
            before, after = str(old) if marker != '+' else '', str(new) if marker != '-' else ''
            records.append((marker + ' ' + content, added if marker == '+' else removed if marker == '-' else text,
                            before, after))
            old += marker != '+'
            new += marker != '-'
            additions += marker == '+'
            deletions += marker == '-'
        else:
            records.append((line, muted, '', ''))
    digits = max(3, max((len(number) for _, _, before, after in records for number in (before, after)), default=0))
    gutter_width = 2 * digits + 3
    show_gutter = width >= gutter_width + 12
    result = []
    for line in (f'{files} file(s) | +{additions} added | -{deletions} removed',
                 'Old / New lines | + added | - removed', ''):
        result.extend(wrap(base + accent + line, width))
    for content, color, before, after in records:
        numbered = bool(before or after)
        prefix = f'{before:>{digits}} {after:>{digits}} | ' if numbered and show_gutter else ''
        chunks = wrap(base + color + visible(content).expandtabs(4), max(1, width - len(prefix)))
        for index, chunk in enumerate(chunks):
            gutter = prefix if index == 0 else ' ' * len(prefix)
            result.append(base + muted + gutter + color + chunk)
    return result


class GitPanel:
    list_title = 'Files / branches / stashes'
    output_title = 'Diff / operation output'
    footer = 's stage · u unstage · c commit · r refresh · Esc close · Ctrl+C cancel job'
    tabs = [('changes', 'Changes'), ('staged', 'Ready to commit'), ('branches', 'Branches'), ('stashes', 'Stashes')]
    hints = {
        'stage': 'Include the selected file in your next commit.',
        'unstage': 'Remove the file from the next commit; keep its working copy.',
        'commit': 'Save the staged files as a commit on the current branch.',
        'fetch': 'Refresh remote branch information without changing your files.',
        'pull': 'Download changes; advance this branch only if no merge is needed.',
        'push': 'Upload commits on this branch; set tracking on the first push.',
        'sync': 'Fetch → fast-forward pull → push. Commit or stash local edits first.',
        'merge': 'Bring the selected branch into your current branch.',
        'stash': 'Save tracked and untracked edits locally and clean the working copy.',
        'apply': 'Restore the selected stash; keep the saved stash.',
        'pr': 'Push this branch, then create a GitHub PR or open the compare page.',
    }

    def __init__(self, directory, auto_refresh=True):
        self.directory = directory
        self.state = None
        self.tab: str = 'changes'
        self.index = self.top = self.offset = 0
        self.output = 'Loading repository…'
        self.message = 'Finding the repository for the current folder…'
        self.form: dict[str, Any] | None = None
        self.focus = 'list'
        self.action_index = 0
        self.busy = False
        self.cancel = threading.Event()
        self.results = queue.Queue()
        self.thread = None
        self.zones: list[tuple[int, int, int, str, Any]] = []
        self.output_rows = 0
        self.output_is_diff = False
        if auto_refresh:
            self.start('refresh')

    def entries(self):
        if not self.state:
            return []
        return self.state[{'changes': 'changed', 'staged': 'staged', 'branches': 'branches', 'stashes': 'stashes'}[self.tab]]

    def actions(self):
        common = [('Refresh', 'refresh'), ('Commit', 'commit'), ('Fetch', 'fetch'), ('Pull', 'pull'),
                  ('Push', 'push'), ('Sync', 'sync'), ('Create PR', 'pr'), ('New branch', 'create'), ('Save stash', 'stash')]
        specific = {'changes': [('View diff', 'diff'), ('Stage file', 'stage')],
                    'staged': [('View diff', 'diff'), ('Unstage file', 'unstage')],
                    'branches': [('Compare', 'compare'), ('Switch', 'switch'), ('Merge into current', 'merge')],
                    'stashes': [('View stash', 'stash_diff'), ('Apply stash', 'apply')]}[self.tab]
        if self.state and self.state['operation']:
            common += [('Continue operation', 'continue'), ('Abort operation', 'abort')]
        return specific + common + [('Explain output', 'explain'), ('Close', 'close')]

    def selected(self):
        entries = self.entries()
        return entries[self.index] if entries and self.index < len(entries) else None

    def entry_action(self):
        return {'branches': 'compare', 'stashes': 'stash_diff'}.get(self.tab, 'diff')

    def heading(self):
        s = self.state
        title = ('Branch: ' + (s['branch'] or 'detached HEAD') + (' · no commits yet' if not s['has_head'] else '')
                 + ' · ' + (s['upstream'] or 'no upstream') + f' · ↑{s["ahead"]} ↓{s["behind"]}'
                 + (f' · {len(s["conflicts"])} conflicts' if s['conflicts'] else '')
                 + (f' · {s["operation"]} in progress' if s['operation'] else '')) if s else 'Repository unavailable / loading'
        return 'GIT · ' + (s['root'] if s else self.directory), title

    def start(self, action, value='', entry=None):
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        self.message = 'Working · ' + action + ' · Ctrl+C cancels'
        directory = self.state['root'] if self.state else self.directory

        def worker():
            repo = None
            state = None
            result = ''
            error = False
            try:
                repo = Repository(directory, self.cancel)
                if action != 'refresh':
                    result = repo.perform(action, value, entry)
                state = repo.state()
            except (OSError, ValueError) as exc:
                result, error = str(exc), True
                if repo and not self.cancel.is_set():
                    try:
                        state = repo.state()
                    except (OSError, ValueError):
                        pass
            self.results.put((action, result, state, error))
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def poll(self):
        updated = False
        while not self.results.empty():
            action, output, state, error = self.results.get()
            self.busy = False
            if state:
                self.state = state
            self.index = max(0, min(self.index, len(self.entries()) - 1))
            if action != 'refresh' or error:
                self.output = output or 'Done.'
                self.offset = 0
            elif not self.state:
                self.output = output
            else:
                self.output = self.overview()
            if not self.state and error:
                self.output += '\n\nClose this panel and /cd to a folder in a Git repository.'
            self.message = ('Failed · ' if error else 'Finished · ') + action
            self.output_is_diff = not error and action in ('diff', 'compare', 'stash_diff')
            if error:
                self.message += ' · inspect output; interactive credentials/signing: use /run git …'
            updated = True
        return updated

    def overview(self):
        if not self.state:
            return 'This folder is not in a Git working tree. Close the panel and use /cd to choose a repository.'
        s = self.state
        return (f'{len(s["changed"])} changed · {len(s["staged"])} ready to commit · {len(s["stashes"])} stashes\n\n'
                'Select a file, then View diff. Stage includes the whole file in the next commit.\n'
                'Select an action with Tab / arrows / Enter, or click it.\n\n'
                'Sync: fetch → fast-forward pull → push. Diverged branches need a deliberate merge.\n'
                'Remote counts reflect the last fetch. Pull does not create an automatic merge.\n'
                'PR: uses existing GitHub CLI authentication, otherwise your browser login.\n\n'
                + (f'Operation in progress: {s["operation"]}. Resolve files, stage them, then Continue.\n'
                   'Abort returns to the pre-operation state and can discard conflict-resolution edits.\n' if s['operation'] else ''))

    def activate(self, action) -> tuple[str, str] | None:
        if action == 'close':
            if self.busy:
                self.message = 'Work is running. Ctrl+C cancels it; close after it finishes.'
                return None
            return ('close', '')
        if self.busy:
            return None
        if action == 'explain':
            return ('explain', 'Explain this Git state/output and suggest the next steps without executing anything.\n'
                    + self.overview() + '\n' + self.output[-16000:])
        entry = self.selected()
        if action in ('diff', 'stage', 'unstage', 'switch', 'merge', 'compare', 'stash_diff', 'apply') and not entry:
            self.message = 'Select a file, branch or stash first.'
            return None
        if action in ('diff', 'stage', 'unstage', 'compare', 'stash_diff'):
            self.start(action, (entry.get('name') or entry.get('ref') or '') if entry else '', entry)
            return None
        fields = {'commit': [('Commit message', '')], 'create': [('New branch name', '')],
                  'stash': [('Stash description', 'Saved from AI Terminal')],
                  'pr': [('Target branch', 'main'), ('PR title', '')]}.get(action, [])
        if action in ('switch', 'merge'):
            assert entry is not None
            value = entry['name']
        elif action == 'apply':
            assert entry is not None
            value = entry['ref']
        else:
            value = ''
        if action == 'refresh':
            self.start(action)
        else:
            summary = self.hints.get(action, action.replace('_', ' ').capitalize())
            if value:
                summary += ' · ' + value
            if action == 'abort':
                summary = 'Abort the current operation; conflict-resolution edits may be discarded.'
            self.form = dict(action=action, fields=fields, index=0, value=value, entry=entry, summary=summary)
        return None

    def event(self, event, value):
        if event == 'key' and value == '\x03':
            if self.busy:
                self.cancel.set()
                self.message = 'Cancelling Git command…'
            else:
                self.form = None
            return None
        if self.form:
            form = self.form
            fields = form['fields']
            if event == 'escape':
                self.form = None
            elif event == 'pane' or event == 'key' and value == '\t':
                if fields:
                    form['index'] = (form['index'] + 1) % len(fields)
            elif event == 'key' and value == '\r' or event == 'click' and any(a == 'submit' and y == value[1] and l <= value[0] <= r for y,l,r,a,_ in self.zones):
                if fields and any(not text.strip() for _, text in fields):
                    self.message = 'Fill in each field. Tab moves between fields.'
                else:
                    self.form = None
                    self.start(form['action'], '\n'.join(text for _, text in fields) if fields else form['value'], form['entry'])
            elif fields and event == 'key' and value in ('\x7f', '\x08'):
                name, text = fields[form['index']]
                fields[form['index']] = (name, text[:-1])
            elif fields and event in ('key', 'paste') and isinstance(value, str):
                name, text = fields[form['index']]
                fields[form['index']] = (name, (text + ''.join(c for c in value if c.isprintable()))[:4096])
            return None
        if event == 'escape':
            return self.activate('close')
        if event == 'pane' or event == 'key' and value == '\t':
            focus = ['list', 'actions', 'output']
            self.focus = focus[(focus.index(self.focus) + (-1 if event == 'pane' else 1)) % 3]
        elif event == 'click':
            x, y = value
            for row, left, right, action, argument in self.zones:
                if row == y and left <= x <= right:
                    if action == 'tab':
                        self.tab, self.index, self.top = argument, 0, 0
                        self.focus = 'list'
                    elif action == 'entry':
                        self.index, self.focus = argument, 'list'
                        return self.activate(self.entry_action())
                    else:
                        return self.activate(action)
                    break
        elif event in ('scroll', 'wheel', 'page', 'home', 'end', 'cursor'):
            if event == 'cursor':
                self.focus = 'actions'
                self.action_index = (self.action_index + value) % len(self.actions())
                return None
            amount = value[0] if event == 'wheel' else value
            amount = amount * (8 if event == 'page' else 1) if isinstance(amount, int) else 0
            if event == 'wheel' or self.focus == 'output':
                maximum = max(0, self.output_rows - 1)
                self.offset = 0 if event == 'home' else maximum if event == 'end' else max(0, min(maximum, self.offset + amount))
            elif self.focus == 'actions':
                self.action_index = (self.action_index + amount) % len(self.actions())
            else:
                maximum = len(self.entries()) - 1
                self.index = max(0, min(maximum, 0 if event == 'home' else maximum if event == 'end' else self.index + amount))
        elif event == 'key':
            if value in '1234':
                self.tab = self.tabs[int(value) - 1][0]
                self.index = self.top = 0
            elif value == '\r':
                return self.activate(self.actions()[self.action_index][1] if self.focus == 'actions' else self.entry_action())
            elif value in {'r', 's', 'u', 'c', 'f', 'p', 'P', 'y', 'b', 't', 'g', 'e'}:
                return self.activate({'r':'refresh', 's':'stage', 'u':'unstage', 'c':'commit', 'f':'fetch',
                                      'p':'pull', 'P':'push', 'y':'sync', 'b':'create', 't':'stash', 'g':'pr', 'e':'explain'}[value])
        return None

    def draw(self, cols, rows, wrap, styles, top=0):
        rows = max(0, rows - top)
        base, muted, text, accent, selected = styles
        self.zones = []
        output = [base + '\033[H\033[2J']

        def put(row, col, value, width, style=muted):
            if 1 <= row <= rows and 1 <= col <= cols and width > 0:
                output.append(f'\033[{row + top};{col}H' + wrap(base + style + visible(value), min(width, cols-col+1))[0])

        heading, title = self.heading()
        put(1, 1, heading, cols, text)
        put(2, 1, title, cols, accent)
        col = 1
        for number, (tab, label) in enumerate(self.tabs, 1):
            label = f' {number} {label} '
            if col + len(label) - 1 <= cols:
                put(3, col, label, len(label), selected if tab == self.tab else accent)
                self.zones.append((3, col, col+len(label)-1, 'tab', tab))
                col += len(label) + 1
        wide = cols >= 85
        left_width = min(40, cols // 3) if wide else cols
        action_rows = []
        col, row = 1, 4
        for index, (label, action) in enumerate(self.actions()):
            label = '[' + label + ']'
            if col + len(label) - 1 > cols and col > 1:
                row, col = row+1, 1
            action_rows.append((row, col, label, action, index))
            col += len(label) + 1
        start = row + 2
        available = max(0, rows - start - 3)
        list_height = available if wide else min(6, available // 2)
        for row, col, label, action, index in action_rows:
            if row < rows - 3:
                put(row, col, label, len(label), selected if self.focus == 'actions' and index == self.action_index else accent)
                self.zones.append((row,col,min(cols,col+len(label)-1),action,None))
        put(start-1, 1, self.list_title + (' · focused' if self.focus == 'list' else ''), left_width, accent)
        self.top = max(0, min(self.top, self.index))
        if self.index >= self.top + max(1, list_height):
            self.top = self.index - max(1, list_height) + 1
        for n, entry in enumerate(self.entries()[self.top:self.top+list_height]):
            index = self.top+n
            put(start+n,1, ('› ' if index == self.index else '  ') + entry['label'], left_width-1, selected if index == self.index else text)
            self.zones.append((start+n,1,left_width,'entry',index))
        if not self.entries():
            put(start,1,'No entries.',left_width)
        out_col, out_row = (left_width+2,start) if wide else (1,start+list_height+1)
        put(out_row-1,out_col,self.output_title + (' · focused' if self.focus == 'output' else ''),cols-out_col+1,accent)
        output_height = max(0, rows-out_row-2)
        # Wrap before scrolling so very long lines remain accessible.
        physical = []
        if self.output_is_diff:
            physical = diff_rows(self.output, max(1,cols-out_col+1), wrap, styles)
        else:
            for line in self.output.splitlines():
                physical.extend(wrap(text + visible(line), max(1,cols-out_col+1)))
        self.output_rows = len(physical)
        self.offset = min(self.offset,max(0,len(physical)-output_height))
        for n,line in enumerate(physical[self.offset:self.offset+output_height]):
            if out_row+n < rows-2:
                output.append(f'\033[{out_row+n+top};{out_col}H'+base+line)
        hint = self.hints.get(self.actions()[self.action_index][1], '') if self.focus == 'actions' else ''
        put(rows-2,1,self.message,cols,accent)
        put(rows-1,1,hint or '1–4 views · Tab focus · arrows select · Enter inspect/action · wheel scroll output',cols)
        put(rows,1,self.footer,cols)
        if self.form:
            form = self.form
            first = max(4, rows-len(form['fields'])-5)
            for row in range(first, rows+1):
                output.append(f'\033[{row+top};1H'+base+' '*cols)
            put(first,1,form['summary'],cols,text)
            for n,(name,value) in enumerate(form['fields']):
                put(first+n+1,1,('› ' if n == form['index'] else '  ')+name+': '+value,cols,selected if n == form['index'] else text)
            row = min(rows-1,first+len(form['fields'])+2)
            put(row,1,'[Apply]',7,accent)
            self.zones = [(row,1,7,'submit',None)]
            put(rows,1,'Enter applies · Tab next field · Esc cancels',cols)
        self.zones = [(row + top, left, right, action, argument)
                      for row, left, right, action, argument in self.zones]
        output.append('\033[?25l')
        return ''.join(output)
