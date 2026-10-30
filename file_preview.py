"""Presentation-only file previews; page text remains suitable for copy/context."""
from datetime import datetime
from pathlib import Path
import re
from tui_theme import TEXT as PLAIN, MUTED, ACCENT, SUCCESS as STRING, WARNING as NUMBER


LANGUAGES = {
    '.py': 'Python', '.js': 'JavaScript', '.jsx': 'JavaScript',
    '.ts': 'TypeScript', '.tsx': 'TypeScript', '.json': 'JSON',
    '.sh': 'Shell', '.bash': 'Shell', '.zsh': 'Shell',
    '.yaml': 'YAML', '.yml': 'YAML', '.toml': 'TOML', '.ini': 'INI',
    '.cfg': 'INI', '.conf': 'Config', '.md': 'Markdown', '.txt': 'Text',
    '.css': 'CSS', '.html': 'HTML', '.xml': 'XML', '.sql': 'SQL',
    '.go': 'Go', '.rs': 'Rust', '.c': 'C', '.h': 'C', '.cpp': 'C++',
}
KEYWORDS = set(('and as assert async await break case catch class const continue '
                'def default del do elif else except export false False finally '
                'fn for from function if impl import in interface is let match '
                'new None not null of or pass raise return select self struct '
                'switch then throw true True try type var while with yield').split())
TOKENS = re.compile(r'''(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`[^`]*`)|(?P<word>\b[A-Za-z_]\w*\b)|(?P<number>\b\d+(?:\.\d+)?\b)''')


def preview_metadata(path, text, start=1, highlight=None):
    path = Path(path)
    stat = path.stat()
    return dict(name=path.name, size=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                start=start, highlight=highlight, total_lines=len(text.splitlines()))


def render_file_preview(text, metadata, width, wrap, filter_text=''):
    """Return a summary and numbered physical rows within the available width."""
    width = max(1, width)
    language = LANGUAGES.get(Path(metadata['name']).suffix.lower(), 'Text')
    if metadata['name'] in ('Dockerfile', 'Makefile'):
        language = metadata['name']
    text = re.sub(r'\033\[[0-?]*[ -/]*[@-~]|\033\][^\x07\033]*(?:\x07|\033\\)', '', text)
    text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)
    lines = text.splitlines()
    start = metadata.get('start', 1)
    digits = len(str(start + max(0, len(lines) - 1)))
    gutter = digits + 4 if width >= digits + 12 else 0
    body_width = max(1, width - gutter)
    rows = []

    def style(line):
        if language in ('Text', 'Markdown'):
            return PLAIN + line
        comment_marker = '#' if language in ('Python', 'Shell', 'YAML', 'TOML', 'INI', 'Config', 'Dockerfile', 'Makefile') else '//' if language in ('JavaScript', 'TypeScript', 'Go', 'Rust', 'C', 'C++') else '--' if language == 'SQL' else None
        result, position = PLAIN, 0
        # Strings are consumed before looking for comment markers so a URL or
        # hash inside a quoted value is not mistaken for a comment.
        for token in TOKENS.finditer(line):
            gap = line[position:token.start()]
            if comment_marker and comment_marker in gap:
                offset = position + gap.index(comment_marker)
                return result + line[position:offset] + MUTED + line[offset:]
            result += gap
            value = token.group()
            color = STRING if token.lastgroup == 'string' else NUMBER if token.lastgroup == 'number' else ACCENT if value in KEYWORDS else PLAIN
            result += color + value + PLAIN
            position = token.end()
        tail = line[position:]
        if comment_marker and comment_marker in tail:
            before, after = tail.split(comment_marker, 1)
            return result + before + MUTED + comment_marker + after
        return result + tail

    for offset, line in enumerate(lines):
        if filter_text and filter_text.casefold() not in line.casefold():
            continue
        number = start + offset
        # Expand tabs relative to source column, not the line-number gutter.
        physical = wrap(style(line.expandtabs(4)), body_width)
        for index, part in enumerate(physical):
            prefix = ''
            if gutter:
                marker = '>' if number == metadata.get('highlight') and index == 0 else ' '
                label = str(number).rjust(digits) if index == 0 else ' ' * digits
                prefix = (ACCENT if marker == '>' else MUTED) + marker + label + ' | '
            rows.append(prefix + part)
    count = metadata.get('total_lines', len(lines))
    details = [language, f'{count:,} lines', f'{metadata["size"]:,} bytes']
    if metadata.get('modified'):
        details.append('modified ' + metadata['modified'])
    if start != 1 or len(lines) != count:
        details.append(f'lines {start}-{start + len(lines) - 1}')
    if filter_text:
        details.append(f'{sum(filter_text.casefold() in line.casefold() for line in lines)} matching lines')
    if not rows:
        rows = list(wrap(MUTED + ('No matching lines.' if filter_text else '(empty file)'), width))
    return ' | '.join(details), rows
