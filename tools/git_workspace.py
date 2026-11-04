"""Git repository operations using the user's existing configuration and credentials."""
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote
from typing import Any
import webbrowser


class GitError(ValueError):
    pass


class Repository:
    def __init__(self, directory, cancel=None):
        self.cwd = str(directory)
        self.cancel = cancel or threading.Event()
        self.root = self.git('rev-parse', '--show-toplevel').strip()
        self.cwd = self.root

    def run(self, args, allowed=(0,), timeout=120):
        if self.cancel.is_set():
            raise GitError('Cancelled. Refresh to inspect repository state.')
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_EDITOR='true',
                   GIT_SEQUENCE_EDITOR='true', GIT_PAGER='cat', GH_PROMPT_DISABLED='1')
        # A file avoids both pipe deadlocks and unbounded in-memory output.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(args, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=output, start_new_session=True)
            started = time.monotonic()
            reason = None
            while process.poll() is None:
                if self.cancel.is_set() or time.monotonic() - started > timeout:
                    reason = 'Cancelled' if self.cancel.is_set() else 'Timed out'
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    break
                time.sleep(.04)
            output.seek(0)
            data = output.read(8 * 1024 * 1024 + 1)
        text = data.decode('utf-8', errors='replace')
        if reason:
            raise GitError(reason + '. Refresh to inspect repository state.\n' + text[-16000:])
        if len(data) > 8 * 1024 * 1024:
            raise GitError('Git output exceeds 8 MiB. Narrow the selection or use /run git … .')
        if process.returncode not in allowed:
            raise GitError(text.strip() or f'{args[0]} exited with status {process.returncode}')
        return text

    def git(self, *args, allowed=(0,)):
        literal = ['--literal-pathspecs'] if args and args[0] in ('add', 'restore', 'rm', 'diff') else []
        return self.run(['git', '--no-pager', *literal, '-c', 'color.ui=false', *args], allowed=allowed)

    def state(self) -> dict[str, Any]:
        records = self.git('status', '--porcelain=v1', '-z', '--untracked-files=all').split('\0')
        changed, staged, conflicts = [], [], []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            status, path = record[:2], record[3:]
            original = None
            if 'R' in status or 'C' in status:
                original = records[index]
                index += 1
            entry: dict[str, Any] = dict(path=path, original=original, status=status,
                         label=path + (f' ← {original}' if original else ''))
            if status in ('DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'):
                conflicts.append(dict(entry, section='conflict', label='Conflict · ' + entry['label']))
                changed.append(dict(entry, section='conflict', label='Conflict · ' + entry['label']))
            else:
                labels = {'M': 'Modified', 'A': 'Added', 'D': 'Deleted', 'R': 'Renamed', 'C': 'Copied', 'T': 'Type changed', '?': 'Untracked'}
                if status[0] not in ' ?':
                    staged.append(dict(entry, section='staged', label=labels.get(status[0], status[0]) + ' · ' + entry['label']))
                if status[1] != ' ' or status == '??':
                    changed.append(dict(entry, section='changed', label=labels.get(status[1], status[1]) + ' · ' + entry['label']))
        branch = self.git('symbolic-ref', '--quiet', '--short', 'HEAD', allowed=(0, 1)).strip()
        head = self.git('rev-parse', '--verify', 'HEAD', allowed=(0, 128)).strip()
        has_head = bool(re.fullmatch(r'[0-9a-f]{40,64}', head))
        upstream = self.git('for-each-ref', '--format=%(upstream:short)', 'refs/heads/' + branch).strip() if branch else ''
        ahead = behind = 0
        if upstream and has_head:
            counts = self.git('rev-list', '--left-right', '--count', 'HEAD...@{upstream}').split()
            ahead, behind = map(int, counts)
        branches = []
        for line in self.git('for-each-ref', '--format=%(refname:short)%00%(HEAD)%00%(upstream:short)', 'refs/heads').splitlines():
            name, current, tracking = line.split('\0')
            branches.append(dict(name=name, current=current == '*', upstream=tracking,
                                 label=name + (' · current' if current == '*' else '') + (f' → {tracking}' if tracking else '')))
        stashes = []
        for line in self.git('stash', 'list', '--format=%gd%x00%gs').splitlines():
            ref, message = line.split('\0', 1)
            stashes.append(dict(ref=ref, label=ref + ' · ' + message))
        operation = None
        for marker, name in [('MERGE_HEAD', 'merge'), ('rebase-merge', 'rebase'),
                             ('rebase-apply', 'rebase'), ('CHERRY_PICK_HEAD', 'cherry-pick'),
                             ('REVERT_HEAD', 'revert')]:
            marker_path = self.git('rev-parse', '--git-path', marker).strip()
            if (Path(self.root) / marker_path).exists():
                operation = name
                break
        remotes = self.git('remote').splitlines()
        remote = self.git('config', '--get', f'branch.{branch}.remote', allowed=(0, 1)).strip() if branch else ''
        if remote not in remotes:
            remote = 'origin' if 'origin' in remotes else remotes[0] if len(remotes) == 1 else ''
        return dict(root=self.root, branch=branch, has_head=has_head, upstream=upstream,
                    ahead=ahead, behind=behind, changed=changed, staged=staged, conflicts=conflicts,
                    branches=branches, stashes=stashes, operation=operation, remotes=remotes, remote=remote)

    def diff(self, entry):
        if entry['section'] == 'changed' and entry['status'] == '??':
            path = Path(self.root) / entry['path']
            if path.is_symlink():
                return 'Untracked symlink → ' + os.readlink(path)
            if not path.is_file():
                return 'Untracked directory or submodule: ' + entry['path']
            with path.open('rb') as handle:
                data = handle.read(64 * 1024 + 1)
            if b'\0' in data:
                return 'Binary untracked file (no text preview).'
            return 'Untracked file — complete file will be staged\n\n' + data[:64 * 1024].decode('utf-8', errors='replace') + ('\n[Preview truncated]' if len(data) > 64 * 1024 else '')
        args = ['diff', '--no-ext-diff', '--no-textconv']
        if entry['section'] == 'staged':
            args.append('--cached')
        paths = [entry['path']] + ([entry['original']] if entry.get('original') else [])
        return self.git(*args, '--', *paths) or 'No text diff (possibly a binary file or submodule).'

    def require_branch(self, state):
        if not state['branch']:
            raise GitError('Detached HEAD: create or switch to a branch first.')

    def push(self, state):
        self.require_branch(state)
        if state['upstream']:
            remote = self.git('config', '--get', f'branch.{state["branch"]}.remote').strip()
            target = self.git('config', '--get', f'branch.{state["branch"]}.merge').strip()
            return self.git('push', remote, 'HEAD:' + target)
        if not state['remote']:
            raise GitError('No unambiguous remote. Configure a remote/upstream with /run git … first.')
        return self.git('push', '--set-upstream', state['remote'], state['branch'])

    def github(self, state):
        self.require_branch(state)
        if not state['remote']:
            raise GitError('No GitHub remote configured.')
        url = self.git('remote', 'get-url', state['remote']).strip()
        match = re.fullmatch(r'(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([^/]+)/([^/]+?)(?:\.git)?', url)
        if not match:
            raise GitError('PR creation currently supports github.com remotes. Configure origin with its GitHub URL.')
        return '/'.join(match.groups())

    def perform(self, action, value='', entry=None):
        state = self.state()
        if action == 'diff':
            return self.diff(entry)
        if action in ('stage', 'unstage'):
            if entry is None:
                raise GitError('Select a file first.')
            if action == 'stage' and entry.get('section') not in ('changed', 'conflict'):
                raise GitError('Select a file in Changes to stage it.')
            if action == 'unstage' and entry.get('section') != 'staged':
                raise GitError('Select a file in Ready to commit to unstage it.')
            paths = [entry['path']] + ([entry['original']] if entry.get('original') else [])
            if action == 'stage':
                return self.git('add', '--', *paths) or 'File staged: included in the next commit.'
            args = ['restore', '--staged'] if state['has_head'] else ['rm', '--cached']
            return self.git(*args, '--', *paths) or 'File unstaged: working copy preserved.'
        if action == 'commit':
            if not state['staged']:
                raise GitError('Stage files before committing.')
            if not value.strip():
                raise GitError('Enter a commit message.')
            return self.git('commit', '-m', value)
        if action == 'fetch':
            return self.git('fetch', '--all') or 'Remote information refreshed. Working files unchanged.'
        if action == 'pull':
            if not state['upstream']:
                raise GitError('No upstream branch. Push this branch first, or configure tracking with /run git … .')
            return self.git('pull', '--ff-only') or 'Already up to date.'
        if action == 'push':
            return self.push(state)
        if action == 'sync':
            if state['operation'] or state['changed'] or state['staged']:
                raise GitError('Commit or stash local changes and finish any current operation before syncing.')
            output = self.git('fetch', '--all')
            state = self.state()
            if state['upstream']:
                output += self.git('pull', '--ff-only')
            return output + self.push(state)
        if action == 'create':
            self.git('check-ref-format', '--branch', value)
            return self.git('switch', '-c', value)
        if action in ('switch', 'merge', 'compare'):
            names = [b['name'] for b in state['branches']]
            if value not in names:
                raise GitError('Select an existing local branch.')
            if action == 'compare':
                return self.git('diff', '--no-ext-diff', '--no-textconv', 'HEAD...' + value, '--') or 'No differences from the selected branch.'
            if action == 'merge':
                if state['changed'] or state['staged']:
                    raise GitError('Commit or stash local changes before merging.')
                return self.git('merge', '--no-edit', value)
            return self.git('switch', value)
        if action == 'stash':
            return self.git('stash', 'push', '--include-untracked', '-m', value or 'Saved from AI Terminal')
        if action in ('stash_diff', 'apply'):
            if value not in [s['ref'] for s in state['stashes']]:
                raise GitError('Select an existing stash.')
            if action == 'stash_diff':
                return self.git('stash', 'show', '-p', '--include-untracked', '--no-ext-diff', '--no-textconv', value)
            return self.git('stash', 'apply', value) or 'Stash applied and retained in the stash list.'
        if action in ('continue', 'abort'):
            if not state['operation']:
                raise GitError('No merge, rebase, cherry-pick or revert in progress.')
            return self.git(state['operation'], '--' + action)
        if action == 'pr':
            if state['changed'] or state['staged'] or state['operation']:
                raise GitError('Commit or stash local changes and finish the current operation before creating a PR.')
            repo = self.github(state)
            fields = value.split('\n', 1)
            base, title = fields[0].strip(), fields[1].strip() if len(fields) > 1 else ''
            if not base or not title:
                raise GitError('Enter a target branch and PR title.')
            # PR head must be the same branch name submitted to GitHub, even
            # when the local branch tracks a differently named remote branch.
            output = self.git('push', '--set-upstream', state['remote'], state['branch'])
            if shutil.which('gh'):
                try:
                    self.run(['gh', 'auth', 'status', '--hostname', 'github.com'])
                except GitError:
                    if self.cancel.is_set():
                        raise
                else:
                    return output + self.run(['gh', 'pr', 'create', '--repo', repo, '--base', base,
                                              '--head', state['branch'], '--title', title, '--body', ''])
            url = f'https://github.com/{repo}/compare/{quote(base, safe="")}...{quote(state["branch"], safe="")}?expand=1&title={quote(title, safe="")}'
            opened = webbrowser.open(url)
            return output + ('Opened GitHub compare page.\n' if opened else 'Open this URL in your browser:\n') + url
        raise GitError('Unknown Git action: ' + action)
