"""Read-only dashboard presentation with GitPanel's forms and search contract."""
import math
import re

from git_panel import GitPanel, diff_rows, visible
from tui_theme import PANEL, BORDER, BUTTON, FOCUSED_BUTTON, ERROR, WARNING, SUCCESS


class DashboardPanel(GitPanel):
    """Subclasses supply entries, actions, heading, activate, start and poll.

    Labels stay plain text; optional entry color and fraction affect only drawing.
    Cards are (label, value, ANSI color) tuples. No Git action is started here.
    """

    list_title = 'Overview'
    output_title = 'Details'
    footer = 'Enter inspect | Tab actions | r refresh | Ctrl+F find | Esc close'
    tabs = [('overview', 'Overview')]
    hints = {}

    def __init__(self, directory, auto_refresh=False):
        super().__init__(directory, auto_refresh=False)
        self.cards = []
        self.empty_message = 'No entries in this view.'
        self.output_visible = False
        self.output = ''
        self.message = ''
        self.tab = self.tabs[0][0] if self.tabs else ''
        if auto_refresh:
            self.start('refresh')

    def event(self, event, value):
        if self.form:
            return super().event(event, value)
        if event == 'key' and value == '\x03':
            if self.busy:
                self.cancel.set()
                self.message = 'Cancelling work...'
            return None
        if event == 'escape' and self.output_visible:
            self.output_visible = False
            self.focus = 'list'
            return None
        if event == 'wheel' and self.focus != 'output':
            event, value = 'scroll', value[0]
        if event == 'key':
            if value == 'r':
                return self.activate('refresh')
            if isinstance(value, str) and value in '123456789' and len(value) == 1:
                if int(value) <= len(self.tabs):
                    self.tab = self.tabs[int(value) - 1][0]
                    self.index = self.top = 0
                return None
            if value not in ('\t', '\r'):
                return None
        return super().event(event, value)

    def draw(self, cols, rows, wrap, styles, top=0):
        if self.form:
            # The inherited renderer owns editing, field clipping and form zones.
            return super().draw(cols, rows, wrap, styles, top)
        height = max(0, rows - top)
        self.zones = []
        if cols <= 0 or height <= 0:
            return ''
        base, muted, text, accent, selected = styles
        result = [f'\033[{row + top};1H' + base + ' ' * cols for row in range(1, height + 1)]
        content_end = max(0, height - 3)

        def put(row, col, value, width, color=muted, styled=False):
            if not (1 <= row <= height and 1 <= col <= cols and width > 0):
                return
            width = min(width, cols - col + 1)
            chunks = wrap(base + color + (value if styled else visible(value)), width)
            result.append(f'\033[{row + top};{col}H' + base + color + ' ' * width)
            if chunks:
                result.append(f'\033[{row + top};{col}H' + self.search.highlight(chunks[0]))

        def zone(row, left, right, action, argument=None):
            if 1 <= row <= content_end and 1 <= left <= right <= cols:
                self.zones.append((row + top, left, right, action, argument))

        def strip(items, current, row, kind):
            if not items or row > content_end:
                return
            current = max(0, min(current, len(items) - 1))
            room = cols - 4 if cols >= 5 else cols
            start = min(getattr(self, '_' + kind + '_start', 0), current)
            while start < current and sum(len(visible(label)) + 1 for label, _ in items[start:current + 1]) - 1 > room:
                start += 1
            setattr(self, '_' + kind + '_start', start)
            col = 3 if cols >= 5 else 1
            stop = start
            for index in range(start, len(items)):
                label, action = items[index]
                width = min(len(visible(label)), room - (col - (3 if cols >= 5 else 1)))
                if width <= 0 or index != start and width < len(visible(label)):
                    break
                color = PANEL + accent if kind == 'tab' else BUTTON
                if index == current:
                    color = selected if kind == 'tab' else FOCUSED_BUTTON if self.focus == 'actions' else BUTTON + accent
                put(row, col, label, width, color)
                zone(row, col, col + width - 1, 'tab' if kind == 'tab' else action,
                     action if kind == 'tab' else None)
                col += width + 1
                stop = index + 1
            if cols >= 5:
                for x, target, label in ((1, start - 1, '<'), (cols, stop, '>')):
                    if 0 <= target < len(items):
                        put(row, x, label, 1, accent)
                        zone(row, x, x, 'tab' if kind == 'tab' else 'action_step',
                             items[target][1] if kind == 'tab' else target - current)

        heading, subtitle = self.heading()
        if content_end >= 1:
            put(1, 1, heading, cols, PANEL + text)
        if content_end >= 2:
            put(2, 1, subtitle, cols, accent)
        tab_index = next((i for i, (name, _) in enumerate(self.tabs) if name == self.tab), 0)
        strip([(f' {i} {label} ', name) for i, (name, label) in enumerate(self.tabs, 1)], tab_index, 3, 'tab')
        actions = self.actions()
        self.action_index = max(0, min(self.action_index, len(actions) - 1))
        strip([('[' + label + ']', action) for label, action in actions], self.action_index, 4, 'action')
        start = 5
        # Cards yield to the list on short screens, rather than consuming its viewport.
        per_row = max(1, cols // 22)
        card_rows = math.ceil(len(self.cards) / per_row) * 2
        if self.cards and content_end - start - card_rows >= 4:
            for i, (label, value, color) in enumerate(self.cards):
                slot, column = divmod(i, per_row)
                width = cols // per_row
                x, y = column * width + 1, start + slot * 2
                put(y, x, ' ' + str(label), width - 1, PANEL + muted)
                put(y + 1, x, ' ' + str(value), width - 1, PANEL + (color or accent))
                targets = getattr(self, 'card_targets', [])
                if i < len(targets):
                    zone(y, x, x + width - 2, 'tab', targets[i])
                    zone(y + 1, x, x + width - 2, 'tab', targets[i])
            start += card_rows
        wide = cols >= 96 and self.output_visible
        left_width = max(36, cols * 5 // 9) if wide else cols
        available = max(0, content_end - start)
        show_output = self.output_visible and (wide or available >= 7)
        list_height = available if wide or not show_output else max(4, (available - 1) * 3 // 5)
        out_col = left_width + 2 if wide else 1
        out_heading = start if wide else start + list_height + 1
        out_height = max(0, content_end - out_heading) if show_output else 0
        entries = self.entries()
        self.index = max(0, min(self.index, len(entries) - 1))
        self.top = max(0, min(self.top, self.index))
        if self.index >= self.top + max(1, list_height):
            self.top = self.index - max(1, list_height) + 1
        if start <= content_end:
            put(start, 1, ('> ' if self.focus == 'list' else '  ') + self.list_title + f' ({len(entries)})', left_width, PANEL + accent)
        for n, entry in enumerate(entries[self.top:self.top + list_height]):
            row, index = start + n + 1, self.top + n
            color = selected if index == self.index else entry.get('color') or text
            fraction = entry.get('fraction')
            bar_width = min(12, max(0, left_width // 5)) if fraction is not None and left_width >= 40 else 0
            label_width = left_width - bar_width - (10 if bar_width else 0)
            put(row, 1, ('> ' if index == self.index else '  ') + entry['label'], label_width, color)
            if bar_width:
                try:
                    fraction = max(0.0, min(1.0, float(fraction)))
                    filled = round(fraction * bar_width) if math.isfinite(fraction) else 0
                except (TypeError, ValueError):
                    fraction, filled = 0.0, 0
                if not math.isfinite(fraction):
                    fraction = 0.0
                put(row, left_width - bar_width - 8, f'{fraction:6.1%}', 6, color)
                put(row, left_width - bar_width, '#' * filled + '.' * (bar_width - filled), bar_width, color)
            zone(row, 1, left_width, 'entry', index)
        if not entries and list_height:
            put(start + 1, 1, self.empty_message, left_width)
        if wide:
            for row in range(start, content_end + 1):
                put(row, left_width + 1, '|', 1, BORDER)
        self._output_line_rows = {}
        width = max(1, cols - out_col + 1)
        if self.output_is_diff:
            physical = diff_rows(self.output, width, wrap, styles, self._output_line_rows)
        else:
            physical = []
            for number, line in enumerate(self.output.splitlines()):
                self._output_line_rows[number] = len(physical)
                physical.extend(wrap(base + text + visible(line), width))
        self.output_rows = len(physical)
        if self._search_output_line is not None:
            self.offset = self._output_line_rows.get(self._search_output_line, 0)
            if not self.search.active:
                self._search_output_line = None
        self.offset = max(0, min(self.offset, max(0, len(physical) - max(1, out_height))))
        if show_output:
            put(out_heading, out_col, ('> ' if self.focus == 'output' else '  ') + self.output_title, width, PANEL + accent)
            for row in range(out_heading, content_end + 1):
                zone(row, out_col, cols, 'output_focus')
            for n, line in enumerate(physical[self.offset:self.offset + out_height]):
                put(out_heading + n + 1, out_col, line, width, text, styled=True)
        status = ERROR if self.message.startswith(('Failed', 'Cannot')) else WARNING if self.busy else SUCCESS
        put(height - 2, 1, self.message, cols, status)
        context = (f'Action {self.action_index + 1}/{len(actions)} | Left/Right choose | Enter apply' if self.focus == 'actions'
                   else f'{self.index + 1 if entries else 0}/{len(entries)} selected | Enter inspect | Tab actions')
        put(height - 1, 1, context, cols)
        put(height, 1, self.footer, cols)
        overlay = self.search.draw(cols, height, wrap, styles)
        if top:
            overlay = re.sub(r'\033\[(\d+);(\d+)H', lambda m: f'\033[{int(m[1]) + top};{m[2]}H', overlay)
        result.extend([overlay, '\033[?25l'])
        return ''.join(result)
