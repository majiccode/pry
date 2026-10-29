"""Streaming process jobs and foreground terminal handoff."""
import codecs
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

MAX_OUTPUT = 48 * 1024


def shell_script(command, state):
    import shlex
    # EXIT also runs after an explicit exit. exec cannot report its final cwd.
    return "trap 'pwd -P > " + shlex.quote(state) + "' EXIT\n" + command


def final_cwd(state, fallback):
    try:
        path = Path(state).read_text().rstrip('\n')
        return path if Path(path).is_dir() else fallback
    except OSError:
        return fallback


class CommandJob:
    def __init__(self, command, cwd, shell, callback, timeout: float =120, workdir=None):
        self.command, self.cwd, self.shell = command, cwd, shell
        self.callback, self.timeout = callback, timeout
        self.workdir = Path(workdir or '~/.ai-tool').expanduser()
        self.process = None
        self.stopped = threading.Event()

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def cancel(self):
        self.stopped.set()

    def run(self):
        output, reason = '', ''
        started = time.monotonic()
        try:
            logdir = self.workdir / 'output'
            logdir.mkdir(parents=True, exist_ok=True)
            log = logdir / (uuid4().hex + '.log')
            with tempfile.TemporaryDirectory(prefix='ai-tool-') as temporary:
                state = str(Path(temporary) / 'cwd')
                self.process = subprocess.Popen([self.shell, '-c', shell_script(self.command, state)],
                    cwd=self.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, start_new_session=True)
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                assert self.process.stdout is not None
                pipe = self.process.stdout
                killed_at = None
                last_update = 0
                with log.open('wb') as handle, selectors.DefaultSelector() as selector:
                    os.chmod(log, 0o600)
                    selector.register(pipe, selectors.EVENT_READ)
                    while selector.get_map() or self.process.poll() is None:
                        now = time.monotonic()
                        if killed_at is None and (self.stopped.is_set() or self.timeout and now - started > self.timeout):
                            reason = 'Cancelled' if self.stopped.is_set() else f'Timed out after {self.timeout}s'
                            self._signal(signal.SIGTERM)
                            killed_at = now
                        if killed_at is not None and now - killed_at > 1:
                            self._signal(signal.SIGKILL)
                        if not selector.get_map():
                            time.sleep(0.1)
                        for key, _ in selector.select(0.1) if selector.get_map() else []:
                            data = os.read(key.fd, 8192)
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            handle.write(data)
                            output = (output + decoder.decode(data))[-MAX_OUTPUT:]
                        if now - last_update > 0.1:
                            self.callback('partial', output)
                            last_update = now
                    output = (output + decoder.decode(b'', final=True))[-MAX_OUTPUT:]
                code = self.process.wait()
                pipe.close()
                directory = final_cwd(state, self.cwd)
            text = output + f'\nExit status: {code}' + (f'\n{reason}' if reason else '')
            text += f'\nFull output: {log}'
            self.callback('done', dict(text=text, stdout=output, cwd=directory,
                                      exit_code=code, log_path=str(log), duration=time.monotonic() - started))
        except Exception as exc:
            if self.process is not None:
                self._signal(signal.SIGKILL)
                self.process.wait()
            self.callback('error', str(exc))

    def _signal(self, sig):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass


def handoff(command, cwd, shell):
    with tempfile.TemporaryDirectory(prefix='ai-tool-') as temporary:
        state = str(Path(temporary) / 'cwd')
        process = subprocess.Popen([shell, '-c', shell_script(command, state)], cwd=cwd)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            # The foreground process receives the terminal's SIGINT too.
            try:
                code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                code = process.wait()
        return dict(text=f'Interactive command returned. Exit status: {code}\n'
                         '(Interactive screen contents were not captured.)',
                    stdout='', exit_code=code, cwd=final_cwd(state, cwd), interactive=None)
