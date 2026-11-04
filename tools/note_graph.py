"""Bounded note graphs, without eval, exec, or third-party dependencies.

One statement per line; # comments and semicolon separators are supported.
Ranges have constant endpoints. A scalar assignment to a ranged name sets its
initial value (and must also be constant). Other assignments are live formulas.
Each plot captures all formula definitions preceding it: every definition of
its output is a curve, and dependencies use their last captured definition.
Forward references within that snapshot are allowed; cycles are not.

render(width, height) returns at most height rows of at most width visible
columns, capped at 240 columns and 64 rows. Height includes the title/legend,
horizontal-axis labels, and playhead readout, not slider controls. Braille uses
real Unicode characters for a 2-by-4 subcell drawing grid. ANSI SGR sequences
do not count toward the width. Small viewports gracefully omit drawing rows.
"""

import ast
from collections.abc import Mapping
from dataclasses import dataclass
import math
import operator
import re

from tui_theme import ACCENT, BASE, BORDER, MUTED, SECONDARY, SUCCESS, WARNING


EXAMPLE_SOURCE = """xaxis=[0..1]
t=[0..1]
t=0
amp=[-2..2]
amp=0
a=sin(2*pi*(xaxis-t))+amp
x=a
x=cos(2*pi*(xaxis-t))+amp
axisy=[-3..3]
plot(x,xaxis,t,amp)"""

_LIMIT = 1e100
_FUNCTIONS = {
    'sin': (math.sin, 1, 1), 'cos': (math.cos, 1, 1),
    'tan': (math.tan, 1, 1), 'asin': (math.asin, 1, 1),
    'acos': (math.acos, 1, 1), 'atan': (math.atan, 1, 1),
    'atan2': (math.atan2, 2, 2), 'sqrt': (math.sqrt, 1, 1),
    'log': (math.log, 1, 2), 'log10': (math.log10, 1, 1),
    'exp': (math.exp, 1, 1), 'abs': (abs, 1, 1),
    'floor': (math.floor, 1, 1), 'ceil': (math.ceil, 1, 1),
    'min': (min, 2, 16), 'max': (max, 2, 16),
}
_CONSTANTS = {'pi': math.pi, 'e': math.e, 'tau': math.tau}
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
_PALETTE = (SECONDARY, SUCCESS, WARNING, '\033[38;5;213m',
            '\033[38;5;81m', '\033[38;5;203m', '\033[38;5;229m',
            '\033[38;5;141m')
_NAME = r'[A-Za-z_][A-Za-z_0-9]{0,31}'
_DOTS = ((1, 8), (2, 16), (4, 32), (64, 128))


class _BudgetExceeded(Exception):
    pass


def _error(line, message):
    return ValueError(f'line {line}: {message}')


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('expected a real number')
    value = float(value)
    if not math.isfinite(value) or abs(value) > _LIMIT:
        raise ValueError('number is non-finite or exceeds 1e100')
    return value


@dataclass(frozen=True)
class _Expression:
    node: ast.AST
    names: frozenset
    line: int


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
        elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            children = (node.left, node.right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            children = (node.operand,)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            spec = _FUNCTIONS.get(node.func.id)
            if spec is None or node.keywords or not spec[1] <= len(node.args) <= spec[2]:
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
        return float(node.value)
    if isinstance(node, ast.Name):
        return _CONSTANTS[node.id] if node.id in _CONSTANTS else resolve(node.id)
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, resolve, budget)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, resolve, budget)
        right = _evaluate(node.right, resolve, budget)
        if isinstance(node.op, ast.Pow) and abs(right) > 128:
            raise ValueError('exponent exceeds 128')
        return _finite(_BINARY[type(node.op)](left, right))
    args = [_evaluate(arg, resolve, budget) for arg in node.args]
    return _finite(_FUNCTIONS[node.func.id][0](*args))


def _constant(expression):
    if expression.names:
        raise _error(expression.line, 'range endpoints and initial values must be constant')
    try:
        return _evaluate(expression.node, lambda name: 0.0, [4096])
    except (ArithmeticError, ValueError, _BudgetExceeded) as exc:
        raise _error(expression.line, 'invalid constant expression') from exc


