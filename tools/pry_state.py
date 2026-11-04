"""Small atomic store for persistent UI state, separate from user content."""
import json
import os
from pathlib import Path
import tempfile


def load_state(path):
    try:
        state = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(state, dict):
        raise ValueError('Expected an object in pry-state.json')
    return state


def save_state(path, section, value):
    path = Path(path)
    state = load_state(path)
    state[section] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         delete=False) as handle:
            temporary = handle.name
            json.dump(state, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
