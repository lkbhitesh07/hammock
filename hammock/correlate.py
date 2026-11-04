"""Pick the lines, from every service, that belong with an incident.

1. Request ID (preferred): every line that mentions one of the incident's request IDs,
   in any service, however long ago it was printed. This follows one request across the stack.
2. Time window (fallback): every line from every service within ±window seconds of the
   incident's errors, ordered by Hammock's arrival stamps. Hammock's own lines are kept
   ("↻ restarting pricing" right before an error is strong evidence).

Health-check chatter is dropped; it is never evidence and would only eat the model's context.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from .config import Config
from .detectors import ErrorEvent
from .logbus import LogLine

Why = Literal["error", "request", "window"]

_BAD_STATUS = re.compile(r'(?:" |status[=:]\s*"?)[45]\d\d\b')


@dataclass(slots=True, frozen=True)
class TimelineEntry:
    line: LogLine
    why: Why  # part of a detected error / carries one of the request IDs / near in time


def health_paths(config: Config) -> set[str]:
    """Paths of HTTP health checks, e.g. {"/health"}. A bare "/" can't be told apart, so it's skipped."""
    paths = set()
    for svc in config.services.values():
        if svc.health and svc.health.startswith("http"):
            path = urlsplit(svc.health).path
            if path not in ("", "/"):
                paths.add(path)
    return paths


def _is_noise(text: str, noise_paths: Iterable[str]) -> bool:
    if not text.strip():
        return True
    for path in noise_paths:
        if re.search(rf'[\s"=]{re.escape(path)}(?=[\s"?]|$)', text) and not _BAD_STATUS.search(text):
            return True
    return False


def build_timeline(
    lines: Iterable[LogLine],
    events: Sequence[ErrorEvent],
    window: float,
    noise_paths: Iterable[str] = (),
) -> list[TimelineEntry]:
    if not events:
        return []
    start = min(e.start for e in events) - window
    end = max(e.end for e in events) + window
    request_ids = {e.request_id for e in events if e.request_id}
    error_lines = {id(line) for e in events for line in e.lines}
    noise_paths = tuple(noise_paths)

    entries: list[TimelineEntry] = []
    for line in lines:
        why: Why
        if id(line) in error_lines:
            why = "error"
        elif request_ids and any(rid in line.text for rid in request_ids):
            why = "request"
        elif start <= line.mono <= end and not _is_noise(line.text, noise_paths):
            why = "window"
        else:
            continue
        entries.append(TimelineEntry(line, why))
    entries.sort(key=lambda e: e.line.mono)
    return entries
