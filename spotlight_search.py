"""Reusable, modal terminal search over text and opaque jump targets."""
import re
from tui_theme import BASE, SEARCH_HIT


SGR = re.compile(r'\033\[[0-9;]*m')


class SpotlightSearch:
    """matches contains (candidate_index, start, end) non-overlapping hits.

    index is the selected hit, or -1. current in open() is a candidate index.
    Queries survive closing/reopening; an empty query has no hits. on_jump is
    called with the original target when selecting a hit, never on a miss.
    """

    def __init__(self, title='Find', on_jump=None):
        self.title = title
        self.on_jump = on_jump
        self.active = False
        self.query = ''
        self.matches = []
        self.index = -1
        self._candidates = []
        self._current = 0

    def open(self, candidates, current=0):
        self._candidates = [(str(text), target) for text, target in candidates]
        self._current = max(0, current)
        self.active = True
        self._match()

    def _match(self):
        self.matches = []
        if self.query:
            pattern = re.compile(re.escape(self.query), re.IGNORECASE)
            self.matches = [(i, hit.start(), hit.end())
                            for i, (text, _) in enumerate(self._candidates)
                            for hit in pattern.finditer(text)]
        self.index = next((i for i, hit in enumerate(self.matches)
                           if hit[0] >= self._current), 0) if self.matches else -1
        self._jump()

    def _jump(self):
        if self.index >= 0 and self.on_jump:
            self.on_jump(self._candidates[self.matches[self.index][0]][1])

    def event(self, event, value):
        """Consume input while active. Arrow events use scroll +/-1, Shift+Tab pane."""
        if not self.active:
            return False
        if event == 'escape' or event == 'key' and value in ('\x1b', '\x03'):
            self.active = False
        elif event == 'find' or event == 'key' and value == '\x06':
            pass
        elif event in ('scroll', 'pane') or event == 'key' and value in ('\r', '\n', '\t'):
            step = -1 if event == 'pane' or event == 'scroll' and value < 0 else 1
            if self.matches:
                self.index = (self.index + step) % len(self.matches)
                self._jump()
        else:
            previous = self.query
            if event == 'key' and value == '\x15':
                self.query = ''
            elif event == 'key' and value in ('\x7f', '\x08'):
                self.query = self.query[:-1]
            elif event in ('key', 'paste') and isinstance(value, str):
                self.query = (self.query + ''.join(c for c in value if c.isprintable()))[:4096]
            if self.query != previous:
                self._match()
        return True

    def highlight(self, text):
        """Highlight literal hits without losing the underlying ANSI styles."""
        if not self.active or not self.query:
            return text
        plain = SGR.sub('', text)
        hits = list(re.finditer(re.escape(self.query), plain, re.IGNORECASE))
        if not hits:
            return text
        result, position, index, style = [], 0, 0, BASE
        for token in re.split(r'(\033\[[0-9;]*m)', text):
            if SGR.fullmatch(token):
                style = token if token in ('\033[0m', '\033[m') else style + token
                result.append(token)
                continue
            for char in token:
                while index < len(hits) and position >= hits[index].end():
                    index += 1
                hit = index < len(hits) and hits[index].start() <= position < hits[index].end()
                result.append((SEARCH_HIT + char + style) if hit else char)
                position += 1
        return ''.join(result)

    def draw(self, cols, rows, wrap, styles):
        """Return a bounded ANSI overlay; wrap must return terminal-width rows."""
        if not self.active or cols <= 0 or rows <= 0:
            return ''
        base, muted, text, accent, selected = styles
        width = min(72, cols)
        left = (cols - width) // 2 + 1
        first = min(3, max(1, rows - 4))

        def safe(value):
            return ''.join(c if c.isprintable() else f'\\x{ord(c):02x}' for c in str(value))

        count = f'{self.index + 1}/{len(self.matches)}' if self.matches else '0/0'
        lines = [(safe(self.title) + ' | ' + count, accent),
                 ('> ' + safe(self.query or 'Type to search'), text)]
        if self.index >= 0:
            candidate, start, end = self.matches[self.index]
            snippet = self._candidates[candidate][0]
            # Start close to the occurrence, rather than clipping long-line hits.
            before = safe(snippet[max(0, start - width // 4):start])
            hit = safe(snippet[start:end])
            after = safe(snippet[end:end + width])
            lines.append((before + SEARCH_HIT + hit + base + text + after, text))
        else:
            lines.append(('No matches' if self.query else 'Literal, case-insensitive search', muted))
        lines.append(('Enter/Down/Tab next | Up/Shift+Tab previous | Esc close', muted))
        result = []
        for offset, (line, style) in enumerate(lines[:rows - first + 1]):
            position = f'\033[{first + offset};{left}H'
            chunks = wrap(base + style + line, width)
            chunk = chunks[-1] if offset == 1 else chunks[0]
            result.append(position + base + ' ' * width + position + base + style + chunk + base)
        result.append('\033[?25l')
        return ''.join(result)
