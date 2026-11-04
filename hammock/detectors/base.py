"""Shared types for the error detectors.

A parser reads one service's lines in order and turns error output into `ErrorEvent`s.
Multi-line blocks (tracebacks, panics, compiler errors) are assembled across lines; a
block that stops growing is closed by `flush()` once the service has been quiet briefly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ..logbus import LogLine

MAX_BLOCK_LINES = 400  # keep memory bounded on pathological output; the rest is still consumed

_REQUEST_ID_RE = re.compile(r"(?:x-)?request[_-]?id[\"']?\s*[=:]\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_NO_ID = {"-", "none", "null"}


def find_request_id(text: str) -> str | None:
    m = _REQUEST_ID_RE.search(text)
    if m and m.group(1).lower() not in _NO_ID:
        return m.group(1)
    return None


@dataclass(slots=True, frozen=True)
class Frame:
    file: str
    line: int
    func: str | None = None
    col: int | None = None
    in_project: bool = True  # False for stdlib / site-packages / cargo registry code

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" + (f":{self.col}" if self.col else "")
        return f"{where} in {self.func}" if self.func else where


@dataclass(slots=True)
class ErrorEvent:
    service: str
    kind: str  # python-traceback, django-500, rust-panic, rust-compile-error, http-5xx, http-auth, error-line
    summary: str  # one line, e.g. "KeyError: 'total'"
    lines: list[LogLine]
    frames: list[Frame] = field(default_factory=list)
    location: Frame | None = None  # where the error most likely is in the service's own code
    request_id: str | None = None
    severity: Literal["error", "warning"] = "error"
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def start(self) -> float:
        return self.lines[0].mono

    @property
    def end(self) -> float:
        return self.lines[-1].mono

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def project_frames(self) -> list[Frame]:
        return [f for f in self.frames if f.in_project]


def request_id_of(lines: list[LogLine]) -> str | None:
    for line in lines:
        if rid := find_request_id(line.text):
            return rid
    return None


class Parser:
    """Stateful, one instance per service.

    `feed(line)` returns (consumed, finished events). A parser that closes its block because
    the line doesn't belong to it tries to start a new block with that same line, and
    reports `consumed=False` if it couldn't, so the line can go to the other parsers.
    """

    def __init__(self, service: str) -> None:
        self.service = service

    @property
    def open(self) -> bool:
        return False

    def feed(self, line: LogLine) -> tuple[bool, list[ErrorEvent]]:
        raise NotImplementedError

    def flush(self) -> list[ErrorEvent]:
        return []


def append_capped(lines: list[LogLine], line: LogLine) -> None:
    if len(lines) < MAX_BLOCK_LINES:
        lines.append(line)
