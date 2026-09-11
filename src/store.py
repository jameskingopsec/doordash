"""Tiny crash-safe session index. It never writes payment card data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .models import OrderSession


class SessionStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[int, OrderSession]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        sessions: dict[int, OrderSession] = {}
        if not isinstance(raw, list):
            return sessions
        for entry in raw:
            try:
                session = OrderSession.from_safe_dict(entry)
                sessions[session.user_id] = session
            except (TypeError, ValueError):
                continue
        return sessions

    def save(self, sessions: Iterable[OrderSession]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        target = self.path.with_suffix(".tmp")
        target.write_text(
            json.dumps([session.safe_dict() for session in sessions], indent=2),
            encoding="utf-8",
        )
        target.replace(self.path)

