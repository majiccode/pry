"""Graphics-protocol presentation for note graphs, with text fallback.

Kitty, iTerm2, and Sixel are supported without third-party packages. Backend
selection is environment-driven when possible and can be forced with
``PRY_GRAPH_RENDERER=kitty|iterm2|sixel|sextant|quadrant``. Kitty and Sixel are
also probed at startup; unsupported terminals simply fall back to blocks.
"""
import base64
import os
import re
import struct
import zlib


_BACKEND = None
_PROBED = False
_FORCED = os.environ.get('PRY_GRAPH_RENDERER', '').strip().lower() in (
    'kitty', 'iterm2', 'sixel', 'sextant', 'quadrant')
_CELL_SIZE = None
_KITTY_PROBE = '\033_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\033\\'
_SIXEL_PROBE = '\033[c'
_SIXEL_PIXEL_LIMIT = 450000


def _detect(env):
    override = env.get('PRY_GRAPH_RENDERER', '').strip().lower()
    if override in ('kitty', 'iterm2', 'sixel', 'sextant', 'quadrant'):
        return override
    term = env.get('TERM', '').lower()
    program = env.get('TERM_PROGRAM', '').lower()
    if env.get('KITTY_WINDOW_ID') or 'kitty' in term or program in (
            'ghostty', 'wezterm', 'kitty'):
        return 'kitty'
    if program in ('iterm.app', 'iterm2') or env.get('LC_TERMINAL', '').lower() in (
            'iterm2', 'iterm'):
        return 'iterm2'
    if 'sixel' in term or 'mlterm' in term or 'foot' in term or program in ('foot', 'mlterm'):
        return 'sixel'
    return None


def _text_fallback(env):
    override = env.get('PRY_GRAPH_TEXT', '').strip().lower()
    if override in ('sextant', 'quadrant'):
        return override
    term = env.get('TERM', '').lower()
    program = env.get('TERM_PROGRAM', '').lower()
    if env.get('KITTY_WINDOW_ID') or 'kitty' in term or program in (
            'ghostty', 'wezterm', 'kitty', 'iterm.app', 'iterm2'):
        return 'sextant'
    return 'quadrant'


def backend():
    global _BACKEND, _FORCED
    if _BACKEND is None:
        override = os.environ.get('PRY_GRAPH_RENDERER', '').strip().lower()
        _FORCED = override in ('kitty', 'iterm2', 'sixel', 'sextant', 'quadrant')
        _BACKEND = _detect(os.environ) or _text_fallback(os.environ)
    return _BACKEND


def text_scheme():
    return backend() if backend() in ('sextant', 'quadrant') else _text_fallback(os.environ)


def is_graphics():
    return backend() in ('kitty', 'iterm2', 'sixel')


def probe_sequence():
    global _PROBED
    if _PROBED or _FORCED:
        return ''
    _PROBED = True
    return _KITTY_PROBE + _SIXEL_PROBE


def observe_probe(payload):
    global _BACKEND
    if _FORCED:
        return False
    payload = payload or ''
    new = None
    if payload.startswith('G') and ';OK' in payload:
        new = 'kitty'
    elif payload.startswith('?') and re.search(r'(?:[?;])4(?:;|c$)', payload):
        new = 'sixel'
    if new is None:
        return False
    if _BACKEND in (None, 'quadrant', 'sextant'):
        _BACKEND = new
        return True
    return False


def observe_cell(height, width):
    global _CELL_SIZE
    if 0 < height <= 4096 and 0 < width <= 4096:
        _CELL_SIZE = (width, height)


def cell_size():
    return _CELL_SIZE or (8, 16)


def pixel_size(cell_cols, cell_rows, limit=None):
    cell_width, cell_height = cell_size()
    width = max(1, cell_cols * cell_width)
    height = max(1, cell_rows * cell_height)
    if backend() == 'sixel':
        return width, height
    if limit is None:
        limit = 200000
    pixels = width * height
    if pixels > limit:
        scale = (limit / pixels) ** 0.5
        width = max(cell_cols, int(width * scale))
        height = max(cell_rows, int(height * scale))
    return width, height


def supports_size(pixel_width, pixel_height):
    if backend() != 'sixel':
        return True
    return (0 < pixel_width <= 2000 and 0 < pixel_height <= 2000
            and pixel_width * pixel_height <= _SIXEL_PIXEL_LIMIT)


def delete_sequence():
    return '\033_Ga=d,d=A,q=2\033\\' if backend() == 'kitty' else ''


