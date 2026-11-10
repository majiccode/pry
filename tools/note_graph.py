"""Bounded note graphs, without eval, exec, or third-party dependencies.

One statement per line; # comments and semicolon separators are supported.
Ranges have constant endpoints. A scalar assignment to a ranged name sets its
initial value (and must also be constant). Other assignments are live formulas.
Each plot captures all formula definitions preceding it.

Simple plots let the graph work out what to draw:

    x_axis=[-1..1]
    y_axis=[-1..1]
    a=[-1..1]
    y=sin(x*tau)*a
    plot(y)              # y=f(x), with a as a slider
    t=[0..1]
    plotxy(cos(t*tau), sin(t*tau))   # parametric
    f=x^2+y^2-1
    plot(f)              # implicit f(x,y)=0
    a=[3,1]
    b=[-1,2]
    plotvec(a,b)         # 2D arrows, one color per argument
    plotvec([[1,0],[0.5,0.5]])       # a list of vectors

`plot(x)` and `plot(y)` also understand `x=f(y)` and `y=f(x)` when the
expression uses the plane coordinate directly. `plot(a,b,...)` draws all of
the supplied functions on one set of axes. Parameters are inferred from the
ranged variables used by the plotted expressions.

The older explicit form `plot(output,sweep,parameters...)` remains supported.
`axisx`/`axisy` and `x_axis`/`y_axis` set the visible ranges; axis range
handles appear as sliders alongside the parameter sliders.

render(width, height) returns at most height rows of at most width visible
columns, capped at 240 columns and 64 rows. Height includes the title/legend,
axis labels, and readout, not slider controls. Quadrant/half-block characters
provide a 2-by-2 subcell grid; sextants provide 2-by-3. render_image() returns
palette pixels for Kitty, iTerm2, and Sixel renderers. ANSI SGR sequences do not
count toward the width. Small viewports gracefully omit drawing rows.
"""

import ast
from collections.abc import Mapping
from dataclasses import dataclass
import math
import operator
import re
import time

from tui_theme import ACCENT, BACKGROUND, BASE, BORDER, MUTED, SECONDARY, SUCCESS, WARNING


EXAMPLE_SOURCE = """x_axis=[-2..2]
y_axis=[-2.5..2.5]

# animated waves: several coloured curves on one set of axes
a=[0.2..1.5]
a=1
wave=sin(x*tau+time)*a
echo=cos(x*tau*2-time)*a*0.7
plot(cyan: wave, orange: echo)

# parametric orbit: t sweeps the curve, time animates it
t=[0..1]
amp=[0.2..1.5]
amp=1
plotxy(magenta: sin(3*t*tau+time)*amp, cos(2*t*tau+time)*amp)

# rotating vectors: each argument gets its own colour
spin=[[cos(time),-sin(time)],[sin(time),cos(time)]]
v=matmul(spin,[1.5,0.5])
w=matmul(spin,[-0.5,1.2])
plotvec(blue: v, green: w, yellow: [1.5,0.5])

# implicit circle with an adjustable radius
r=[0.5..2]
r=1
f=x^2+y^2-r^2
plot(red: f)"""

_LIMIT = 1e100


def _cbrt(value):
    return -((-value) ** (1 / 3)) if value < 0 else value ** (1 / 3)


# name: (callable, minimum args, maximum args, elementwise over vectors)
_FUNCTIONS = {
    'sin': (math.sin, 1, 1, True), 'cos': (math.cos, 1, 1, True),
    'tan': (math.tan, 1, 1, True), 'asin': (math.asin, 1, 1, True),
    'acos': (math.acos, 1, 1, True), 'atan': (math.atan, 1, 1, True),
    'atan2': (math.atan2, 2, 2, True),
    'sec': (lambda x: 1 / math.cos(x), 1, 1, True),
    'csc': (lambda x: 1 / math.sin(x), 1, 1, True),
    'cot': (lambda x: 1 / math.tan(x), 1, 1, True),
    'sqrt': (math.sqrt, 1, 1, True),
    'cbrt': (_cbrt, 1, 1, True),
    'log': (math.log, 1, 2, True), 'log10': (math.log10, 1, 1, True),
    'log2': (math.log2, 1, 1, True), 'log1p': (math.log1p, 1, 1, True),
    'exp': (math.exp, 1, 1, True), 'expm1': (math.expm1, 1, 1, True),
    'sinh': (math.sinh, 1, 1, True), 'cosh': (math.cosh, 1, 1, True),
    'tanh': (math.tanh, 1, 1, True), 'asinh': (math.asinh, 1, 1, True),
    'acosh': (math.acosh, 1, 1, True), 'atanh': (math.atanh, 1, 1, True),
    'abs': (abs, 1, 1, True), 'sign': (lambda x: (x > 0) - (x < 0), 1, 1, True),
    'floor': (math.floor, 1, 1, True), 'ceil': (math.ceil, 1, 1, True),
    'round': (round, 1, 1, True), 'trunc': (math.trunc, 1, 1, True),
    'pow': (operator.pow, 2, 2, True), 'hypot': (math.hypot, 2, 2, True),
    'degrees': (math.degrees, 1, 1, True), 'radians': (math.radians, 1, 1, True),
    'min': (min, 2, 16, False), 'max': (max, 2, 16, False),
    'clamp': (lambda x, lo, hi: max(lo, min(hi, x)), 3, 3, True),
}
_VECTOR_FUNCTIONS = {
    'vec': (2, 3), 'vector': (2, 3), 'dot': (2, 2), 'cross': (2, 2),
    'norm': (1, 1), 'length': (1, 1), 'mag': (1, 1),
    'normalize': (1, 1), 'unit': (1, 1), 'angle': (1, 2),
    'sum': (1, 1), 'mean': (1, 1), 'prod': (1, 1),
    'rows': (1, 1), 'cols': (1, 1), 'transpose': (1, 1),
    'det': (1, 1), 'determinant': (1, 1), 'inverse': (1, 1), 'inv': (1, 1),
    'matmul': (2, 2), 'identity': (1, 1), 'rotate': (2, 2),
}
_CONSTANTS = {'pi': math.pi, 'PI': math.pi, 'e': math.e, 'E': math.e,
              'tau': math.tau, 'TAU': math.tau}
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
_PALETTE = (SECONDARY, SUCCESS, WARNING, '\033[38;5;213m',
            '\033[38;5;81m', '\033[38;5;203m', '\033[38;5;229m',
            '\033[38;5;141m')
_COLORS = {
    'red': '\033[38;5;203m', 'orange': '\033[38;5;215m',
    'yellow': '\033[38;5;229m', 'green': '\033[38;5;150m',
    'cyan': '\033[38;5;81m', 'blue': '\033[38;5;110m',
    'purple': '\033[38;5;141m', 'magenta': '\033[38;5;213m',
    'pink': '\033[38;5;218m', 'white': '\033[38;5;252m',
    'black': '\033[38;5;232m', 'gray': '\033[38;5;245m',
    'grey': '\033[38;5;245m', 'lime': '\033[38;5;118m',
    'teal': '\033[38;5;37m', 'navy': '\033[38;5;25m',
    'gold': '\033[38;5;220m', 'violet': '\033[38;5;129m',
    'brown': '\033[38;5;130m', 'coral': '\033[38;5;209m',
    'salmon': '\033[38;5;216m', 'turquoise': '\033[38;5;80m',
    'silver': '\033[38;5;250m',
}
_PAN_SENSITIVITY = 0.3
_NAME = r'[A-Za-z_][A-Za-z_0-9]{0,31}'
_QUADRANTS = (' ', '▘', '▝', '▀', '▖', '▌', '▞', '▛',
              '▗', '▚', '▐', '▜', '▄', '▙', '▟', '█')
_SEXTANTS = (
    ' ', '🬀', '🬁', '🬂', '🬃', '🬄', '🬅', '🬆',
    '🬇', '🬈', '🬉', '🬊', '🬋', '🬌', '🬍', '🬎',
    '🬏', '🬐', '🬑', '🬒', '🬓', '▌', '🬔', '🬕',
    '🬖', '🬗', '🬘', '🬙', '🬚', '🬛', '🬜', '🬝',
    '🬞', '🬟', '🬠', '🬡', '🬢', '🬣', '🬤', '🬥',
    '🬦', '🬧', '▐', '🬨', '🬩', '🬪', '🬫', '🬬',
    '🬭', '🬮', '🬯', '🬰', '🬱', '🬲', '🬳', '🬴',
    '🬵', '🬶', '🬷', '🬸', '🬹', '🬺', '🬻', '█',
)
_GLYPHS = {'quadrant': (2, 2, _QUADRANTS), 'sextant': (2, 3, _SEXTANTS)}
_AXIS_DECLARATIONS = {'axisx': 'x', 'axisy': 'y', 'x_axis': 'x', 'y_axis': 'y',
                      'xrange': 'x', 'yrange': 'y'}
_AXIS_CONTROL_SUFFIX = {'.min': 0, '.max': 1}

