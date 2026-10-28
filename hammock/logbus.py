"""Every line from every service passes through here, stamped with Hammock's own arrival time.

Stamping on arrival means we never parse each service's timestamp format; correlation
across services uses these stamps.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

HAMMOCK = "hammock"  # pseudo-service for Hammock's own messages

# CSI sequences (colors, cursor moves, clear screen), OSC sequences (titles, links), lone ESC codes.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


@dataclass(slots=True, frozen=True)
class LogLine:
    service: str
    raw: str  # as emitted, ANSI codes included (for display)
    text: str  # ANSI-stripped (for parsing)
    stream: Literal["stdout", "stderr", "hammock"]
    mono: float  # time.monotonic() on arrival; use for ordering and windows
    wall: float  # time.time() on arrival; use for display


class LogBus:
    def __init__(self, maxlen: int = 2000) -> None:
        self._maxlen = maxlen
        self._buffers: dict[str, deque[LogLine]] = {}
        self._subscribers: list[Callable[[LogLine], None]] = []

    def subscribe(self, callback: Callable[[LogLine], None]) -> None:
        self._subscribers.append(callback)

    def publish(self, service: str, raw: str, stream: Literal["stdout", "stderr", "hammock"]) -> LogLine:
        line = LogLine(service, raw, strip_ansi(raw), stream, time.monotonic(), time.time())
        self._buffers.setdefault(service, deque(maxlen=self._maxlen)).append(line)
        for callback in self._subscribers:
            callback(line)
        return line

    def info(self, message: str) -> None:
        self.publish(HAMMOCK, message, "hammock")

    def lines(self, service: str) -> list[LogLine]:
        return list(self._buffers.get(service, ()))

    def window(self, start: float, end: float) -> list[LogLine]:
        """All services' lines with start <= mono <= end, in arrival order."""
        found = [l for buf in self._buffers.values() for l in buf if start <= l.mono <= end]
        return sorted(found, key=lambda l: l.mono)