class Graph:
    """Parse eagerly; invalid declarations raise line-specific ValueError.

    ranges excludes axisx/axisy. values contains only ranged variables, with
    finite persisted numbers clamped to their ranges; invalid entries are
    ignored. Defaults are the range midpoint, or a constant scalar initializer.
    controls() returns the sweep first, explicit parameters next, then inferred
    ranged dependencies in declaration order. adjust() accepts an absolute
    fraction, clamps it to [0, 1], and updates values in place.
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
        self.plots = []
        definitions, axes, initial = {}, {}, {}
        node_count = [0]
        assignments = 0
        for line, raw in enumerate(lines, 1):
            for statement in raw.split('#', 1)[0].split(';'):
                statement = statement.strip()
                if not statement:
                    continue
                plot_match = re.fullmatch(r'plot\s*\((.*)\)', statement)
                if plot_match:
                    args = [arg.strip() for arg in plot_match[1].split(',')]
                    if not 2 <= len(args) <= 18 or not all(re.fullmatch(_NAME, a) for a in args):
                        raise _error(line, 'use plot(output, sweep, optional_parameters...)')
                    if len(set(args)) != len(args):
                        raise _error(line, 'plot names must be distinct')
                    if len(self.plots) >= 8:
                        raise _error(line, 'at most 8 plots are allowed')
                    curves = tuple(definitions.get(args[0], ()))
                    self.plots.append(_Plot(args[0], args[1], tuple(args[2:]),
                                            {k: tuple(v) for k, v in definitions.items()},
                                            curves, dict(axes), line))
                    continue
                match = re.fullmatch(rf'({_NAME})\s*=\s*(.+)', statement)
                if not match:
                    raise _error(line, 'expected name=expression, name=[lo..hi], or plot(...)')
                name, text = match.groups()
                if name in _CONSTANTS or name in _FUNCTIONS or name == 'plot':
                    raise _error(line, f'reserved name {name}')
                assignments += 1
                if assignments > 96:
                    raise _error(line, 'at most 96 assignments are allowed')
                if text.startswith('['):
                    bounds = re.fullmatch(r'\[\s*(.+?)\s*\.\.\s*(.+?)\s*\]', text)
                    if not bounds:
                        raise _error(line, 'expected range [lo..hi]')
                    lo, hi = (_constant(_expression(part, line, node_count)) for part in bounds.groups())
                    if lo >= hi:
                        raise _error(line, 'range lower bound must be less than upper bound')
                    if name in ('axisx', 'axisy'):
                        axes[name] = (lo, hi)
                    else:
                        if name in self.ranges or name in definitions:
                            raise _error(line, f'duplicate range or formula for {name}')
                        if len(self.ranges) >= 16:
                            raise _error(line, 'at most 16 ranged variables are allowed')
                        self.ranges[name] = (lo, hi)
                        self.values[name] = lo / 2 + hi / 2
                else:
                    if name in ('axisx', 'axisy'):
                        raise _error(line, f'{name} requires a range')
                    expression = _expression(text, line, node_count)
                    if name in self.ranges:
                        initial[name] = _constant(expression)
                    else:
                        variants = definitions.setdefault(name, [])
                        if len(variants) >= 24:
                            raise _error(line, 'at most 24 curves per name are allowed')
                        variants.append(expression)
        # Also validate unused formulas, rather than silently accepting typos.
        self._validate(definitions)
        if not self.plots:
            raise _error(max(1, len(lines)), 'at least one plot is required')
        for plot in self.plots:
            if not plot.curves:
                raise _error(plot.line, f'no formula defined for {plot.output}')
            self._validate(plot.definitions)
            for name in (plot.sweep,) + plot.parameters:
                if name not in self.ranges:
                    raise _error(plot.line, f'{name} must have a declared range')
            dependencies = set()

            def collect(expression):
                for name in sorted(expression.names):
                    if name in dependencies:
                        continue
                    dependencies.add(name)
                    if name not in self.ranges:
                        collect(plot.definitions[name][-1])

            for curve in plot.curves:
                collect(curve)
            controls = list(dict.fromkeys((plot.sweep,) + plot.parameters))
            controls.extend(name for name in self.ranges if name in dependencies and name not in controls)
            plot.controls = tuple(controls)
        for name, value in initial.items():
            lo, hi = self.ranges[name]
            self.values[name] = max(lo, min(hi, value))
        if isinstance(values, Mapping):
            for name in self.ranges:
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

    def _validate(self, definitions):
        depths, active = {}, set()

        def visit(name, line, depth):
            if name in self.ranges:
                return 0
            if name not in definitions:
                raise _error(line, f'unknown name {name}')
            if name in active:
                raise _error(line, f'circular dependency involving {name}')
            if depth > 24:
                raise _error(line, 'dependency depth exceeds 24')
            if name in depths:
                if depth + depths[name] - 1 > 24:
                    raise _error(line, 'dependency depth exceeds 24')
                return depths[name]
            active.add(name)
            expression = definitions[name][-1]
            height = 1
            for dependency in sorted(expression.names):
                height = max(height, 1 + visit(dependency, expression.line, depth + 1))
            active.remove(name)
            depths[name] = height
            return height

        for name, variants in definitions.items():
            visit(name, variants[-1].line, 1)
            # Earlier output variants are still executable curves.
            for expression in variants:
                for dependency in sorted(expression.names):
                    visit(dependency, expression.line, 1)

    def _plot(self, index):
        if type(index) is not int or not 0 <= index < len(self.plots):
            raise ValueError('invalid plot index')
        return self.plots[index]

    def controls(self, plot_index=0):
        return list(self._plot(plot_index).controls)

    def adjust(self, name, fraction):
        if name not in self.ranges:
            raise ValueError(f'unknown ranged variable {name}')
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise ValueError('fraction must be finite')
        try:
            fraction = float(fraction)
        except OverflowError as exc:
            raise ValueError('fraction must be finite') from exc
        if not math.isfinite(fraction):
            raise ValueError('fraction must be finite')
        fraction = max(0.0, min(1.0, fraction))
        lo, hi = self.ranges[name]
        self.values[name] = lo * (1 - fraction) + hi * fraction

    def _sample(self, plot, expression, sweep, budget):
        cache = {**self.values, plot.sweep: sweep}

        def resolve(name):
            if name not in cache:
                cache[name] = _evaluate(plot.definitions[name][-1].node, resolve, budget)
            return cache[name]

        try:
            return _evaluate(expression.node, resolve, budget)
        except (ArithmeticError, ValueError, RecursionError):
            return None

    def render(self, width, height, plot_index=0):
        """Draw within the supplied viewport; axisx overrides horizontal bounds.

        Domain/overflow errors leave gaps. Large adjacent jumps are not joined.
        An exhausted shared operation budget leaves gaps and a visible notice.
        No terminal I/O, terminal-size detection, or slider rendering occurs.
        """
        plot = self._plot(plot_index)
        if type(width) is not int or type(height) is not int:
            raise ValueError('width and height must be integers')
        width, height = max(0, min(240, width)), max(0, min(64, height))
        if not width or not height:
            return []

        def styled(text, color=MUTED):
            return color + text[:width] + BASE

        legend = f'{plot.output} vs {plot.sweep} | '
        title = ACCENT + legend[:width]
        used = min(width, len(legend))
        for index, expression in enumerate(plot.curves):
            label = f'{plot.output}#{index + 1}(L{expression.line}) '
            title += _PALETTE[index % len(_PALETTE)] + label[:max(0, width - used)]
            used = min(width, used + len(label))
        rows = [title + BASE]
        if height == 1:
            return rows
        lo, hi = self.ranges[plot.sweep]
        if 'axisx' in plot.axes:
            lo, hi = plot.axes['axisx']
        empty = lo >= hi
        budget = [200000]
        exhausted = False
        drawing_height = max(0, height - 3)
        margin = 10 if width >= 24 else 0
        cols = width - margin
        nx, ny = max(2, cols * 2), max(4, drawing_height * 4)
        samples = []
        all_y = []
        if not empty and drawing_height:
            for expression in plot.curves:
                curve = []
                for i in range(nx):
                    t = lo * (1 - i / (nx - 1)) + hi * (i / (nx - 1))
                    try:
                        value = self._sample(plot, expression, t, budget)
                    except _BudgetExceeded:
                        exhausted = True
                        value = None
                    curve.append(value)
                    if value is not None:
                        all_y.append(value)
                samples.append(curve)
        if 'axisy' in plot.axes:
            bottom, top = plot.axes['axisy']
        elif all_y:
            bottom, top = min(all_y), max(all_y)
            pad = max(abs(bottom) * .05, 1.0) if bottom == top else (top - bottom) * .08
            bottom, top = max(-_LIMIT, bottom - pad), min(_LIMIT, top + pad)
        else:
            bottom, top = -1.0, 1.0
        span = top - bottom
        bits = [[0] * cols for _ in range(drawing_height)]
        colors = [[BORDER] * cols for _ in range(drawing_height)]

        def dot(x, y, color):
            if 0 <= x < nx and 0 <= y < drawing_height * 4:
                row, col = y // 4, x // 2
                bits[row][col] |= _DOTS[y % 4][x % 2]
                colors[row][col] = color

        def pixel_y(value):
            return round((top - value) / span * (ny - 1))

        if drawing_height and not empty:
            if bottom <= 0 <= top:
                y = pixel_y(0)
                for x in range(nx):
                    dot(x, y, BORDER)
            if lo <= 0 <= hi:
                x = round((0 - lo) / (hi - lo) * (nx - 1))
                for y in range(ny):
                    dot(x, y, BORDER)
            playhead = self.values[plot.sweep]
            if lo <= playhead <= hi:
                x = round((playhead - lo) / (hi - lo) * (nx - 1))
                for y in range(0, ny, 4):
                    dot(x, y, MUTED)
            for index, curve in enumerate(samples):
                previous = None
                color = _PALETTE[index % len(_PALETTE)]
                for x, value in enumerate(curve):
                    if value is None or not bottom <= value <= top:
                        previous = None
                        continue
                    y = pixel_y(value)
                    dot(x, y, color)
                    # Do not connect poles or jumps across a substantial axis span.
                    if previous is not None and abs(y - previous) <= max(2, ny // 4):
                        distance = abs(y - previous)
                        for step in range(1, distance):
                            dot(x, previous + (1 if y > previous else -1) * step, color)
                    previous = y
        for row in range(drawing_height):
            prefix = ''
            if margin:
                label = f'{top:.3g}' if row == 0 else f'{bottom:.3g}' if row == drawing_height - 1 else ''
                prefix = MUTED + label[:margin - 1].rjust(margin - 1) + ' '
            rendered = prefix
            last_color = None
            for col in range(cols):
                color = colors[row][col]
                if color != last_color:
                    rendered += color
                    last_color = color
                rendered += chr(0x2800 + bits[row][col]) if bits[row][col] else ' '
            rows.append(rendered + BASE)
        if height >= 3:
            left = f'{lo:.4g}' if not empty else 'axisx outside sweep range'
            right = f'{hi:.4g} {plot.sweep}' if not empty else ''
            label = left + ' ' * max(1, cols - len(left) - len(right)) + right
            rows.append(styled(' ' * margin + label))
        readout = f'{plot.sweep}={self.values[plot.sweep]:.4g}'
        for index, expression in enumerate(plot.curves):
            try:
                value = self._sample(plot, expression, self.values[plot.sweep], budget)
            except _BudgetExceeded:
                exhausted = True
                value = None
            readout += f' {plot.output}#{index + 1}=' + (f'{value:.4g}' if value is not None else 'gap')
        if exhausted:
            readout = 'budget limit | ' + readout
        elif empty:
            readout = 'empty axis | ' + readout
        elif drawing_height and not all_y:
            readout = 'no finite samples | ' + readout
        rows.append(styled(readout, WARNING if exhausted or empty else ACCENT))
        return rows[:height]