def _indexed_rgb(index):
    if index < 16:
        basic = ((0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16),
                 (36, 114, 200), (188, 63, 188), (17, 168, 205), (229, 229, 229),
                 (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67),
                 (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255))
        return basic[index]
    if index < 232:
        index -= 16
        levels = (0, 95, 135, 175, 215, 255)
        return levels[index // 36], levels[(index // 6) % 6], levels[index % 6]
    level = 8 + 10 * max(0, min(23, index - 232))
    return level, level, level


def _sgr_rgb(style):
    match = re.search(r'(?:38|48);5;(\d+)', style)
    if match:
        return _indexed_rgb(int(match[1]))
    match = re.search(r'(?:38|48);2;(\d+);(\d+);(\d+)', style)
    if match:
        return tuple(max(0, min(255, int(value))) for value in match.groups())
    return _indexed_rgb(252)


def _rgb_pixels(raster):
    palette = [_sgr_rgb(style) for style in raster.palette]
    tables = []
    for channel in range(3):
        tables.append(bytes(palette[index][channel] if index < len(palette) else 0
                            for index in range(256)))
    source = bytes(raster.pixels)
    output = bytearray(len(source) * 3)
    output[0::3] = source.translate(tables[0])
    output[1::3] = source.translate(tables[1])
    output[2::3] = source.translate(tables[2])
    return bytes(output)


def _png(raster):
    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload
                + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff))

    width, height = raster.width, raster.height
    rgb = _rgb_pixels(raster)
    stride = width * 3
    raw = b''.join(b'\x00' + rgb[row * stride:(row + 1) * stride]
                   for row in range(height))
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def _kitty(raster, row, col, cell_cols, cell_rows):
    payload = base64.b64encode(_png(raster)).decode('ascii')
    control = f'a=T,f=100,c={cell_cols},r={cell_rows},q=2'
    parts = [f'\0337\033[{row};{col}H']
    for offset in range(0, len(payload), 4096):
        chunk = payload[offset:offset + 4096]
        more = '1' if offset + 4096 < len(payload) else '0'
        if offset == 0:
            parts.append(f'\033_G{control},m={more};{chunk}\033\\')
        else:
            parts.append(f'\033_Gm={more};{chunk}\033\\')
    parts.append('\0338')
    return ''.join(parts)


def _iterm2(raster, row, col, cell_cols, cell_rows):
    data = base64.b64encode(_png(raster)).decode('ascii')
    return (f'\0337\033[{row};{col}H\033]1337;File=inline=1;'
            f'width={cell_cols};height={cell_rows};preserveAspectRatio=0:'
            f'{data}\007\0338')


def _runs(values):
    output = []
    index = 0
    while index < len(values):
        end = index + 1
        while end < len(values) and values[end] == values[index]:
            end += 1
        run = end - index
        if run >= 4:
            output.append(f'!{run}{values[index]}')
        else:
            output.append(values[index] * run)
        index = end
    return ''.join(output)


def _sixel(raster, row, col):
    palette = [_sgr_rgb(style) for style in raster.palette]
    if len(palette) > 256:
        palette = palette[:256]
    output = ['\033P0;1;0q']
    for index, (red, green, blue) in enumerate(palette):
        output.append(f'#{index};2;{round(red * 100 / 255)};'
                      f'{round(green * 100 / 255)};{round(blue * 100 / 255)}')
    width, height = raster.width, raster.height
    pixels = raster.pixels
    blank = b'\x00' * width
    patterns = {}
    for top in range(0, height, 6):
        # Interleave six rows so each column becomes one 48-bit key.
        rows = [pixels[(top + bit) * width:(top + bit + 1) * width]
                if top + bit < height else blank for bit in range(6)]
        columns = [['?'] * width for _ in palette]
        present = [False] * len(palette)
        for x, values in enumerate(zip(*rows)):
            key = 0
            for bit, color in enumerate(values):
                key |= color << (bit * 8)
            pattern = patterns.get(key)
            if pattern is None:
                pattern = []
                for color in range(len(palette)):
                    mask = 0
                    for bit, value in enumerate(values):
                        if value == color:
                            mask |= 1 << bit
                    if mask:
                        pattern.append((color, mask))
                patterns[key] = pattern
            for color, mask in pattern:
                columns[color][x] = chr(63 + mask)
                present[color] = True
        for color, used in enumerate(present):
            if used:
                output.append(f'#{color}' + _runs(columns[color]) + '$')
        output.append('-')
    output.append('\033\\')
    return '\0337\033[%d;%dH' % (row, col) + ''.join(output) + '\0338'


def image_sequence(raster, row, col, cell_cols, cell_rows):
    kind = backend()
    if kind == 'kitty':
        return _kitty(raster, row, col, cell_cols, cell_rows)
    if kind == 'iterm2':
        return _iterm2(raster, row, col, cell_cols, cell_rows)
    if kind == 'sixel':
        return _sixel(raster, row, col)
    return ''
