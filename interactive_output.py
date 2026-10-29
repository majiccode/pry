"""Recognize terminal results and load small, read-only inline inspector pages."""

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any
import inspector_registry as inspectors
from file_preview import preview_metadata

def run(args, cwd):
    result = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, errors='replace', timeout=8)
    if result.returncode:
        raise ValueError(result.stderr.strip()[:2000] or 'Item is no longer available.')
    return result.stdout


def file_entry(path, label=None):
    path = Path(path).absolute()
    return dict(label=label or path.name or str(path),
                target=dict(kind='path', path=str(path)))


def directory_entries(path, hidden=True):
    entries = []
    with os.scandir(path) as iterator:
        for item in iterator:
            if hidden or not item.name.startswith('.'):
                entries.append(file_entry(Path(path) / item.name,
                                          item.name + ('/' if item.is_dir(follow_symlinks=False) else '')))
    return sorted(entries, key=lambda e: e['label'].casefold())


def git_entries(cwd):
    root = run(['git', 'rev-parse', '--show-toplevel'], cwd).strip()
    # NUL-delimited records preserve spaces, quoting and rename destinations.
    records = run(['git', 'status', '--porcelain=v1', '-z', '--untracked-files=normal'], root).split('\0')
    entries, index = [], 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        status, name = record[:2], record[3:]
        if 'R' in status or 'C' in status:
            index += 1
        entries.append(dict(label=f'{status} {name}', target=dict(kind='git', cwd=root, path=name)))
    return entries


def recognize_output(command, stdout, cwd, exit_code):
    """Use conservative command recognition; never execute parsed output as shell code."""
    entries, title, directory, name = [], 'Links / files', None, ''
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
        lexer.whitespace_split = True
        words = list(lexer)
        simple = words and not any(set(word) <= set(';&|<>()') for word in words)
        name = Path(words[0]).name if simple else ''
        args = words[1:]
        specialized = inspectors.recognize(command, stdout, cwd)
        if specialized is not None:
            return specialized
        if exit_code == 0 and name == 'ls':
            # Reinspect only ordinary listing flags; complex shell expressions fall back below.
            flags = [arg for arg in args if arg.startswith('-') and arg != '--']
            supported = all(re.fullmatch(r'-[1alhtrSdFAGnp]+|--all|--almost-all', flag) for flag in flags)
            if supported and not any(any(c in arg for c in '*?`$') for arg in args):
                targets, options = [], True
                for arg in args:
                    if arg == '--' and options:
                        options = False
                    elif not (options and arg.startswith('-')):
                        targets.append(Path(cwd) / os.path.expanduser(arg))
                hidden = any(flag in ('--all', '--almost-all') or 'a' in flag or 'A' in flag for flag in flags)
                for target in targets or [Path(cwd)]:
                    if target.is_dir() and not any('d' in flag for flag in flags):
                        entries.extend(directory_entries(target, hidden))
                    elif target.exists():
                        entries.append(file_entry(target))
                title = 'Files'
                candidates = targets or [Path(cwd)]
                if len(candidates) == 1 and candidates[0].is_dir():
                    directory = str(candidates[0].resolve())
        elif exit_code == 0 and name == 'ps':
            rows = stdout.splitlines()
            header_index = next((i for i, row in enumerate(rows[:5]) if 'PID' in row.split()), None)
            if header_index is not None:
                pid_column = rows[header_index].split().index('PID')
                for row in rows[header_index + 1:]:
                    fields = row.split()
                    if len(fields) > pid_column and fields[pid_column].isdigit():
                        entries.append(dict(label=row.strip(), target=dict(kind='process', pid=int(fields[pid_column]), cwd=cwd)))
                title = 'Processes'
                # Preserve the snapshot's sortable CPU/memory columns.
                columns = rows[header_index].split()
                for entry in entries:
                    fields = entry['label'].split()
                    for column, key in [('%CPU', 'cpu'), ('%MEM', 'memory'), ('PCPU', 'cpu'), ('PMEM', 'memory')]:
                        if column in columns and len(fields) > columns.index(column):
                            try:
                                entry[key] = float(fields[columns.index(column)])
                            except ValueError:
                                pass
        elif exit_code == 0 and name == 'git' and args and args[0] == 'status' and all(a.startswith('-') for a in args[1:]):
            entries, title = git_entries(cwd), 'Git changes'
        if not entries and name != 'ls':
            records = stdout.split('\0') if '\0' in stdout else stdout.splitlines()
            for line in records:
                candidate = Path(cwd) / line
                if line and candidate.exists():
                    entries.append(file_entry(candidate))
        for url in dict.fromkeys(re.findall(r'https?://[^\s<>\x00-\x1f]+', stdout)):
            url = url.rstrip('.,;)]}')
            entries.append(dict(label=url, target=dict(kind='url', url=url)))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    page: dict[str, Any] | None = dict(title=title, entries=entries, note='Filesystem and process details are live.') if entries or directory else None
    if page is not None and directory:
        page.update(attach=directory, up=dict(kind='path', path=str(Path(directory).parent)))
    if page is not None and name == 'ps':
        page['sortable'] = True
    return page