GRAPH_HELP = (
    'Graph expressions - F1/Esc close, Up/Down scroll',
    '',
    'DECLARATIONS',
    '  a=[lo..hi]          slider range',
    '  a=value             slider initial value (must be constant)',
    '  f=expression        formula used by later plot(...) calls',
    '  v=[1,2]             2D vector literal',
    '  m=[[1,2],[3,4]]     matrix literal (rows of numbers)',
    '',
    'PLOTS',
    '  plot(expr,...)          y=f(x); x/y coordinates make an implicit curve',
    '  plotxy(xexpr,yexpr)     parametric curve',
    '  plotvec(v,...)           arrows from the origin, one color per argument',
    '  plotvec([[1,2],[3,4]])  a list of vectors',
    '  plotvec([x0,y0,x1,y1])  a directed segment',
    '',
    'COLOURS',
    '  plot(red: sin(x), cyan: cos(x))   colour each curve',
    '  plot(sin(x), cos(x), color=orange) same colour for all',
    '  plot(a, b, color=red, color=blue) one colour per curve',
    '  plotvec(blue: v, red: w)          colour vector lists',
    '  names: red orange yellow green cyan blue purple magenta',
    '         pink white gray lime teal navy gold violet brown',
    '         coral salmon turquoise silver',
    '  also color=196 (xterm-256) or color=0xff8800 / rgb(255,136,0)',
    '',
    'CONSTANTS',
    '  pi  tau  e            (uppercase PI, TAU, E also work)',
    '',
    'ARITHMETIC',
    '  +  -  *  /  %  **  ^   ^ means power',
    '  scalar*vector and vector/scalar scale; vectors otherwise combine',
    '  element by element and must have matching shapes',
    '',
    'SCALAR FUNCTIONS (also applied elementwise to vectors/matrices)',
    '  sin cos tan sec csc cot asin acos atan atan2',
    '  sinh cosh tanh asinh acosh atanh',
    '  sqrt cbrt exp expm1 log log2 log10 log1p',
    '  abs sign floor ceil round trunc',
    '  pow hypot degrees radians clamp',
    '  min max (two or more scalar arguments)',
    '',
    'VECTOR AND MATRIX FUNCTIONS',
    '  vec(x,y)  vector(x,y)      build a vector (2 or 3 components)',
    '  dot(a,b)                   dot product',
    '  cross(a,b)                 2D scalar or 3D vector cross product',
    '  norm(v)  length(v)  mag(v) Euclidean length',
    '  normalize(v)  unit(v)      unit vector',
    '  angle(v)  angle(a,b)        direction angle or angle between vectors',
    '  sum(v)  mean(v)  prod(v)   aggregate all components',
    '  rows(m)  cols(m)           matrix dimensions',
    '  transpose(m)               swap matrix rows and columns',
    '  det(m)  inverse(m)         determinant / inverse (2x2, 3x3)',
    '  matmul(a,b)                matrix*matrix, matrix*vector, vector*matrix',
    '  identity(n)                n x n identity matrix',
    '  rotate(v,angle)            rotate a 2D vector',
    '',
    'ANIMATION',
    '  time                       elapsed seconds since the graph opened',
    '  dt                         seconds since the previous frame (~0.016)',
    '  time drives motion: y=sin(time*tau)',
    '  dt is the frame duration: pace=clamp(dt*60,0,1)',
    '  plot(cyan: y, purple: pace)',
    '',
    'EXAMPLES',
    '  a=[3,1]',
    '  b=[-1,2]',
    '  plotvec(red: a, blue: b, green: a+b)',
    '  plot(red: sin(x), cyan: cos(x))',
    '  s=a+b',
    '  plotvec(s)',
    '  vs=[[1,0],[0.7,0.7],[-1,1]]',
    '  plotvec(vs)',
    '  t=[0..1]',
    '  plotvec([cos(t*tau),sin(t*tau)])',
    '  v=5+5*sin(time)',
    '  plot(v)',
)


class _BudgetExceeded(Exception):
    pass


def _error(line, message):
    return ValueError(f'line {line}: {message}')


def _finite(value):
    if type(value) is not int and type(value) is not float:
        raise ValueError('expected a real number')
    value = float(value)
    if not -_LIMIT <= value <= _LIMIT:
        raise ValueError('number is non-finite or exceeds 1e100')
    return value


def _value(value):
    """Validate and normalise a scalar, vector, or matrix result."""
    if isinstance(value, bool):
        raise ValueError('expected a real number')
    if isinstance(value, (int, float)):
        return _finite(value)
    if isinstance(value, (list, tuple)):
        if not 1 <= len(value) <= 32:
            raise ValueError('vector or matrix must have 1..32 entries')
        if all(isinstance(item, (int, float)) and not isinstance(item, bool)
               for item in value):
            return tuple(_finite(item) for item in value)
        if all(isinstance(item, (list, tuple)) for item in value):
            rows = tuple(_value(item) for item in value)
            if not all(isinstance(row, tuple) and row
                       and not isinstance(row[0], tuple) for row in rows):
                raise ValueError('matrices may only contain vectors')
            width = len(rows[0])
            if any(len(row) != width for row in rows):
                raise ValueError('matrix rows must have equal length')
            return rows
    raise ValueError('expected a number, vector, or matrix')


def _is_sequence(value):
    return type(value) is tuple


def _same_shape(left, right):
    if _is_sequence(left) != _is_sequence(right):
        return False
    if not _is_sequence(left):
        return True
    return (len(left) == len(right)
            and all(_same_shape(a, b) for a, b in zip(left, right)))


def _scale(value, factor):
    factor = _finite(factor)
    if _is_sequence(value):
        return tuple(_scale(item, factor) for item in value)
    return _finite(value * factor)


def _zip_values(left, right, function):
    if _is_sequence(left) or _is_sequence(right):
        if not _same_shape(left, right):
            raise ValueError('vector or matrix shapes do not match')
        return tuple(_zip_values(a, b, function) for a, b in zip(left, right))
    return _finite(function(left, right))


def _broadcast(function, args):
    sequence = next((arg for arg in args if _is_sequence(arg)), None)
    if sequence is None:
        return _finite(function(*args))
    for arg in args:
        if _is_sequence(arg) and not _same_shape(arg, sequence):
            raise ValueError('vector or matrix shapes do not match')
    if sequence and _is_sequence(sequence[0]):
        return tuple(_broadcast(function, [arg[index] if _is_sequence(arg) else arg
                                          for arg in args])
                     for index in range(len(sequence)))
    return tuple(_finite(function(*(arg[index] if _is_sequence(arg) else arg
                                   for arg in args)))
                 for index in range(len(sequence)))


def _as_vector(value, size=None):
    if not _is_sequence(value) or (value and _is_sequence(value[0])):
        raise ValueError('expected a vector')
    if size is not None and len(value) != size:
        raise ValueError(f'expected a {size}D vector')
    return value


def _as_matrix(value):
    if _is_sequence(value) and value and _is_sequence(value[0]):
        width = len(value[0])
        if not 2 <= len(value) <= 8 or not 1 <= width <= 8:
            raise ValueError('matrix must be 2..8 by 1..8')
        if any(len(row) != width for row in value):
            raise ValueError('matrix rows must have equal length')
        return value
    raise ValueError('expected a matrix')


def _flatten(value):
    if _is_sequence(value):
        for item in value:
            yield from _flatten(item)
    else:
        yield value


def _determinant(matrix):
    size = len(matrix)
    if size == 2:
        return _finite(matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0])
    if size == 3:
        a, b, c = matrix
        return _finite(a[0] * (b[1] * c[2] - b[2] * c[1])
                       - a[1] * (b[0] * c[2] - b[2] * c[0])
                       + a[2] * (b[0] * c[1] - b[1] * c[0]))
    raise ValueError('determinant supports 2x2 and 3x3 matrices')


def _inverse(matrix):
    size = len(matrix)
    if size == 2:
        det = _determinant(matrix)
        if det == 0:
            raise ValueError('matrix is singular')
        return ((_finite(matrix[1][1] / det), _finite(-matrix[0][1] / det)),
                (_finite(-matrix[1][0] / det), _finite(matrix[0][0] / det)))
    if size == 3:
        a, b, c = matrix
        det = _determinant(matrix)
        if det == 0:
            raise ValueError('matrix is singular')
        cofactors = (
            (b[1] * c[2] - b[2] * c[1], b[2] * c[0] - b[0] * c[2], b[0] * c[1] - b[1] * c[0]),
            (a[2] * c[1] - a[1] * c[2], a[0] * c[2] - a[2] * c[0], a[1] * c[0] - a[0] * c[1]),
            (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]),
        )
        return tuple(tuple(_finite(cofactors[column][row] / det)
                           for column in range(3)) for row in range(3))
    raise ValueError('inverse supports 2x2 and 3x3 matrices')


