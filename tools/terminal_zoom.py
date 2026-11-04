"""Terminal-owned font zoom, with cell measurements independent of window size."""
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


GHOSTTY_SCRIPT = '''on run argv
    tell application "Ghostty"
        repeat with t in terminals
            if tty of t is item 1 of argv then
                if perform action (item 2 of argv) on t then return "ok"
                error "Ghostty rejected the font action."
            end if
        end repeat
        error "Pry's terminal was not found in Ghostty."
    end tell
end run'''


class TerminalZoom:
    def __init__(self):
        self.baseline = None
        self.height = None
        self.next_query = 0
        self.results = queue.Queue()
        self.busy = False
        self.terminal = os.environ.get('TERM_PROGRAM', '')
        self.local = sys.platform == 'darwin' and not any(
            os.environ.get(key) for key in ('SSH_CONNECTION', 'SSH_TTY', 'TMUX', 'STY'))
        try:
            self.tty = os.ttyname(sys.stdin.fileno())
        except (OSError, ValueError):
            self.tty = ''

    @property
    def label(self):
        return f'{round(100 * self.height / self.baseline)}%' if self.baseline and self.height else '--%'

    def observe_cell(self, height, width):
        if not (0 < height <= 4096 and 0 < width <= 4096):
            return False
        previous = self.label
        if self.baseline is None:
            self.baseline = height
        self.height = height
        return previous != self.label

    def measurement_request(self):
        now = time.monotonic()
        if now < self.next_query:
            return ''
        self.next_query = now + 0.5
        return '\033[16t'

    def request(self, action):
        if action not in ('in', 'out', 'reset'):
            return 'Unknown zoom action.'
        if self.busy:
            return 'Terminal zoom is still in progress.'
        if not self.local or self.terminal not in ('ghostty', 'iTerm.app'):
            return 'Use your terminal’s native zoom shortcuts (macOS: Cmd + / Cmd - / Cmd 0).'
        if self.terminal == 'ghostty':
            if not self.tty:
                return 'Cannot identify this terminal. Use Cmd + / Cmd - / Cmd 0.'
            native = {'in': 'increase_font_size:1', 'out': 'decrease_font_size:1',
                      'reset': 'reset_font_size'}[action]
            command = ['osascript', '-e', GHOSTTY_SCRIPT, self.tty, native]
        else:
            session = os.environ.get('ITERM_SESSION_ID', '').split(':')[-1]
            if not session:
                return 'Cannot identify this iTerm2 session. Use Cmd + / Cmd - / Cmd 0.'
            command = [sys.executable, str(Path(__file__).resolve()), '--iterm', session, action]
        self.busy = True
        threading.Thread(target=self._run, args=(command,), daemon=True).start()
        return 'Changing terminal font size…'

    def _run(self, command):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            if result.returncode:
                detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
                message = detail[-1][:240] if detail else 'Terminal rejected zoom.'
                self.results.put(f'Zoom unavailable: {message} Use Cmd + / Cmd - / Cmd 0.')
            else:
                self.results.put('Terminal zoom updated. Cmd + / Cmd - / Cmd 0 also work.')
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.results.put(f'Zoom unavailable: {exc}. Use Cmd + / Cmd - / Cmd 0.')

    def poll(self):
        try:
            message = self.results.get_nowait()
        except queue.Empty:
            return None
        self.busy = False
        self.next_query = 0
        return message


def resized_font(font, delta):
    name, size = font.rsplit(' ', 1)
    return f'{name} {max(1, min(200, float(size) + delta)):g}'


def iterm_main(session_id, action):
    try:
        import iterm2
    except ImportError:
        raise RuntimeError('Install the iterm2 package in Pry’s Python and enable iTerm2’s Python API.') from None

    async def change(connection):
        app = await iterm2.async_get_app(connection)
        session = app.get_session_by_id(session_id)
        if session is None:
            raise RuntimeError('Pry’s iTerm2 session was not found.')
        profile = await session.async_get_profile()
        if action == 'reset':
            defaults = await iterm2.Profile.async_get(connection, [profile.original_guid or profile.guid])
            if not defaults:
                raise RuntimeError('The session’s default profile was not found.')
            normal, non_ascii = defaults[0].normal_font, defaults[0].non_ascii_font
        else:
            delta = 1 if action == 'in' else -1
            normal = resized_font(profile.normal_font, delta)
            non_ascii = resized_font(profile.non_ascii_font, delta) if profile.use_non_ascii_font else None
        await profile.async_set_normal_font(normal)
        if non_ascii:
            await profile.async_set_non_ascii_font(non_ascii)

    iterm2.run_until_complete(change, retry=False)


if __name__ == '__main__':
    try:
        iterm_main(sys.argv[2], sys.argv[3])
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
