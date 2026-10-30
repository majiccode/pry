"""A persistent, terminal-native Markdown journal with source-based editing."""
from datetime import datetime
from difflib import SequenceMatcher
import hashlib
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit
import webbrowser

from response_rendering import render_markdown
from spotlight_search import SpotlightSearch
from pry_state import load_state, save_state
from note_graph import Graph, EXAMPLE_SOURCE
from tui_theme import BASE, TEXT, MUTED, ACCENT, SELECTED, ERROR, SEARCH_HIT


SGR = re.compile(r'\033\[[0-9;]*m')
LINK = re.compile(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)|https?://[^\s<>]+', re.IGNORECASE)


class ScratchpadPanel:
    def __init__(self, path, cells, clipboard_copy=None, clipboard_paste=None, open_url=None):
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
        self.graph_values = {}
        self.graph_cache = {}
        self.graph_render_cache = {}
        self.graph_focus = None
        self.graph_slider = 0
        self.graph_zones = []
        self.state_path = self.path.parent / 'pry-state.json'
        try:
            position = load_state(self.state_path).get('scratchpad', {})
            if isinstance(position, dict):
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
                        'graphs': self.graph_values})
            return True
        except (OSError, ValueError) as exc:
            self.message = f'Could not save scratchpad position: {exc}'
            return False

    @property
    def busy(self):
        return self.dirty

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
        return False

    def replace(self, start, end, value):
        if self.text[start:end] == value:
            return
        self.undo.append((self.text, self.cursor, self.selection, self.title))
        self.undo = self.undo[-100:]
        self.redo.clear()
        if self.title:
            left, right, body = self.title
            if left <= start <= end <= right and '\n' not in value:
                delta = len(value) - (end - start)
                self.title = left, right + delta, body + delta
            else:
                self.title = None
        self.text = self.text[:start] + value + self.text[end:]
        self.cursor = start + len(value)
        self.selection = self.anchor = None
        self.dirty = self.follow = True
        self.hit_rows = {}
        self.zones = []
        self.save()

    def insert(self, value):
        value = value.replace('\r\n', '\n').replace('\r', '\n').replace('\t', '    ')
        value = ''.join(c for c in value if c == '\n' or c.isprintable() or c == '\u200d')
        if value:
            start, end = self.selection or (self.cursor, self.cursor)
            self.replace(start, end, value)

    def new_note(self, start, end):
        before = self.text[:start].rstrip('\n')
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
            self.message = 'All note text selected | Ctrl+C copy | Backspace/Delete remove'
            return
        if event == 'key' and value == '\x07':
            block = next((b for b in self.graph_blocks() if b[0] <= self.cursor <= b[2]), None)
            if block:
                self.open_graph(block[3], block[4])
            return
        if event == 'click':
            x, y = value
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
                self.insert(text)
            except Exception as exc:
                self.message = f'Paste failed: {exc}'
            return
        if event == 'key' and value == '\x13':
            self.save()
            return
        if event == 'key' and value in ('\x1a', '\x19'):
            source, destination = (self.undo, self.redo) if value == '\x1a' else (self.redo, self.undo)
            if source:
                destination.append((self.text, self.cursor, self.selection, self.title))
                self.text, self.cursor, self.selection, self.title = source.pop()
                self.anchor = self.selection[0] if self.selection else None
                self.dirty = self.follow = True
                self.hit_rows = {}
                self.zones = []
                self.save()
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
            elif self.text[start:end].strip() == '/graph':
                source = '```graph\n' + EXAMPLE_SOURCE + '\n```\n'
                self.replace(start, end, source)
                self.cursor = start + len('```graph\n')
                key = hashlib.sha256(EXAMPLE_SOURCE.encode('utf-8')).hexdigest()
                self.open_graph(key, EXAMPLE_SOURCE)
            else:
                self.insert('\n')
        elif event == 'paste':
            self.insert(value)
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

    def draw(self, cols, rows, wrap, styles, top=1):
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
        put(top + 1, 1, ' Scratchpad / notes.md', cols, ACCENT)
        put(top + 2, 1, ' Ctrl+A all | Ctrl+C/X/V clipboard | Ctrl+Z/Y undo/redo | /new /graph | Ctrl+F find', cols, MUTED)
        width, origin = max(1, cols - 2), 2 if cols > 2 else 1
        physical = []
        cursor_row = cursor_col = 0
        position, fence = 0, None
        blocks = {block[2]: block for block in self.graph_blocks()}
        graph_rows = {}
        hits = []
        if self.search.active:
            hits = [(self.search_offsets[i] + left, self.search_offsets[i] + right)
                    for i, left, right in self.search.matches]
        for line in self.text.split('\n'):
            end = position + len(line)
            active = position <= self.cursor <= end
            marker = re.match(r'^\s*(`{3,}|~{3,})(.*)$', line)
            safe, mapping = self._visible(line, position)
            selected = self.selection and position < self.selection[1] and end >= self.selection[0]
            raw = active or selected or fence is not None or marker is not None
            if self.anchor is not None and self.drag_raw is not None:
                raw = position in self.drag_raw
            if raw:
                self.raw_lines.add(position)
            links = []
            for match in LINK.finditer(line):
                url = match[2] or match[0].rstrip('.,;:!?)\'"')
                try:
                    parsed = urlsplit(url)
                    valid = parsed.scheme.lower() in ('http', 'https') and bool(parsed.hostname)
                except ValueError:
                    valid = False
                if valid and all(c.isprintable() for c in url):
                    links.append((position + match.start(), position + match.start() + (len(match[0]) if match[2] else len(url)), url))
            if raw:
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
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                    fence = None
            if end in blocks:
                _, _, _, key, source = blocks[end]
                graph = self.graph(key, source)
                if isinstance(graph, Graph):
                    for plot_index in range(len(graph.plots)):
                        for rendered in self.graph_preview(key, graph, width, plot_index):
                            graph_rows[len(physical)] = (key, source, plot_index)
                            physical.append((rendered, end, end, [], []))
                        graph_rows[len(physical)] = (key, source, plot_index)
                        physical.append((MUTED + ' Click graph to expand / adjust sliders | Ctrl+G from source' + BASE,
                                         end, end, [], []))
                else:
                    physical.append((ERROR + 'Graph: ' + graph + BASE, end, end, [], []))
            position = end + 1
        first_row = top + 3
        self.height = max(1, rows - first_row)
        self.max_offset = max(0, len(physical) - self.height)
        if self.follow and not self.offset <= cursor_row < self.offset + self.height:
            self.offset = max(0, cursor_row - self.height // 2)
        self.offset = min(self.offset, self.max_offset)
        for index, (value, left, right, spans, links) in enumerate(physical[self.offset:self.offset + self.height]):
            row = first_row + index
            if not top < row < rows:
                continue
            put(row, origin, value, width)
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
        elif self.offset <= cursor_row < self.offset + self.height:
            row = first_row + cursor_row - self.offset
            if top < row < rows:
                output.append(f'\033[{row};{min(cols, cursor_col + origin)}H\033[?25h')
        return ''.join(output)
