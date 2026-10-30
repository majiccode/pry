"""Terminal-native four-channel tracker. Audio dependencies load only on demand."""

from copy import deepcopy
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import wave

import synth_engine as engine
from pry_state import load_state, save_state
from tui_theme import (BASE, TEXT, MUTED, ACCENT, SECONDARY, BORDER, PANEL,
                       SELECTED, BUTTON, FOCUSED_BUTTON, ERROR, SUCCESS, WARNING)


def _safe(value):
    return ''.join(c if 32 <= ord(c) <= 126 else '?' for c in str(value))


class SynthPanel:
    """Edit ``path`` (normally synth.json); draw never touches the shell header.

    A worker owns player lifecycle calls while rendering. close() cancels and
    joins it before stopping the device, even when saving fails. The guarded
    stream-start boundary prevents a late render from opening audio on exit.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.state_path = self.path.parent / 'pry-state.json'
        self.cancel = threading.Event()
        self.thread = None
        self._lock = threading.Lock()
        self._closing = self._closed = False
        self._results = queue.Queue()
        self.player = None
        self.job = ''
        self.dirty = False
        self.message = 'All changes saved | Space play | ? help'
        self.pattern = self.row = self.channel = self.instrument = self.field = 0
        self.octave = 4
        self.focus = 'tracker'
        self.loop = True
        self.muted = set()
        self.solo = None
        self.target = 'volume'
        self.form = None
        self.modal = None
        self.example = 0
        self.help_offset = 0
        self.zones = []
        self.height = 1
        self._last_frame = 0.0
        self._was_playing = False
        self._audio_error = ''
        self._state_dirty = False
        self._state_at = 0.0
        before = self._stamp()
        try:
            self.song = engine.load_song(self.path)
        except FileNotFoundError:
            if before is not None:
                raise ValueError('Synth project changed while opening; reopen it')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.song = {
                'version': 1, 'title': 'Untitled circuit', 'bpm': 120, 'rows': 32,
                'patterns': [[[None] * 4 for _ in range(32)]], 'order': [0],
                'instruments': [engine.default_instrument('Pulse lead')],
            }
            self.stamp = None
            self.dirty = True
            if not self.save():
                raise ValueError(self.message)
        except (ValueError, UnicodeError, RecursionError, TypeError, KeyError) as exc:
            raise ValueError('Invalid synth project: ' + _safe(exc)) from exc
        else:
            self.stamp = self._stamp()
            if before != self.stamp:
                raise ValueError('Synth project changed while opening; reopen it')
        try:
            state = load_state(self.state_path).get('synth', {})
            if isinstance(state, dict):
                for name, limit in (('pattern', len(self.song['patterns'])),
                                    ('row', self.song['rows']), ('channel', 4),
                                    ('instrument', len(self.song['instruments'])),
                                    ('field', len(self._fields())), ('octave', 8)):
                    value = state.get(name)
                    if type(value) is int and 0 <= value < limit:
                        if name != 'octave' or 1 <= value <= 7:
                            setattr(self, name, value)
                if state.get('focus') in ('tracker', 'editor'):
                    self.focus = state['focus']
                if state.get('target') in engine.TARGETS:
                    self.target = state['target']
                if type(state.get('loop')) is bool:
                    self.loop = state['loop']
                muted = state.get('muted', [])
                if isinstance(muted, list):
                    self.muted = {v for v in muted if type(v) is int and 0 <= v < 4}
                if type(state.get('solo')) is int and 0 <= state['solo'] < 4:
                    self.solo = state['solo']
        except (OSError, ValueError) as exc:
            self.message = 'UI state unavailable: ' + _safe(exc)

    def _stamp(self):
        try:
            stat = self.path.stat()
            return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
        except FileNotFoundError:
            return None

    @property
    def busy(self):
        return bool(self.dirty or self.thread is not None and self.thread.is_alive())

    def save(self):
        temporary = None
        try:
            if self.dirty and self._stamp() != self.stamp:
                raise OSError('synth.json changed externally; reopen to reconcile. Memory edits retained')
            if self.dirty:
                engine.validate_song(self.song)
                payload = json.dumps(self.song, ensure_ascii=True, allow_nan=False, indent=2) + '\n'
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                                 dir=self.path.parent, prefix='.synth-',
                                                 suffix='.tmp', delete=False) as handle:
                    temporary = handle.name
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self._stamp() != self.stamp:
                    raise OSError('synth.json changed externally; memory edits retained')
                os.replace(temporary, self.path)
                temporary = None
                self.stamp = self._stamp()
            self.dirty = False
            self.message = 'All changes saved'
            return True
        except (OSError, ValueError) as exc:
            self.dirty = True
            self.message = 'NOT SAVED: ' + _safe(exc) + ' | Ctrl+S retry'
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _save_state(self):
        try:
            state = {name: getattr(self, name) for name in
                     ('pattern', 'row', 'channel', 'instrument', 'field', 'octave',
                      'focus', 'target', 'loop', 'solo')}
            state['muted'] = sorted(self.muted)
            save_state(self.state_path, 'synth', state)
            self._state_dirty = False
            return True
        except (OSError, ValueError) as exc:
            if not self.dirty:
                self.message = 'UI state NOT SAVED: ' + _safe(exc)
            return False

    def _edit(self, change):
        old = deepcopy(self.song)
        try:
            change()
            engine.validate_song(self.song)
        except (ValueError, IndexError) as exc:
            self.song = old
            self.message = 'Not changed: ' + _safe(exc)
            return False
        if self.song != old:
            self.dirty = True
            if self.save() and (self.job or self.player and self.player.playing):
                self.message = 'Saved | Playback uses a snapshot; stop and replay to hear edits'
        return True

    def _start_job(self, kind, overwrite=False):
        if self._closed or self._closing:
            return
        if self.thread is not None and self.thread.is_alive():
            self.message = 'Rendering; Space / Ctrl+C cancels, Esc closes safely'
            return
        self.poll()
        if not self._stop():
            return
        snapshot = deepcopy(self.song)
        instrument = deepcopy(snapshot['instruments'][self.instrument])
        cell = snapshot['patterns'][self.pattern][self.row][self.channel]
        note = cell['note'] if cell and type(cell['note']) is int else (self.octave + 1) * 12
        note = max(24, min(107, note))
        muted = self.muted | ({c for c in range(4) if c != self.solo}
                              if self.solo is not None else set())
        loop = self.loop
        destination = self.path.with_suffix('.wav')
        export_stamp = None
        if kind == 'export':
            try:
                if destination.exists():
                    if not overwrite:
                        self.modal = 'export'
                        return
                    s = destination.stat()
                    export_stamp = (s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            except OSError as exc:
                self.message = 'Export unavailable: ' + _safe(exc)
                return
        self.cancel.clear()
        self.job = kind
        self.message = 'Rendering ' + kind + ' snapshot... | Space / Ctrl+C cancel'
        panel = self

        class GuardedPlayer(engine.AudioPlayer):
            def _start(self, pcm, loop_start):
                with panel._lock:
                    if panel.cancel.is_set() or panel._closing or panel._closed:
                        return
                    super()._start(pcm, loop_start)

        if self.player is None:
            self.player = GuardedPlayer()

        def worker():
            temporary = None
            try:
                if kind == 'export':
                    pcm = engine.render_song(snapshot, muted=muted)
                    if not self.cancel.is_set():
                        # numpy is already loaded by render_song, never by opening the UI.
                        import numpy as np
                        with tempfile.NamedTemporaryFile(dir=destination.parent,
                                                         prefix='.synth-wav-', delete=False) as handle:
                            temporary = handle.name
                            with wave.open(handle, 'wb') as output:
                                output.setnchannels(1)
                                output.setsampwidth(2)
                                output.setframerate(22050)
                                output.writeframes((np.clip(pcm, -1, 1) * 32767).astype('<i2').tobytes())
                            handle.flush()
                            os.fsync(handle.fileno())
                        with self._lock:
                            if not self.cancel.is_set() and not self._closing:
                                try:
                                    stat = destination.stat()
                                    current = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                                except FileNotFoundError:
                                    current = None
                                if current != export_stamp:
                                    raise OSError('WAV changed externally; export again to confirm replacement')
                                os.replace(temporary, destination)
                                temporary = None
                elif kind == 'audition':
                    self.player.audition(instrument, note)
                else:
                    self.player.play(snapshot, loop=loop, muted=muted)
                if self.cancel.is_set():
                    self.player.stop()
                self._results.put(('done', 'Canceled' if self.cancel.is_set() else
                                   'Exported ' + str(destination) if kind == 'export' else
                                   'Playing snapshot | Space stop; replay after edits'))
            except Exception as exc:
                self._results.put(('error', _safe(exc)))
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass

        self.thread = threading.Thread(target=worker, name='synth-' + kind, daemon=True)
        self.thread.start()

    def _stop(self):
        if self.thread is not None and self.thread.is_alive():
            self.cancel.set()
            self.message = 'Cancel requested; waiting for render to finish (no audio will start)'
            return False
        if self.player is not None:
            try:
                self.player.stop()
            except Exception as exc:
                self.message = 'Audio stop failed: ' + _safe(exc)
                return False
        return True

    def poll(self):
        changed = False
        while True:
            try:
                kind, message = self._results.get_nowait()
            except queue.Empty:
                break
            self.job = ''
            if not self.dirty:
                self.message = ('Audio/export failed: ' if kind == 'error' else '') + _safe(message)
            changed = True
        playing = bool(self.player and self.player.playing)
        if self.cancel.is_set() and playing and not (self.thread and self.thread.is_alive()):
            self._stop()
            playing = False
            changed = True
        if playing != self._was_playing:
            self._was_playing = playing
            changed = True
        if self.player and self.player.error != self._audio_error:
            self._audio_error = self.player.error
            if self._audio_error and not self.dirty:
                self.message = _safe(self._audio_error)
            changed = True
        now = time.monotonic()
        if playing and now - self._last_frame >= 0.06:
            self._last_frame = now
            changed = True
        if self._state_dirty and now - self._state_at >= 0.5:
            self._state_at = now
            self._save_state()
        return changed

    def close(self):
        if self._closed:
            return True
        with self._lock:
            self._closing = True
            self.cancel.set()
        if self.thread is not None:
            self.thread.join()
        stopped = self._stop()
        stop_message = self.message
        saved = self.save()
        state_saved = self._save_state()
        if not stopped and saved and state_saved:
            self.message = stop_message
        self._closed = bool(stopped and saved)
        self._closing = False
        if not saved and self._stamp() != self.stamp:
            self.modal = 'recover'
        return self._closed

    def _fields(self):
        inst = self.song['instruments'][self.instrument]
        env = inst['envelopes'][self.target]
        return [
            ('Instrument', f'{self.instrument + 1:02d} / {len(self.song["instruments"]):02d}'),
            ('Name', inst['name']), ('Waveform', inst['wave']),
            ('Volume', f'{inst["volume"]:.2f}'), ('Tune (semitones)', f'{inst["tune"]:+.1f}'),
            ('Pulse width', f'{inst["pulse"]:.2f}'), ('Cutoff (Hz)', f'{inst["cutoff"]:.0f}'),
            ('Envelope target', self.target), ('Attack (s)', f'{env["attack"]:.3f}'),
            ('Decay (s)', f'{env["decay"]:.3f}'), ('Sustain', f'{env["sustain"]:.2f}'),
            ('Release (s)', f'{env["release"]:.3f}'), ('Depth', f'{env["depth"]:+.3f}'),
            ('Tempo (BPM)', f'{self.song["bpm"]:g}'), ('Pattern', f'{self.pattern + 1:02d}'),
            ('Pattern length', str(self.song['rows'])),
            ('Song title', self.song['title']),
            ('Pattern order', ' '.join(str(i + 1) for i in self.song['order'])),
        ]

    def _adjust(self, delta):
        field = self.field
        inst = self.song['instruments'][self.instrument]
        if field == 0:
            self.instrument = (self.instrument + delta) % len(self.song['instruments'])
        elif field == 7:
            self.target = engine.TARGETS[(engine.TARGETS.index(self.target) + delta) % 4]
        elif field == 14:
            self.pattern = (self.pattern + delta) % len(self.song['patterns'])
        elif field in (1, 16, 17):
            self._open_field()
        elif field == 15:
            length = (16, 32, 64)[max(0, min(2, (16, 32, 64).index(self.song['rows']) + delta))]
            if length < self.song['rows'] and any(
                    any(cell is not None for cell in row)
                    for pattern in self.song['patterns'] for row in pattern[length:]):
                self.modal = ('resize', length)
            else:
                self._resize(length)
        else:
            def change():
                if field == 2:
                    inst['wave'] = engine.WAVES[(engine.WAVES.index(inst['wave']) + delta) % len(engine.WAVES)]
                elif field == 13:
                    self.song['bpm'] = max(40, min(300, self.song['bpm'] + delta))
                else:
                    if field in (3, 4, 5, 6):
                        key, step, low, high = {3: ('volume', .05, 0, 1), 4: ('tune', 1, -24, 24),
                                               5: ('pulse', .05, .05, .95),
                                               6: ('cutoff', 100, 100, 18000)}[field]
                        dest = inst
                    else:
                        dest = inst['envelopes'][self.target]
                        key = {8: 'attack', 9: 'decay', 10: 'sustain', 11: 'release', 12: 'depth'}[field]
                        step, low, high = (.01, 0, 10) if key != 'sustain' else (.05, 0, 1)
                        if key == 'depth':
                            step, low, high = {'volume': (.05, 0, 1), 'pitch': (1, -48, 48),
                                               'cutoff': (100, -18000, 18000),
                                               'pulse': (.05, -.9, .9)}[self.target]
                    dest[key] = round(max(low, min(high, dest[key] + delta * step)), 5)
            self._edit(change)

    def _resize(self, length):
        def change():
            for pattern in self.song['patterns']:
                pattern[:] = (pattern + [[None] * 4 for _ in range(max(0, length - len(pattern)))])[:length]
            self.song['rows'] = length
        if self._edit(change):
            self.row = min(self.row, length - 1)

    def _open_field(self):
        if self.field == 1:
            self.form = {'kind': 'name', 'text': self.song['instruments'][self.instrument]['name'],
                         'label': 'Instrument name (1-80 ASCII characters)'}
        elif self.field == 16:
            self.form = {'kind': 'title', 'text': self.song['title'], 'label': 'Song title (1-80 ASCII characters)'}
        elif self.field == 17:
            self.form = {'kind': 'order', 'text': ' '.join(str(i + 1) for i in self.song['order']),
                         'label': f'Order: space-separated pattern numbers 1..{len(self.song["patterns"])} (max 64)'}
        else:
            self._start_job('audition')
        if self.form:
            self.form['replace'] = True

    def _submit(self):
        form = self.form
        def change():
            if form['kind'] == 'order':
                try:
                    self.song['order'] = [int(n) - 1 for n in form['text'].split()]
                except ValueError:
                    raise ValueError('Use space-separated pattern numbers') from None
            elif form['kind'] == 'title':
                self.song['title'] = form['text']
            else:
                self.song['instruments'][self.instrument]['name'] = form['text']
        if self._edit(change):
            self.form = None

    def _load_example(self):
        if self.thread and self.thread.is_alive():
            self.message = 'Cancel rendering and wait before replacing the project'
            return
        if not self.save() or not self._stop():
            return
        # A unique backup protects even a previously confirmed replacement.
        backup = self.path.with_name(self.path.stem + '.backup-' + str(time.time_ns()) + '.json')
        try:
            engine.save_song(backup, self.song)
        except (OSError, ValueError) as exc:
            self.message = 'Replacement blocked: backup failed: ' + _safe(exc)
            return
        self.song = engine.example_song(self.example)
        self.pattern = self.row = self.channel = self.instrument = 0
        self.dirty = True
        self.modal = None
        if self.save():
            self.message = 'Example loaded; previous project backed up to ' + _safe(backup.name)

    def _action(self, action):
        if action == 'close':
            if self.close():
                return ('close', '')
        elif action == 'play':
            if self.job or self.player and self.player.playing:
                if self._stop():
                    self.message = 'Stopped'
            else:
                self._start_job('play')
        elif action == 'save':
            self.save()
        elif action == 'examples':
            if self.thread and self.thread.is_alive():
                self.message = 'Cancel rendering and wait before switching projects'
            elif self.save():
                self.modal = 'examples'
        elif action == 'help':
            self.modal = 'help'
        elif action == 'export':
            self._start_job('export')
        elif action == 'audition':
            self._start_job('audition')
        elif action in ('new_instrument', 'copy_instrument'):
            index = len(self.song['instruments'])
            inst = engine.default_instrument(f'Instrument {index + 1:02d}') if action == 'new_instrument' else deepcopy(self.song['instruments'][self.instrument])
            if action == 'copy_instrument':
                inst['name'] = inst['name'][:75] + ' copy'
            if self._edit(lambda: self.song['instruments'].append(inst)):
                self.instrument = index
                self.focus, self.field = 'editor', 1
                self._open_field()
        elif action in ('add_pattern', 'duplicate_pattern'):
            pattern = [[None] * 4 for _ in range(self.song['rows'])] if action == 'add_pattern' else deepcopy(self.song['patterns'][self.pattern])
            if self._edit(lambda: self.song['patterns'].append(pattern)):
                self.pattern = len(self.song['patterns']) - 1
                if not self.dirty:
                    self.message = 'Pattern added | Edit Order to include it in playback'
        elif action == 'loop':
            self.loop = not self.loop
            self.message = 'Loop ' + ('on' if self.loop else 'off') + ' | Replay to apply'
        elif action == 'mute':
            self.muted.symmetric_difference_update({self.channel})
            self.message = 'Channel mute changed | Replay to apply'
        elif action == 'solo':
            self.solo = None if self.solo == self.channel else self.channel
            self.message = 'Channel solo changed | Replay to apply'
        elif action in ('tracker', 'editor'):
            self.focus = action
        elif action == 'order':
            self.focus, self.field = 'editor', 17
            self._open_field()
        elif action == 'title':
            self.focus, self.field = 'editor', 16
            self._open_field()
        return None

    def event(self, event, value):
        if self._closed:
            return None
        self.poll()
        self._state_dirty = True
        if event == 'eof':
            return self._action('close')
        if self.form:
            if event == 'escape':
                self.form = None
            elif event == 'key' and value in ('\r', '\n'):
                self._submit()
            elif event == 'key' and value in ('\x7f', '\x08') or event == 'delete':
                self.form['text'] = self.form['text'][:-1]
                self.form['replace'] = False
            elif event in ('key', 'paste') and isinstance(value, str):
                text = ''.join(c for c in value if 32 <= ord(c) <= 126)
                if text:
                    self.form['text'] = ('' if self.form['replace'] else self.form['text']) + text
                    self.form['text'] = self.form['text'][:320 if self.form['kind'] == 'order' else 80]
                    self.form['replace'] = False
            elif event == 'click':
                for y, left, right, action, arg in self.zones:
                    if y == value[1] and left <= value[0] <= right:
                        if action == 'submit':
                            self._submit()
                        elif action == 'dismiss':
                            self.form = None
                        break
            return None
        if self.modal is not None:
            return self._modal_event(event, value)
        if event == 'escape':
            return self._action('close')
        if event == 'pane' or event == 'key' and value == '\t':
            self.focus = 'editor' if self.focus == 'tracker' else 'tracker'
        elif event == 'key' and value in {
                ' ', '\x13', '\x05', '\x0e', '\x10', '\x0c', '\x15', '\x0f', '\x17', '?', '\x03'}:
            if value == '\x03':
                self.cancel.set()
                self._stop()
            else:
                return self._action({' ': 'play', '\x13': 'save', '\x05': 'examples',
                                     '\x0e': 'new_instrument', '\x10': 'duplicate_pattern',
                                     '\x0c': 'loop', '\x15': 'mute', '\x0f': 'solo',
                                     '\x17': 'export', '?': 'help'}[value])
        elif event == 'click':
            x, y = value
            for zy, left, right, action, arg in reversed(self.zones):
                if zy == y and left <= x <= right:
                    if action == 'cell':
                        self.focus = 'tracker'
                        self.row, self.channel = arg
                    elif action == 'field':
                        self.focus, self.field = 'editor', arg
                    elif action == 'adjust':
                        self.focus, self.field = 'editor', arg[0]
                        self._adjust(arg[1])
                    else:
                        return self._action(action)
                    break
        elif event == 'wheel':
            delta, x, y = value
            for zy, left, right, action, arg in self.zones:
                if zy == y and left <= x <= right and action in ('cell', 'field'):
                    self.focus = 'tracker' if action == 'cell' else 'editor'
                    break
            self._move(delta)
        elif event in ('scroll', 'page'):
            self._move(value * (max(1, self.height - 1) if event == 'page' else 1))
        elif event in ('cursor', 'horizontal'):
            if self.focus == 'tracker':
                self.channel = max(0, min(3, self.channel + value))
            else:
                self._adjust(value)
        elif event in ('home', 'end'):
            if self.focus == 'tracker':
                self.row = 0 if event == 'home' else self.song['rows'] - 1
            else:
                self.field = 0 if event == 'home' else len(self._fields()) - 1
        elif event == 'key' and value in ('\r', '\n'):
            if self.focus == 'editor':
                self._open_field()
            else:
                self._start_job('audition')
        elif self.focus == 'tracker':
            if event == 'key' and value in ('[', ']'):
                self.octave = max(1, min(7, self.octave + (1 if value == ']' else -1)))
            elif event == 'key' and value in ('+', '-', '='):
                self.instrument = (self.instrument + (-1 if value == '-' else 1)) % len(self.song['instruments'])
            else:
                notes = 'zsxdcvgbhnjm'
                cell = None
                if event == 'key' and value in notes and len(value) == 1:
                    note = (self.octave + 1) * 12 + notes.index(value)
                    if not 24 <= note <= 107:
                        self.message = 'Note outside engine range C1..B7; change octave with [ ]'
                        return None
                    cell = {'note': note, 'instrument': self.instrument}
                elif event == 'key' and value == '.':
                    cell = {'note': 'off', 'instrument': self.instrument}
                elif not (event == 'delete' or event == 'key' and value in ('\x08', '\x7f')):
                    return None
                if self._edit(lambda: self.song['patterns'][self.pattern][self.row].__setitem__(self.channel, cell)):
                    self.row = (self.row + 1) % self.song['rows']
        return None

    def _move(self, delta):
        if self.focus == 'tracker':
            self.row = max(0, min(self.song['rows'] - 1, self.row + delta))
        else:
            self.field = max(0, min(len(self._fields()) - 1, self.field + delta))

    def _modal_event(self, event, value):
        if event == 'escape':
            self.modal = None
            return None
        if event == 'click':
            for y, left, right, action, arg in self.zones:
                if y == value[1] and left <= value[0] <= right:
                    if action == 'dismiss':
                        self.modal = None
                    elif action == 'example':
                        self.example = arg
                        self.modal = 'confirm_example'
                    elif action == 'confirm':
                        return self._modal_event('key', '\r')
                    break
            return None
        if self.modal == 'help':
            if event in ('scroll', 'page', 'wheel'):
                delta = value[0] if event == 'wheel' else value
                self.help_offset = max(0, self.help_offset + delta * (5 if event == 'page' else 1))
            elif event == 'key' and value in ('?', '\r', '\n'):
                self.modal = None
        elif self.modal == 'examples':
            if event in ('scroll', 'cursor', 'page'):
                self.example = (self.example + value) % 3
            elif event in ('home', 'end'):
                self.example = 0 if event == 'home' else 2
            elif event == 'key' and value in ('\r', '\n'):
                self.modal = 'confirm_example'
        elif event == 'key' and value in ('\r', '\n'):
            if self.modal == 'confirm_example':
                self._load_example()
            elif self.modal == 'export':
                self.modal = None
                self._start_job('export', overwrite=True)
            elif self.modal == 'recover':
                recovery = self.path.with_name(self.path.stem + '.recovered-' + str(time.time_ns()) + '.json')
                try:
                    engine.save_song(recovery, self.song)
                except (OSError, ValueError) as exc:
                    self.message = 'Recovery failed: ' + _safe(exc)
                    return None
                self.dirty = False
                self.modal = None
                return self._action('close')
            elif isinstance(self.modal, tuple) and self.modal[0] == 'resize':
                length = self.modal[1]
                self.modal = None
                self._resize(length)
        return None

    @staticmethod
    def _help():
        return [
            'CIRCUIT / FOUR-CHANNEL SYNTH - HELP',
            'Space play / stop. Esc save, stop and close; Esc in a dialog cancels it.',
            'Tab / Shift+Tab switches tracker and editor (also on narrow screens).',
            'Tracker: Up/Down row; Left/Right channel; Page scroll; Home/End first/last.',
            'Notes: z s x d c v g b h n j m = C C# D D# E F F# G G# A A# B.',
            '[ / ] change octave; default C4. + / - choose instrument.',
            '. enters OFF (release). Delete / Backspace clears. Entry advances one row.',
            'Empty cells sustain the previous note. Each row is a sixteenth note.',
            'Enter auditions the selected note/instrument without entering a note.',
            'Editor: Up/Down select; Left/Right adjust; Enter audition or edit text.',
            'Click fields to select; click their < / > controls to adjust.',
            'All waveform/base parameters and ADSR for volume/pitch/cutoff/pulse are editable.',
            'ADSR times are seconds; pitch depth semitones, cutoff Hz, pulse fraction.',
            'Volume depth 0 disables its envelope; depth 1 uses full ADSR.',
            'Name / Title / Order: Enter opens form; typing replaces selection; Enter applies.',
            'Order uses 1-based pattern numbers, e.g. 1 2 1 2. Max 64 entries.',
            'Add / Duplicate creates a pattern; add its number to Order to play it.',
            'Length changes ALL patterns: 16 / 32 / 64; truncation requires confirmation.',
            'New / Copy creates an instrument, then opens its name. Max 32 instruments.',
            'Ctrl+S save/retry; Ctrl+E examples; Ctrl+P duplicate pattern; Ctrl+L loop.',
            'Ctrl+O solo selected channel; Ctrl+W export to the project basename .wav.',
            'Ctrl+I / Ctrl+M are Tab / Enter in this shell, so use buttons instead.',
            'Ctrl+N = New instrument; Ctrl+U = Mute selected channel (unambiguous aliases).',
            'Loop / mute / solo affect the NEXT playback/export snapshot, not live audio.',
            'Edits autosave atomically. External changes block overwriting; close offers recovery.',
            'Examples REPLACE synth.json only after confirmation and a unique JSON backup.',
            'Save failures retain memory edits and block replacement / closing.',
            'Audio / WAV render in a worker. Ctrl+C / Space cancels; render must finish first.',
            'Close waits for rendering; canceled renders never start a late audio stream.',
            'Wave / FFT show actual device output, not a decorative animation.',
            'Audio: pip install numpy sounddevice; macOS may need brew install portaudio.',
            'WAV needs numpy only: mono 22050 Hz, 16-bit PCM, includes release tail.',
            'Existing WAV requires explicit confirmation. Export respects mute / solo.',
            'Songs including release are limited to 120 seconds. ? / Enter / Esc closes help.',
        ]

    def draw(self, cols, rows, wrap, styles, top=1):
        self.zones = []
        if cols <= 0 or rows <= top:
            return ''
        output = ['\033[?25l']

        def put(y, x, text, width, style=TEXT, action=None, arg=None):
            if not top < y <= rows or not 1 <= x <= cols or width <= 0:
                return
            width = min(width, cols - x + 1)
            text = _safe(text)[:width]
            output.append(f'\033[{y};{x}H' + BASE + style + text.ljust(width) + BASE)
            if action:
                self.zones.append((y, x, x + width - 1, action, arg))

        for y in range(top + 1, rows + 1):
            put(y, 1, '', cols)
        if self.form or self.modal is not None:
            self._draw_modal(cols, rows, top, put)
            return ''.join(output)
        playing = bool(self.player and self.player.playing)
        state = 'RENDER' if self.job else 'PLAY' if playing else 'STOP'
        put(top + 1, 1, f' CIRCUIT / {state}  {self.song["title"]}', cols, PANEL + ACCENT, 'title')
        put(top + 2, 1, f' {self.song["bpm"]:g} BPM | P{self.pattern + 1:02d}/{len(self.song["patterns"]):02d} | '
            f'{self.song["rows"]} rows | Oct {self.octave} | I{self.instrument + 1:02d} '
            + self.song['instruments'][self.instrument]['name'], cols, SECONDARY)
        actions = [('Play', 'play'), ('Save', 'save'), ('Examples', 'examples'),
                   ('Help ?', 'help'), ('WAV', 'export'), ('Close', 'close')]
        x = 1
        for label, action in actions:
            label = '[' + label + ']'
            if x + len(label) - 1 > cols:
                break
            put(top + 3, x, label, len(label), BUTTON, action)
            x += len(label) + 1
        actions = [('Tracker', 'tracker'), ('Editor', 'editor'), ('New I', 'new_instrument'),
                   ('Copy I', 'copy_instrument'), ('Add P', 'add_pattern'),
                   ('Dup P', 'duplicate_pattern'), ('Order', 'order'),
                   ('Loop ' + ('on' if self.loop else 'off'), 'loop'),
                   ('Mute', 'mute'), ('Solo', 'solo')]
        # Wrap controls rather than hide actions on ordinary narrow terminals.
        y, x = top + 4, 1
        for label, action in actions:
            label = '[' + label + ']'
            if x > 1 and x + len(label) - 1 > cols:
                y, x = y + 1, 1
            if y >= rows - 3:
                break
            put(y, x, label, min(len(label), cols), FOCUSED_BUTTON if action == self.focus else BUTTON, action)
            x += len(label) + 1
        start = y + 1
        end = rows - 4
        self.height = max(1, end - start)
        wide = cols >= 100
        left = min(58, cols // 2) if wide else cols
        editor_x = left + 2 if wide else 1
        editor_width = cols - editor_x + 1
        if wide:
            for yy in range(start, end + 1):
                put(yy, left + 1, '|', 1, BORDER)
        if wide or self.focus == 'tracker':
            put(start, 1, ('> ' if self.focus == 'tracker' else '  ') + 'PATTERN / NOTE:INSTRUMENT', left, PANEL + ACCENT)
            capacity = max(0, end - start - 1)
            visible_channels = 4 if left >= 39 else max(1, min(4, (left - 3) // 9))
            first_channel = max(0, min(self.channel, self.channel - visible_channels + 1))
            channel_width = max(1, (left - 3) // visible_channels)
            for offset in range(visible_channels):
                channel = first_channel + offset
                label = f'CH{channel + 1}' + (' S' if self.solo == channel else ' M' if channel in self.muted else '')
                put(start + 1, 4 + offset * channel_width, label, channel_width, SECONDARY)
            row_start = max(0, self.row - capacity // 2)
            row_start = min(row_start, max(0, self.song['rows'] - capacity))
            position = self.player.position if playing and self.player else None
            for offset in range(capacity):
                index = row_start + offset
                if index >= self.song['rows']:
                    break
                yy = start + 2 + offset
                current = bool(position and self.song['order'][min(position[0], len(self.song['order']) - 1)] == self.pattern and position[1] == index)
                put(yy, 1, f'{index:02X}' + ('>' if current else ' '), min(3, left), SUCCESS if current else ACCENT if index % 4 == 0 else MUTED)
                for offset_ch in range(visible_channels):
                    channel = first_channel + offset_ch
                    cell = self.song['patterns'][self.pattern][index][channel]
                    text = f'{engine.note_name(cell["note"])}:{cell["instrument"] + 1:02d}' if cell else '...:..'
                    style = SELECTED if index == self.row and channel == self.channel else TEXT if cell else MUTED
                    put(yy, 4 + offset_ch * channel_width, text, channel_width, style, 'cell', (index, channel))
        if wide or self.focus == 'editor':
            put(start, editor_x, ('> ' if self.focus == 'editor' else '  ') + 'VOICE / ENVELOPE / SONG', editor_width, PANEL + ACCENT)
            fields = self._fields()
            count = max(0, end - start)
            first = min(max(0, self.field - count + 1), max(0, len(fields) - count))
            for offset, (label, value) in enumerate(fields[first:first + count]):
                index = first + offset
                yy = start + 1 + offset
                style = SELECTED if index == self.field else TEXT
                text = f'{label:<19} {value}' if editor_width >= 34 else f'{label}: {value}'
                put(yy, editor_x, text, editor_width, style, 'field', index)
                if editor_width >= 8:
                    put(yy, cols - 3, '<', 1, ACCENT, 'adjust', (index, -1))
                    put(yy, cols - 1, '>', 1, ACCENT, 'adjust', (index, 1))
        # A compact oscilloscope and real FFT stay visible in either view.
        if rows - top >= 10:
            width = max(1, min(100, cols - 7))
            wave_line = '-' * width
            fft_line = '.' * width
            if playing and self.player and not (self.thread and self.thread.is_alive()):
                try:
                    levels = ' .:-=+*#%@'
                    wave_line = ''.join('_' if v < -.08 else '^' if v > .08 else '-' for v in self.player.waveform(width))
                    fft_line = ''.join(levels[min(9, max(0, int(v * 9)))] for v in self.player.spectrum(width))
                except Exception as exc:
                    if not self.dirty:
                        self.message = 'Visualization unavailable: ' + _safe(exc)
            put(rows - 2, 1, 'WAVE ' + wave_line, cols, SECONDARY)
            put(rows - 1, 1, 'FFT  ' + fft_line, cols, ACCENT)
        # On tiny screens prioritize actionable instructions over decorative content.
        put(rows, 1, self.message if self.dirty or rows - top >= 10 else 'Space play | Tab view | ? help | Esc close', cols,
            ERROR if self.dirty else WARNING if self.job else MUTED)
        if rows - top >= 10:
            put(rows - 3, 1, 'Space play | Tab view | arrows edit | Enter hear | Ctrl+S save | ? help | Esc close', cols, MUTED)
        return ''.join(output)

    def _draw_modal(self, cols, rows, top, put):
        if self.form:
            put(top + 1, 1, self.form['label'], cols, PANEL + ACCENT)
            put(top + 2, 1, self.form['text'][-cols:], cols, SELECTED)
            put(top + 3, 1, 'Typing replaces text; Backspace edits; Enter apply; Esc cancel', cols, MUTED)
            put(rows, 1, self.message, cols, ERROR if self.dirty else MUTED)
            put(top + 4, 1, '[Apply]', min(7, cols), BUTTON, 'submit')
            put(top + 5, 1, '[Cancel]', min(8, cols), BUTTON, 'dismiss')
            return
        if self.modal == 'help':
            put(top + 1, 1, 'HELP | Up/Down / Page / wheel | Enter or Esc returns', cols, PANEL + ACCENT)
            import textwrap
            lines = [part for line in self._help() for part in textwrap.wrap(line, max(1, cols)) or ['']]
            capacity = max(0, rows - top - 2)
            self.help_offset = min(self.help_offset, max(0, len(lines) - capacity))
            for offset, line in enumerate(lines[self.help_offset:self.help_offset + capacity]):
                put(top + 2 + offset, 1, line, cols, TEXT)
            put(rows, 1, '[Back]  ? / Enter / Esc', cols, BUTTON, 'dismiss')
        elif self.modal == 'examples':
            put(top + 1, 1, 'EXAMPLES | arrows browse | Enter choose | Esc cancel', cols, PANEL + ACCENT)
            for index, name in enumerate(engine.EXAMPLE_NAMES):
                put(top + 2 + index, 1, f'{index + 1}. {name}', cols, SELECTED if index == self.example else TEXT, 'example', index)
            put(top + 5, 1, 'Next: confirm replacement of this project. A JSON backup is kept.', cols, WARNING)
            put(rows, 1, '[Cancel]', cols, BUTTON, 'dismiss')
        else:
            if self.modal == 'confirm_example':
                heading = 'REPLACE PROJECT with ' + engine.EXAMPLE_NAMES[self.example] + '?'
                detail = 'Replaces synth.json. Current work is saved to a unique .backup-*.json first.'
            elif self.modal == 'export':
                heading = 'REPLACE EXISTING WAV?'
                detail = 'Export overwrites ' + _safe(self.path.with_suffix('.wav').name) + ' with the current snapshot.'
            elif self.modal == 'recover':
                heading = 'PROJECT CHANGED EXTERNALLY'
                detail = 'Confirm to save memory edits as synth.recovered-*.json and close. The external project stays untouched.'
            else:
                heading = 'TRUNCATE ALL PATTERNS?'
                detail = f'Rows beyond {self.modal[1]} will be removed. This cannot be undone.'
            put(top + 1, 1, heading, cols, PANEL + WARNING)
            import textwrap
            detail_lines = textwrap.wrap(detail, max(1, cols))
            for index, line in enumerate(detail_lines):
                put(top + 2 + index, 1, line, cols, TEXT)
            put(top + 3 + len(detail_lines), 1, '[Confirm: Enter]', min(16, cols), BUTTON, 'confirm')
            put(top + 4 + len(detail_lines), 1, '[Cancel: Esc]', min(13, cols), BUTTON, 'dismiss')
            put(rows, 1, self.message, cols, ERROR if self.dirty else MUTED)
