"""Group error events into incidents.

One bug usually produces several events: a traceback, the 500 in the access log, maybe a
panic in another service a few milliseconds earlier. Errors that arrive close together
become one incident, closed once the stack has been quiet for `settle` seconds. When it
closes, the cross-service timeline is captured right away, before the ring buffers roll.

- 401/403s are routine on their own; they open an incident only as a burst.
- The same incident again within `cooldown` (a retrying frontend) bumps a repeat counter
  instead of producing a new incident.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .correlate import TimelineEntry, build_timeline
from .detectors import ErrorEvent
from .logbus import HAMMOCK, LogLine

# Lower is more informative when picking the event that headlines an incident.
_KIND_RANK = {
    "rust-compile-error": 0, "rust-panic": 0, "python-traceback": 0,
    "django-500": 1, "service-exited": 2, "error-line": 3, "http-5xx": 4, "http-auth": 5,
}


_EXIT_HINTS = {
    126: "the command isn't executable (permissions?)",
    127: "command not found: is it installed and on PATH in the shell that runs Hammock?",
}
_EXIT_TAIL = 15


def service_exit_event(service: str, returncode: int | None, tail: list[LogLine] = ()) -> ErrorEvent:
    """A service that died. Its last output lines are the evidence ("cargo: command not found")."""
    tail = [l for l in tail if l.text.strip()][-_EXIT_TAIL:]
    summary = f"{service} exited on its own (code {returncode})"
    if tail:
        summary += f": {tail[-1].text.strip()[:200]}"
    details: dict[str, Any] = {"returncode": returncode}
    if hint := _EXIT_HINTS.get(returncode):  # type: ignore[arg-type]
        details["hint"] = hint
    if not tail:
        text = f"{service} exited on its own (code {returncode})"
        tail = [LogLine(service, text, text, "hammock", time.monotonic(), time.time())]
    return ErrorEvent(service=service, kind="service-exited", summary=summary, lines=list(tail),
                      details=details)


@dataclass
class Incident:
    id: int
    events: list[ErrorEvent]  # ordered by first line
    timeline: list[TimelineEntry]
    opened_at: float  # wall clock
    repeats: int = 1
    saved_to: Path | None = None

    @property
    def primary(self) -> ErrorEvent:
        return min(self.events, key=lambda e: (_KIND_RANK.get(e.kind, 9), e.start))

    @property
    def services(self) -> list[str]:
        """Services with errors, in the order their first error appeared."""
        return list(dict.fromkeys(e.service for e in self.events))

    @property
    def request_ids(self) -> list[str]:
        return list(dict.fromkeys(e.request_id for e in self.events if e.request_id))

    @property
    def request_services(self) -> list[str]:
        """Every service that logged one of the incident's request IDs."""
        return list(dict.fromkeys(
            t.line.service for t in self.timeline
            if t.why in ("error", "request") and t.line.service != HAMMOCK
        ))

    @property
    def signature(self) -> tuple[str, str, str, str]:
        p = self.primary
        return p.service, p.kind, p.summary, str(p.location)

    def to_dict(self) -> dict[str, Any]:
        def event(e: ErrorEvent) -> dict[str, Any]:
            return {
                "service": e.service, "kind": e.kind, "severity": e.severity, "summary": e.summary,
                "location": str(e.location) if e.location else None, "request_id": e.request_id,
                "project_frames": [str(f) for f in e.project_frames], "details": e.details,
                "lines": [l.text for l in e.lines],
            }

        return {
            "id": self.id,
            "opened_at": datetime.fromtimestamp(self.opened_at).isoformat(timespec="seconds"),
            "repeats": self.repeats,
            "primary": event(self.primary),
            "services": self.services,
            "request_ids": self.request_ids,
            "request_services": self.request_services,
            "events": [event(e) for e in self.events],
            "timeline": [
                {"time": datetime.fromtimestamp(t.line.wall).isoformat(timespec="milliseconds"),
                 "service": t.line.service, "why": t.why, "text": t.line.text}
                for t in self.timeline
            ],
        }


def save_incident(incident: Incident, directory: Path) -> Path | None:
    """Write the incident as JSON; never fails the caller (read-only checkout, full disk…)."""
    stamp = datetime.fromtimestamp(incident.opened_at).strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}-{incident.id:03d}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(incident.to_dict(), indent=2, default=str) + "\n")
    except OSError:
        return None
    incident.saved_to = path
    return path


@dataclass
class _Recent:
    incident: Incident
    seen: float  # monotonic


class IncidentTracker:
    def __init__(
        self,
        lines: Callable[[], Iterable[LogLine]],
        on_incident: Callable[[Incident], None],
        *,
        window: float = 5.0,
        settle: float = 1.5,
        max_open: float = 10.0,
        cooldown: float = 30.0,
        auth_burst: int = 3,
        noise_paths: Iterable[str] = (),
    ) -> None:
        self._lines = lines
        self.on_incident = on_incident  # also called again with `repeats` bumped for a repeat
        self.window = window
        self.settle = settle
        self.max_open = max_open
        self.cooldown = cooldown
        self.auth_burst = auth_burst
        self.noise_paths = tuple(noise_paths)
        self._open: list[ErrorEvent] = []
        self._opened = self._last = 0.0
        self._auth: deque[ErrorEvent] = deque()
        self._recent: list[_Recent] = []
        self._next_id = 1

    def add(self, event: ErrorEvent, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if event.severity == "warning":
            self._add_warning(event, now)
            return
        if not self._open:
            self._opened = now
        self._open.append(event)
        self._last = now

    def _add_warning(self, event: ErrorEvent, now: float) -> None:
        if self._open:
            self._open.append(event)  # context for the open incident, but don't keep it open longer
            return
        self._auth.append(event)
        while self._auth and self._auth[0].end < event.end - self.window:
            self._auth.popleft()
        if len(self._auth) >= self.auth_burst:
            self._open = list(self._auth)
            self._auth.clear()
            self._opened = self._last = now

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self._open and (now - self._last >= self.settle or now - self._opened >= self.max_open):
            self._finalize(now)

    async def run(self, interval: float = 0.25) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                self.tick()
            except Exception:  # keep tracking; a bug here must not take log streaming down
                traceback.print_exc()

    def _finalize(self, now: float) -> None:
        events = sorted(self._open, key=lambda e: e.start)
        self._open = []
        timeline = build_timeline(self._lines(), events, self.window, self.noise_paths)
        incident = Incident(self._next_id, events, timeline, time.time())

        self._recent = [r for r in self._recent if now - r.seen < self.cooldown]
        for recent in self._recent:
            if recent.incident.signature == incident.signature:
                recent.incident.repeats += 1
                recent.seen = now
                self.on_incident(recent.incident)
                return
        self._next_id += 1
        self._recent.append(_Recent(incident, now))
        self.on_incident(incident)
