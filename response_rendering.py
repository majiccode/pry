"""Small, dependency-free Markdown renderer for assistant prose."""
import re


RESET = '\033[0m\033[49m\033[38;5;252m'
ACCENT = '\033[38;5;109m'
MUTED = '\033[38;5;245m'
BOLD = '\033[1m'
SGR = re.compile(r'\033\[[0-9;]*m')
INLINE = re.compile(
    r'(`+)(.+?)\1|\*\*(.+?)\*\*|__(.+?)__|(?<!\w)\*([^*\n]+)\*(?!\w)'
    r'|\[([^\]\n]+)\]\(([^\s)]+)\)')


def render_markdown(text, width, wrap, cells):
    """Return physical rows; callers retain the original text for copy/actions."""
    width = max(1, width)
    # Model text is data, never terminal control sequences.
    text = re.sub(r'\033\[[0-?]*[ -/]*[@-~]|\033\][^\x07\033]*(?:\x07|\033\\)', '', text)
    text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)
    source = text.splitlines()
    rows = []
    base = wrap('', width)[0]

    def length(value):
        return sum(right - left + 1 for _, left, right in cells(value))

    def inline(value):
        def replace(match):
            if match[1]:
                return ACCENT + match[2] + RESET
            if match[3] or match[4]:
                return BOLD + (match[3] or match[4]) + '\033[22m'
            if match[5]:
                return '\033[3m' + match[5] + '\033[23m'
            label, url = match[6], match[7]
            return ACCENT + label + RESET + (f' ({url})' if label != url else '')
        return INLINE.sub(replace, value)

    def prose(value, prefix='', continuation=None):
        continuation = continuation if continuation is not None else ' ' * length(prefix)
        if length(prefix) >= width:
            prefix, continuation = '', ''
        line, used, styles = RESET + prefix, length(prefix), ''
        pending_space = False
        has_word = False
        for token in re.findall(r'\S+|[ \t]+', inline(value)):
            if token.isspace():
                pending_space = has_word
                continue
            size = length(token)
            if has_word and used + int(pending_space) + size > width:
                rows.extend(wrap(line, width))
                line, used = RESET + continuation + styles, length(continuation)
                pending_space = False
                has_word = False
            if pending_space:
                line += ' '
                used += 1
            # Use the terminal's grapheme-aware wrapper for unbroken long words.
            pieces = wrap(line + token, width)
            rows.extend(pieces[:-1])
            line = pieces[-1]
            # wrap() ends each row with its base style; remove that suffix so
            # inline emphasis can continue through subsequent words.
            if base and line.endswith(base):
                line = line[:-len(base)]
            used = length(line)
            for style in SGR.findall(token):
                styles = '' if style == '\033[0m' else styles + style
            has_word, pending_space = True, False
        rows.extend(wrap(line, width))

    def table_cells(line):
        return [part.strip() for part in re.split(r'(?<!\\)\|', line.strip().strip('|'))]

    index = 0
    fence = None
    while index < len(source):
        line = source[index]
        marker = re.match(r'^\s*(`{3,}|~{3,})(.*)$', line)
        if marker:
            if fence is None:
                fence = marker[1]
                prose(marker[2].strip() or 'code', prefix='')
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            else:
                rows.extend(wrap(RESET + line, width))
            index += 1
            continue
        if fence:
            rows.extend(wrap(RESET + line, width))
            index += 1
            continue
        if index + 1 < len(source) and '|' in line:
            headers = table_cells(line)
            separators = table_cells(source[index + 1])
            if len(headers) == len(separators) and all(re.fullmatch(r':?-{3,}:?', cell) for cell in separators):
                records = []
                index += 2
                while index < len(source) and '|' in source[index] and source[index].strip():
                    record = table_cells(source[index])
                    if len(record) != len(headers):
                        break
                    records.append(record)
                    index += 1
                sizes = [max(length(inline(row[col])) for row in [headers] + records) for col in range(len(headers))]
                if sum(sizes) + 3 * (len(sizes) - 1) <= width:
                    for row_index, record in enumerate([headers] + records):
                        formatted = ' | '.join(inline(value) + ' ' * (size - length(inline(value)))
                                               for value, size in zip(record, sizes))
                        rows.extend(wrap(RESET + (BOLD if row_index == 0 else '') + formatted, width))
                        if row_index == 0:
                            rows.extend(wrap(MUTED + '-+-'.join('-' * size for size in sizes), width))
                else:
                    for record in records or [headers]:
                        if records and record is not records[0]:
                            rows.append('')
                        for label, value in zip(headers, record):
                            prose(f'**{label}:** {value}')
                continue
        heading = re.match(r'^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?$', line)
        listing = re.match(r'^(\s*)([-+*]|\d+[.)])\s+(.*)$', line)
        quote = re.match(r'^\s*>\s?(.*)$', line)
        if heading:
            prose('**' + heading[1] + '**')
        elif re.fullmatch(r'\s*(?:\*\s*){3,}|\s*(?:-\s*){3,}|\s*(?:_\s*){3,}', line):
            rows.extend(wrap(MUTED + '-' * min(width, 48), width))
        elif listing:
            indent = ' ' * min(len(listing[1].expandtabs(4)), max(0, width - 4))
            marker_text = '-' if listing[2] in '-+*' else listing[2]
            prose(listing[3], indent + marker_text + ' ')
        elif quote:
            prose(quote[1], '> ', '> ')
        elif not line.strip():
            if rows and rows[-1] != '':
                rows.append('')
        else:
            prose(line.strip())
        index += 1
    return rows
