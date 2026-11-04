"""Source ranges and JSON colors for the notes editor."""
import json
import re

from tui_theme import TEXT, SECONDARY, SUCCESS, WARNING, ACCENT


def sections(text, excluded=()):
    result, fence, offset = [], None, 0
    for line in text.splitlines(keepends=True):
        if any(b['start'] <= offset < b['end'] for b in excluded):
            offset += len(line)
            continue
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)', line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif fence is None and re.match(r'^##\s+\S', line):
            if result:
                result[-1]['end'] = offset
            result.append(dict(start=offset, body=offset + len(line), end=len(text),
                               kind='section', label=line.strip()[3:].strip()))
        offset += len(line)
    return result


def prepare_paste(value):
    """Only complete JSON objects/arrays are reformatted; malformed text is literal."""
    stripped = value.strip()
    if stripped.startswith(('{', '[')):
        try:
            parsed = json.loads(stripped, parse_int=str, parse_float=str,
                                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            if isinstance(parsed, (dict, list)):
                # Re-indent original tokens: preserve numeric precision, duplicate
                # keys and escape spelling rather than round-tripping Python values.
                tokens = re.findall(r'"(?:\\.|[^"\\])*"|[^\s"{}\[\]:,]+|[{}\[\]:,]', stripped)
                output, depth = [], 0
                for index, token in enumerate(tokens):
                    previous = tokens[index - 1] if index else ''
                    following = tokens[index + 1] if index + 1 < len(tokens) else ''
                    if token in ('{', '['):
                        output.append(token)
                        depth += 1
                        if following not in ('}', ']'):
                            output.append('\n' + '  ' * depth)
                    elif token in ('}', ']'):
                        depth -= 1
                        if previous not in ('{', '['):
                            output.append('\n' + '  ' * depth)
                        output.append(token)
                    elif token == ',':
                        output.append(',\n' + '  ' * depth)
                    elif token == ':':
                        output.append(': ')
                    else:
                        output.append(token)
                value = '```json\n' + ''.join(output) + '\n```'
        except (ValueError, RecursionError):
            pass
    return value


def highlight_json(line):
    tokens = r'"(?:\\.|[^"\\])*"|\b(?:true|false|null)\b|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}\[\]:,]'
    def color(match):
        value = match[0]
        if value.startswith('"'):
            style = SECONDARY if line[match.end():].lstrip().startswith(':') else SUCCESS
        elif value in ('true', 'false', 'null'):
            style = ACCENT
        elif value[0].isdigit() or value[0] == '-':
            style = WARNING
        else:
            style = TEXT
        return style + value + TEXT
    return re.sub(tokens, color, line)
