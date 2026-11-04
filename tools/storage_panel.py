"""Bounded, cancellable, read-only logical-size snapshots and storage dashboard."""
import os
import queue
import stat
import threading
import time
from dataclasses import dataclass, field

from dashboard_panel import DashboardPanel
from git_panel import visible
from tui_theme import ACCENT, SECONDARY, SUCCESS, WARNING


class ScanCancelled(Exception):
    """Cancellation never publishes an incomplete replacement snapshot."""


@dataclass
class StorageEntry:
    path: str
    kind: str
    size: int = 0
    children: list = field(default_factory=list)
    note: str = ''


@dataclass
class StorageSnapshot:
    root: str
    entries: dict
    elapsed: float
    visited: int
    files: int
    folders: int
    symlinks: int
    mounts: int
    special: int
    error_count: int
    errors: list
    partial_reasons: list
    scanned_at: float = field(default_factory=time.time)

    @property
    def partial(self):
        return bool(self.partial_reasons or self.error_count)

    @property
    def size(self):
        return self.entries[''].size


def scan_storage(directory, cancel=None, *, max_entries=50000, max_seconds=10.0, max_errors=30):
    """Scan regular-file st_size per path, excluding links and nested mounts.

    Limits are cooperative: a single blocked filesystem syscall cannot be
    interrupted by stdlib threads. Directory descriptors and O_NOFOLLOW prevent
    symlink replacement races. No file contents are opened or read.
    """
    cancel = cancel if cancel is not None else threading.Event()
    root = os.path.abspath(os.path.expanduser(os.fspath(directory)))
    began = time.monotonic()
    nodes = {'': StorageEntry('', 'directory')}
    identities = {}
    reasons, errors = [], []
    visited = files = folders = symlinks = mounts = special = error_count = 0
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd:
        raise OSError('Safe no-follow directory scanning is unavailable on this platform.')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def check():
        if cancel.is_set():
            raise ScanCancelled()
        if time.monotonic() - began >= max_seconds:
            if 'Time limit reached' not in reasons:
                reasons.append('Time limit reached')
            return False
        return True

    def open_path(path, parent=None):
        fd = os.open('/' if parent is None else '.', flags, dir_fd=parent)
        try:
            traversed = ''
            for component in path.split(os.sep):
                if not component:
                    continue
                if not check():
                    raise TimeoutError('Time limit reached')
                child = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                traversed = os.path.join(traversed, component)
                if parent is not None:
                    metadata = os.fstat(fd)
                    if (metadata.st_dev, metadata.st_ino) != identities.get(traversed):
                        raise OSError('Directory changed during scan; excluded')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def record_error(path, exc):
        nonlocal error_count
        error_count += 1
        if len(errors) < max(0, max_errors):
            errors.append(f'{visible(path or ".")}: {visible(exc)}')
        if path in nodes:
            nodes[path].note = 'Unreadable or changed during scan'

    if not check():
        raise TimeoutError('Time limit reached before opening root')
    root_fd = open_path(root)
    try:
        device = os.fstat(root_fd).st_dev
        # Linux bind mounts may share st_dev and escape os.path.ismount detection.
        mount_paths = set()
        if os.path.exists('/proc/self/mountinfo'):
            try:
                with open('/proc/self/mountinfo', encoding='utf-8', errors='replace') as source:
                    for line in source:
                        if not check():
                            break
                        fields = line.split()
                        if len(fields) > 4:
                            path = fields[4]
                            for escaped, literal in (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
                                path = path.replace(escaped, literal)
                            mount_paths.add(path)
            except OSError as exc:
                record_error('', exc)
        pending = ['']
        while pending and check():
            relative = pending.pop()
            fd = None
            try:
                fd = open_path(relative, root_fd)
                if os.fstat(fd).st_dev != device:
                    nodes[relative].note = 'Mount point excluded'
                    mounts += 1
                    continue
                with os.scandir(fd) as iterator:
                    while check():
                        if visited >= max(0, max_entries):
                            # Do not consume an unbounded iterator just to count omissions.
                            try:
                                next(iterator)
                            except StopIteration:
                                break
                            reasons.append('Entry limit reached')
                            pending.clear()
                            break
                        try:
                            entry = next(iterator)
                        except StopIteration:
                            break
                        visited += 1
                        path = os.path.join(relative, entry.name)
                        try:
                            metadata = entry.stat(follow_symlinks=False)
                            mode = metadata.st_mode
                            kind = ('symlink' if stat.S_ISLNK(mode) else 'directory' if stat.S_ISDIR(mode)
                                    else 'file' if stat.S_ISREG(mode) else 'special')
                            node = StorageEntry(path, kind, metadata.st_size if kind == 'file' else 0)
                            nodes[path] = node
                            nodes[relative].children.append(path)
                            if kind == 'file':
                                files += 1
                            elif kind == 'symlink':
                                symlinks += 1
                                node.note = 'Symbolic link excluded; target never followed'
                            elif kind == 'directory':
                                folders += 1
                                identities[path] = (metadata.st_dev, metadata.st_ino)
                                absolute = os.path.join(root, path)
                                if metadata.st_dev != device or absolute in mount_paths or os.path.ismount(absolute):
                                    mounts += 1
                                    node.note = 'Mount point excluded'
                                else:
                                    pending.append(path)
                            else:
                                special += 1
                                node.note = 'Special file excluded'
                        except OSError as exc:
                            record_error(path, exc)
                if reasons:
                    break
            except TimeoutError:
                break
            except OSError as exc:
                if not relative:
                    raise
                record_error(relative, exc)
            finally:
                if fd is not None:
                    os.close(fd)
        if cancel.is_set():
            raise ScanCancelled()
        # Parents are inserted before children, so reverse insertion is postorder.
        for path, node in reversed(list(nodes.items())):
            if cancel.is_set():
                raise ScanCancelled()
            if path:
                nodes[os.path.dirname(path)].size += node.size
        return StorageSnapshot(root, nodes, time.monotonic() - began, visited, files, folders,
                               symlinks, mounts, special, error_count, errors, reasons)
    finally:
        os.close(root_fd)


def format_size(size):
    value = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        if value < 1024 or unit == 'PiB':
            return f'{int(value)} B' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024


class StoragePanel(DashboardPanel):
    tabs = [('overview', 'Folders & files'), ('largest', 'Largest files')]
    list_title = 'Logical sizes'
    output_title = 'Storage details'
    footer = 'Enter open/inspect | Tab actions | r rescan | Ctrl+F find | Esc details/close'

    def __init__(self, directory, auto_refresh=True, *, max_entries=50000, max_seconds=10.0):
        super().__init__(os.path.abspath(os.path.expanduser(os.fspath(directory))))
        self.scan_root = self.directory
        self.snapshot = None
        self.snapshots = {}
        self.current = ''
        self.max_entries, self.max_seconds = max_entries, max_seconds
        self.results = queue.Queue(maxsize=1)
        self.empty_message = 'No scanned entries. Choose a path or scan this folder.'
        if auto_refresh:
            self.start('refresh')

    def heading(self):
        root = self.snapshot.root if self.snapshot else self.scan_root
        return ('STORAGE | ' + os.path.join(root, self.current),
                'Read-only | logical file bytes | cached navigation | ' +
                ('PARTIAL snapshot' if self.snapshot and self.snapshot.partial else 'bounded filesystem scan'))

    def actions(self):
        return [('Open / inspect', 'inspect'), ('Parent', 'parent'), ('Scanned root', 'root'),
                ('Choose path', 'path'), ('Rescan root', 'refresh'), ('Scan details', 'details'), ('Close', 'close')]

    def entry_action(self):
        return 'inspect'

    def entries(self):
        if not self.snapshot:
            return []
        nodes = self.snapshot.entries
        paths = ([path for path, node in nodes.items() if node.kind == 'file'] if self.tab == 'largest'
                 else nodes[self.current].children)
        ordered = sorted(paths, key=lambda path: (-nodes[path].size, path.casefold(), path))
        total = self.snapshot.size if self.tab == 'largest' else nodes[self.current].size
        return [dict(label=f'{format_size(nodes[path].size):>10}  ' +
                           (visible(path) if self.tab == 'largest' else visible(os.path.basename(path))) +
                           ('/' if nodes[path].kind == 'directory' else '') + (' [excluded]' if nodes[path].note else ''),
                     path=path, color=SECONDARY if nodes[path].kind == 'directory' else ACCENT,
                     fraction=nodes[path].size / total if total else 0) for path in ordered]

    def overview(self):
        s = self.snapshot
        if not s:
            return 'No completed scan yet. Choose a path or rescan. Filesystem contents are never modified.'
        return (f'Root: {visible(s.root)}\nStatus: {"PARTIAL" if s.partial else "Complete within scan policy"}\n'
                f'Logical size: {format_size(s.size)} ({s.size:,} bytes)\n'
                f'{s.files:,} regular files | {s.folders:,} folders | {s.visited:,} entries visited\n'
                f'Excluded: {s.symlinks} symlinks, {s.mounts} mount points, {s.special} special files\n'
                f'Errors: {s.error_count} | Scan time: {s.elapsed:.2f}s\n'
                f'Scanned: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.scanned_at))}\n\n'
                'Sizes are logical regular-file lengths (st_size), NOT allocated disk usage or free space. '
                'Hardlinks count once per path. Sparse files, compression, clones and filesystem overhead '
                'make physical storage differ. Directory metadata and symlink sizes are excluded.\n\n'
                'Navigation uses this cached snapshot; no rescan until requested. Files can change during '
                'and after scanning. Symlinks and nested mount points are never traversed. '
                'Limits are cooperative; a blocked filesystem call may delay cancellation.\n'
                + ('\nPartial results: ' + '; '.join(s.partial_reasons) if s.partial_reasons else '')
                + ('\nUnreadable/changed entries make these totals partial.\n' + '\n'.join(s.errors) if s.error_count else ''))

    def start(self, action, value='', entry=None):
        if self.busy:
            return
        if action == 'path':
            path = os.path.expanduser(value.strip())
            self.scan_root = os.path.abspath(os.path.join(self.directory, path))
            if self.scan_root in self.snapshots:
                self.snapshot = self.snapshots[self.scan_root]
                self.current = ''
                self.index = self.top = 0
                self._present()
                self.message = 'Loaded cached snapshot; Rescan root updates it.'
                return
        elif action != 'refresh':
            return
        root = self.scan_root
        self.cancel.clear()
        self.busy = True
        self.message = 'Scanning logical sizes | Ctrl+C cancels; previous snapshot retained'

        def worker():
            try:
                result = scan_storage(root, self.cancel, max_entries=self.max_entries, max_seconds=self.max_seconds)
                self.results.put(('success', result))
            except ScanCancelled:
                self.results.put(('cancelled', None))
            except Exception as exc:
                self.results.put(('failed', visible(exc)))
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def _present(self):
        s = self.snapshot
        if s:
            self.cards = [('This folder', format_size(s.entries[self.current].size), ACCENT),
                          ('Scanned total', format_size(s.size), SECONDARY),
                          ('Coverage', 'PARTIAL' if s.partial else 'Complete*', WARNING if s.partial else SUCCESS),
                          ('Excluded / errors', f'{s.symlinks + s.mounts + s.special} / {s.error_count}', WARNING)]
        self.output = self.overview()
        self.offset = 0

    def poll(self):
        try:
            status, result = self.results.get_nowait()
        except queue.Empty:
            return False
        self.busy = False
        if status == 'success' and not self.cancel.is_set():
            self.snapshot = result
            self.snapshots[result.root] = result
            self.current = ''
            self.index = self.top = 0
            self._present()
            self.message = ('Finished | PARTIAL scan; inspect Scan details' if result.partial else
                            'Finished | snapshot ready; navigation does not rescan')
        else:
            self.message = ('Cancelled' if status == 'cancelled' or self.cancel.is_set() else 'Failed | ' + str(result)) + ' | previous snapshot retained'
        return True

    def activate(self, action):
        if action == 'close':
            if self.busy:
                self.message = 'Scan running. Ctrl+C cancels; close after it finishes.'
                return None
            return ('close', '')
        if action == 'refresh':
            self.start('refresh')
        elif action == 'path' and not self.busy:
            self.form = dict(action='path', fields=[('Directory (relative to session cwd)', self.scan_root)],
                             index=0, value='', entry=None, summary='Choose a read-only scan root; symlink paths are rejected.')
        elif action in ('parent', 'root') and self.snapshot:
            self.current = os.path.dirname(self.current) if action == 'parent' else ''
            self.index = self.top = 0
            self._present()
        elif action == 'details':
            self._present()
            self.output_visible = True
        elif action == 'inspect':
            selected = self.selected()
            if selected and self.snapshot:
                node = self.snapshot.entries[selected['path']]
                if node.kind == 'directory' and not node.note:
                    self.current = node.path
                    self.tab = 'overview'
                    self.index = self.top = 0
                    self._present()
                self.output = (f'Path: {visible(os.path.join(self.snapshot.root, node.path))}\n'
                               f'Type: {node.kind}\nLogical bytes: {node.size:,} ({format_size(node.size)})\n'
                               f'Cached children: {len(node.children)}\n{node.note}\n\n' + self.overview())
                self.offset = 0
                self.output_visible = True
        return None
