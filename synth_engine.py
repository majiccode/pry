"""Small four-channel, mono chiptune engine with optional audio dependencies.

Patterns are lists of rows, not objects; a row has exactly four cells. Each row
lasts one sixteenth note. Song duration, including the final amplitude release,
is capped at 120 seconds. PCM is float32 at 22050 or 44100 Hz. Playback renders
before opening the device; callbacks only copy PCM and a recent-output buffer.
Looping stores two cycles (startup and steady state), at most 42.4 MB at 44100
Hz. Rendering uses bounded-size vector operations, not a per-sample Python loop.

Channels are monophonic: rests sustain, OFF releases, and a new note steals the
old voice with a 5 ms fade. Cutoff is a one-pole low-pass updated every 256
samples. No numpy or sounddevice import occurs until an audio operation.
"""

import bisect
import copy
import importlib
import json
import math
import os
import tempfile


WAVES = ("sine", "triangle", "saw", "square", "noise")
TARGETS = ("volume", "pitch", "cutoff", "pulse")
EXAMPLE_NAMES = ("Copper Comet", "Pocket Observatory", "Rain on the Arcade")
MAX_DURATION_SECONDS = 120
_ATTACK_FLOOR = 0.003
_RELEASE_FLOOR = 0.005
_DEPTH_BOUNDS = {"volume": (0, 1), "pitch": (-48, 48),
                 "cutoff": (-18000, 18000), "pulse": (-0.9, 0.9)}


def default_instrument(name="New instrument"):
    """Return a fresh instrument; envelope times are seconds, sustain is 0..1.

    Volume multiplies by (1 - depth + depth * ADSR); other targets add depth *
    ADSR to their base value. Pitch depth is semitones, cutoff Hz, pulse fraction.
    """
    return {
        "name": name, "wave": "square", "volume": 0.65, "tune": 0,
        "pulse": 0.5, "cutoff": 12000,
        "envelopes": {
            target: {"attack": 0.005, "decay": 0.12, "sustain": 0.65,
                     "release": 0.12, "depth": 1 if target == "volume" else 0}
            for target in TARGETS
        },
    }


def _number(value, low, high, label):
    if (type(value) not in (int, float) or not low <= value <= high
            or not math.isfinite(value)):
        raise ValueError(f"{label} must be a finite number in {low}..{high}")


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer in {low}..{high}")


def _text(value, label):
    if (not isinstance(value, str) or not 1 <= len(value) <= 80
            or not value.strip() or any(not 32 <= ord(c) <= 126 for c in value)):
        raise ValueError(f"{label} must be 1..80 printable ASCII characters")


