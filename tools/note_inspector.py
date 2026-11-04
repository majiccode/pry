"""Read-only inspector state and asynchronous loading for embedded note blocks."""
from copy import deepcopy
from pathlib import Path
import queue
import threading
from uuid import uuid4

from interactive_output import recognize_output, load_view


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

    def add(self, start, end, command, stdout, cwd, exit_code, page=None):
        block = dict(start=start, end=end, page=deepcopy(page), id=uuid4().hex,
                     source=dict(command=command, stdout=stdout, cwd=cwd, exit_code=exit_code))
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

    def open(self, block):
        if block.get('page') is None:
            return
        self.request += 1
        self.focus = block
        self.page = block['page']
        self.index = self.offset = 0
        self.stack = []

    def close(self):
        self.request += 1
        self.focus = self.page = None
        self.stack = []

    def back(self):
        self.request += 1
        if self.stack:
            self.page, self.index, self.offset = self.stack.pop()
        elif self.page and self.page.get('up'):
            self.load(self.page['up'], remember=False)

    def enter(self, open_url):
        entries = self.page.get('entries', []) if self.page else []
        if not entries:
            return
        target = entries[self.index].get('target')
        if not target:
            return
        if target['kind'] == 'url':
            open_url(target['url'])
            return
        # Command actions belong to chat's explicit execution flow, not navigation.
        if target['kind'] == 'inspector_command':
            return
        self.load(target)

    def load(self, target, remember=True):
        self.request += 1
        request = self.request
        previous = self.page, self.index, self.offset
        def loaded(page):
            if request == self.request and self.focus is not None:
                if remember:
                    self.stack.append(previous)
                self.page = page or dict(title='Unavailable', text='No preview available', entries=[])
                self.index = self.offset = 0
        self.worker(lambda: load_view(target, read_text), loaded)

    def event(self, event, value, open_url):
        if event == 'escape':
            self.close()
        elif event == 'cursor' and value < 0 or event == 'key' and value in ('\x7f', '\x08'):
            self.back()
        elif event == 'newline' or event == 'key' and value in ('\r', '\n') or event == 'cursor' and value > 0:
            self.enter(open_url)
        elif event == 'scroll':
            count = len((self.page or {}).get('entries', []))
            self.index = max(0, min(max(0, count - 1), self.index + value))
            if count:
                self.offset = self.index
            else:
                self.offset = max(0, self.offset + value)
        elif event in ('page', 'wheel'):
            delta = value[0] if event == 'wheel' else value * 10
            self.offset = max(0, self.offset + delta)
            count = len((self.page or {}).get('entries', []))
            if count:
                self.index = min(count - 1, self.offset)

    def rows(self, width, height, wrap, render=True):
        page = self.page or {}
        result = [('Inspect · ' + page.get('title', ''), None),
                  ('↑↓ select · Enter open · ← back/up · Esc edit · PgUp/PgDn scroll', None),
                   ('Esc saves this view into the output block.', None)]
        limit = max(1, height - len(result))
        entries = page.get('entries', [])
        # Text layout is independent of selection and scrolling. Keep row ranges,
        # and only invoke the terminal formatter for lines intersecting the window.
        text = page.get('text', '')
        key = (text, width, wrap)
        if getattr(self, '_layout_key', None) != key:
            from terminal_layout import wrapped_height
            layout, count = [], 0
            for line in text.splitlines():
                size = wrapped_height(line, max(1, width - 2))
                layout.append((count, count + size, line))
                count += size
            self._layout_key, self._text_layout, self._text_height = key, layout, count
        total = len(entries) + self._text_height
        self.offset = min(self.offset, max(0, total - limit))
        start, stop = self.offset, self.offset + limit
        if not render:
            return [('', None)] * (len(result) + max(0, min(limit, total - start)))
        body = [(('› ' if i == self.index else '  ') + entries[i]['label'], i)
                for i in range(start, min(stop, len(entries)))]
        for first, last, line in self._text_layout:
            first, last = first + len(entries), last + len(entries)
            if first >= stop:
                break
            if last > start:
                body.extend((part, None) for part in wrap(line, max(1, width - 2))
                            [max(0, start - first):stop - first])
        return result + body
