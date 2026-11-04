"""Turn each service's log stream into error events.

Every service gets every parser (cheap, and it means a Python helper script inside a Rust
service is still understood). Parsers are tried most-specific first; a line belongs to at
most one of them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable

from ..logbus import HAMMOCK, LogLine
from .base import ErrorEvent, Frame, Parser
from .generic import GenericErrorParser
from .http import HttpParser
from .python_tb import PythonTracebackParser
from .rust import RustCompileParser, RustPanicParser

__all__ = ["DetectorHub", "ErrorEvent", "Frame", "ServiceDetector"]

PARSERS: tuple[type[Parser], ...] = (
    PythonTracebackParser,
    RustPanicParser,
    RustCompileParser,
    HttpParser,
    GenericErrorParser,
)


class ServiceDetector:
    def __init__(self, service: str) -> None:
        self.service = service
        self.parsers = [cls(service) for cls in PARSERS]
        self.last_mono = 0.0

    @property
    def open(self) -> bool:
        return any(p.open for p in self.parsers)

    def feed(self, line: LogLine) -> list[ErrorEvent]:
        self.last_mono = line.mono
        events: list[ErrorEvent] = []
        # An open block gets first refusal: a traceback line must not be read as an error line.
        offered = [p for p in self.parsers if p.open]
        for parser in offered:
            consumed, found = parser.feed(line)
            events += found
            if consumed:
                return events
        for parser in self.parsers:
            if parser in offered:
                continue
            consumed, found = parser.feed(line)
            events += found
            if consumed:
                break
        return events

    def flush(self) -> list[ErrorEvent]:
        return [e for p in self.parsers for e in p.flush()]


class DetectorHub:
    """LogBus subscriber: routes lines to per-service detectors and reports finished events.

    Blocks that stop growing (a panic with nothing printed after it) are closed by
    `flush()`, which `run()` calls periodically.
    """

    def __init__(self, on_event: Callable[[ErrorEvent], None], services: Iterable[str] = (),
                 idle: float = 0.5) -> None:
        self.on_event = on_event
        self.idle = idle
        self.detectors = {s: ServiceDetector(s) for s in services}

    def __call__(self, line: LogLine) -> None:
        if line.service == HAMMOCK:
            return
        detector = self.detectors.get(line.service)
        if detector is None:
            detector = self.detectors[line.service] = ServiceDetector(line.service)
        self._emit(detector.feed(line))

    def flush(self, now: float | None = None, force: bool = False) -> None:
        now = time.monotonic() if now is None else now
        for detector in self.detectors.values():
            if detector.open and (force or now - detector.last_mono >= self.idle):
                self._emit(detector.flush())

    async def run(self, interval: float = 0.25) -> None:
        while True:
            await asyncio.sleep(interval)
            self.flush()

    def _emit(self, events: list[ErrorEvent]) -> None:
        for event in events:
            try:
                self.on_event(event)
            except Exception:  # a broken consumer must never stop log streaming
                pass