def _vector_call(name, args):
    if name in ('vec', 'vector'):
        vector = tuple(_finite(value) for value in args)
        return vector
    if name == 'dot':
        left, right = _as_vector(args[0]), _as_vector(args[1])
        if len(left) != len(right):
            raise ValueError('dot product vectors must have equal length')
        return _finite(sum(a * b for a, b in zip(left, right)))
    if name == 'cross':
        left, right = _as_vector(args[0]), _as_vector(args[1])
        if len(left) == 2 and len(right) == 2:
            return _finite(left[0] * right[1] - left[1] * right[0])
        if len(left) == 3 and len(right) == 3:
            return (_finite(left[1] * right[2] - left[2] * right[1]),
                    _finite(left[2] * right[0] - left[0] * right[2]),
                    _finite(left[0] * right[1] - left[1] * right[0]))
        raise ValueError('cross product supports two 2D or two 3D vectors')
    if name in ('norm', 'length', 'mag'):
        vector = _as_vector(args[0])
        return _finite(math.sqrt(sum(value * value for value in vector)))
    if name in ('normalize', 'unit'):
        vector = _as_vector(args[0])
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude == 0:
            raise ValueError('cannot normalise a zero vector')
        return tuple(_finite(value / magnitude) for value in vector)
    if name == 'angle':
        if len(args) == 1:
            vector = _as_vector(args[0], 2)
            return _finite(math.atan2(vector[1], vector[0]))
        left, right = _as_vector(args[0]), _as_vector(args[1])
        if len(left) != len(right):
            raise ValueError('angle vectors must have equal length')
        denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
        if denominator == 0:
            raise ValueError('angle requires non-zero vectors')
        cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right)) / denominator))
        return _finite(math.acos(cosine))
    if name in ('sum', 'mean', 'prod'):
        values = list(_flatten(args[0]))
        if name == 'sum':
            return _finite(sum(values))
        if name == 'prod':
            result = 1.0
            for value in values:
                result *= value
            return _finite(result)
        return _finite(sum(values) / len(values))
    if name == 'rows':
        return float(len(args[0])) if _is_sequence(args[0]) else 1.0
    if name == 'cols':
        value = args[0]
        return float(len(value[0])) if _is_sequence(value) and value and _is_sequence(value[0]) else (
            float(len(value)) if _is_sequence(value) else 1.0)
    if name == 'transpose':
        matrix = _as_matrix(args[0])
        return tuple(tuple(row[column] for row in matrix) for column in range(len(matrix[0])))
    if name in ('det', 'determinant'):
        return _determinant(_as_matrix(args[0]))
    if name in ('inverse', 'inv'):
        return _inverse(_as_matrix(args[0]))
    if name == 'identity':
        requested = _finite(args[0])
        if not requested.is_integer():
            raise ValueError('identity size must be an integer')
        size = int(requested)
        if not 1 <= size <= 8:
            raise ValueError('identity size must be 1..8')
        return tuple(tuple(1.0 if row == column else 0.0 for column in range(size))
                     for row in range(size))
    if name == 'matmul':
        left, right = args
        if _is_sequence(left) and left and _is_sequence(left[0]):
            matrix = _as_matrix(left)
            if _is_sequence(right) and right and _is_sequence(right[0]):
                other = _as_matrix(right)
                if len(matrix[0]) != len(other):
                    raise ValueError('matrix dimensions do not match')
                return tuple(tuple(_finite(sum(matrix[row][k] * other[k][column]
                                               for k in range(len(other))))
                                   for column in range(len(other[0])))
                             for row in range(len(matrix)))
            vector = _as_vector(right)
            if len(matrix[0]) != len(vector):
                raise ValueError('matrix and vector dimensions do not match')
            return tuple(_finite(sum(value * vector[index] for index, value in enumerate(row)))
                         for row in matrix)
        vector = _as_vector(left)
        matrix = _as_matrix(right)
        if len(vector) != len(matrix):
            raise ValueError('vector and matrix dimensions do not match')
        return tuple(_finite(sum(vector[row] * matrix[row][column]
                                 for row in range(len(vector))))
                     for column in range(len(matrix[0])))
    if name == 'rotate':
        vector = _as_vector(args[0], 2)
        angle = _finite(args[1])
        cosine, sine = math.cos(angle), math.sin(angle)
        return (_finite(vector[0] * cosine - vector[1] * sine),
                _finite(vector[0] * sine + vector[1] * cosine))
    raise ValueError(f'unknown function {name}')


@dataclass(frozen=True)
class _Expression:
    node: ast.AST
    names: frozenset
    line: int


@dataclass
class _Curve:
    label: str
    kind: str = 'function'          # function, parametric, implicit
    expression: _Expression = None
    name: str = None
    variant: int = None
    xexpr: _Expression = None
    xname: str = None
    yexpr: _Expression = None
    yname: str = None
    orientation: str = 'y_of_x'     # y_of_x or x_of_y
    sweep: str = None
    sweep_axis: str = 'x'
    domain: tuple = None
    line: int = 1
    color: str = None


@dataclass
class _Plot:
    output: str
    sweep: str
    parameters: tuple
    definitions: dict
    curves: tuple
    axes: dict
    line: int
    controls: tuple = ()
    old: bool = False
    sweep_axis: str = 'x'


@dataclass(frozen=True)
class Raster:
    """Palette-indexed pixels for a graphics-protocol renderer."""
    width: int
    height: int
    pixels: bytes
    palette: tuple


@dataclass
class GraphImage:
    """Text rows around a raster plus the raster's cell-relative placement."""
    rows: list
    raster: Raster
    row: int
    col: int
    width: int
    height: int


@dataclass
class _Scene:
    plot: _Plot
    height: int
    drawing_height: int
    margin: int
    cols: int
    nx: int
    ny: int
    sampled: list
    xs: list
    x_bounds: tuple
    y_bounds: tuple
    empty: bool
    exhausted: bool
    xspan: float
    yspan: float
    title: str
    x_label: str
    readout: str
    hover: tuple
    hover_readout: str


def _split_args(text):
    """Split a comma-separated argument list, respecting nested brackets."""
    parts, current, depth = [], [], 0
    for char in text:
        if char in '([{':
            depth += 1
        elif char in ')]}':
            depth -= 1
        if char == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    tail = ''.join(current).strip()
    if tail or parts:
        parts.append(tail)
    return parts


def _expression(text, line, node_count):
    try:
        root = ast.parse(text.replace('^', '**'), mode='eval').body
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise _error(line, 'invalid expression') from exc
    names = set()
    stack = [(root, 1)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        node_count[0] += 1
        if count > 192 or node_count[0] > 4096 or depth > 24:
            raise _error(line, 'expression complexity limit exceeded')
        if isinstance(node, ast.Constant):
            try:
                _finite(node.value)
            except (ValueError, OverflowError) as exc:
                raise _error(line, 'expected a finite numeric literal <= 1e100') from exc
            children = ()
        elif isinstance(node, ast.Name):
            if not re.fullmatch(_NAME, node.id):
                raise _error(line, 'invalid name')
            if node.id not in _CONSTANTS:
                names.add(node.id)
            children = ()
        elif isinstance(node, (ast.List, ast.Tuple)):
            if not 1 <= len(node.elts) <= 32:
                raise _error(line, 'vector or matrix literals must have 1..32 entries')
            children = tuple(node.elts)
        elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            children = (node.left, node.right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            children = (node.operand,)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name in _VECTOR_FUNCTIONS:
                minimum, maximum = _VECTOR_FUNCTIONS[name]
            else:
                spec = _FUNCTIONS.get(name)
                minimum, maximum = (spec[1], spec[2]) if spec else (1, 0)
            if node.keywords or not minimum <= len(node.args) <= maximum:
                raise _error(line, 'unknown function or invalid argument count')
            children = node.args
        else:
            raise _error(line, 'unsupported expression syntax')
        stack.extend((child, depth + 1) for child in children)
    return _Expression(root, frozenset(names), line)


def _evaluate(node, resolve, budget):
    budget[0] -= 1
    if budget[0] < 0:
        raise _BudgetExceeded
    if isinstance(node, ast.Constant):
        return _finite(node.value)
    if isinstance(node, ast.Name):
        return _CONSTANTS[node.id] if node.id in _CONSTANTS else resolve(node.id)
    if isinstance(node, (ast.List, ast.Tuple)):
        return _value(tuple(_evaluate(element, resolve, budget) for element in node.elts))
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, resolve, budget)
        if isinstance(node.op, ast.UAdd):
            return value
        return _scale(value, -1) if _is_sequence(value) else _finite(-value)
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, resolve, budget)
        right = _evaluate(node.right, resolve, budget)
        if isinstance(node.op, ast.Pow):
            if _is_sequence(left) or _is_sequence(right):
                raise ValueError('powers require scalar operands')
            if abs(right) > 128:
                raise ValueError('exponent exceeds 128')
        if isinstance(node.op, ast.Mod) and (_is_sequence(left) or _is_sequence(right)):
            raise ValueError('modulo requires scalar operands')
        if isinstance(node.op, ast.Mult):
            if _is_sequence(left) and not _is_sequence(right):
                return _scale(left, right)
            if _is_sequence(right) and not _is_sequence(left):
                return _scale(right, left)
        if isinstance(node.op, ast.Div) and _is_sequence(left) and not _is_sequence(right):
            return _scale(left, 1 / _finite(right))
        if not _is_sequence(left) and not _is_sequence(right):
            return _finite(_BINARY[type(node.op)](left, right))
        return _zip_values(left, right, _BINARY[type(node.op)])
    args = [_evaluate(arg, resolve, budget) for arg in node.args]
    name = node.func.id
    if name in _VECTOR_FUNCTIONS:
        return _value(_vector_call(name, args))
    function, _, _, elementwise = _FUNCTIONS[name]
    if elementwise:
        if not any(_is_sequence(arg) for arg in args):
            return _finite(function(*args))
        return _value(_broadcast(function, args))
    if any(_is_sequence(arg) for arg in args):
        raise ValueError(f'{name} requires scalar arguments')
    return _finite(function(*args))


