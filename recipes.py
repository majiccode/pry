"""Atomic persistent command recipes."""
import json
import os
from pathlib import Path
import tempfile


class RecipeStore:
    def __init__(self, path):
        self.path = Path(path)
        self.items = json.loads(self.path.read_text()) if self.path.exists() else {}
        if not isinstance(self.items, dict) or any(not isinstance(value, dict) or
                not isinstance(value.get('steps'), list) or not value['steps'] or
                not all(isinstance(step, str) for step in value['steps']) for value in self.items.values()):
            raise ValueError('Invalid recipe file')

    def save(self, name, steps, platform='any', verification='', requirements=''):
        if not name.strip() or not steps or not all(isinstance(step, str) and step.strip() for step in steps):
            raise ValueError('Recipe needs a name and at least one command.')
        items = {**self.items, name: dict(steps=steps, platform=platform,
                                        verification=verification, requirements=requirements)}
        self._write(items)

    def get(self, name):
        if name not in self.items:
            raise ValueError(f'No recipe named {name}')
        return self.items[name]

    def delete(self, name):
        self.get(name)
        self._write({key: value for key, value in self.items.items() if key != name})

    def _write(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as handle:
                temporary = handle.name
                json.dump(items, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.items = items
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
