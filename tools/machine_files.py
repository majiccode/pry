"""Bounded, read-only machine configuration snapshots, independent of the UI.

Each source is independent, including missing/unreadable roots. ``complete``
means enumeration finished within policy, not that every file has content.
Intentional exclusions are blocked even on complete sources. Comparisons MUST
ignore a path equal to, or below, any blocked prefix in EITHER source (using
path components, not a bare string prefix). In particular, never infer removals
below a blocked prefix. A missing source is not an empty snapshot either.

Only roots are canonicalized; descendants are never followed through symlinks.
Snapshots are not atomic. Identity checks detect ordinary concurrent mutations,
not adversarial changes that restore metadata. Deadlines and cancellation are
cooperative between syscalls; a stalled filesystem syscall cannot be timed out.
No source is returned on cancellation, so callers can retain previous results.
"""

import hashlib
import os
import stat
import sys
import time
from collections import deque
from dataclasses import dataclass


MAX_ROOT_SECONDS = 20.0
MAX_TOTAL_SECONDS = 60.0
MAX_ROOT_ENTRIES = 100_000
MAX_TOTAL_ENTRIES = 200_000
MAX_DEPTH = 64
HASH_FILE_BYTES = 1024 * 1024
HASH_ROOT_BYTES = 32 * 1024 * 1024
TEXT_FILE_BYTES = 32 * 1024
TEXT_ROOT_BYTES = 1024 * 1024
MAX_NOTES = 64
PROGRESS_INTERVAL = 0.25

# These names are excluded at every depth, including when they name files.
EXCLUDED_NAMES = (
    'node_modules', '.git', '__pycache__', '.venv', 'venv', '.cache', '.Trash',
    '.ssh', '.gnupg', '.aws', '.kube',
)
HOME_EXCLUSIONS = (
    'Library/Caches', 'Library/Logs', 'Library/Developer',
    'Library/Containers', 'Library/Group Containers', 'Library/Metadata',
)
SYSTEM_ROOTS = ('/etc',)
MAC_SYSTEM_ROOTS = (
    '/Library/LaunchAgents', '/Library/LaunchDaemons',
    '/System/Library/LaunchAgents', '/System/Library/LaunchDaemons',
)


@dataclass(frozen=True)
class FileRecord:
    kind: str
    size: int
    mtime_ns: int
    mode: int
    digest: str | None = None
    text: str | None = None
    target: str | None = None
    hash_note: str = ''
    text_note: str = ''


def file_roots(scope='machine', custom=None):
    """Return canonical absolute roots, never filtering out unavailable roots.

    Custom scope selects exactly one folder, not additional machine roots. Its
    existence/type is checked during capture so an unavailable selection yields
    an explicit blocked source rather than an apparently empty successful scan.
    """
    if scope == 'machine':
        paths = [os.path.expanduser('~'), *SYSTEM_ROOTS]
        if sys.platform == 'darwin':
            paths.extend(MAC_SYSTEM_ROOTS)
    elif scope == 'custom':
        if custom is None or not os.fspath(custom):
            raise ValueError('Custom scope requires a folder path.')
        paths = [custom]
    else:
        raise ValueError('File scope must be machine or custom.')
    return list(dict.fromkeys(
        os.path.realpath(os.path.abspath(os.path.expanduser(os.fsdecode(path))))
        for path in paths
    ))


