"""Unstyled terminal row measurements, matching Pry's grapheme-aware wrapper."""
import re
import unicodedata

SGR = re.compile(r'\033\[[0-9;]*m')


def wrapped_height(text, width):
    text = SGR.sub('', text)
    width = max(1, width)
    index, column, rows = 0, 0, 1
    while index < len(text):
        char = text[index]
        index += 1
        if char == '\t':
            sizes = [1] * (8 - column % 8)
        elif unicodedata.category(char).startswith('C'):
            continue
        else:
            unit = char
            while index < len(text):
                following = text[index]
                if unicodedata.combining(following) or following in ('\ufe0e', '\ufe0f') or '\U0001f3fb' <= following <= '\U0001f3ff':
                    unit += following
                    index += 1
                elif following == '\u200d' and index + 1 < len(text):
                    unit += text[index:index + 2]
                    index += 2
                elif len(unit) == 1 and '\U0001f1e6' <= unit <= '\U0001f1ff' and '\U0001f1e6' <= following <= '\U0001f1ff':
                    unit += following
                    index += 1
                else:
                    break
            size = max((0 if unicodedata.combining(c) or c in ('\ufe0e', '\ufe0f', '\u200d')
                        else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in unit), default=0)
            if '\ufe0f' in unit or '\u200d' in unit or any('\U0001f1e6' <= c <= '\U0001f1ff' for c in unit):
                size = 2
            sizes = [size if size <= width else 1]
        for size in sizes:
            if column + size > width:
                rows, column = rows + 1, 0
            column += size
    return rows
