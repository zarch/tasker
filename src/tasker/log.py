"""JSONL iteration logger — records every QA↔Dev exchange."""

from __future__ import annotations

import json
from pathlib import Path

from .models import IterationEntry


class IterationLog:
    """Append-only JSONL logger for QA/Dev iterations."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Create file if it doesn't exist
        if not self._path.exists():
            self._path.touch()

    def append(self, entry: IterationEntry) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        entries: list[dict] = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    @property
    def count(self) -> int:
        if not self._path.exists():
            return 0
        with open(self._path, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