def _keys(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError(f"{label} must contain exactly: {', '.join(keys)}")


def _validate_instrument(instrument):
    _keys(instrument, ("name", "wave", "volume", "tune", "pulse", "cutoff",
                       "envelopes"), "instrument")
    _text(instrument["name"], "instrument name")
    if instrument["wave"] not in WAVES:
        raise ValueError("instrument wave must be one of " + ", ".join(WAVES))
    for key, low, high in (("volume", 0, 1), ("tune", -24, 24),
                           ("pulse", 0.05, 0.95), ("cutoff", 100, 18000)):
        _number(instrument[key], low, high, key)
    _keys(instrument["envelopes"], TARGETS, "envelopes")
    for target, env in instrument["envelopes"].items():
        _keys(env, ("attack", "decay", "sustain", "release", "depth"), target)
        for key in ("attack", "decay", "release"):
            _number(env[key], 0, 10, f"{target} {key}")
        _number(env["sustain"], 0, 1, f"{target} sustain")
        _number(env["depth"], *_DEPTH_BOUNDS[target], f"{target} depth")


def _tail_seconds(song):
    return max(_RELEASE_FLOOR, *(i["envelopes"]["volume"]["release"]
                                       for i in song["instruments"]))


def validate_song(song):
    """Validate without coercing or mutating; return the original object.

    Envelope times are bounded to 0..10 seconds. Unknown keys are rejected so
    damaged or unsupported files cannot be silently rewritten as valid songs.
    """
    _keys(song, ("version", "title", "bpm", "rows", "patterns", "order",
                 "instruments"), "song")
    _integer(song["version"], 1, 1, "version")
    _text(song["title"], "title")
    _number(song["bpm"], 40, 300, "bpm")
    if type(song["rows"]) is not int or song["rows"] not in (16, 32, 64):
        raise ValueError("rows must be 16, 32, or 64")
    for key, low, high in (("patterns", 1, 16), ("instruments", 1, 32),
                            ("order", 1, 64)):
        if type(song[key]) is not list or not low <= len(song[key]) <= high:
            raise ValueError(f"{key} must be a list with {low}..{high} entries")
    for instrument in song["instruments"]:
        _validate_instrument(instrument)
    for pattern in song["patterns"]:
        if type(pattern) is not list or len(pattern) != song["rows"]:
            raise ValueError("all patterns must have exactly song['rows'] rows")
        for row in pattern:
            if type(row) is not list or len(row) != 4:
                raise ValueError("each row must contain exactly four cells")
            for cell in row:
                if cell is None:
                    continue
                _keys(cell, ("note", "instrument"), "cell")
                if cell["note"] != "off":
                    _integer(cell["note"], 24, 107, "note")
                _integer(cell["instrument"], 0, len(song["instruments"]) - 1,
                         "instrument index")
    for index in song["order"]:
        _integer(index, 0, len(song["patterns"]) - 1, "order index")
    duration = len(song["order"]) * song["rows"] * 15 / song["bpm"]
    if duration + _tail_seconds(song) > MAX_DURATION_SECONDS:
        raise ValueError("song including release tail must not exceed 120 seconds")
    return song


def example_song(index=0):
    """Return one of three original 32-row, two-pattern, four-channel songs."""
    _integer(index, 0, len(EXAMPLE_NAMES) - 1, "example index")
    lead = default_instrument("Comet lead")
    lead.update(wave=("square", "triangle", "saw")[index], volume=0.55,
                cutoff=(7000, 4200, 3200)[index], pulse=0.3)
    lead["envelopes"]["volume"].update(decay=0.09, sustain=0.5, release=0.1)
    lead["envelopes"]["cutoff"].update(depth=2200, decay=0.16, sustain=0.1)
    bass = default_instrument("Orbit bass")
    bass.update(wave="triangle", volume=0.75, cutoff=1600)
    bass["envelopes"]["volume"].update(decay=0.12, sustain=0.5)
    kick = default_instrument("Tiny kick")
    kick.update(wave="sine", volume=0.85, cutoff=1800)
    kick["envelopes"]["volume"].update(attack=0, decay=0.11, sustain=0, release=0.04)
    kick["envelopes"]["pitch"].update(attack=0, decay=0.045, sustain=0, depth=20)
    hat = default_instrument("Pixel snare")
    hat.update(wave="noise", volume=0.35, cutoff=6500)
    hat["envelopes"]["volume"].update(attack=0, decay=0.055, sustain=0, release=0.025)
    melodies = ((72, 76, 79, 83, 79, 76, 74, 71),
                (69, 72, 76, 74, 81, 76, 72, 67),
                (74, 77, 81, 84, 79, 77, 72, 69))
    roots = ((36, 41), (33, 38), (38, 34))[index]
    patterns = []
    for part in range(2):
        rows = [[None] * 4 for _ in range(32)]
        for row in range(32):
            if row % 4 == 0:
                note = melodies[index][(row // 4 + part * 3) % 8]
                rows[row][0] = {"note": note + (0 if part == 0 else -2),
                                "instrument": 0}
            if row % 4 == 3:
                rows[row][0] = {"note": "off", "instrument": 0}
            if row % 8 == 0:
                rows[row][1] = {"note": roots[part] + (7 if row == 24 else 0),
                                "instrument": 1}
            if row % 8 == 6:
                rows[row][1] = {"note": "off", "instrument": 1}
            if row % 8 == 0 or (part == 1 and row == 22):
                rows[row][2] = {"note": 36, "instrument": 2}
            if row % 4 == 2 or row % 8 == 4:
                rows[row][3] = {"note": 60, "instrument": 3}
        patterns.append(rows)
    return validate_song({"version": 1, "title": EXAMPLE_NAMES[index],
                          "bpm": (132, 112, 148)[index], "rows": 32,
                          "patterns": patterns, "order": [0, 1],
                          "instruments": [lead, bass, kick, hat]})


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_song(path):
    """Read bounded UTF-8 JSON, rejecting duplicate keys and invalid values."""
    with open(path, "rb") as source:
        data = source.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("song JSON exceeds 2 MB")
    try:
        song = json.loads(data.decode("utf-8"), object_pairs_hook=_json_object)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid song JSON") from exc
    return validate_song(song)


def save_song(path, song):
    """Validate first, then fsync a sibling temporary file and atomically replace."""
    snapshot = copy.deepcopy(song)
    validate_song(snapshot)
    payload = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    destination = os.path.abspath(os.fspath(path))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=os.path.dirname(destination),
                                         prefix=".synth-", suffix=".tmp",
                                         delete=False) as output:
            temporary = output.name
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def note_name(note):
    if note == "off":
        return "OFF"
    if type(note) is not int or not 0 <= note <= 127:
        return "..."
    return ("C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-",
            "A#", "B-")[note % 12] + str(note // 12 - 1)


def _numpy():
    try:
        return importlib.import_module("numpy")
    except (ImportError, OSError) as exc:
        raise RuntimeError("Audio needs numpy. Install it with: pip install numpy sounddevice") from exc


def row_sample(row_index, bpm, sample_rate=22050):
    """Absolute sample for a sixteenth-note boundary; no accumulated rounding."""
    return round(row_index * sample_rate * 15 / bpm)


def _adsr(times, envelope, gate, amplitude=False):
    """Vector ADSR, including releases during attack/decay and zero-length stages."""
    np = _numpy()
    attack = max(envelope["attack"], _ATTACK_FLOOR if amplitude else 0)
    decay, sustain = envelope["decay"], envelope["sustain"]
    release = max(envelope["release"], _RELEASE_FLOOR if amplitude else 0)

    def held(t):
        values = np.full_like(t, sustain, dtype=float)
        if decay:
            values = np.where(t < attack + decay,
                              1 - (1 - sustain) * (t - attack) / decay, values)
        if attack:
            values = np.where(t < attack, t / attack, values)
        return np.where(t < 0, 0, values)

    values = held(times)
    level = float(held(np.asarray(gate)))
    if release:
        values = np.where(times >= gate,
                          level * np.maximum(0, 1 - (times - gate) / release), values)
    else:
        values = np.where(times >= gate, 0, values)
    return values


def _voice(output, start, gate, end, instrument, note, sample_rate, seed):
    """Add one voice in short blocks; retain oscillator and filter state."""
    np = _numpy()
    envs = instrument["envelopes"]
    phase = 0.0
    previous = 0.0
    rng = np.random.default_rng(seed)
    gate_seconds = (gate - start) / sample_rate
    for offset in range(start, min(end, len(output)), 256):
        count = min(256, end - offset, len(output) - offset)
        times = (np.arange(count) + offset - start) / sample_rate
        levels = {target: _adsr(times, envs[target], gate_seconds,
                                amplitude=(target == "volume"))
                  for target in TARGETS}
        pitch = instrument["tune"] + levels["pitch"] * envs["pitch"]["depth"]
        frequency = np.minimum(sample_rate * 0.45,
                               440 * 2 ** ((note - 69 + pitch) / 12))
        increments = frequency / sample_rate
        phases = phase + np.cumsum(increments) - increments
        phase = float((phase + np.sum(increments)) % 1)
        cycle = phases % 1
        wave = instrument["wave"]
        if wave == "sine":
            signal = np.sin(2 * np.pi * cycle)
        elif wave == "triangle":
            signal = 1 - 4 * np.abs(cycle - 0.5)
        elif wave == "saw":
            signal = 2 * cycle - 1
        elif wave == "square":
            pulse = np.clip(instrument["pulse"] + levels["pulse"] *
                            envs["pulse"]["depth"], 0.05, 0.95)
            signal = np.where(cycle < pulse, 1.0, -1.0)
        else:
            signal = rng.uniform(-1, 1, count)
        cutoff = float(np.mean(np.clip(instrument["cutoff"] + levels["cutoff"] *
                                       envs["cutoff"]["depth"], 100,
                                       sample_rate * 0.45)))
        coefficient = math.exp(-2 * math.pi * cutoff / sample_rate)
        # Solve the block's one-pole recurrence in the frequency domain, then
        # correct its periodic initial state to match the preceding block.
        bins = np.arange(count // 2 + 1)
        transfer = (1 - coefficient) / (1 - coefficient *
                    np.exp(-2j * np.pi * bins / count))
        filtered = np.fft.irfft(np.fft.rfft(signal) * transfer, n=count)
        correction = coefficient * (previous - filtered[-1])
        filtered += correction * coefficient ** np.arange(count)
        previous = float(filtered[-1])
        amp = 1 - envs["volume"]["depth"] + envs["volume"]["depth"] * levels["volume"]
        # Even a disabled volume envelope must start/stop without a DC step.
        amp *= np.minimum(1, times / _ATTACK_FLOOR)
        amp *= np.clip((end - (offset + np.arange(count))) /
                       (sample_rate * _RELEASE_FLOOR), 0, 1)
        output[offset:offset + count] += (filtered * amp * instrument["volume"] * 0.18)


def _sample_rate(sample_rate):
    if type(sample_rate) is not int or sample_rate not in (22050, 44100):
        raise ValueError("sample_rate must be 22050 or 44100")


def _muted_channels(muted):
    try:
        muted = tuple(muted)
    except TypeError as exc:
        raise ValueError("muted must contain channel indexes 0..3") from exc
    for channel in muted:
        _integer(channel, 0, 3, "muted channel")
    return frozenset(muted)


def _render(song, sample_rate, muted, loop=False):
    np = _numpy()
    total_rows = song["rows"] * len(song["order"])
    body = row_sample(total_rows, song["bpm"], sample_rate)
    tail = math.ceil(_tail_seconds(song) * sample_rate)
    output = np.zeros(2 * body if loop else body + tail, dtype=np.float32)
    for channel in range(4):
        if channel in muted:
            continue
        events = []
        for cycle in range(3 if loop else 1):
            for order_index, pattern_index in enumerate(song["order"]):
                for row_index, row in enumerate(song["patterns"][pattern_index]):
                    cell = row[channel]
                    if cell is not None:
                        position = cycle * body + row_sample(
                            order_index * song["rows"] + row_index,
                            song["bpm"], sample_rate)
                        events.append((position, cell))
        # A note lasts until the next event, including OFF. Subsequent rests
        # leave it alone. A later note steals any outstanding release tail.
        next_note = len(output) + tail
        following_notes = [next_note] * len(events)
        for index in range(len(events) - 1, -1, -1):
            following_notes[index] = next_note
            if events[index][1]["note"] != "off":
                next_note = events[index][0]
        for index, (start, cell) in enumerate(events):
            if cell["note"] == "off" or start >= len(output):
                continue
            instrument = song["instruments"][cell["instrument"]]
            gate = events[index + 1][0] if index + 1 < len(events) else body
            release = max(instrument["envelopes"]["volume"]["release"], _RELEASE_FLOOR)
            end = min(gate + math.ceil(release * sample_rate),
                      following_notes[index] + math.ceil(_RELEASE_FLOOR * sample_rate))
            _voice(output, start, gate, end, instrument, cell["note"], sample_rate,
                   channel * 100003 + index % max(1, len(events) // (3 if loop else 1)))
    np.tanh(output, out=output)
    return output


def render_song(song, sample_rate=22050, muted=()):
    """Return mono float32 PCM, including a bounded final release tail.

    This is synchronous and device-independent. For UI applications, render or
    call play from a worker if a worst-case 120-second song would block the UI.
    """
    validate_song(song)
    _sample_rate(sample_rate)
    return _render(song, sample_rate, _muted_channels(muted))


class AudioPlayer:
    """Device playback facade. Constructing it needs only the Python stdlib.

    Lifecycle methods should be serialized by the caller. Getters can be used
    by the UI while the PortAudio callback runs. No UI toolkit is imported.
    """

    def __init__(self):
        self._stream = None
        self._pcm = None
        self._recent = None
        self._recent_index = 0
        self._recent_count = 0
        self._cursor = 0
        self._loop_start = None
        self._boundaries = None
        self._rows = 0
        self._body = 0
        self._playing = False
        self._error = ""
        self._generation = 0
        self.sample_rate = 22050

    @property
    def playing(self):
        return self._playing

    @property
    def error(self):
        return self._error

    @property
    def position(self):
        if not self._playing or self._boundaries is None:
            return None
        cursor = self._cursor
        if self._loop_start is not None:
            cursor %= self._body
        row = bisect.bisect_right(self._boundaries, min(cursor, self._body - 1)) - 1
        return (row // self._rows, row % self._rows)

    def play(self, song, loop=True, muted=()):
        snapshot = copy.deepcopy(song)
        validate_song(snapshot)
        if type(loop) is not bool:
            raise ValueError("loop must be a boolean")
        muted = _muted_channels(muted)
        self.stop()
        self._error = ""
        try:
            pcm = _render(snapshot, self.sample_rate, muted, loop=loop)
            rows = snapshot["rows"] * len(snapshot["order"])
            self._rows = snapshot["rows"]
            self._boundaries = [row_sample(i, snapshot["bpm"], self.sample_rate)
                                for i in range(rows)]
            self._body = row_sample(rows, snapshot["bpm"], self.sample_rate)
            self._start(pcm, self._body if loop else None)
        except Exception as exc:
            self._failed(exc)

    def audition(self, instrument, note=60):
        snapshot = copy.deepcopy(instrument)
        _validate_instrument(snapshot)
        _integer(note, 24, 107, "note")
        self.stop()
        self._error = ""
        try:
            np = _numpy()
            gate = round(0.45 * self.sample_rate)
            release = max(snapshot["envelopes"]["volume"]["release"], _RELEASE_FLOOR)
            pcm = np.zeros(gate + math.ceil(release * self.sample_rate), dtype=np.float32)
            _voice(pcm, 0, gate, len(pcm), snapshot, note, self.sample_rate, 0)
            np.tanh(pcm, out=pcm)
            self._start(pcm, None)
        except Exception as exc:
            self._failed(exc)

    def _failed(self, exc):
        message = (f"Audio playback failed: {exc}. "
                   "Check your output device and audio permissions.")
        try:
            self.stop()
        except RuntimeError:
            pass
        self._error = message
        raise RuntimeError(message) from exc

    def _start(self, pcm, loop_start):
        np = _numpy()
        try:
            sd = importlib.import_module("sounddevice")
        except (ImportError, OSError) as exc:
            raise RuntimeError("Audio needs sounddevice and PortAudio. Install with: "
                               "pip install numpy sounddevice; on macOS, brew install portaudio") from exc
        self._pcm = pcm
        self._loop_start = loop_start
        self._recent = np.zeros(8192, dtype=np.float32)
        self._recent_count = self._recent_index = self._cursor = 0
        generation = self._generation

        def finished():
            if generation == self._generation:
                self._playing = False

        def callback(outdata, frames, time_info, status):
            if generation != self._generation:
                outdata.fill(0)
                raise sd.CallbackStop
            if status:
                self._error = f"Audio stream warning: {status}. Try another output device."
            offset = 0
            while offset < frames:
                if self._cursor == len(pcm):
                    if loop_start is None:
                        outdata[offset:, 0] = 0
                        break
                    self._cursor = loop_start
                count = min(frames - offset, len(pcm) - self._cursor)
                outdata[offset:offset + count, 0] = pcm[self._cursor:self._cursor + count]
                self._cursor += count
                offset += count
            # Record exactly what went to the device, including final silence.
            source = outdata[max(0, frames - len(self._recent)):, 0]
            count = len(source)
            first = min(count, len(self._recent) - self._recent_index)
            self._recent[self._recent_index:self._recent_index + first] = source[:first]
            self._recent[:count - first] = source[first:]
            self._recent_index = (self._recent_index + count) % len(self._recent)
            self._recent_count = min(len(self._recent), self._recent_count + count)
            if loop_start is None and self._cursor == len(pcm):
                raise sd.CallbackStop

        self._stream = sd.OutputStream(samplerate=self.sample_rate, channels=1,
                                       dtype="float32", blocksize=512,
                                       callback=callback, finished_callback=finished)
        self._playing = True
        self._stream.start()

    def stop(self):
        self._generation += 1
        stream, self._stream = self._stream, None
        self._playing = False
        failure = None
        if stream is not None:
            try:
                stream.stop()
            except Exception as exc:
                failure = exc
            finally:
                try:
                    stream.close()
                except Exception as exc:
                    failure = failure or exc
        # PortAudio has now joined its callback. Keep buffers alive until then.
        self._pcm = self._recent = self._boundaries = None
        self._cursor = self._recent_count = self._recent_index = 0
        self._loop_start = None
        if failure is not None:
            self._error = f"Audio cleanup failed: {failure}. Check your output device."
            raise RuntimeError(self._error) from failure

    def close(self):
        self.stop()

    def _recent_output(self):
        recent = self._recent
        count, index = self._recent_count, self._recent_index
        if recent is None or not count:
            return None
        np = _numpy()
        return np.concatenate((recent, recent))[index + len(recent) - count:index + len(recent)].copy()

    def waveform(self, points=60):
        """Resample the last 1024 actual output samples (silence before playback)."""
        _integer(points, 1, 4096, "waveform points")
        recent = self._recent_output()
        if recent is None:
            return [0.0] * points
        np = _numpy()
        recent = recent[-1024:]
        return np.interp(np.linspace(0, len(recent) - 1, points),
                         np.arange(len(recent)), recent).tolist()

    def spectrum(self, bands=32):
        """Peak-normalized linear-frequency FFT bands, DC through Nyquist."""
        _integer(bands, 1, 4096, "spectrum bands")
        recent = self._recent_output()
        if recent is None or len(recent) < 2:
            return [0.0] * bands
        np = _numpy()
        spectrum = np.abs(np.fft.rfft(recent * np.hanning(len(recent)), n=8192))
        edges = np.linspace(0, len(spectrum), bands + 1, dtype=int)
        values = np.array([spectrum[edges[i]:edges[i + 1]].max()
                           for i in range(bands)])
        peak = float(values.max())
        if peak < 1e-8:
            return [0.0] * bands
        return np.clip(values / peak, 0, 1).tolist()