def refresh_output(command, stdout, cwd):
    words = inspectors.words(command)
    if not words:
        raise ValueError('Refresh unavailable for compound commands.')
    name = Path(words[0]).name
    # Never replay curl or arbitrary container/service commands on refresh.
    if name in inspectors.REGISTRY:
        args = None
        if name == 'tail':
            paths = [arg for arg in words[1:] if not arg.startswith('-')]
            if paths and Path(cwd, paths[-1]).is_file():
                args = ['tail', '-n', '200', '--', paths[-1]]
        elif name == 'journalctl':
            args = ['journalctl', '-n', '200', '--no-pager']
            for flag in ('-u', '--unit', '--user-unit'):
                if flag in words and words.index(flag) + 1 < len(words):
                    args += [flag, words[words.index(flag) + 1]]
            if '--user' in words:
                args.append('--user')
        elif name in ('docker', 'podman') and words[1:2] == ['logs']:
            args = [name, 'logs', '--tail', '200', words[-1]]
        elif name in ('docker', 'podman') and words[1:2] == ['ps']:
            args = [name, 'ps', '-a', '--no-trunc']
        elif name == 'systemctl':
            args = ['systemctl'] + (['--user'] if '--user' in words else []) + ['list-units', '--all', '--no-pager']
        elif name == 'launchctl':
            args = ['launchctl', 'list']
        elif name == 'lsof':
            args = ['lsof', '-nP', '-i']
        elif name == 'ss':
            args = ['ss', '-tunap']
        elif name == 'netstat':
            args = ['netstat', '-an']
        elif name == 'df':
            args = ['df', '-h']
        elif name == 'du':
            return recognize_output(command, stdout, cwd, 0)
        if args:
            return recognize_output(shlex.join(args), inspectors.query(args, cwd), cwd, 0)
        result = recognize_output(command, stdout, cwd, 0)
        if result:
            result['note'] = 'Reopened captured snapshot. Use a new command to obtain fresh data.'
        return result
    if words and Path(words[0]).name == 'ps':
        # ps is read-only, but reject shell operators and shell expressions.
        if any(any(c in word for c in ';|&<>`$()\n') for word in words):
            raise ValueError('Refresh unavailable for compound process commands.')
        stdout = run(['ps'] + words[1:], cwd)
    return recognize_output(command, stdout, cwd, 0)


def load_view(target, read_text):
    """Return a bounded page. All subprocesses use argument arrays and a timeout."""
    kind = target['kind']
    specialized = inspectors.load(target, read_text)
    if specialized is not None:
        return specialized
    if kind == 'search_match' and Path(target['path']).is_dir():
        return load_view(dict(kind='path', path=target['path']), read_text)
    if kind == 'path':
        path = Path(target['path'])
        if path.is_symlink():
            raise ValueError('Symlink previews are unavailable.')
        if path.is_dir():
            return dict(title=str(path), entries=directory_entries(path), text='', attach=str(path),
                         up=dict(kind='path', path=str(path.parent)))
        if inspectors.is_archive(path):
            return inspectors.archive_page(str(path))
        text = read_text(path)
        return dict(title=str(path), entries=[], text=text, attach=str(path),
                    file_preview=preview_metadata(path, text))
    if kind == 'process':
        return inspectors.process_details(int(target['pid']), target['cwd'])
    if kind == 'git':
        cwd, name = target['cwd'], target['path']
        text = run(['git', '--no-pager', 'diff', '--no-ext-diff', '--no-textconv', '--', name], cwd)
        staged = run(['git', '--no-pager', 'diff', '--cached', '--no-ext-diff', '--no-textconv', '--', name], cwd)
        path = Path(cwd) / name
        content = 'Working tree diff:\n' + (text or '(none)') + '\nStaged diff:\n' + (staged or '(none)')
        if not text and not staged and path.is_file():
            content += '\nFile preview:\n' + read_text(path)
        return dict(title=name, text=content, entries=[], attach=str(path) if path.exists() else None)
    raise ValueError('Unsupported inline view')


def mark_unavailable(page, read_text):
    """Keep binary, symlink and unavailable paths visible but noninteractive."""
    if page:
        for entry in page.get('entries', []):
            target = entry.get('target', {})
            if target.get('kind') != 'path':
                continue
            path = Path(target['path'])
            try:
                if path.is_symlink():
                    raise ValueError('symlink')
                if not path.is_dir() and not inspectors.is_archive(path):
                    read_text(path)
            except (OSError, ValueError) as exc:
                entry['label'] += f' (unavailable: {str(exc)[:120]})'
                entry['target'] = None
    return page