def _constant(expression):
    if expression.names:
        raise _error(expression.line, 'range endpoints and initial values must be constant')
    try:
        value = _evaluate(expression.node, lambda name: 0.0, [4096])
    except (ArithmeticError, ValueError, _BudgetExceeded) as exc:
        raise _error(expression.line, 'invalid constant expression') from exc
    if _is_sequence(value):
        raise _error(expression.line, 'range endpoints and initial values must be scalar')
    return _finite(value)


def _color_style(text, line):
    key = text.strip().lower()
    if key in _COLORS:
        return _COLORS[key]
    if key.startswith('0x'):
        digits = key[2:]
        if len(digits) == 3:
            digits = ''.join(char * 2 for char in digits)
        if len(digits) == 6:
            try:
                red, green, blue = (int(digits[index:index + 2], 16)
                                    for index in (0, 2, 4))
            except ValueError:
                pass
            else:
                return f'\033[38;2;{red};{green};{blue}m'
    match = re.fullmatch(r'rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)', key)
    if match:
        red, green, blue = (min(255, int(value)) for value in match.groups())
        return f'\033[38;2;{red};{green};{blue}m'
    if key.isdigit() and 0 <= int(key) <= 255:
        return f'\033[38;5;{int(key)}m'
    raise _error(line, f'unknown colour {text.strip()}')


def _resolve_colors(explicit, defaults, count):
    colors, default_index = [], 0
    for index in range(count):
        color = explicit[index] if index < len(explicit) else None
        if color is None and defaults:
            color = defaults[min(default_index, len(defaults) - 1)]
            default_index += 1
        colors.append(color)
    return colors


