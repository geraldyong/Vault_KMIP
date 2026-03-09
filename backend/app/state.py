from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DemoState:
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    lock: RLock = field(default_factory=RLock)

    def add_log(self, level: str, message: str, **details: Any) -> None:
        with self.lock:
            self.logs.insert(
                0,
                {
                    "ts": utc_now(),
                    "level": level.upper(),
                    "message": message,
                    "details": details,
                },
            )
            del self.logs[500:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "groups": self.groups,
                "logs": self.logs,
            }


STATE = DemoState()