def describe_file_policy(scope='machine', custom=None):
    """Human-readable coverage, available without starting a capture."""
    roots = file_roots(scope, custom)
    home = os.path.realpath(os.path.expanduser('~'))
    return [
        'Files scope: ' + scope + '; selected folders only, not the whole disk.',
        *['Root: ' + repr(root) for root in roots],
        'Unavailable roots remain independent, blocked sources; custom scope '
        'requires a readable existing directory for a usable capture.',
        'Excluded names at every depth: ' + ', '.join(EXCLUDED_NAMES) + '.',
        'Excluded home subtrees (also when inside a custom root): '
        + ', '.join(repr(os.path.join(home, name)) for name in HOME_EXCLUSIONS) + '.',
        'Known credential directories above are excluded, but other files may '
        'contain secrets; this is not a comprehensive secret filter.',
        'Breadth-first, no descendant symlink traversal or detected mount '
        'crossing (device changes and platform mount checks); '
        'symlink targets and special-file metadata only.',
        f'Limits: {MAX_ROOT_SECONDS:g}s and {MAX_ROOT_ENTRIES:,} entries per root; '
        f'{MAX_TOTAL_SECONDS:g}s and {MAX_TOTAL_ENTRIES:,} entries overall; '
        f'directory depth {MAX_DEPTH}; at most 5 scanner-owned open descriptors.',
        f'SHA-256: at most {HASH_FILE_BYTES // 1024} KiB/file and '
        f'{HASH_ROOT_BYTES // (1024 * 1024)} MiB/root; larger files are metadata-only.',
        f'UTF-8, non-NUL text: at most {TEXT_FILE_BYTES // 1024} KiB/file; '
        f'{TEXT_ROOT_BYTES // (1024 * 1024)} MiB/root each for sampled bytes '
        'and retained string memory. No special-file contents are read.',
        'Unstable/unreadable paths, mounts, exclusions and unseen directories '
        'are blocked. Ignore blocked prefixes from BOTH captures in comparisons.',
        'Cancellation raises ValueError without returning partial results. '
        'Deadlines are cooperative; snapshots are not atomic.',
    ]


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _record(info, **content):
    kind = ('directory' if stat.S_ISDIR(info.st_mode) else
            'file' if stat.S_ISREG(info.st_mode) else
            'symlink' if stat.S_ISLNK(info.st_mode) else 'special')
    return FileRecord(kind, info.st_size, info.st_mtime_ns,
                      stat.S_IMODE(info.st_mode), **content)


class _LimitReached(Exception):
    pass


class _ChangedPath(Exception):
    def __init__(self, path):
        self.path = path


