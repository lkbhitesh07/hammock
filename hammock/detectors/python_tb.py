"""Python tracebacks (including chained exceptions) and Django's error headers."""

from __future__ import annotations

import re

from ..logbus import LogLine
from .base import ErrorEvent, Frame, Parser, append_capped, request_id_of

_TB_START = re.compile(r"^\s*Traceback \(most recent call last\):\s*$")
# `, in <func>` is missing on SyntaxError frames.
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?$')
_EXC = re.compile(r"^(?P<exc>[A-Za-z_][\w.]*)(?::\s?(?P<msg>.*))?$")
_CHAIN = {
    "During handling of the above exception, another exception occurred:",
    "The above exception was the direct cause of the following exception:",
}
_DJANGO_500 = re.compile(r"Internal Server Error: (?P<path>\S+)")
_THREAD_EXC = re.compile(r"^Exception in thread (?P<thread>\S+):$")
# Library code: venvs, system site-packages, the stdlib (e.g. /nix/store/…/lib/python3.12/), frozen modules.
_LIBRARY = re.compile(r"site-packages|dist-packages|/lib/python\d+\.\d+/|^<")


class PythonTracebackParser(Parser):
    def __init__(self, service: str) -> None:
        super().__init__(service)
        self._reset()

    def _reset(self) -> None:
        self.state = "idle"  # header -> frames -> after_exc (-> chain -> frames ...)
        self.lines: list[LogLine] = []
        self.held: list[LogLine] = []  # blank/chain lines kept until we know a chained traceback follows
        self.frames: list[Frame] = []
        self.exceptions: list[str] = []
        self.details: dict[str, str] = {}

    @property
    def open(self) -> bool:
        return self.state != "idle"

    def feed(self, line: LogLine) -> tuple[bool, list[ErrorEvent]]:
        text = line.text
        if self.state == "idle":
            return self._start(line), []

        if self.state == "header":
            if _TB_START.match(text):
                self._add(line)
                self.state = "frames"
                return True, []
            return self._close_and_retry(line)

        if self.state == "frames":
            if m := _FRAME.match(text):
                file = m["file"]
                self.frames.append(Frame(file, int(m["line"]), m["func"], None, not _LIBRARY.search(file)))
                self._add(line)
            elif text[:1].isspace() or not text.strip():
                self._add(line)  # source line, ^^^^ markers
            else:
                self._add(line)  # first unindented line ends the frames: the exception
                self.exceptions.append(text.strip())
                self.state = "after_exc"
            return True, []

        # after_exc / chain
        stripped = text.strip()
        if not stripped:
            self.held.append(line)
            return True, []
        if stripped in _CHAIN:
            self.held.append(line)
            self.state = "chain"
            return True, []
        if self.state == "chain" and _TB_START.match(text):
            for held in self.held:
                self._add(held)
            self.held = []
            self._add(line)
            self.state = "frames"
            return True, []
        if self.state == "after_exc" and not self.held and text[:1].isspace():
            self._add(line)  # multi-line exception message
            return True, []
        return self._close_and_retry(line)

    def flush(self) -> list[ErrorEvent]:
        return self._close() if self.open else []

    def _add(self, line: LogLine) -> None:
        append_capped(self.lines, line)

    def _start(self, line: LogLine) -> bool:
        text = line.text
        if _TB_START.match(text):
            self.state = "frames"
        elif m := _DJANGO_500.search(text):
            self.details["path"] = m["path"]
            self.state = "header"
        elif m := _THREAD_EXC.match(text):
            self.details["thread"] = m["thread"]
            self.state = "header"
        else:
            return False
        self._add(line)
        return True

    def _close_and_retry(self, line: LogLine) -> tuple[bool, list[ErrorEvent]]:
        events = self._close()
        return self._start(line), events

    def _close(self) -> list[ErrorEvent]:
        lines, frames, exceptions, details = self.lines, self.frames, self.exceptions, self.details
        self._reset()
        if exceptions:
            kind, summary = "python-traceback", exceptions[-1]
            if m := _EXC.match(summary):
                details["exception"] = m["exc"]
                details["message"] = m["msg"] or ""
            if len(exceptions) > 1:
                details["chain"] = exceptions  # oldest (root cause) first
        elif "path" in details:
            kind, summary = "django-500", lines[0].text.strip()
        elif frames:
            kind, summary = "python-traceback", "Python traceback (no exception line seen)"
        else:
            kind, summary = "python-traceback", lines[0].text.strip()
        project = [f for f in frames if f.in_project]
        return [ErrorEvent(
            service=self.service,
            kind=kind,
            summary=summary,
            lines=lines,
            frames=frames,
            location=project[-1] if project else None,  # innermost project frame
            request_id=request_id_of(lines),
            details=details,
        )]
