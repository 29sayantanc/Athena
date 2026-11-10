"""Long-term user facts ("remember that I prefer…")."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import MEMORY_PATH


class Memory:
    def __init__(self, path: Path = MEMORY_PATH):
        self.path = path
        self.items: list[dict] = []
        if path.exists():
            try:
                self.items = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.items = []

    def _save(self):
        self.path.write_text(json.dumps(self.items, indent=2, ensure_ascii=False), encoding="utf-8")

    def facts(self) -> list[str]:
        return [i["fact"] for i in self.items]

    def add(self, fact: str) -> None:
        fact = fact.strip()
        if not fact or any(i["fact"].lower() == fact.lower() for i in self.items):
            return
        self.items.append({"fact": fact[:300], "at": time.time()})
        self.items = self.items[-200:]
        self._save()

    def delete(self, index: int) -> None:
        if 0 <= index < len(self.items):
            self.items.pop(index)
            self._save()

    def clear(self) -> None:
        self.items = []
        self._save()
