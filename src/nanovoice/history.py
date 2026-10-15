"""Append-only JSONL log of transcriptions (text only, never audio)."""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from pathlib import Path


class History:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()

    def append(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)

    def tail(self, n: int) -> list[dict]:
        """Last `n` entries, oldest first. Unparseable lines are skipped."""
        if n <= 0 or not self.path.exists():
            return []
        with self._lock, self.path.open(encoding="utf-8") as f:
            lines = deque(f, maxlen=n)
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def find(self, entry_id: str) -> dict | None:
        """Most recent entry with this id."""
        if not self.path.exists():
            return None
        found = None
        with self._lock, self.path.open(encoding="utf-8") as f:
            for line in f:
                if entry_id in line:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("id") == entry_id:
                        found = e
        return found
