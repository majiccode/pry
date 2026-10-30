"""A persistent, terminal-native Markdown journal with source-based editing."""
from datetime import datetime
from difflib import SequenceMatcher
from copy import deepcopy
import hashlib
import os
import queue
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit
import webbrowser

from response_rendering import render_markdown, INLINE
from spotlight_search import SpotlightSearch
from pry_state import load_state, save_state
from note_graph import Graph, EXAMPLE_SOURCE
from note_folding import sections, prepare_paste, highlight_json
from command_runner import CommandJob
from note_inspector import NoteInspector
from tui_theme import BASE, TEXT, MUTED, ACCENT, SELECTED, ERROR, SEARCH_HIT


SGR = re.compile(r'\033\[[0-9;]*m')
LINK = re.compile(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)|https?://[^\s<>]+', re.IGNORECASE)


class ScratchpadPanel:
    def __init__(self, path, cells, clipboard_copy=None, clipboard_paste=None, open_url=None,
                 cwd=None, shell=None, timeout: float =120):
        self.path = Path(path)
        self.cells = cells
        self.clipboard_copy = clipboard_copy
        self.clipboard_paste = clipboard_paste
        self.open_url = open_url if open_url is not None else webbrowser.open
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.text = self.path.read_text(encoding='utf-8')
        self.stamp = self.file_stamp()
        self.cursor = len(self.text)
        self.selection = None
        self.anchor = None
        self.drag_raw = None
        self.raw_lines = set()
        self.measure_cache = {}
        self.title = None
        self.offset = 0
        self.max_offset = 0
        self.follow = True
        self.dirty = False
        self.message = 'All changes saved'
        self.search = SpotlightSearch('Find in notes.md', self._search_jump)
        self.search_offsets = []
        self.undo = []
        self.redo = []
        self.zones = []
        self.hit_rows = {}
        self.height = 1
        self.cancel = threading.Event()
        self.thread = None
        self.cwd = cwd or str(Path.cwd())
        self.shell = shell or os.environ.get('SHELL', '/bin/sh')
        self.timeout = timeout
        self.capture_job = None
        self.capture_events = queue.Queue()
        self.capture_range = None
        self.graph_values = {}
        self.graph_cache = {}
        self.graph_render_cache = {}
        self.graph_focus = None
        self.graph_slider = 0
        self.graph_zones = []
        self.paste_blocks = []
        self.inspector = NoteInspector()
        self.collapsed_sections = set()
        self.state_path = self.path.parent / 'pry-state.json'
        try:
            position = load_state(self.state_path).get('scratchpad', {})
            if isinstance(position, dict):
                if position.get('fold_digest') == hashlib.sha256(self.text.encode()).hexdigest():
                    self.inspector.blocks = [b for b in position.get('inspectors', [])
                                             if isinstance(b, dict) and type(b.get('start')) is int
                                             and type(b.get('end')) is int and 0 <= b['start'] < b['end'] <= len(self.text)
                                             and (isinstance(b.get('page'), dict) or
                                                  isinstance(b.get('source'), dict)) and isinstance(b.get('id'), str)]
                    for block in self.inspector.blocks:
                        if block.get('page') is None:
                            self.inspector.recognize(block)
                    self.collapsed_sections = {p for p in position.get('collapsed_sections', [])
                                               if type(p) is int and 0 <= p < len(self.text)}
                    self.paste_blocks = [b for b in position.get('paste_blocks', [])
                                         if isinstance(b, dict) and type(b.get('start')) is int
                                         and type(b.get('end')) is int and 0 <= b['start'] < b['end'] <= len(self.text)
                                         and isinstance(b.get('collapsed'), bool)]
                if isinstance(position.get('graphs'), dict):
                    self.graph_values = position['graphs']
                cursor, offset = position.get('cursor'), position.get('offset')
                if type(cursor) is int and cursor >= 0:
                    self.cursor = min(cursor, len(self.text))
                if type(offset) is int and offset >= 0:
                    self.offset = offset
                    # Preserve a deliberately scrolled viewport, even off-caret.
                    self.follow = False
        except (OSError, ValueError) as exc:
            self.message = f'Could not restore scratchpad position: {exc}'

    def save_position(self):
        try:
            save_state(self.state_path, 'scratchpad',
                       {'cursor': self.cursor, 'offset': self.offset,
                          'graphs': self.graph_values,
                          'inspectors': self.inspector.blocks,
                         'fold_digest': hashlib.sha256(self.text.encode()).hexdigest(),
                         'collapsed_sections': sorted(self.collapsed_sections),
                         'paste_blocks': self.paste_blocks})
            return True
        except (OSError, ValueError) as exc:
            self.message = f'Could not save scratchpad position: {exc}'
            return False

    @property
    def busy(self):
        return self.dirty or self.capture_job is not None

    def file_stamp(self):
        stat = self.path.stat()
        return stat.st_mtime_ns, stat.st_size, stat.st_ino

    def save(self):
        temporary = None
        try:
            if self.file_stamp() != self.stamp:
                raise OSError('notes.md changed outside Pry; edits kept in memory, not overwritten')
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             delete=False) as handle:
                temporary = handle.name
                handle.write(self.text)
                handle.flush()
                os.fsync(handle.fileno())
            if self.file_stamp() != self.stamp:
                raise OSError('notes.md changed outside Pry; edits kept in memory, not overwritten')
            os.replace(temporary, self.path)
            self.stamp = self.file_stamp()
            self.dirty = False
            self.message = 'All changes saved'
        except OSError as exc:
            self.dirty = True
            self.message = f'NOT SAVED: {exc} | Ctrl+S retry'
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def poll(self):
        changed = self.inspector.poll()
        if changed:
            self.save_position()
        if self.cancel.is_set() and self.capture_job is not None:
            self.capture_job.cancel()
        while not self.capture_events.empty():
            kind, result = self.capture_events.get_nowait()
            if kind == 'partial':
                continue
            job = self.capture_job
            self.capture_job = None
            if job is None:
                continue
            if kind == 'done':
                # CommandJob retains only a tail in memory; snapshots keep the log.
                try:
                    output = Path(result['log_path']).read_text(encoding='utf-8', errors='replace')
                except OSError:
                    output = result['stdout']
                details = (f"Exit status: {result['exit_code']}\nDuration: {result['duration']:.2f}s"
                           f"\nFull output: {result['log_path']}")
                if result.get('reason'):
                    details += '\n' + result['reason']
            else:
                output, details = '', f'Capture failed: {result}'
            fence = '`' * max(3, max((len(m[0]) + 1 for m in re.finditer(r'`+', output)), default=3))
            snapshot = (f"Captured: {self.capture_started}\nCommand: {job.command}\n"
                        f"Working directory: {job.cwd}\n{details}\n\n{fence}text\n{output}"
                        + ('' if output.endswith('\n') else '\n') + f'{fence}\n')
            start, end = self.capture_range or (len(self.text), len(self.text))
            self.capture_range = None
            prefix = '\n' if start and self.text[start - 1] != '\n' else ''
            self.replace(start, end, prefix + snapshot)
            begin = start + len(prefix)
            if len(snapshot) > 1000 or len(snapshot.splitlines()) > 10:
                self.paste_blocks.append(dict(start=begin, end=begin + len(snapshot), collapsed=True))
            self.cursor = begin
            if kind == 'done' and not result.get('reason'):
                self.inspector.add(begin, begin + len(snapshot), job.command, output,
                                   result.get('cwd', job.cwd), result['exit_code'])
            self.save_position()
            changed = True
        return changed

    def start_capture(self, start, end, command):
        if not command:
            self.message = 'Usage: /capture <command>'
            return
        if self.capture_job is not None:
            self.message = 'Capture running | Ctrl+C cancel'
            return
        self.capture_range = (start, end)
        self.capture_source = self.text[start:end]
        self.capture_started = datetime.now().astimezone().isoformat(timespec='seconds')
        self.cancel.clear()
        cwd = self.cwd() if callable(self.cwd) else self.cwd
        self.capture_job = CommandJob(command, cwd, self.shell,
                                     lambda kind, value: self.capture_events.put((kind, value)),
                                     timeout=self.timeout, workdir=self.path.parent).start()
        self.thread = self.capture_job.thread
        self.message = 'Capture running | Ctrl+C cancel'

    def replace(self, start, end, value):
        if self.text[start:end] == value:
            return
        if self.capture_range is not None:
            left, right = self.capture_range
            if end <= left:
                shift = len(value) - (end - start)
                self.capture_range = (left + shift, right + shift)
            elif start < right:
                self.capture_range = None
        self.undo.append(self.snapshot())
        self.inspector.edit(start, end, len(value))
        self.undo = self.undo[-100:]
        self.redo.clear()
        self.reveal(start, end, before=start == end and value.endswith('\n'))
        delta = len(value) - (end - start)
        self.collapsed_sections = {p + delta if p >= end else p for p in self.collapsed_sections
                                   if not start <= p < end}
        retained = []
        for block in self.paste_blocks:
            block = block.copy()
            if end <= block['start']:
                block['start'] += delta
                block['end'] += delta
            elif start < block['end']:
                if start >= block['start'] and end <= block['end']:
                    block['end'] += delta
                    block['collapsed'] = False
                else:
                    continue
            retained.append(block)
        self.paste_blocks = retained
        if self.title:
            left, right, body = self.title
            if left <= start <= end <= right and '\n' not in value:
                delta = len(value) - (end - start)
                self.title = left, right + delta, body + delta
            else:
                self.title = None
        self.text = self.text[:start] + value + self.text[end:]
        self.paste_blocks = [b for b in self.paste_blocks if b['start'] < b['end']
                             and (b['start'] == 0 or self.text[b['start'] - 1] == '\n')
                             and (b['end'] == len(self.text) or self.text[b['end'] - 1] == '\n')]
        self.cursor = start + len(value)
        self.selection = self.anchor = None
        self.dirty = self.follow = True
        self.hit_rows = {}
        self.zones = []
        self.save()

    def snapshot(self):
        return (self.text, self.cursor, self.selection, self.title,
                deepcopy(self.paste_blocks), self.collapsed_sections.copy(), deepcopy(self.inspector.blocks))

    def folds(self):
        # Older ranges could absorb edits after a pasted JSON fence. Recover
        # their actual boundary before excluding headings from section parsing.
        for block in self.paste_blocks:
            source = self.text[block['start']:block['end']]
            opening = re.match(r'(`{3,}|~{3,})json[^\n]*\n', source)
            if opening:
                closing = re.search(r'^' + re.escape(opening[1][0]) + '{' + str(len(opening[1]))
                                    + r',}[ \t]*(?:\n|$)', source[opening.end():], re.MULTILINE)
                if closing:
                    block['end'] = block['start'] + opening.end() + closing.end()
        result = sections(self.text, self.paste_blocks)
        for block in result:
            block['collapsed'] = block['start'] in self.collapsed_sections
        for block in self.paste_blocks:
            preview = self.text[block['start']:block['end']].splitlines()
            result.append(dict(block, body=block['start'], kind='paste',
                               label='Paste: ' + (preview[0][:60] if preview else '')))
        return sorted(result, key=lambda b: (b['start'], -b['end']))

    def reveal(self, start, end=None, before=False):
        end = start if end is None else end
        changed = False
        for block in self.folds():
            if before and start == block['start']:
                continue
            if block['collapsed'] and start < block['end'] and end >= block['body']:
                if block['kind'] == 'section':
                    self.collapsed_sections.discard(block['start'])
                else:
                    next(b for b in self.paste_blocks if b['start'] == block['start'])['collapsed'] = False
                changed = True
        return changed

    def toggle_fold(self, position=None):
        position = self.cursor if position is None else position
        candidates = [b for b in self.folds() if b['start'] <= position < b['end']
                      or b['end'] == position == len(self.text)]
        if not candidates:
            return
        block = min(candidates, key=lambda b: b['end'] - b['start'])
        if block['kind'] == 'section':
            if block['collapsed']:
                self.collapsed_sections.discard(block['start'])
            else:
                self.collapsed_sections.add(block['start'])
        else:
            next(b for b in self.paste_blocks if b['start'] == block['start'])['collapsed'] = not block['collapsed']
        self.cursor = block['start']
        self.selection = self.anchor = None
        self.follow = True
        self.save_position()

    def paste(self, value):
        value = value.replace('\r\n', '\n').replace('\r', '\n').replace('\t', '    ')
        value = ''.join(c for c in value if c == '\n' or c.isprintable() or c == '\u200d')
        value = prepare_paste(value)
        large = len(value) > 1000 or len(value.splitlines()) > 10
        if not value:
            return
        start, end = self.selection or (self.cursor, self.cursor)
        structured = large or value.startswith('```json\n')
        prefix = '\n' if structured and start and self.text[start - 1] != '\n' else ''
        suffix = '\n' if structured and not value.endswith('\n') else ''
        self.replace(start, end, prefix + value + suffix)
        if large:
            begin = start + len(prefix)
            self.paste_blocks.append(dict(start=begin, end=begin + len(value) + len(suffix), collapsed=True))
            self.cursor = begin
        self.save_position()

    def receive_result(self, item):
        start = self.cursor
        previous_length = len(self.text)
        cwd = self.cwd() if callable(self.cwd) else self.cwd
        self.selection = None
        self.reveal(start)
        response = prepare_paste(item.get('response', ''))
        if item.get('kind') == 'command':
            trailers = re.search(r'\n(?:\n)?Exit status: [^\n]+(?:\n[^\n]*)*$', item.get('response', ''))
            details = trailers[0].strip() if trailers else ''
            raw_output = item.get('stdout', item.get('response', '')[:trailers.start()] if trailers else item.get('response', ''))
            if item.get('log_path'):
                try:
                    raw_output = Path(item['log_path']).read_text(encoding='utf-8', errors='replace')
                except OSError:
                    pass
            response = prepare_paste(raw_output)
            if response == raw_output:
                marker = '`' * max(3, max((len(m[0]) + 1 for m in re.finditer(r'`+', response)), default=3))
                response = marker + 'text\n' + response + ('' if response.endswith('\n') else '\n') + marker + '\n'
            response = (f"Command: {item['prompt']}\nWorking directory: {item.get('cwd', cwd)}\n"
                        + (details + '\n' if details else '') + '\n'
                        + response)
        prefix = '\n' if start and self.text[start - 1] != '\n' else ''
        self.paste(prefix + response.rstrip('\n') + '\n')
        self.cursor = start + len(prefix)
        if item.get('kind') == 'command':
            stdout = item.get('stdout', item.get('response', ''))
            if item.get('log_path'):
                try:
                    stdout = Path(item['log_path']).read_text(encoding='utf-8', errors='replace')
                except OSError:
                    pass
            self.inspector.add(self.cursor, start + len(self.text) - previous_length,
                               item['prompt'], stdout,
                               item.get('cwd', cwd), item.get('exit_code', 0), item.get('interactive'))
        self.follow = True
        self.save_position()

    def inspector_boundary(self, block):
        """Find the saved output fence without depending on presentation rows."""
        source = self.text[block['start']:block['end']]
        opening = re.search(r'^(`{3,}|~{3,})[^\n]*\n', source, re.M)
        if opening is None:
            return None
        closing = re.search(r'^' + re.escape(opening[1][0]) + '{' + str(len(opening[1])) + r',}[ \t]*$', source[opening.end():], re.M)
        if closing is None:
            return None
        return (block['start'] + opening.start(), block['start'] + opening.end(),
                block['start'] + opening.end() + closing.start())

    def finish_inspection(self):
        block, page = self.inspector.focus, deepcopy(self.inspector.page)
        boundary = self.inspector_boundary(block)
        if boundary is None:
            self.inspector.close()
            return
        _, start, end = boundary
        location = page.get('attach') or page.get('title', 'Inspector')
        parts = ['View: ' + str(location)]
        if page.get('text'):
            parts.append(page['text'])
        parts.extend(entry['label'] for entry in page.get('entries', []))
        value = SGR.sub('', '\n'.join(parts)).rstrip('\n') + '\n'
        # Grow the fence if the destination preview itself contains backticks.
        opening = self.text[boundary[0]:start]
        marker = re.match(r'(`{3,}|~{3,})', opening)[1]
        longest = max((len(m[0]) for m in re.finditer(re.escape(marker[0]) + '+', value)), default=0)
        if longest >= len(marker):
            new_marker = marker[0] * (longest + 1)
            closing_end = self.text.find('\n', end)
            closing_end = len(self.text) if closing_end < 0 else closing_end
            start, end = boundary[0], closing_end
            value = new_marker + 'text\n' + value + new_marker
        saved = deepcopy(block)
        self.replace(start, end, value)
        self.inspector.close()
        saved['end'] += len(value) - (end - start)
        saved['page'] = page
        self.inspector.blocks = [b for b in self.inspector.blocks if b['id'] != saved['id']]
        self.inspector.blocks.append(saved)
        self.cursor = start
        self.follow = True
        self.save_position()

    def insert(self, value):
        value = value.replace('\r\n', '\n').replace('\r', '\n').replace('\t', '    ')
        value = ''.join(c for c in value if c == '\n' or c.isprintable() or c == '\u200d')
        if value:
            start, end = self.selection or (self.cursor, self.cursor)
            self.replace(start, end, value)

    def new_note(self, start, end):
        before = self.text[:start].rstrip('\n')
        # /new is a structural command, even when typed inside a paste range.
        # Keep the preceding paste independently foldable and end it before
        # the new divider/heading rather than extending it with the new note.
        boundary = len(before)
        for block in self.paste_blocks:
            if block['start'] <= boundary < block['end']:
                block['end'] = boundary
        self.paste_blocks = [b for b in self.paste_blocks if b['start'] < b['end']]
        divider = '\n\n---\n\n' if before else ''
        value = divider + '## New Note\n\nStarted: ' + datetime.now().astimezone().isoformat(timespec='minutes') + '\n\n'
        self.replace(len(before), end, value)
        left = len(before) + len(divider) + 3
        self.title = left, left + len('New Note'), self.cursor
        self.selection = (left, self.title[1])
        self.anchor = left
        self.cursor = self.title[1]

    def _search_jump(self, offset):
        # A candidate is one source line; the selected occurrence supplies its offset.
        _, start, _ = self.search.matches[self.search.index]
        self.cursor = offset + start
        self.reveal(self.cursor)
        self.selection = self.anchor = None
        self.follow = True

    def _boundary(self, direction):
        start = self.text.rfind('\n', 0, self.cursor) + 1
        end = self.text.find('\n', self.cursor)
        end = len(self.text) if end < 0 else end
        boundaries = [start]
        # Never pass loaded escape sequences to cells(), which strips ANSI styles.
        safe, mapping = self._visible(self.text[start:end], start)
        index = 0
        for unit, _, _ in self.cells(safe):
            index += len(unit)
            boundaries.append(mapping[index - 1][1])
        if direction < 0:
            return max((p for p in boundaries if p < self.cursor), default=max(0, start - 1))
        return min((p for p in boundaries if p > self.cursor), default=min(len(self.text), end + 1))

    def _copy(self, cut=False):
        if not self.selection:
            return
        try:
            if self.clipboard_copy is None:
                raise OSError('Clipboard unavailable')
            left, right = self.selection
            if self.clipboard_copy(self.text[left:right]) is False:
                raise OSError('Clipboard unavailable')
            if cut:
                self.replace(left, right, '')
            else:
                self.message = 'Selection copied'
        except Exception as exc:
            self.message = f'Copy failed: {exc}'

    def _point(self, value):
        x, y = value
        if y not in self.hit_rows:
            return None
        left, right, spans = self.hit_rows[y]
        for first, last, start, end in spans:
            if x <= last:
                return start
        return right

    def graph_blocks(self):
        """Only closed graph fences outside other code blocks are executable."""
        blocks, offset, fence, start = [], 0, None, 0
        for line in self.text.splitlines(keepends=True):
            marker = re.fullmatch(r' {0,3}(`{3,}|~{3,})([^\n]*)\n?', line)
            if marker:
                if fence is None:
                    fence = (marker[1], marker[2].strip() == 'graph')
                    start = offset + len(line)
                elif marker[1][0] == fence[0][0] and len(marker[1]) >= len(fence[0]) and not marker[2].strip():
                    if fence[1]:
                        source = self.text[start:offset].rstrip('\n')
                        key = hashlib.sha256(source.encode('utf-8')).hexdigest()
                        blocks.append((start, offset, offset + len(line.rstrip('\n')), key, source))
                    fence = None
            offset += len(line)
        return blocks

    def graph(self, key, source):
        if key not in self.graph_cache:
            if len(self.graph_cache) >= 64:
                self.graph_cache.pop(next(iter(self.graph_cache)))
            try:
                self.graph_cache[key] = Graph(source, self.graph_values.get(key))
            except ValueError as exc:
                self.graph_cache[key] = str(exc)
        return self.graph_cache[key]

    def open_graph(self, key, source, index=0):
        graph = self.graph(key, source)
        if isinstance(graph, Graph):
            self.graph_focus = (key, source, index)
            self.graph_slider = 0
        else:
            self.message = 'Graph: ' + graph

    def graph_preview(self, key, graph, width, index):
        cache_key = (key, width, index, tuple(graph.values.items()))
        if cache_key not in self.graph_render_cache:
            if len(self.graph_render_cache) >= 64:
                self.graph_render_cache.pop(next(iter(self.graph_render_cache)))
            self.graph_render_cache[cache_key] = graph.render(width, 10, index)
        return self.graph_render_cache[cache_key]

    def graph_event(self, event, value):
        key, source, index = self.graph_focus
        graph = self.graph(key, source)
        controls = graph.controls(index)
        if event == 'escape' or event == 'key' and value in ('\x03', 'e', '\r'):
            self.graph_focus = None
            if event == 'key' and value == 'e':
                block = next((b for b in self.graph_blocks() if b[3] == key), None)
                if block:
                    self.cursor = block[0]
                    self.selection = self.anchor = None
                    self.follow = True
            return
        if event in ('pane', 'scroll') or event == 'key' and value == '\t':
            step = -1 if event == 'pane' else value if event == 'scroll' else 1
            self.graph_slider = (self.graph_slider + step) % len(controls)
        elif event == 'key' and value in ('[', ']'):
            self.graph_focus = (key, source, (index + (1 if value == ']' else -1)) % len(graph.plots))
            self.graph_slider = 0
        elif event in ('cursor', 'home', 'end'):
            name = controls[self.graph_slider]
            lo, hi = graph.ranges[name]
            fraction = (graph.values[name] - lo) / (hi - lo)
            graph.adjust(name, 0 if event == 'home' else 1 if event == 'end' else fraction + value * .01)
            self.graph_values[key] = dict(graph.values)
            self.save_position()
        elif event in ('click', 'drag'):
            x, y = value
            for row, left, right, number in self.graph_zones:
                if row == y:
                    self.graph_slider = number
                    graph.adjust(controls[number], (x - left) / max(1, right - left))
                    self.graph_values[key] = dict(graph.values)
                    self.save_position()
                    break

    def draw_graph(self, cols, rows, wrap, top):
        key, source, index = self.graph_focus
        graph = self.graph(key, source)
        controls = graph.controls(index)
        output = ['\033[?25l']
        self.graph_zones = []

        def put(row, value, style=TEXT):
            if top < row <= rows:
                output.append(f'\033[{row};1H' + BASE + ' ' * cols)
                output.append(f'\033[{row};1H' + BASE + style + wrap(value, cols)[0])

        for row in range(top + 1, rows + 1):
            put(row, '')
        put(top + 1, f' Graph {index + 1}/{len(graph.plots)} | [ / ] previous / next plot', ACCENT)
        room = max(0, rows - top - 3)
        slider_rows = min(len(controls), max(0, room // 3))
        graph_height = max(0, room - slider_rows)
        for number, line in enumerate(graph.render(cols, graph_height, index)):
            put(top + 2 + number, line)
        first = max(0, self.graph_slider - slider_rows + 1)
        for number, name in enumerate(controls[first:first + slider_rows], first):
            lo, hi = graph.ranges[name]
            value = graph.values[name]
            prefix = f'{">" if number == self.graph_slider else " "} {name}={value:.4g} '
            suffix = f' [{lo:.4g}..{hi:.4g}]'
            track_width = max(1, cols - len(prefix) - len(suffix))
            position = round((value - lo) / (hi - lo) * (track_width - 1))
            track = '-' * position + 'O' + '-' * (track_width - position - 1)
            row = top + 2 + graph_height + number - first
            put(row, prefix + track + suffix, ACCENT if number == self.graph_slider else MUTED)
            self.graph_zones.append((row, len(prefix) + 1, len(prefix) + track_width, number))
        put(rows - 1, self.message if self.message.startswith('Could not') else ' Sweep slider moves the playhead; parameters reshape the curves.', MUTED)
        put(rows, ' Tab/Up/Down slider | Left/Right adjust | Home/End bounds | e edit | Esc close', MUTED)
        return ''.join(output)

    def event(self, event, value):
        if self.inspector.focus is not None:
            if event == 'escape':
                self.finish_inspection()
                return
            if event == 'click':
                x, y = value
                hit = next((arg for row, left, right, action, arg in self.zones
                            if row == y and left <= x <= right and action == 'inspector_entry'), None)
                if hit is not None:
                    self.inspector.index = hit
                    self.inspector.enter(self.open_url)
                    return
            self.inspector.event(event, value, self.open_url)
            return
        if event == 'key' and value == '\x03' and self.capture_job is not None:
            self.capture_job.cancel()
            self.message = 'Cancelling capture…'
            return
        if event == 'escape' and self.capture_job is not None:
            self.message = 'Capture running | Ctrl+C cancel before leaving Notes'
            return
        if self.graph_focus is not None:
            if event == 'find' or event == 'key' and value == '\x06':
                self.graph_focus = None
                self.event(event, value)
                return
            self.graph_event(event, value)
            return
        if self.search.active:
            self.search.event(event, value)
            return
        if event == 'key' and value == '\x01':
            self.selection = (0, len(self.text)) if self.text else None
            self.anchor = 0 if self.text else None
            self.cursor = len(self.text)
            self.follow = True
            self.reveal(0, len(self.text))
            self.message = 'All note text selected | Ctrl+C copy | Backspace/Delete remove'
            return
        if event == 'key' and value == '\x05':
            self.toggle_fold()
            return
        if event == 'key' and value == '\x07':
            block = next((b for b in self.graph_blocks() if b[0] <= self.cursor <= b[2]), None)
            if block:
                self.open_graph(block[3], block[4])
            return
        if event == 'click':
            x, y = value
            inspect = next((arg for row, left, right, action, arg in self.zones
                            if row == y and left <= x <= right and action == 'inspect'), None)
            if inspect is not None:
                self.reveal(inspect['start'], inspect['end'])
                self.inspector.open(inspect)
                self.follow = True
                return
            folded = next((argument for row, left, right, action, argument in self.zones
                           if row == y and left <= x <= right and action == 'fold'), None)
            if folded is not None:
                self.toggle_fold(folded)
                return
            hit = next((argument for row, left, right, action, argument in self.zones
                        if row == y and left <= x <= right and action == 'graph'), None)
            if hit is not None:
                self.open_graph(*hit)
                return
        if event == 'find' or event == 'key' and value == '\x06':
            candidates, offset, current = [], 0, 0
            for index, line in enumerate(self.text.split('\n')):
                candidates.append((line, offset))
                if offset <= self.cursor:
                    current = index
                offset += len(line) + 1
            self.selection = self.anchor = None
            self.search_offsets = [offset for _, offset in candidates]
            self.search.open(candidates, current)
            return
        if event == 'key' and value in ('\x03', '\x18'):
            self._copy(cut=value == '\x18')
            return
        if event == 'escape':
            if self.selection:
                self.selection = self.anchor = None
                return
            if self.dirty:
                self.save()
            if not self.dirty and self.save_position():
                return 'close', ''
            return
        if event == 'key' and value == '\x16':
            try:
                if self.clipboard_paste is None:
                    raise OSError('Clipboard unavailable')
                text = self.clipboard_paste()
                if not isinstance(text, str):
                    raise OSError('Clipboard did not return text')
                self.paste(text)
            except Exception as exc:
                self.message = f'Paste failed: {exc}'
            return
        if event == 'key' and value == '\x13':
            self.save()
            return
        if event == 'key' and value in ('\x1a', '\x19'):
            source, destination = (self.undo, self.redo) if value == '\x1a' else (self.redo, self.undo)
            if source:
                target = self.capture_range
                destination.append(self.snapshot())
                (self.text, self.cursor, self.selection, self.title,
                  self.paste_blocks, self.collapsed_sections, self.inspector.blocks) = source.pop()
                for block in self.inspector.blocks:
                    if block.get('page') is None:
                        self.inspector.recognize(block)
                if self.capture_job is not None:
                    matches = [m.start() for m in re.finditer(re.escape(self.capture_source), self.text)]
                    position = min(matches, key=lambda p: abs(p - target[0])) if matches and target else None
                    self.capture_range = (position, position + len(self.capture_source)) if position is not None else None
                self.anchor = self.selection[0] if self.selection else None
                self.dirty = self.follow = True
                self.hit_rows = {}
                self.zones = []
                self.save()
                self.save_position()
            return
        start = self.text.rfind('\n', 0, self.cursor) + 1
        end = self.text.find('\n', self.cursor)
        end = len(self.text) if end < 0 else end
        if event == 'newline' or event == 'key' and value in ('\r', '\n'):
            if self.selection and (not self.title or self.selection != self.title[:2]):
                self.insert('\n')
            elif self.title and self.title[0] <= self.cursor <= self.title[1]:
                self.cursor = self.title[2]
                self.title = self.selection = self.anchor = None
                self.follow = True
            elif self.text[start:end].strip() == '/new':
                self.new_note(start, end)
            elif self.text[start:end].strip().split(' ', 1)[0] == '/capture':
                self.start_capture(start, end, self.text[start:end].strip()[len('/capture'):].strip())
            elif self.text[start:end].strip() == '/graph':
                source = '```graph\n' + EXAMPLE_SOURCE + '\n```\n'
                self.replace(start, end, source)
                self.cursor = start + len('```graph\n')
                key = hashlib.sha256(EXAMPLE_SOURCE.encode('utf-8')).hexdigest()
                self.open_graph(key, EXAMPLE_SOURCE)
            else:
                self.insert('\n')
        elif event == 'paste':
            self.paste(value)
        elif event == 'key' and value in ('\x7f', '\x08'):
            left, right = self.selection or (self._boundary(-1), self.cursor)
            self.replace(left, right, '')
        elif event == 'delete':
            left, right = self.selection or (self.cursor, self._boundary(1))
            self.replace(left, right, '')
        elif event.removeprefix('select_') in ('cursor', 'home', 'end', 'document_home', 'document_end', 'scroll', 'page'):
            selecting = event.startswith('select_')
            movement = event.removeprefix('select_')
            previous = self.cursor
            if movement == 'cursor':
                if not selecting and self.selection:
                    self.cursor = self.selection[0 if value < 0 else 1]
                else:
                    self.cursor = self._boundary(value)
            elif movement in ('document_home', 'document_end'):
                self.cursor = 0 if movement == 'document_home' else len(self.text)
            elif movement in ('home', 'end'):
                self.cursor = start if movement == 'home' else end
            else:
                column = self.cursor - start
                for _ in range(self.height if movement == 'page' else 1):
                    if value < 0 and start:
                        end = start - 1
                        start = self.text.rfind('\n', 0, end) + 1
                    elif value > 0 and end < len(self.text):
                        start = end + 1
                        end = self.text.find('\n', start)
                        end = len(self.text) if end < 0 else end
                self.cursor = min(start + column, end)
            if selecting:
                if self.anchor is None:
                    self.anchor = previous
                bounds = tuple(sorted((self.anchor, self.cursor)))
                self.selection = bounds if bounds[0] != bounds[1] else None
            else:
                self.selection = self.anchor = None
            self.follow = True
            self.reveal(self.cursor)
        elif event == 'wheel':
            self.offset = max(0, min(self.max_offset, self.offset + value[0]))
            self.follow = False
        elif event == 'link_click':
            x, y = value
            for row, left, right, action, url in self.zones:
                if row == y and left <= x <= right and action == 'link':
                    try:
                        if self.open_url(url) is False:
                            raise OSError('Browser unavailable')
                    except Exception as exc:
                        self.message = f'Open link failed: {exc}'
                    break
        elif event in ('click', 'drag', 'release'):
            point = self._point(value)
            if event == 'click':
                if point is not None:
                    self.drag_raw = self.raw_lines.copy()
                    self.cursor = self.anchor = point
                    self.selection = None
            elif self.anchor is not None and point is not None:
                self.cursor = point
                bounds = tuple(sorted((self.anchor, point)))
                self.selection = bounds if bounds[0] != bounds[1] else None
            if event == 'release':
                self.anchor = None
                self.drag_raw = None
        elif event == 'key' and isinstance(value, str) and (value.isprintable() or value == '\t'):
            self.insert(value)

    def _visible(self, line, offset):
        """Display controls as data, retaining offsets into the unmodified file."""
        text, mapping = [], []
        for index, char in enumerate(line):
            if char == '\t':
                column = sum(last - first + 1 for _, first, last in self.cells(''.join(text)))
                value = ' ' * (4 - column % 4)
            else:
                value = char if char.isprintable() or char in ('\u200d', '\ufe0f') else f'\\x{ord(char):02x}'
            text.append(value)
            mapping.extend([(offset + index, offset + index + 1)] * len(value))
        return ''.join(text), mapping

    def measure_line(self, line, width, raw):
        """Measure single-line layout without styling, source maps or link parsing."""
        key = (line, width, raw)
        if key in self.measure_cache:
            return self.measure_cache[key]
        safe, _ = self._visible(line, 0)
        if raw:
            sizes = [last - first + 1 for _, first, last in self.cells(safe)]
            count, used = 1, 0
            for size in sizes:
                if used and used + size > width:
                    count, used = count + 1, 0
                used += size
        else:
            heading = re.match(r'^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?$', safe)
            listing = re.match(r'^(\s*)([-+*]|\d+[.)])\s+(.*)$', safe)
            quote = re.match(r'^\s*>\s?(.*)$', safe)
            prefix = ''
            if heading:
                safe = '**' + heading[1] + '**'
            elif re.fullmatch(r'\s*(?:\*\s*){3,}|\s*(?:-\s*){3,}|\s*(?:_\s*){3,}', safe):
                safe = '-' * min(width, 48)
            elif listing:
                marker = '-' if listing[2] in '-+*' else listing[2]
                prefix = ' ' * min(len(listing[1].expandtabs(4)), max(0, width - 4)) + marker + ' '
                safe = listing[3]
            elif quote:
                safe, prefix = quote[1], '> '
            else:
                safe = safe.strip()
            safe = INLINE.sub(lambda m: (m[2] or m[3] or m[4] or m[5]
                                         or m[6] + (f' ({m[7]})' if m[6] != m[7] else '')), safe)
            prefix_width = sum(b - a + 1 for _, a, b in self.cells(prefix))
            if prefix_width >= width:
                prefix_width = 0
            count, used, has_word = 1, prefix_width, False
            for word in re.findall(r'\S+', safe):
                sizes = [b - a + 1 for _, a, b in self.cells(word)]
                if has_word and used + 1 + sum(sizes) > width:
                    count, used, has_word = count + 1, prefix_width, False
                if has_word:
                    used += 1
                for size in sizes:
                    size = min(size, width)
                    if used + size > width:
                        count, used = count + 1, 0
                    used += min(size, width)
                has_word = True
        if len(self.measure_cache) >= 8192:
            self.measure_cache.clear()
        self.measure_cache[key] = (count, used)
        return count, used

    def draw(self, cols, rows, wrap, styles, top=1, _settled=False):
        self.hit_rows = {}
        self.zones = []
        self.raw_lines = set()
        if cols <= 0 or rows <= top:
            return ''
        if self.graph_focus is not None:
            return self.draw_graph(cols, rows, wrap, top)
        output = ['\033[?25l']

        def put(row, col, value, width, style=TEXT):
            if top < row <= rows and 1 <= col <= cols and width > 0:
                output.append(f'\033[{row};{col}H' + BASE + style + wrap(value, min(width, cols - col + 1))[0])

        for row in range(top + 1, rows + 1):
            put(row, 1, ' ' * cols, cols)
        put(top + 1, 1, ' Scratchpad / notes.md | Tab AI chat', cols, ACCENT)
        put(top + 2, 1, ' Ctrl+E fold | Ctrl+C/X/V clipboard | Ctrl+Z/Y undo/redo | /new /graph /capture | Ctrl+F find', cols, MUTED)
        width, origin = max(1, cols - 4), 4 if cols > 4 else 1
        viewport_height = max(1, rows - (top + 3))
        viewport_start = self.offset
        physical = []
        cursor_row = cursor_col = 0
        position, fence = 0, None
        blocks = {block[2]: block for block in self.graph_blocks()}
        graph_rows = {}
        inspector_rows = {}
        fold_rows = {}
        folds = self.folds()
        fold_starts = {b['start']: b for b in folds}
        hidden = [b for b in folds if b['collapsed']]
        json_fence = False
        hits = []
        inspector_bounds = {b['id']: self.inspector_boundary(b) for b in self.inspector.blocks}
        if self.search.active:
            hits = [(self.search_offsets[i] + left, self.search_offsets[i] + right)
                    for i, left, right in self.search.matches]
        for line in self.text.split('\n'):
            end = position + len(line)
            for inspector in self.inspector.blocks:
                boundary = inspector_bounds[inspector['id']]
                control_position = boundary[1] if boundary else inspector['start']
                if not position <= control_position <= end or not inspector.get('page'):
                    continue
                if any(b['kind'] == 'section' and b['body'] <= position < b['end'] for b in hidden):
                    continue
                inspector_rows[len(physical)] = ('inspect', inspector)
                physical.append((ACCENT + '[ Inspect · ' + self._visible(inspector['page'].get('title', ''), 0)[0] + ' ]',
                                 position, position, [], []))
                if self.inspector.focus is inspector:
                    inspect_visible = (len(physical) < viewport_start + viewport_height
                                       and len(physical) + max(8, rows - top - 7) > viewport_start)
                    for label, entry in self.inspector.rows(width, max(8, rows - top - 7), wrap, render=inspect_visible):
                        if entry is not None:
                            inspector_rows[len(physical)] = ('inspector_entry', entry)
                        physical.append((ACCENT + self._visible(label, 0)[0], position, position, [], []))
            focused_boundary = inspector_bounds.get((self.inspector.focus or {}).get('id'))
            if focused_boundary and focused_boundary[1] <= position < focused_boundary[2]:
                position = end + 1
                continue
            block = fold_starts.get(position)
            covering = next((b for b in hidden if b['body'] <= position < b['end']
                             and not (b['kind'] == 'section' and position == b['start'])), None)
            if covering:
                if covering['kind'] == 'paste' and position == covering['start']:
                    count = len(self.text[position:covering['end']].splitlines())
                    fold_rows[len(physical)] = covering
                    summary = covering['label'] + f' … ({count} lines)'
                    safe_summary, _ = self._visible(summary, 0)
                    summary_visible = viewport_start <= len(physical) < viewport_start + viewport_height
                    physical.append((MUTED + wrap(safe_summary, width)[0] if summary_visible else '', position, position, [], []))
                    if position <= self.cursor < covering['end']:
                        cursor_row, cursor_col = len(physical) - 1, 0
                position = end + 1
                continue
            if block:
                fold_rows[len(physical)] = block
            active = position <= self.cursor <= end
            marker = re.match(r'^\s*(`{3,}|~{3,})(.*)$', line)
            selected = self.selection and position < self.selection[1] and end >= self.selection[0]
            raw = active or selected or fence is not None or marker is not None
            if self.anchor is not None and self.drag_raw is not None:
                raw = position in self.drag_raw
            if raw:
                self.raw_lines.add(position)
            count, used = self.measure_line(line, width, raw)
            if active and raw and used == width and self.cursor == end:
                count += 1
            visible = len(physical) < viewport_start + viewport_height and len(physical) + count > viewport_start
            if active:
                before, _ = self.measure_line(line[:self.cursor - position], width, True)
                cursor_row = len(physical) + min(count - 1, before - 1)
                if self.cursor == end and raw and used == width:
                    cursor_row = len(physical) + count - 1
            if not visible:
                physical.extend(('', position, end, [], []) for _ in range(count))
            safe, mapping = self._visible(line, position) if visible else ('', [])
            links = []
            for match in LINK.finditer(line if visible else ''):
                url = match[2] or match[0].rstrip('.,;:!?)\'"')
                try:
                    parsed = urlsplit(url)
                    valid = parsed.scheme.lower() in ('http', 'https') and bool(parsed.hostname)
                except ValueError:
                    valid = False
                if valid and all(c.isprintable() for c in url):
                    links.append((position + match.start(), position + match.start() + (len(match[0]) if match[2] else len(url)), url))
            if not visible:
                chunks, display_map = [], []
            elif raw:
                chunks, chunk, used = [], '', 0
                for unit, first, last in self.cells(safe):
                    size = last - first + 1
                    if used and used + size > width:
                        chunks.append(chunk)
                        chunk, used = '', 0
                    chunk += unit
                    used += size
                chunks.append(chunk)
                if active and used == width and self.cursor == end:
                    chunks.append('')
                display_map = mapping
                if json_fence and marker is None:
                    chunks = [highlight_json(chunk) for chunk in chunks]
            else:
                chunks = render_markdown(safe, width, wrap, self.cells) or ['']
                plain = ''.join(SGR.sub('', chunk) for chunk in chunks)
                # Markdown removes delimiters and may normalize whitespace. Align the
                # visible result back to source; inserted decoration uses its neighbor.
                display_map = [None] * len(plain)
                for a, b, size in SequenceMatcher(None, safe, plain, autojunk=False).get_matching_blocks():
                    display_map[b:b + size] = mapping[a:a + size]
                neighbor = (position, position)
                for i in range(len(display_map) - 1, -1, -1):
                    if display_map[i] is not None:
                        neighbor = display_map[i]
                    else:
                        display_map[i] = neighbor
            consumed = 0
            for chunk in chunks:
                spans, styled, column, style = [], '', 0, BASE + TEXT
                plain = SGR.sub('', chunk)
                local_map = display_map[consumed:consumed + len(plain)]
                left = local_map[0][0] if local_map else end
                right = local_map[-1][1] if local_map else end
                if active and left <= self.cursor <= right:
                    if self.cursor < right or chunk == chunks[-1]:
                        cursor_row = len(physical)
                        cursor_col = 0
                index = 0
                for token in re.split(r'(\033\[[0-9;]*m)', chunk):
                    if SGR.fullmatch(token):
                        style = token if token == '\033[0m' else style + token
                        continue
                    for unit, first, last in self.cells(token):
                        size = last - first + 1
                        source = local_map[index:index + len(unit)]
                        a, b = min(p[0] for p in source), max(p[1] for p in source)
                        chosen = self.selection and a < self.selection[1] and b > self.selection[0]
                        hit = any(a < stop and b > begin for begin, stop in hits)
                        if size > width:
                            unit, size = '?', 1
                        styled += (SELECTED if chosen else SEARCH_HIT if hit else BASE + style) + unit
                        spans.append((column, column + size - 1, a, b))
                        if active and left <= self.cursor <= right and b <= self.cursor:
                            cursor_col = column + size
                        column += size
                        index += len(source)
                consumed += len(plain)
                physical.append((styled + BASE, left, right, spans, links))
            if marker:
                if fence is None:
                    fence = marker[1]
                    json_fence = marker[2].strip().lower() == 'json'
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                    fence = None
                    json_fence = False
            if end in blocks:
                _, _, _, key, source = blocks[end]
                graph = self.graph(key, source)
                if isinstance(graph, Graph):
                    for plot_index in range(len(graph.plots)):
                        plot_visible = len(physical) < viewport_start + viewport_height and len(physical) + 10 > viewport_start
                        preview = self.graph_preview(key, graph, width, plot_index) if plot_visible else [''] * 10
                        for rendered in preview:
                            graph_rows[len(physical)] = (key, source, plot_index)
                            physical.append((rendered, end, end, [], []))
                        graph_rows[len(physical)] = (key, source, plot_index)
                        physical.append((MUTED + ' Click graph to expand / adjust sliders | Ctrl+G from source' + BASE,
                                         end, end, [], []))
                else:
                    physical.append((ERROR + 'Graph: ' + graph + BASE, end, end, [], []))
            position = end + 1
        first_row = top + 3
        if self.inspector.focus is not None:
            cursor_row = next((i + 1 for i, (action, block) in inspector_rows.items()
                               if action == 'inspect' and block is self.inspector.focus), cursor_row)
            self.follow = True
        self.height = max(1, rows - first_row)
        self.max_offset = max(0, len(physical) - self.height)
        if self.inspector.focus is not None:
            self.offset = max(0, cursor_row - 1)
        if self.follow and not self.offset <= cursor_row < self.offset + self.height:
            self.offset = max(0, cursor_row - self.height // 2)
        self.offset = min(self.offset, self.max_offset)
        if self.offset != viewport_start and not _settled:
            return self.draw(cols, rows, wrap, styles, top, _settled=True)
        for index, (value, left, right, spans, links) in enumerate(physical[self.offset:self.offset + self.height]):
            row = first_row + index
            if not top < row < rows:
                continue
            put(row, origin, value, width)
            inspect = inspector_rows.get(self.offset + index)
            if inspect is not None:
                self.zones.append((row, origin, cols, inspect[0], inspect[1]))
                continue
            fold = fold_rows.get(self.offset + index)
            if fold is not None:
                put(row, max(1, origin - 2), '▸' if fold['collapsed'] else '▾', 1, ACCENT)
                self.zones.append((row, max(1, origin - 2), max(1, origin - 2), 'fold', fold['start']))
                if fold['kind'] == 'paste' and fold['collapsed']:
                    self.zones.append((row, origin, cols, 'fold', fold['start']))
            self.hit_rows[row] = (left, right, [(a + origin, b + origin, c, d) for a, b, c, d in spans])
            target = graph_rows.get(self.offset + index)
            if target is not None:
                self.zones.append((row, origin, cols, 'graph', target))
                self.hit_rows.pop(row, None)
            for a, b, start, end in spans:
                for link_start, link_end, url in links:
                    if start < link_end and end > link_start:
                        self.zones.append((row, a + origin, b + origin, 'link', url))
        message, _ = self._visible(self.message, 0)
        put(rows, 1, ' ' + message, cols, ERROR if self.dirty else MUTED)
        if self.search.active:
            overlay = self.search.draw(cols, rows - top, wrap, (BASE, MUTED, TEXT, ACCENT, SEARCH_HIT))
            output.append(re.sub(r'\033\[(\d+);(\d+)H',
                                 lambda m: f'\033[{int(m[1]) + top};{m[2]}H', overlay))
        elif self.inspector.focus is None and self.offset <= cursor_row < self.offset + self.height:
            row = first_row + cursor_row - self.offset
            if top < row < rows:
                output.append(f'\033[{row};{min(cols, cursor_col + origin)}H\033[?25h')
        return ''.join(output)