def capture_files(scope='machine', custom=None, cancel=None, progress=None):
    """Capture ``files:<canonical root>`` sources with absolute record keys.

    ``cancel`` is an optional Event-like object with ``is_set()``; ``progress``
    accepts a string, at most four times/second. Exceptions from the callback
    propagate. Hash budgets charge bytes read, including unsuccessful attempts.
    Open files are revalidated before/after reading, including their directory
    entry. Content budgets produce explicit metadata-only records, not gaps.
    """
    started = time.monotonic()
    last_progress = float('-inf')
    total_entries = 0
    sources = {}

    def cancelled():
        if cancel is not None and cancel.is_set():
            raise ValueError('File capture cancelled; previous results retained.')

    cancelled()
    roots = file_roots(scope, custom)
    home = os.path.realpath(os.path.expanduser('~'))
    home_excluded = {os.path.join(home, name) for name in HOME_EXCLUSIONS}
    # Fail closed on platforms lacking the descriptor-relative safety contract.
    supported = (hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY')
                 and all(fn in os.supports_dir_fd
                         for fn in (os.open, os.stat, os.readlink))
                 and os.scandir in os.supports_fd
                 and os.stat in os.supports_follow_symlinks)
    flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
    dir_flags = flags | getattr(os, 'O_DIRECTORY', 0)

    for root in roots:
        cancelled()
        root_started = time.monotonic()
        records = {}
        blocked = set()
        notes = []
        omitted_notes = 0
        complete = True
        entries = hashed = text_read = text_memory = 0
        metadata_only = text_unavailable = 0
        pending = deque()
        directories = {}
        root_fd = None
        current = None

        def note(message):
            nonlocal omitted_notes
            if len(notes) < MAX_NOTES:
                notes.append(message[:800])
            else:
                omitted_notes += 1

        def block(path, reason):
            nonlocal complete
            blocked.add(path)
            complete = False
            note(reason + ': ' + repr(path))

        def check():
            nonlocal last_progress
            cancelled()
            now = time.monotonic()
            if (now - root_started >= MAX_ROOT_SECONDS
                    or now - started >= MAX_TOTAL_SECONDS):
                raise _LimitReached('Time limit reached')
            if progress is not None and now - last_progress >= PROGRESS_INTERVAL:
                last_progress = now
                progress(f'Files: {root!r} ({entries:,} entries; '
                         f'{total_entries:,} overall)')
                cancelled()

        def open_root():
            # Even ancestors of the canonical root are opened without following
            # links, guarding against replacement since realpath resolution.
            fd = os.open(os.path.sep, dir_flags)
            try:
                for component in root.split(os.path.sep):
                    if not component:
                        continue
                    check()
                    before = os.stat(component, dir_fd=fd, follow_symlinks=False)
                    child = os.open(component, dir_flags, dir_fd=fd)
                    try:
                        if _identity(os.fstat(child)) != _identity(before):
                            raise _ChangedPath(root)
                    except BaseException:
                        os.close(child)
                        raise
                    os.close(fd)
                    fd = child
                return fd
            except BaseException:
                os.close(fd)
                raise

        def open_directory(path):
            # Reopen the queued ancestry instead of keeping one fd per queued
            # directory. At most root + two traversal fds are held here.
            fd = os.dup(root_fd)
            prefix = root
            try:
                if _identity(os.fstat(fd)) != directories[root]:
                    raise _ChangedPath(root)
                relative = os.path.relpath(path, root)
                for component in (() if relative == '.' else relative.split(os.path.sep)):
                    check()
                    prefix = os.path.join(prefix, component)
                    try:
                        child = os.open(component, dir_flags, dir_fd=fd)
                    except OSError as exc:
                        raise _ChangedPath(prefix) from exc
                    os.close(fd)
                    fd = child
                    info = os.fstat(fd)
                    if (info.st_dev != directories[root][0]
                            or _identity(info) != directories[prefix]):
                        raise _ChangedPath(prefix)
                return fd
            except BaseException:
                os.close(fd)
                raise

        def capture_entry(fd, name, path, before):
            nonlocal hashed, text_read, text_memory, metadata_only, text_unavailable
            content = {}
            if stat.S_ISLNK(before.st_mode):
                content['target'] = os.readlink(name, dir_fd=fd)
            elif stat.S_ISREG(before.st_mode):
                hash_ok = (before.st_size <= HASH_FILE_BYTES
                           and hashed + before.st_size <= HASH_ROOT_BYTES)
                text_ok = (before.st_size <= TEXT_FILE_BYTES
                           and text_read + before.st_size <= TEXT_ROOT_BYTES
                           and text_memory + before.st_size <= TEXT_ROOT_BYTES)
                content['hash_note'] = ('' if hash_ok else
                    'Metadata-only detection: hash file/root byte limit exceeded.')
                content['text_note'] = ('' if text_ok else
                    'Text diff unavailable: file/root text limit exceeded.')
                if hash_ok or text_ok:
                    file_fd = os.open(name, flags, dir_fd=fd)
                    try:
                        opened = os.fstat(file_fd)
                        if (not stat.S_ISREG(opened.st_mode)
                                or _identity(opened) != _identity(before)):
                            raise _ChangedPath(path)
                        hasher = hashlib.sha256() if hash_ok else None
                        chunks = []
                        remaining = before.st_size
                        while remaining:
                            check()
                            data = os.read(file_fd, min(65536, remaining))
                            if not data:
                                raise _ChangedPath(path)
                            remaining -= len(data)
                            if hash_ok:
                                hashed += len(data)
                                hasher.update(data)
                            if text_ok:
                                text_read += len(data)
                                chunks.append(data)
                        if _identity(os.fstat(file_fd)) != _identity(before):
                            raise _ChangedPath(path)
                        if hash_ok:
                            content['digest'] = hasher.hexdigest()
                        if text_ok:
                            data = b''.join(chunks)
                            try:
                                if b'\0' in data:
                                    raise UnicodeError()
                                text = data.decode('utf-8')
                                memory = sys.getsizeof(text)
                                if text_memory + memory > TEXT_ROOT_BYTES:
                                    content['text_note'] = (
                                        'Text diff unavailable: root string memory limit exceeded.')
                                else:
                                    text_memory += memory
                                    content['text'] = text
                            except UnicodeError:
                                content['text_note'] = (
                                    'Text diff unavailable: binary or non-UTF-8 content.')
                    finally:
                        os.close(file_fd)
                metadata_only += not bool(content.get('digest'))
                text_unavailable += content.get('text') is None
            if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(before):
                raise _ChangedPath(path)
            return _record(before, **content)

        # Seed fixed exclusions even when absent, making the policy independent
        # of which of the two captures happens to contain an excluded subtree.
        blocked.update(os.path.join(root, name) for name in EXCLUDED_NAMES)
        blocked.update(path for path in home_excluded
                       if path == root or path.startswith(root.rstrip('/') + '/'))
        note('Policy exclusions are blocked; content is bounded and may contain secrets.')
        try:
            check()
            if not supported:
                block(root, 'Required safe descriptor-relative operations unavailable')
            elif (root in home_excluded
                  or any(part in EXCLUDED_NAMES for part in root.split(os.path.sep))
                  or any(root.startswith(path + '/') for path in home_excluded)):
                blocked.add(root)
                note('Selected root lies inside a policy exclusion; not scanned.')
            else:
                root_fd = open_root()
                info = os.fstat(root_fd)
                directories[root] = _identity(info)
                records[root] = _record(info)
                pending.append((root, 0))
                while pending:
                    current, depth = pending.popleft()
                    check()
                    fd = None
                    try:
                        fd = open_directory(current)
                        with os.scandir(fd) as iterator:
                            for entry in iterator:
                                check()
                                if (entries >= MAX_ROOT_ENTRIES
                                        or total_entries >= MAX_TOTAL_ENTRIES):
                                    raise _LimitReached('Entry limit reached')
                                entries += 1
                                total_entries += 1
                                path = os.path.join(current, entry.name)
                                if entry.name in EXCLUDED_NAMES or path in home_excluded:
                                    blocked.add(path)
                                    continue
                                try:
                                    before = os.stat(entry.name, dir_fd=fd,
                                                     follow_symlinks=False)
                                    if (before.st_dev != info.st_dev
                                            or (stat.S_ISDIR(before.st_mode)
                                                and os.path.ismount(path))):
                                        block(path, 'Mount boundary')
                                        continue
                                    record = capture_entry(fd, entry.name, path, before)
                                    records[path] = record
                                    if record.kind == 'directory':
                                        if depth >= MAX_DEPTH:
                                            block(path, 'Directory depth limit reached')
                                        else:
                                            directories[path] = _identity(before)
                                            pending.append((path, depth + 1))
                                except _ChangedPath as exc:
                                    block(exc.path, 'Entry changed during capture')
                                except OSError as exc:
                                    block(path, 'Entry unavailable (' + type(exc).__name__ + ')')
                        if _identity(os.fstat(fd)) != directories[current]:
                            block(current, 'Directory changed during enumeration')
                    except _ChangedPath as exc:
                        block(exc.path, 'Directory changed or became unavailable')
                    except OSError as exc:
                        block(current, 'Directory enumeration unavailable ('
                              + type(exc).__name__ + ')')
                    finally:
                        if fd is not None:
                            os.close(fd)
                    current = None
                check()
                # The root name must still refer to the captured directory,
                # rather than an unlinked/replaced directory pinned by root_fd.
                verify_fd = open_root()
                try:
                    if _identity(os.fstat(verify_fd)) != directories[root]:
                        block(root, 'Root changed during capture')
                finally:
                    os.close(verify_fd)
        except _LimitReached as exc:
            # An interrupted iterator leaves the entire current directory
            # incomparable, not just its not-yet-seen entries.
            if current is not None:
                block(current, str(exc))
            for path, _depth in pending:
                blocked.add(path)
            if current is None and not pending:
                blocked.add(root)
            complete = False
            note(str(exc) + '; current and all pending directory prefixes blocked.')
            # Also validate the pinned root after an interrupted scan.
            if root_fd is not None and _identity(os.fstat(root_fd)) != directories[root]:
                blocked.add(root)
        except _ChangedPath as exc:
            block(root, 'Root path changed during capture')
        except OSError as exc:
            block(root, 'Root unavailable (' + type(exc).__name__ + ')')
        finally:
            if root_fd is not None:
                os.close(root_fd)
        cancelled()
        if metadata_only or text_unavailable:
            note(f'{metadata_only:,} regular files without hashes; '
                 f'{text_unavailable:,} without captured text.')
        if omitted_notes:
            notes.append(f'{omitted_notes:,} additional notes omitted.')
        sources['files:' + root] = dict(
            category='files', records=records, blocked=tuple(sorted(blocked)),
            complete=complete, notes=notes, captured_at=time.time(),
        )
    cancelled()
    return sources
