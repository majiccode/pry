"""Read-only inspector state and asynchronous loading for embedded note blocks."""
from copy import deepcopy
import json
from pathlib import Path
import queue
import re
import threading
from uuid import uuid4

from interactive_output import recognize_output, load_view

ANSI = re.compile(r'\033(?:\[[0-?]*[ -/]*[@-~]|\][^\007]*(?:\007|\033\\)|[@-_])')


def read_text(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Not a regular text file')
    with path.open('rb') as handle:
        data = handle.read(128 * 1024 + 1)
    if len(data) > 128 * 1024 or any(b < 32 and b not in (9, 10, 13) for b in data):
        raise ValueError('Binary file or larger than 128 KiB')
    return data.decode('utf-8')


class NoteInspector:
    def __init__(self):
        self.blocks = []
        self.focus = None
        self.page = None
        self.index = 0
        self.offset = 0
        self.stack = []
        self.events = queue.Queue()
        self.pending = 0
        self.request = 0
        self.preview = None
        self.preview_loading = False
        self.preview_request = 0
        self.preview_index = None
        self.preview_cache = {}
        self.pane = 'list'
        self.action_index = 0

    def worker(self, function, callback):
        self.pending += 1
        def run():
            try:
                result = function()
            except Exception as exc:
                result = dict(title='Unavailable', text=str(exc), entries=[])
            self.events.put((callback, result))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        changed = False
        while not self.events.empty():
            callback, result = self.events.get_nowait()
            self.pending -= 1
            callback(result)
            changed = True
        return changed

    def add(self, start, end, command, stdout, cwd, exit_code, page=None, owner_user_id=None,
            block_type='capture'):
        block = dict(start=start, end=end, page=deepcopy(page), id=uuid4().hex,
                     source=dict(command=command, stdout=stdout, cwd=cwd, exit_code=exit_code),
                     owner_user_id=owner_user_id, block_type=block_type)
        self.blocks.append(block)
        if page is None:
            self.recognize(block)

    def recognize(self, block):
        identity = block['id']
        def recognized(result):
            current = next((b for b in self.blocks if b['id'] == identity), None)
            if current is not None:
                if result is None:
                    self.blocks.remove(current)
                else:
                    current['page'] = result
                    self.open(current)
        self.worker(lambda: recognize_output(**block['source']), recognized)

    def edit(self, start, end, length):
        retained = []
        for block in self.blocks:
            if end <= block['start']:
                shift = length - (end - start)
                block['start'] += shift
                block['end'] += shift
            elif start < block['end']:
                if self.focus is block:
                    self.close()
                continue
            retained.append(block)
        self.blocks = retained

    def open(self, block, index=0):
        if block.get('page') is None:
            return
        self.request += 1
        self.focus = block
        self.page = block['page']
        self.index = max(0, min(index, max(0, len(self.items()) - 1)))
        self.offset = 0
        self.stack = []
        self.preview = None
        self.preview_loading = False
        self.preview_request += 1
        self.preview_index = None
        self.preview_cache = {}
        self.pane = 'list'
        self.action_index = 0
        self.request_preview()

    def close(self):
        self.request += 1
        self.preview_request += 1
        self.focus = self.page = self.preview = None
        self.preview_loading = False
        self.preview_index = None
        self.preview_cache = {}
        self.pane = 'list'
        self.stack = []

    def items(self):
        page = self.page or {}
        return page.get('entries', []) + page.get('actions', [])

    def back(self):
        self.request += 1
        if self.stack:
            self.page, self.index, self.offset = self.stack.pop()
        elif self.page and self.page.get('up'):
            self.load(self.page['up'], remember=False)
            return
        else:
            return
        self.pane = 'list'
        self.request_preview()

    def preview_actions(self):
        return (self.preview or {}).get('actions', [])

    def request_preview(self):
        self.preview_request += 1
        request = self.preview_request
        self.preview = None
        self.preview_loading = False
        self.pane = 'list'
        self.action_index = 0
        items = self.items()
        if not items:
            return
        self.index = max(0, min(len(items) - 1, self.index))
        self.preview_index = self.index
        item = items[self.index]
        target = item.get('target')
        if not target:
            self.preview = dict(title=item.get('label', ''), text='No preview available.', entries=[])
            return
        if target.get('kind') == 'inspector_command':
            self.preview = dict(title=item.get('label', ''), entries=[],
                                text='Command: ' + target.get('command', '') +
                                     '\n\nPress Space to load this command for review.')
            return
        if target.get('kind') == 'url':
            self.preview = dict(title=item.get('label', ''), entries=[],
                                text='URL: ' + target.get('url', '') + '\n\nPress Space to open this link.')
            return
        if target.get('kind') == 'file_retrieve':
            self.preview = dict(title='Retrieve from peer', entries=[],
                                text=('File not available locally.\n'
                                      f'Shared by: {target.get("user_name", "unknown")} '
                                      f'({target.get("user_id", "unknown")})\n'
                                      f'Requested path: {target.get("path", "")}\n'
                                      'Activate to fetch it from the peer.'))
            return
        preview_target = dict(target, details=True) if target.get('kind') == 'path' else target
        key = json.dumps(preview_target, sort_keys=True, default=str)
        if key in self.preview_cache:
            self.preview = deepcopy(self.preview_cache[key])
            return
        self.preview_loading = True
        def loaded(page):
            if request == self.preview_request and self.focus is not None:
                page = page or dict(title='Unavailable', text='No preview available.', entries=[])
                self.preview = page
                self.preview_loading = False
                if len(self.preview_cache) >= 32:
                    self.preview_cache.clear()
                self.preview_cache[key] = deepcopy(page)
        self.worker(lambda: load_view(preview_target, read_text), loaded)

    def activate_preview(self, open_url):
        actions = self.preview_actions()
        if not actions:
            return
        target = actions[max(0, min(len(actions) - 1, self.action_index))].get('target')
        if not target:
            return
        if target.get('kind') == 'inspector_command':
            return target
        if target.get('kind') == 'url':
            open_url(target['url'])
        if target.get('kind') == 'file_retrieve':
            return target

    def enter(self, open_url):
        items = self.items()
        if not items:
            return
        target = items[self.index].get('target')
        if not target:
            return
        if target['kind'] == 'url':
            open_url(target['url'])
            return
        if target['kind'] == 'inspector_command':
            return target
        if target.get('kind') == 'file_retrieve':
            return target
        self.load(target)

    def load(self, target, remember=True):
        self.request += 1
        self.preview_request += 1
        self.preview = None
        self.preview_loading = False
        self.pane = 'list'
        request = self.request
        previous = self.page, self.index, self.offset
        def loaded(page):
            if request == self.request and self.focus is not None:
                if remember:
                    self.stack.append(previous)
                self.page = page or dict(title='Unavailable', text='No preview available', entries=[])
                self.index = self.offset = 0
                self.request_preview()
        self.worker(lambda: load_view(target, read_text), loaded)

    def event(self, event, value, open_url):
        if self.pane == 'preview':
            if event == 'escape' or event == 'key' and value in ('\x7f', '\x08', '\t'):
                self.pane = 'list'
                return
            if event in ('cursor', 'scroll'):
                actions = self.preview_actions()
                if actions:
                    step = -1 if value < 0 else 1
                    self.action_index = (self.action_index + step) % len(actions)
                return
            if event == 'key' and value == ' ':
                return self.activate_preview(open_url)
            return
        if event == 'escape':
            self.close()
        elif event == 'key' and value == '\t':
            if self.preview_actions():
                self.pane = 'preview'
                self.action_index = 0
            return
        elif event == 'cursor' and value < 0 or event == 'key' and value in ('\x7f', '\x08'):
            self.back()
        elif event == 'key' and value == ' ' or event == 'cursor' and value > 0:
            return self.enter(open_url)
        elif event == 'scroll':
            count = len(self.items())
            previous = self.index
            self.index = max(0, min(max(0, count - 1), self.index + value))
            if not count:
                self.offset = max(0, self.offset + value)
            elif self.index != previous:
                self.request_preview()
        elif event in ('page', 'wheel'):
            delta = value[0] if event == 'wheel' else value * 10
            self.offset = max(0, self.offset + delta)
            count = len(self.items())
            if count:
                self.index = min(count - 1, self.offset)
            self.request_preview()

    @staticmethod
    def _safe(value):
        value = ANSI.sub('', str(value)).expandtabs(4)
        return ''.join(char if char.isprintable() or char in ('\u200d', '\ufe0f')
                       else f'\\x{ord(char):02x}' for char in value)

    @staticmethod
    def _clip(value, width):
        if width <= 0:
            return ''
        return value if len(value) <= width else value[:max(0, width - 1)] + '…'

    def side_rows(self, result, width, height, wrap, items, entries, render):
        left_width = max(20, min(40, width // 2))
        right_width = max(8, width - left_width - 3)
        limit = max(1, height - len(result))
        total = len(items)
        self.offset = min(self.offset, max(0, total - limit))
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + limit:
            self.offset = self.index - limit + 1
        left = []
        for i in range(self.offset, min(total, self.offset + limit)):
            label = self._safe(items[i].get('label', ''))
            if i >= len(entries):
                label = 'Action · ' + label
            label = ('› ' if i == self.index else '  ') + label
            left.append((self._clip(label, left_width), i))
        right = []
        def add(text, action=None):
            for part in wrap(self._safe(text), right_width) or ['']:
                right.append((ANSI.sub('', part), action))
        if self.preview_loading:
            add('Loading…')
        else:
            preview = self.preview or {}
            if preview.get('title'):
                add(preview['title'])
            for line in str(preview.get('text', '')).splitlines():
                add(line)
            for item in preview.get('entries', []):
                add('· ' + item.get('label', ''))
            actions = self.preview_actions()
            if actions:
                add('')
                for i, item in enumerate(actions):
                    prefix = '› ' if self.pane == 'preview' and i == self.action_index else '  '
                    add(prefix + '[ ' + item.get('label', '') + ' ]', i)
            if not right:
                add('Select an entry to preview.')
        body = []
        count = min(limit, max(len(left), len(right)))
        for i in range(count):
            value = (left[i][0] if i < len(left) else '').ljust(left_width)
            index = left[i][1] if i < len(left) else None
            action = None
            if i < len(right):
                text, action = right[i]
                value = value.ljust(left_width) + ' │ ' + self._clip(text, right_width)
            entry = ({'left': index, 'right': action, 'left_width': left_width,
                      'selected': index is not None and index == self.index}
                     if index is not None or action is not None else None)
            body.append((value, entry))
        if not render:
            return [('', None)] * (len(result) + len(body))
        return result + body

    def rows(self, width, height, wrap, render=True):
        page = self.page or {}
        result = [('Inspect · ' + self._safe(page.get('title', '')), None),
                  ('↑↓ select · Space open · ← back/up · Esc edit · PgUp/PgDn scroll', None),
                   ('Esc saves this view into the output block.', None)]
        limit = max(1, height - len(result))
        entries = page.get('entries', [])
        items = entries + page.get('actions', [])
        if width >= 56 and items:
            result[1] = (('↑↓ select · Space open · Tab/click actions · ← back/up · Esc edit')
                         if self.preview_actions() else
                         ('↑↓ select · Space open · ← back/up · Esc edit'), None)
            return self.side_rows(result, width, height, wrap, items, entries, render)
        text = '\n'.join(self._safe(line) for line in page.get('text', '').splitlines())
        key = (text, width, wrap)
        if getattr(self, '_layout_key', None) != key:
            from terminal_layout import wrapped_height
            layout, count = [], 0
            for line in text.splitlines():
                size = wrapped_height(line, max(1, width - 2))
                layout.append((count, count + size, line))
                count += size
            self._layout_key, self._text_layout, self._text_height = key, layout, count
        total = len(items) + self._text_height
        self.offset = min(self.offset, max(0, total - limit))
        if items:
            if self.index < self.offset:
                self.offset = self.index
            elif self.index >= self.offset + limit:
                self.offset = self.index - limit + 1
        start, stop = self.offset, self.offset + limit
        if not render:
            return [('', None)] * (len(result) + max(0, min(limit, total - start)))
        body = []
        for i in range(start, min(stop, len(items))):
            label = self._safe(items[i]['label'])
            if i >= len(entries):
                label = 'Action · ' + label
            body.append((('› ' if i == self.index else '  ') + label, i))
        for first, last, line in self._text_layout:
            first, last = first + len(items), last + len(items)
            if first >= stop:
                break
            if last > start:
                body.extend((part, None) for part in wrap(line, max(1, width - 2))
                            [max(0, start - first):stop - first])
        return result + body
