"""Bounded retrieval from installed manuals and known help interfaces."""
import os
import re
import shlex
import shutil
import subprocess
from functools import lru_cache

KNOWN = {'find', 'grep', 'sed', 'awk', 'ls', 'ps', 'ssh', 'scp', 'rsync', 'tar',
         'curl', 'git', 'chmod', 'chown', 'df', 'du', 'lsof', 'sort', 'uniq', 'xargs',
         'systemctl', 'launchctl', 'brew', 'apt', 'dnf', 'ip', 'ping', 'ss', 'netstat'}


@lru_cache(maxsize=64)
def document(command):
    words = shlex.split(command)
    if not words or not re.fullmatch(r'[A-Za-z0-9_.+-]+', words[0]):
        raise ValueError('Specify a manual name, for example: /docs rsync')
    name = words[0]
    source, text = f'man {name} (installed documentation)', ''
    env = {**os.environ, 'MANPAGER': 'cat', 'PAGER': 'cat', 'MANWIDTH': '90', 'TERM': 'dumb'}
    if shutil.which('man'):
        try:
            result = subprocess.run(['man', name], capture_output=True, text=True, errors='replace',
                                    timeout=5, env=env)
            if result.returncode == 0:
                text = re.sub(r'.\x08', '', result.stdout)
                text = re.sub(r'\x1b\[[0-9;]*m', '', text)
        except subprocess.SubprocessError:
            pass
    # Never invoke an arbitrary question-derived executable just to obtain help.
    if not text and name in KNOWN and shutil.which(name):
        try:
            result = subprocess.run([name, '--help'], capture_output=True, text=True,
                                    errors='replace', timeout=3, env=env)
            text = result.stdout or result.stderr
            source = f'{name} --help (installed tool)'
        except (OSError, subprocess.SubprocessError):
            pass
    if not text:
        return name, source, ''
    return name, source, text[:1024 * 1024]


def retrieve(command):
    words = shlex.split(command)
    if len(words) > 1:
        return search(words[0], ' '.join(words[1:]))
    name, source, text = document(command)
    if not text:
        return f'No installed documentation found for {name}.'
    return f'Source: {source}\n{text[:14000]}' + ('\n[Documentation excerpt truncated; follow-ups search the full cached manual]' if len(text) > 14000 else '')


def search(command, question):
    """Return ranked line windows with stable, verifiable source identifiers."""
    name, source, text = document(command)
    if not text:
        return f'No installed documentation found for {name}.'
    lines = text.splitlines()
    requested = re.fullmatch(r'L(\d+)-L(\d+)', question.strip(), re.IGNORECASE)
    if requested:
        start, end = map(int, requested.groups())
        if not 1 <= start <= end <= len(lines) or end - start >= 100:
            raise ValueError('Specify an existing manual line range of at most 100 lines.')
        return (f'[{name}:L{start}-L{end}] Source: {source}\n' +
                '\n'.join(f'{i + 1}: {lines[i]}' for i in range(start - 1, end)))
    flags = re.findall(r'(?<!\w)--?[A-Za-z][A-Za-z0-9-]*', question.lower())
    ignored = {'what', 'does', 'this', 'that', 'with', 'about', 'explain', 'which', 'please', name}
    terms = [word for word in re.findall(r'[a-z][a-z0-9_-]{2,}', question.lower()) if word not in ignored]
    ranked = []
    for i, line in enumerate(lines):
        lower = line.lower()
        score = sum(12 for flag in flags if re.search(re.escape(flag) + r'(?![\w-])', lower))
        score += sum(1 for term in set(terms) if term in lower)
        if score:
            ranked.append((score, i))
    chosen = []
    for _, index in sorted(ranked, key=lambda pair: (-pair[0], pair[1])):
        start, end = max(0, index - 2), min(len(lines), index + 16)
        if any(start < b and end > a for a, b in chosen):
            continue
        chosen.append((start, end))
        if len(chosen) == 3:
            break
    if not chosen:
        chosen = [(0, min(30, len(lines)))]
    excerpts = []
    for start, end in chosen:
        label = f'[{name}:L{start + 1}-L{end}]'
        excerpts.append(label + ' Source: ' + source + '\n' +
                        '\n'.join(f'{i + 1}: {lines[i]}' for i in range(start, end)))
    return '\n\n'.join(excerpts)


def for_question(question, commands=()):
    explicit = [word.lower() for word in re.findall(r'\b[A-Za-z][A-Za-z0-9_-]*\b', question)
                if word.lower() in KNOWN]
    names = list(dict.fromkeys(explicit or commands))[:2]
    return '\n\n'.join(search(name, question) for name in names)