class _CellCanvas:
    def __init__(self, cols, rows, sx, sy, glyphs):
        self.cols = cols
        self.rows = rows
        self.sx = sx
        self.sy = sy
        self.glyphs = glyphs
        self.bits = [[0] * cols for _ in range(rows)]
        self.colors = [[BORDER] * cols for _ in range(rows)]

    def dot(self, x, y, color):
        if 0 <= x < self.cols * self.sx and 0 <= y < self.rows * self.sy:
            row, col = y // self.sy, x // self.sx
            bit = (y % self.sy) * self.sx + (x % self.sx)
            self.bits[row][col] |= 1 << bit
            self.colors[row][col] = color

    def line(self, x0, y0, x1, y1, color):
        dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
        dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.dot(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            doubled = 2 * err
            if doubled >= dy:
                err += dy
                x0 += sx
            if doubled <= dx:
                err += dx
                y0 += sy


class _PixelCanvas:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.pixels = bytearray(width * height)
        self.palette = [BACKGROUND]
        self._indices = {BACKGROUND: 0}

    def _index(self, color):
        index = self._indices.get(color)
        if index is None:
            index = len(self.palette)
            self._indices[color] = index
            self.palette.append(color)
        return index

    def dot(self, x, y, color):
        if 0 <= x < self.width and 0 <= y < self.height:
            self.pixels[y * self.width + x] = self._index(color)

    def line(self, x0, y0, x1, y1, color):
        dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
        dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.dot(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            doubled = 2 * err
            if doubled >= dy:
                err += dy
                x0 += sx
            if doubled <= dx:
                err += dx
                y0 += sy


class Graph:
    """Parse eagerly; invalid declarations raise line-specific ValueError.

    ``ranges`` contains the adjustable user variables followed by axis-range
    controls (for declared axes). ``values`` holds their current values.
    ``controls()`` returns the user variables for a particular plot, and
    ``sliders()`` appends the axis-range handles used by the graph view.

    New-style plots derive their orientation and domain from the expression
    names. A lone ``x`` dependency means y=f(x); a lone ``y`` dependency means
    x=f(y). If both appear the expression is an implicit curve. Named ``x`` and
    ``y`` expressions follow the same x=f(y) and y=f(x) convention even when
    their formulas only use parameter variables. Parametric plots use the first
    ranged variable they find as their sweep; implicit plots sample the plane.
    """

    def __init__(self, source, values=None):
        if not isinstance(source, str):
            raise _error(1, 'source must be text')
        if len(source) > 16384:
            raise _error(1, 'source exceeds 16384 characters')
        lines = source.splitlines()
        if len(lines) > 256:
            raise _error(257, 'source exceeds 256 lines')
        self.ranges = {}
        self.values = {}
        self._start = time.monotonic()
        self._last_render = self._start
        self.axis_ranges = {}
        self.axis_views = {}
        self.axis_controls = {}
        self.plots = []
        definitions, axes, initial = {}, {}, {}
        node_count = [0]
        assignments = 0
        for line, raw in enumerate(lines, 1):
            code = raw.split('#', 1)[0]
            if code.lstrip().startswith(';'):
                continue
            for statement in code.split(';'):
                statement = statement.strip()
                if not statement:
                    continue
                plot_match = re.fullmatch(
                    r'(plot|plotxy|plotvec|plotvecs|plotvectors|quiver)\s*\((.*)\)', statement)
                if plot_match:
                    kind = 'plotvec' if plot_match[1] in ('plotvec', 'plotvecs', 'plotvectors', 'quiver') else plot_match[1]
                    self._parse_plot(kind, plot_match[2], line,
                                     definitions, axes, node_count)
                    continue
                match = re.fullmatch(rf'({_NAME})\s*=\s*(.+)', statement)
                if not match:
                    raise _error(line, 'expected name=expression, name=[lo..hi], or plot(...)')
                name, text = match.groups()
                if (name in _CONSTANTS or name in _FUNCTIONS or name in _VECTOR_FUNCTIONS
                        or name in ('plot', 'plotxy', 'plotvec', 'plotvecs', 'plotvectors', 'quiver', 'time', 'dt')):
                    raise _error(line, f'reserved name {name}')
                assignments += 1
                if assignments > 96:
                    raise _error(line, 'at most 96 assignments are allowed')
                bounds = (re.fullmatch(r'\[\s*(.+?)\s*\.\.\s*(.+?)\s*\]', text)
                          if text.startswith('[') else None)
                if bounds:
                    lo, hi = (_constant(_expression(part, line, node_count)) for part in bounds.groups())
                    if lo >= hi:
                        raise _error(line, 'range lower bound must be less than upper bound')
                    axis = _AXIS_DECLARATIONS.get(name)
                    if axis is None and name in ('x', 'y'):
                        axis = name
                    if axis is not None:
                        axes[axis] = (lo, hi)
                        self.axis_ranges[axis] = (lo, hi)
                        self.axis_views[axis] = (lo, hi)
                    else:
                        if name in self.ranges or name in definitions:
                            raise _error(line, f'duplicate range or formula for {name}')
                        if len(self.ranges) >= 16:
                            raise _error(line, 'at most 16 ranged variables are allowed')
                        self.ranges[name] = (lo, hi)
                        self.values[name] = lo / 2 + hi / 2
                else:
                    if name in _AXIS_DECLARATIONS:
                        raise _error(line, f'{name} requires a range')
                    expression = _expression(text, line, node_count)
                    if name in self.ranges:
                        initial[name] = _constant(expression)
                    else:
                        variants = definitions.setdefault(name, [])
                        if len(variants) >= 24:
                            raise _error(line, 'at most 24 curves per name are allowed')
                        variants.append(expression)
        self.values['time'] = 0.0
        self.values['dt'] = 0.0
        # Also validate unused formulas, rather than silently accepting typos.
        self._validate(definitions)
        if not self.plots:
            raise _error(max(1, len(lines)), 'at least one plot is required')
        for plot in self.plots:
            if plot.old:
                self._finalize_old_plot(plot)
            else:
                self._finalize_new_plot(plot)
        for name, value in initial.items():
            lo, hi = self.ranges[name]
            self.values[name] = max(lo, min(hi, value))
        self._add_axis_controls()
        if isinstance(values, Mapping):
            for name in self.ranges:
                if name in self.axis_controls:
                    continue
                value = values.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                try:
                    value = float(value)
                except (ValueError, OverflowError):
                    continue
                if math.isfinite(value):
                    lo, hi = self.ranges[name]
                    self.values[name] = max(lo, min(hi, value))
        self._sync_axis_views()
        self.initial_values = dict(self.values)

    # ------------------------------------------------------------------
    # parsing helpers

    def _parse_plot(self, kind, body, line, definitions, axes, node_count):
        args, explicit, defaults = [], [], []
        for raw in _split_args(body):
            option = re.fullmatch(r'(?i)(?:color|colour)\s*=\s*(.+)', raw)
            if option:
                defaults.append(_color_style(option[1], line))
                continue
            color = None
            prefix = re.fullmatch(r'\s*([^:]+?)\s*:\s*(.+)', raw)
            if prefix:
                color = _color_style(prefix[1], line)
                raw = prefix[2]
            args.append(raw)
            explicit.append(color)
        if kind == 'plotxy':
            if len(args) != 2 or not all(args):
                raise _error(line, 'use plotxy(x_expression, y_expression)')
        elif kind == 'plotvec':
            if not 1 <= len(args) <= 18 or not all(args):
                raise _error(line, 'use plotvec(vector_expression, optional_vectors...)')
        else:
            if not 1 <= len(args) <= 18 or not all(args):
                raise _error(line, 'use plot(expression, optional_expressions...)')
        if len(self.plots) >= 8:
            raise _error(line, 'at most 8 plots are allowed')
        old = (kind == 'plot' and len(args) >= 2
               and all(re.fullmatch(_NAME, arg) for arg in args)
               and args[0] in definitions and args[1] in self.ranges)
        if old:
            output, sweep = args[0], args[1]
            if len(set(args)) != len(args):
                raise _error(line, 'plot names must be distinct')
            parameters = tuple(args[2:])
            variants = definitions[output]
            old_domain = axes.get('x', self.ranges[sweep])
            colors = _resolve_colors(explicit, defaults, len(variants))
            curves = tuple(_Curve(label=f'{output}#{index + 1}',
                                  expression=expression, name=output,
                                  variant=index, sweep=sweep, domain=old_domain,
                                  line=expression.line, color=colors[index])
                           for index, expression in enumerate(variants))
            self.plots.append(_Plot(output, sweep, parameters,
                                    {k: tuple(v) for k, v in definitions.items()},
                                    curves, dict(axes), line, old=True))
            return
        specs = [self._plot_spec(arg, line, definitions, node_count) for arg in args]
        if kind == 'plotxy':
            xspec, yspec = specs
            color = next((value for value in explicit if value is not None), None)
            if color is None and defaults:
                color = defaults[0]
            curve = _Curve(label=f'{xspec["label"]},{yspec["label"]}',
                           kind='parametric', xexpr=xspec['expr'], xname=xspec['name'],
                           yexpr=yspec['expr'], yname=yspec['name'], line=line,
                           color=color)
            curves = (curve,)
            output = curve.label
        elif kind == 'plotvec':
            colors = _resolve_colors(explicit, defaults, len(specs))
            curves = tuple(_Curve(label=spec['label'], kind='vector',
                                  expression=spec['expr'], name=spec['name'],
                                  line=line, color=colors[index])
                           for index, spec in enumerate(specs))
            output = ','.join(spec['label'] for spec in specs)
        else:
            colors = _resolve_colors(explicit, defaults, len(specs))
            curves = tuple(self._curve_from_spec(spec, line, definitions, colors[index])
                           for index, spec in enumerate(specs))
            output = ','.join(spec['label'] for spec in specs)
        definitions_snapshot = {k: tuple(v) for k, v in definitions.items()}
        self.plots.append(_Plot(output, '', tuple(), definitions_snapshot,
                                curves, dict(axes), line))

    def _plot_spec(self, text, line, definitions, node_count):
        if re.fullmatch(_NAME, text) and text in definitions:
            variants = definitions[text]
            return {'label': text, 'name': text, 'expr': variants[-1]}
        return {'label': text, 'name': None,
                'expr': _expression(text, line, node_count)}

    def _curve_from_spec(self, spec, line, definitions, color=None):
        expression = spec['expr']
        name = spec['name']
        label = spec['label']
        free = set(expression.names)
        if name is not None:
            free.discard(name)
        if 'x' in free and 'y' in free:
            return _Curve(label=label, kind='implicit', expression=expression,
                          name=name, line=line, color=color)
        if name == 'x' or ('y' in free and name != 'y'):
            # x=f(y): x is horizontal, the y plane coordinate is the domain.
            return _Curve(label=label, expression=expression, name=name,
                          orientation='x_of_y', sweep='y', sweep_axis='y',
                          domain=self.axis_ranges.get('y'), line=line, color=color)
        # y=f(x), or an unqualified function of the horizontal x coordinate.
        return _Curve(label=label, expression=expression, name=name,
                      orientation='y_of_x', sweep='x', sweep_axis='x',
                      domain=self.axis_ranges.get('x'), line=line, color=color)

    def _curve_ranges(self, plot, curve, skip=()):
        """Return the ranged variables reachable from a single curve."""
        used, seen = set(), set()

        def walk_expression(expression, owner=None, index=None):
            for name in expression.names:
                if name in skip:
                    continue
                if name in self.ranges:
                    used.add(name)
                    continue
                variants = plot.definitions.get(name)
                if not variants:
                    continue
                if owner is not None and name == owner:
                    next_index = index - 1
                else:
                    next_index = len(variants) - 1
                if next_index >= 0:
                    walk_definition(name, next_index)

        def walk_definition(name, index):
            key = (name, index)
            if key in seen:
                return
            seen.add(key)
            walk_expression(plot.definitions[name][index], name, index)

        if curve.kind == 'parametric':
            for expression, name in ((curve.xexpr, curve.xname),
                                     (curve.yexpr, curve.yname)):
                variants = plot.definitions.get(name) if name else None
                if variants:
                    walk_definition(name, len(variants) - 1)
                else:
                    walk_expression(expression)
        elif curve.name:
            variants = plot.definitions.get(curve.name)
            if variants:
                index = len(variants) - 1 if curve.variant is None else curve.variant
                walk_definition(curve.name, index)
            else:
                walk_expression(curve.expression)
        else:
            walk_expression(curve.expression)
        return [name for name in self.ranges if name in used]

    def _finalize_old_plot(self, plot):
        if not plot.curves:
            raise _error(plot.line, f'no formula defined for {plot.output}')
        for name in (plot.sweep,) + plot.parameters:
            if name not in self.ranges:
                raise _error(plot.line, f'{name} must have a declared range')
        dependencies = set()

        def collect(expression):
            for name in sorted(expression.names):
                if name in dependencies:
                    continue
                dependencies.add(name)
                if name not in self.ranges and name in plot.definitions:
                    collect(plot.definitions[name][-1])

        for curve in plot.curves:
            collect(curve.expression)
        controls = list(dict.fromkeys((plot.sweep,) + plot.parameters))
        controls.extend(name for name in self.ranges
                        if name in dependencies and name not in controls)
        plot.controls = tuple(controls)

    def _finalize_new_plot(self, plot):
        if len(plot.curves) == 1 and plot.curves[0].kind == 'parametric':
            curve = plot.curves[0]
            used = self._curve_ranges(plot, curve)
            if used:
                curve.sweep = used[0]
                curve.domain = self.ranges[curve.sweep]
            else:
                curve.sweep = None
                curve.domain = self.axis_ranges.get('x', (0.0, 1.0))
            plot.sweep = curve.sweep or ''
            plot.sweep_axis = curve.sweep_axis
            plot.controls = tuple(used)
            return
        used = set()
        for curve in plot.curves:
            if curve.kind == 'implicit':
                skip = {'x', 'y'}
            elif curve.sweep in ('x', 'y'):
                skip = {curve.sweep}
            else:
                skip = set()
            used.update(self._curve_ranges(plot, curve, skip))
        order = [name for name in self.ranges if name in used]
        sweep = next((c.sweep for c in plot.curves if c.sweep), None)
        plot.sweep = sweep or ''
        plot.sweep_axis = next((c.sweep_axis for c in plot.curves
                                if c.sweep == sweep), 'x')
        plot.controls = tuple(order)

    def _add_axis_controls(self):
        for axis in ('x', 'y'):
            base = self.axis_ranges.get(axis)
            if base is None:
                continue
            lo, hi = self.axis_views[axis]
            for suffix, part in _AXIS_CONTROL_SUFFIX.items():
                name = f'{axis}_axis{suffix}'
                self.axis_controls[name] = (axis, part)
                self.ranges[name] = base
                self.values[name] = lo if part == 0 else hi

    def _sync_axis_views(self):
        for axis in ('x', 'y'):
            min_name = f'{axis}_axis.min'
            max_name = f'{axis}_axis.max'
            if min_name not in self.values or max_name not in self.values:
                continue
            lo, hi = self.values[min_name], self.values[max_name]
            if lo > hi:
                lo, hi = hi, lo
            if lo == hi:
                hi = lo + max(1e-9, abs(lo) * 1e-9)
            self.axis_views[axis] = (lo, hi)
            self.values[min_name] = lo
            self.values[max_name] = hi
            for plot in self.plots:
                plot.axes[axis] = (lo, hi)

    # ------------------------------------------------------------------
    # validation and evaluation

    def _validate(self, definitions):
        depths, active = {}, set()

        def visit(name, line, depth, index=None):
            if name in self.ranges:
                return 0
            if name in ('x', 'y', 'time', 'dt'):
                return 0
            if name not in definitions:
                raise _error(line, f'unknown name {name}')
            variants = definitions[name]
            if index is None:
                index = len(variants) - 1
            if index < 0:
                raise _error(line, f'circular dependency involving {name}')
            key = (name, index)
            if key in active:
                raise _error(line, f'circular dependency involving {name}')
            if depth > 24:
                raise _error(line, 'dependency depth exceeds 24')
            if key in depths:
                if depth + depths[key] - 1 > 24:
                    raise _error(line, 'dependency depth exceeds 24')
                return depths[key]
            active.add(key)
            expression = variants[index]
            height = 1
            for dependency in sorted(expression.names):
                next_index = index - 1 if dependency == name else None
                height = max(height, 1 + visit(dependency, expression.line,
                                               depth + 1, next_index))
            active.remove(key)
            depths[key] = height
            return height

        for name, variants in definitions.items():
            visit(name, variants[-1].line, 1)
            # Earlier output variants are still executable curves.
            for index, expression in enumerate(variants):
                for dependency in sorted(expression.names):
                    next_index = index - 1 if dependency == name else None
                    visit(dependency, expression.line, 1, next_index)

    def _resolver(self, plot, cache, budget):
        def resolve(name, index=None):
            if index is None:
                if name in cache:
                    return cache[name]
                variants = plot.definitions.get(name)
                if not variants:
                    raise ValueError(f'unknown name {name}')
                index = len(variants) - 1
            key = (name, index)
            if key in cache:
                return cache[key]
            variants = plot.definitions.get(name)
            if not variants or index < 0:
                raise ValueError(f'circular dependency involving {name}')
            expression = variants[index]

            def inner(inner_name):
                if inner_name == name:
                    return resolve(name, index - 1)
                return resolve(inner_name)

            value = _evaluate(expression.node, inner, budget)
            cache[key] = value
            return value

        def value(expression, name=None, variant=None):
            if name is not None:
                return resolve(name, variant)
            return _evaluate(expression.node, lambda n: resolve(n), budget)

        return value

    def _eval_curve(self, plot, curve, u, budget):
        cache = dict(self.values)
        if curve.sweep:
            cache[curve.sweep] = u
        value = self._resolver(plot, cache, budget)
        try:
            if curve.kind == 'parametric':
                x = value(curve.xexpr, curve.xname)
                y = value(curve.yexpr, curve.yname)
                if _is_sequence(x) or _is_sequence(y):
                    return None
                return (x, y)
            result = value(curve.expression, curve.name, curve.variant)
            if _is_sequence(result):
                return None
            if curve.orientation == 'x_of_y':
                return (result, u)
            return (u, result)
        except (ArithmeticError, ValueError, RecursionError):
            return None

    def _eval_implicit(self, plot, curve, x, y, budget):
        cache = dict(self.values)
        cache['x'] = x
        cache['y'] = y
        value = self._resolver(plot, cache, budget)
        try:
            result = value(curve.expression, curve.name)
            return None if _is_sequence(result) else result
        except (ArithmeticError, ValueError, RecursionError):
            return None

    def _eval_vector(self, plot, curve, budget):
        cache = dict(self.values)
        value = self._resolver(plot, cache, budget)
        try:
            result = value(curve.expression, curve.name, curve.variant)
            return self._vector_segments(result)
        except (ArithmeticError, ValueError, RecursionError):
            return []

    @staticmethod
    def _vector_segments(value):
        """Turn a vector or matrix into (tail, tip) pairs.

        A single 2D/3D vector is drawn from the origin. A four-component vector
        is treated as (x0, y0, x1, y1), a directed segment. A matrix or a list
        of vectors contributes one segment per row.
        """
        if not _is_sequence(value) or (value and _is_sequence(value[0])):
            rows = value
        elif _is_sequence(value):
            rows = (value,)
        else:
            raise ValueError('plotvec expects a vector or list of vectors')
        if not _is_sequence(rows) or (rows and not _is_sequence(rows[0])):
            raise ValueError('plotvec expects a vector or list of vectors')
        segments = []
        for row in rows:
            if not _is_sequence(row) or (row and _is_sequence(row[0])):
                raise ValueError('plotvec expects vectors of numbers')
            if len(row) in (2, 3):
                segments.append(((0.0, 0.0), (row[0], row[1])))
            elif len(row) == 4:
                segments.append(((row[0], row[1]), (row[2], row[3])))
            else:
                raise ValueError('plotvec vectors must have 2, 3, or 4 components')
        return segments

    # ------------------------------------------------------------------
    # public API

    def _plot(self, index):
        if type(index) is not int or not 0 <= index < len(self.plots):
            raise ValueError('invalid plot index')
        return self.plots[index]

    def controls(self, plot_index=0):
        return list(self._plot(plot_index).controls)

    def _plot_names(self, plot):
        names = set()

        def collect(expression):
            for name in expression.names:
                if name in names:
                    continue
                names.add(name)
                for variant in plot.definitions.get(name, ()):
                    collect(variant)

        for curve in plot.curves:
            for expression in (curve.expression, curve.xexpr, curve.yexpr):
                if expression is not None:
                    collect(expression)
        return names

    def animated(self, plot_index=0):
        """Whether this plot depends on the built-in animation values."""
        return bool(self._plot_names(self._plot(plot_index)) & {'time', 'dt'})

    def cache_values(self, plot_index=0):
        """Values that affect this plot, ignoring idle time for static plots."""
        animated = self.animated(plot_index)
        return tuple((name, value) for name, value in self.values.items()
                     if animated or name not in ('time', 'dt'))

    def saved_values(self):
        """Persist slider values, not animation state or transient zoom views."""
        return {name: value for name, value in self.values.items()
                if name in self.ranges and name not in self.axis_controls}

    def sliders(self, plot_index=0):
        """Variable controls followed by the axis-range handles."""
        return self.controls(plot_index) + list(self.axis_controls)

    def adjust(self, name, fraction):
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise ValueError('fraction must be finite')
        try:
            fraction = float(fraction)
        except OverflowError as exc:
            raise ValueError('fraction must be finite') from exc
        if not math.isfinite(fraction):
            raise ValueError('fraction must be finite')
        fraction = max(0.0, min(1.0, fraction))
        if name in self.axis_controls:
            axis, part = self.axis_controls[name]
            lo, hi = self.axis_ranges[axis]
            target = lo * (1 - fraction) + hi * fraction
            view = list(self.axis_views[axis])
            margin = max(1e-9, abs(hi - lo) * 1e-6)
            if part == 0:
                view[0] = min(target, view[1] - margin)
            else:
                view[1] = max(target, view[0] + margin)
            self.axis_views[axis] = (view[0], view[1])
            self.values[f'{axis}_axis.min'] = view[0]
            self.values[f'{axis}_axis.max'] = view[1]
            for plot in self.plots:
                plot.axes[axis] = (view[0], view[1])
            return
        if name not in self.ranges:
            raise ValueError(f'unknown ranged variable {name}')
        lo, hi = self.ranges[name]
        self.values[name] = lo * (1 - fraction) + hi * fraction

    def pan(self, axis, delta_fraction):
        """Shift the visible axis view by delta_fraction of its current span.

        The drawing follows the cursor (direct manipulation): dragging right
        shifts x toward smaller values, dragging down shifts y toward larger
        values. The view can be panned past the declared axis range; only the
        window itself must stay valid (lo < hi).
        """
        if isinstance(delta_fraction, bool) or not isinstance(delta_fraction, (int, float)):
            return
        try:
            delta_fraction = float(delta_fraction)
        except (OverflowError, ValueError):
            return
        if not math.isfinite(delta_fraction):
            return
        if axis not in self.axis_views:
            return
        lo, hi = self.axis_views[axis]
        span = hi - lo
        if span <= 0:
            return
        shift = span * delta_fraction * _PAN_SENSITIVITY
        view_lo, view_hi = lo - shift, hi - shift
        base = self.axis_ranges.get(axis)
        if base is not None:
            blo, bhi = base
            if view_lo < blo:
                view_lo = blo
                view_hi = min(bhi, blo + span)
            if view_hi > bhi:
                view_hi = bhi
                view_lo = max(blo, bhi - span)
            if view_hi <= view_lo:
                view_hi = min(bhi, blo + span)
                view_lo = max(blo, bhi - span)
                if view_hi <= view_lo:
                    pad = max(1e-9, abs(view_lo) * 1e-9)
                    view_hi = view_lo + pad
        else:
            if view_hi <= view_lo:
                pad = max(1e-9, abs(view_lo) * 1e-9)
                view_hi = view_lo + pad
        self.axis_views[axis] = (view_lo, view_hi)
        for plot in self.plots:
            plot.axes[axis] = (view_lo, view_hi)

    def zoom(self, axis, factor, center_fraction):
        """Zoom the visible axis view around center_fraction (0..1 across the view).

        factor > 1 zooms in (shrinks the span), factor < 1 zooms out. The view
        is clamped to the declared axis range so it never exceeds the domain.
        """
        if isinstance(factor, bool) or not isinstance(factor, (int, float)):
            return
        try:
            factor = float(factor)
        except (OverflowError, ValueError):
            return
        if not math.isfinite(factor) or factor <= 0:
            return
        base = self.axis_ranges.get(axis)
        if base is None or axis not in self.axis_views:
            return
        lo, hi = self.axis_views[axis]
        span = hi - lo
        if span <= 0:
            return
        new_span = span / factor
        center = lo + span * center_fraction
        new_lo = center - new_span * center_fraction
        new_hi = center + new_span * (1.0 - center_fraction)
        blo, bhi = base
        full = bhi - blo
        if new_span >= full:
            new_lo, new_hi = blo, bhi
        else:
            if new_lo < blo:
                new_lo = blo
                new_hi = blo + new_span
            if new_hi > bhi:
                new_hi = bhi
                new_lo = bhi - new_span
        self.axis_views[axis] = (new_lo, new_hi)
        self.values[f'{axis}_axis.min'] = new_lo
        self.values[f'{axis}_axis.max'] = new_hi
        for plot in self.plots:
            plot.axes[axis] = (new_lo, new_hi)

    def reset_origin(self):
        """Reset every adjustable value to what was declared in the graph
        section: axis views back to their declared ranges, and every ranged
        variable (slider) back to its initial value."""
        for axis in ('x', 'y'):
            base = self.axis_ranges.get(axis)
            if base is None or axis not in self.axis_views:
                continue
            blo, bhi = base
            self.axis_views[axis] = (blo, bhi)
            self.values[f'{axis}_axis.min'] = blo
            self.values[f'{axis}_axis.max'] = bhi
            for plot in self.plots:
                plot.axes[axis] = (blo, bhi)
        for name, value in self.initial_values.items():
            if name not in self.ranges:
                continue
            lo, hi = self.ranges[name]
            self.values[name] = max(lo, min(hi, value))

    def tick(self):
        """Advance the built-in ``time`` (elapsed seconds) and ``dt``
        (delta since last tick) by one frame. Called only from the panel's
        idle poll so animation does not run while the user is scrolling or
        typing."""
        now = time.monotonic()
        dt = max(0.0, min(0.25, now - self._last_render))
        self._last_render = now
        self.values['time'] = max(0.0, now - self._start)
        self.values['dt'] = dt

    # ------------------------------------------------------------------
    # rendering

    def _domain(self, curve, plot, x_bounds=None):
        domain = curve.domain
        if domain is None:
            if curve.orientation == 'x_of_y':
                domain = plot.axes.get('y') or self.axis_ranges.get('y')
            else:
                domain = plot.axes.get('x') or self.axis_ranges.get('x')
        if domain is None:
            domain = (-1.0, 1.0)
        return domain

    def _sample_points(self, plot, curve, nx, budget):
        domain = self._domain(curve, plot)
        lo, hi = domain
        points = []
        for i in range(nx):
            u = lo * (1 - i / (nx - 1)) + hi * (i / (nx - 1))
            try:
                point = self._eval_curve(plot, curve, u, budget)
            except _BudgetExceeded:
                raise
            points.append(point)
        return points, (lo, hi)

    def _implicit_points(self, plot, curve, bounds, nx, ny, budget,
                         max_columns=120, max_rows=90):
        xlo, xhi, ylo, yhi = bounds
        columns = max(2, min(max_columns, nx))
        rows = max(2, min(max_rows, ny))
        grid = []
        for j in range(rows + 1):
            y = yhi - (yhi - ylo) * j / rows
            row = []
            for i in range(columns + 1):
                x = xlo + (xhi - xlo) * i / columns
                try:
                    row.append(self._eval_implicit(plot, curve, x, y, budget))
                except _BudgetExceeded:
                    raise
            grid.append(row)
        points = []
        for j in range(rows):
            for i in range(columns):
                corners = (grid[j][i], grid[j][i + 1],
                           grid[j + 1][i], grid[j + 1][i + 1])
                if any(value is not None and value == 0 for value in corners):
                    x = xlo + (xhi - xlo) * (i + .5) / columns
                    y = yhi - (yhi - ylo) * (j + .5) / rows
                    points.append((x, y))
                    continue
                finite = [value for value in corners if value is not None]
                if len(finite) < 2:
                    continue
                if min(finite) < 0 < max(finite):
                    x = xlo + (xhi - xlo) * (i + .5) / columns
                    y = yhi - (yhi - ylo) * (j + .5) / rows
                    points.append((x, y))
        return points

    def _title(self, plot, width):
        labels = [curve.label for curve in plot.curves]
        legend = (f'{plot.output} vs {plot.sweep} | ' if plot.old
                  else ' | '.join(labels))
        title = ACCENT + legend[:width]
        used = min(width, len(legend))
        if len(labels) > 1 or plot.old:
            for index, label in enumerate(labels):
                extra = f'{label}(L{plot.curves[index].line}) '
                color = plot.curves[index].color or _PALETTE[index % len(_PALETTE)]
                title += color + extra[:max(0, width - used)]
                used = min(width, used + len(extra))
        return title + BASE

    def _scene(self, width, height, plot_index, hover, sub_x=2, sub_y=2, pixel_size=None):
        plot = self._plot(plot_index)
        title = self._title(plot, width)
        drawing_height = max(0, height - 3)
        margin = 10 if width >= 24 else 0
        cols = width - margin
        if height == 1:
            return _Scene(plot=plot, height=height, drawing_height=0, margin=0,
                          cols=width, nx=2, ny=2, sampled=[], xs=[],
                          x_bounds=(-1.0, 1.0), y_bounds=(-1.0, 1.0),
                          empty=True, exhausted=False, xspan=0, yspan=0,
                          title=title, x_label='', readout='', hover=None,
                          hover_readout='')
        budget = [200000]
        exhausted = False
        if pixel_size is None:
            nx, ny = max(2, cols * sub_x), max(2, drawing_height * sub_y)
        else:
            nx = max(2, min(2000, int(pixel_size[0])))
            ny = max(2, min(2000, int(pixel_size[1])))
        implicit_columns = min(120, max(2, cols * sub_x))
        implicit_rows = min(90, max(2, drawing_height * sub_y))
        sampled = []
        xs, ys = [], []
        has_vectors = False
        if drawing_height:
            for curve in plot.curves:
                if curve.kind == 'implicit':
                    continue
                if curve.kind == 'vector':
                    try:
                        segments = self._eval_vector(plot, curve, budget)
                    except _BudgetExceeded:
                        exhausted = True
                        segments = []
                    for tail, tip in segments:
                        for x, y in (tail, tip):
                            if math.isfinite(x) and math.isfinite(y):
                                xs.append(x)
                                ys.append(y)
                    has_vectors = has_vectors or bool(segments)
                    sampled.append((curve, segments, 'vector'))
                    continue
                try:
                    points, domain = self._sample_points(plot, curve, nx, budget)
                except _BudgetExceeded:
                    exhausted = True
                    points, domain = [], self._domain(curve, plot)
                for point in points:
                    if point is None:
                        continue
                    x, y = point
                    if math.isfinite(x) and math.isfinite(y):
                        xs.append(x)
                        ys.append(y)
                sampled.append((curve, points, 'line'))
        if has_vectors:
            xs.append(0.0)
            ys.append(0.0)
        x_bounds = plot.axes.get('x')
        y_bounds = plot.axes.get('y')
        if x_bounds is None:
            if xs:
                xlo, xhi = min(xs), max(xs)
                pad = max(abs(xlo) * .05, 1.0) if xlo == xhi else (xhi - xlo) * .08
                x_bounds = (xlo - pad, xhi + pad)
            else:
                x_bounds = self.axis_ranges.get('x', (-1.0, 1.0))
        if y_bounds is None:
            if ys:
                ylo, yhi = min(ys), max(ys)
                pad = max(abs(ylo) * .05, 1.0) if ylo == yhi else (yhi - ylo) * .08
                y_bounds = (ylo - pad, yhi + pad)
            elif 'y' in self.axis_ranges:
                y_bounds = self.axis_ranges['y']
            elif x_bounds:
                y_bounds = x_bounds
            else:
                y_bounds = (-1.0, 1.0)
        xlo, xhi = x_bounds
        ylo, yhi = y_bounds
        empty = xlo >= xhi or ylo >= yhi
        if drawing_height and not empty:
            for curve in plot.curves:
                if curve.kind != 'implicit':
                    continue
                try:
                    points = self._implicit_points(
                        plot, curve, (xlo, xhi, ylo, yhi), nx, ny, budget,
                        max_columns=implicit_columns, max_rows=implicit_rows)
                except _BudgetExceeded:
                    exhausted = True
                    points = []
                sampled.append((curve, points, 'implicit'))
        xspan, yspan = xhi - xlo, yhi - ylo
        hover_readout = ''
        if hover is not None and drawing_height and not empty:
            hx, hy = hover
            if 0 <= hx < nx and 0 <= hy < ny:
                x_data = xlo + hx / (nx - 1) * xspan if xspan else xlo
                y_data = yhi - hy / (ny - 1) * yspan if yspan else ylo
                hover_readout = f' x={x_data:.4g} y={y_data:.4g}'
        x_label = ''
        if height >= 3:
            left = f'{xlo:.4g}' if not empty else 'axes outside range'
            right = f'{xhi:.4g} {plot.sweep if plot.old else "x"}' if not empty else ''
            label = left + ' ' * max(1, cols - len(left) - len(right)) + right
            x_label = MUTED + (' ' * margin + label)[:width] + BASE
        if plot.old:
            readout = f'{plot.sweep}={self.values[plot.sweep]:.4g}'
            for curve in plot.curves:
                try:
                    point = self._eval_curve(plot, curve, self.values[plot.sweep], budget)
                except _BudgetExceeded:
                    exhausted = True
                    point = None
                value = None if point is None else point[1]
                readout += f' {curve.label}=' + (f'{value:.4g}' if value is not None else 'gap')
        else:
            names = [name for name in plot.controls
                     if name not in self.axis_controls and name != plot.sweep]
            readout = ' '.join(f'{name}={self.values[name]:.4g}' for name in names)
            if plot.sweep and plot.sweep in self.values:
                prefix = f'{plot.sweep}={self.values[plot.sweep]:.4g}'
                readout = prefix + ((' | ' + readout) if readout else '')
            if not readout:
                if plot.curves and all(c.kind == 'vector' for c in plot.curves):
                    count = sum(len(points) for _, points, kind in sampled if kind == 'vector')
                    readout = f'{count} vector' + ('' if count == 1 else 's')
                else:
                    readout = 'x/y graph'
        if exhausted:
            readout = 'budget limit | ' + readout
        elif empty:
            readout = 'empty axis | ' + readout
        elif (drawing_height and not xs
              and not any(c.kind in ('implicit', 'vector') for c in plot.curves)):
            readout = 'no finite samples | ' + readout
        readout += hover_readout
        readout = (WARNING if exhausted or empty else ACCENT) + readout[:width] + BASE
        return _Scene(plot=plot, height=height, drawing_height=drawing_height,
                      margin=margin, cols=cols, nx=nx, ny=ny, sampled=sampled,
                      xs=xs, x_bounds=(xlo, xhi), y_bounds=(ylo, yhi),
                      empty=empty, exhausted=exhausted, xspan=xspan, yspan=yspan,
                      title=title, x_label=x_label, readout=readout,
                      hover=hover, hover_readout=hover_readout)

    @staticmethod
    def _arrow(canvas, x0, y0, x1, y1, color, nx, ny):
        canvas.line(x0, y0, x1, y1, color)
        canvas.dot(x1, y1, color)
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1:
            return
        head = min(length * 0.6, max(2.0, min(20.0, min(nx, ny) * 0.05)))
        angle = math.atan2(dy, dx)
        for offset in (2.6, -2.6):
            canvas.line(x1, y1,
                        round(x1 - head * math.cos(angle + offset)),
                        round(y1 - head * math.sin(angle + offset)), color)

    def _draw(self, scene, canvas):
        if not scene.drawing_height or scene.empty:
            return
        plot, nx, ny = scene.plot, scene.nx, scene.ny
        xlo, xhi = scene.x_bounds
        ylo, yhi = scene.y_bounds

        def pixel_x(value):
            return round((value - xlo) / scene.xspan * (nx - 1)) if scene.xspan else 0

        def pixel_y(value):
            return round((yhi - value) / scene.yspan * (ny - 1)) if scene.yspan else 0

        if ylo <= 0 <= yhi:
            canvas.line(0, pixel_y(0), nx - 1, pixel_y(0), BORDER)
        if xlo <= 0 <= xhi:
            canvas.line(pixel_x(0), 0, pixel_x(0), ny - 1, BORDER)
        if plot.sweep and plot.sweep in self.values:
            playhead = self.values[plot.sweep]
            if plot.sweep_axis == 'y' and ylo <= playhead <= yhi:
                y = pixel_y(playhead)
                xstep = max(1, nx // max(1, scene.cols * 2)) * 4
                for x in range(0, nx, xstep):
                    canvas.dot(x, y, MUTED)
            elif xlo <= playhead <= xhi:
                x = pixel_x(playhead)
                ystep = max(1, ny // max(1, scene.drawing_height * 2)) * 2
                for y in range(0, ny, ystep):
                    canvas.dot(x, y, MUTED)
        for index, (curve, points, kind) in enumerate(scene.sampled):
            color = curve.color or _PALETTE[index % len(_PALETTE)]
            if kind == 'vector':
                for tail, tip in points:
                    x0, y0 = tail
                    x1, y1 = tip
                    if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
                        continue
                    if not (xlo <= x0 <= xhi and ylo <= y0 <= yhi
                            and xlo <= x1 <= xhi and ylo <= y1 <= yhi):
                        continue
                    self._arrow(canvas, pixel_x(x0), pixel_y(y0),
                                pixel_x(x1), pixel_y(y1), color, nx, ny)
                continue
            discrete = kind == 'implicit'
            previous = None
            for point in points:
                if point is None:
                    previous = None
                    continue
                x, y = point
                if not (math.isfinite(x) and math.isfinite(y)):
                    previous = None
                    continue
                if not (xlo <= x <= xhi and ylo <= y <= yhi):
                    previous = None
                    continue
                px, py = pixel_x(x), pixel_y(y)
                canvas.dot(px, py, color)
                if not discrete and previous is not None:
                    if abs(py - previous[1]) <= max(2, ny // 2):
                        canvas.line(previous[0], previous[1], px, py, color)
                previous = None if discrete else (px, py)
        if scene.hover is not None:
            hx, hy = scene.hover
            if 0 <= hx < nx and 0 <= hy < ny:
                canvas.line(hx, 0, hx, ny - 1, ACCENT)
                canvas.line(0, hy, nx - 1, hy, ACCENT)

    def _text_rows(self, scene, canvas):
        rows = [scene.title]
        for row in range(scene.drawing_height):
            prefix = ''
            if scene.margin:
                label = (f'{scene.y_bounds[1]:.3g}' if row == 0
                         else f'{scene.y_bounds[0]:.3g}' if row == scene.drawing_height - 1 else '')
                prefix = MUTED + label[:scene.margin - 1].rjust(scene.margin - 1) + ' '
            rendered = prefix
            last_color = None
            for col in range(scene.cols):
                color = canvas.colors[row][col]
                if color != last_color:
                    rendered += color
                    last_color = color
                rendered += canvas.glyphs[canvas.bits[row][col]]
            rows.append(rendered + BASE)
        if scene.height >= 3:
            rows.append(scene.x_label)
        rows.append(scene.readout)
        return rows[:scene.height]

    def _image_rows(self, scene):
        rows = [scene.title]
        for row in range(scene.drawing_height):
            prefix = ''
            if scene.margin:
                label = (f'{scene.y_bounds[1]:.3g}' if row == 0
                         else f'{scene.y_bounds[0]:.3g}' if row == scene.drawing_height - 1 else '')
                prefix = MUTED + label[:scene.margin - 1].rjust(scene.margin - 1) + ' '
            rows.append(prefix + ' ' * scene.cols + BASE)
        if scene.height >= 3:
            rows.append(scene.x_label)
        rows.append(scene.readout)
        return rows[:scene.height]

    def render(self, width, height, plot_index=0, hover=None, scheme='quadrant'):
        """Draw within the supplied viewport using the current axis views.

        Domain/overflow errors leave gaps. Large adjacent jumps are not joined.
        An exhausted shared operation budget leaves gaps and a visible notice.
        No terminal I/O, terminal-size detection, or slider rendering occurs.

        ``hover`` is an optional (px, py) pixel position within the drawing
        area; when supplied a crosshair is drawn at that point and the
        corresponding data values are appended to the readout. ``scheme`` is
        ``quadrant`` (2x2) or ``sextant`` (2x3).
        """
        plot = self._plot(plot_index)
        if type(width) is not int or type(height) is not int:
            raise ValueError('width and height must be integers')
        width, height = max(0, min(240, width)), max(0, min(64, height))
        if not width or not height:
            return []
        if scheme not in _GLYPHS:
            scheme = 'quadrant'
        sx, sy, glyphs = _GLYPHS[scheme]
        scene = self._scene(width, height, plot_index, hover, sub_x=sx, sub_y=sy)
        if height == 1:
            return [scene.title]
        canvas = _CellCanvas(scene.cols, scene.drawing_height, sx, sy, glyphs)
        self._draw(scene, canvas)
        return self._text_rows(scene, canvas)

    def render_image(self, width, height, plot_index=0, hover=None,
                     pixel_width=None, pixel_height=None):
        """Return text rows and a palette raster for a graphics protocol.

        The raster covers ``image.width`` terminal columns and ``image.height``
        rows starting at ``image.row``/``image.col`` relative to the returned
        rows. ``hover`` uses raster pixel coordinates.
        """
        plot = self._plot(plot_index)
        if type(width) is not int or type(height) is not int:
            raise ValueError('width and height must be integers')
        width, height = max(0, min(240, width)), max(0, min(64, height))
        if not width or height < 2:
            return None
        margin = 10 if width >= 24 else 0
        cols = width - margin
        drawing_height = max(0, height - 3)
        if drawing_height <= 0 or cols <= 0:
            return None
        if pixel_width is None:
            pixel_width = cols * 4
        if pixel_height is None:
            pixel_height = drawing_height * 8
        pixel_width = max(1, min(2000, int(pixel_width)))
        pixel_height = max(1, min(2000, int(pixel_height)))
        scene = self._scene(width, height, plot_index, hover,
                            pixel_size=(pixel_width, pixel_height))
        canvas = _PixelCanvas(pixel_width, pixel_height)
        self._draw(scene, canvas)
        raster = Raster(pixel_width, pixel_height, bytes(canvas.pixels),
                        tuple(canvas.palette))
        return GraphImage(rows=self._image_rows(scene), raster=raster, row=1,
                          col=margin, width=cols, height=drawing_height)
