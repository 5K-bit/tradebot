"""
state.py — tiny durable key/value store for things that must survive a restart.

The kill switch and the per-symbol candle cursor are both worthless if they
live only in memory: restart after a bad day and the bot forgets it was
halted, or re-evaluates a candle it already traded. This persists them as a
small JSON file next to the bot.

Writes are atomic (temp file + os.replace) so a crash mid-write can't leave a
truncated file that silently resets the kill switch on the next start.
"""
import json
import os
import tempfile
from pathlib import Path


class JsonState:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            with self.path.open(encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self._data = loaded
            else:
                print(f"[state] {self.path} is not a JSON object — ignoring it.")
        except (json.JSONDecodeError, OSError) as e:
            # Don't crash the bot over a corrupt state file, but be loud: a
            # lost kill-switch baseline is a real safety event.
            print(f"[state] could not read {self.path} ({e}) — starting with empty state.")

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, **kwargs) -> None:
        self._data.update(kwargs)
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
