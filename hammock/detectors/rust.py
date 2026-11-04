"""Rust panics (with or without RUST_BACKTRACE) and cargo/rustc compile errors."""

from __future__ import annotations

import re

from ..logbus import LogLine
from .base import ErrorEvent, Frame, Parser, append_capped, request_id_of

# Rust >= 1.73 puts the message on the next line; newer versions add the thread id: `'main' (1234)`.
_PANIC = re.compile(
    r"^thread '(?P<thread>[^']*)'(?: \(\d+\))? panicked at "
    r"(?:'(?P<oldmsg>.*)', )?(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):?\s*$"
)
_BT_START = re.compile(r"^stack backtrace:\s*$")
_BT_FUNC = re.compile(r"^\s*\d+: (?P<func>.+)$")
_BT_AT = re.compile(r"^\s+at (?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+)$")
_NOTE = re.compile(r"^note: (?:run with `RUST_BACKTRACE|Some details are omitted)")
_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d[T ]\d")  # a timestamped log line ends a message
_MAX_MESSAGE_LINES = 20

_COMPILE_ERR = re.compile(r"^error(?:\[(?P<code>E\d{4})\])?: (?P<msg>.+)$")
_COULD_NOT = re.compile(r"^error: could not compile `(?P<crate>[^`]+)`")
_ARROW = re.compile(r"^\s*--> (?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+)$")
# Lines that can appear inside a rustc diagnostic.
_DIAG = re.compile(r"^(?:\s|$|error|warning|help:|note:|For more information|Some errors have|\d+\s*\|)")

_LIBRARY = re.compile(r"^/rustc/|/\.cargo/registry/|/\.cargo/git/|/rustlib/src/rust/")


def _frame(file: str, line: str, col: str, func: str | None = None) -> Frame:
    file = file.removeprefix("./")
    return Frame(file, int(line), func, int(col), not _LIBRARY.search(file))


class RustPanicParser(Parser):
    def __init__(self, service: str) -> None:
        super().__init__(service)
        self._reset()

    def _reset(self) -> None:
        self.state = "idle"  # message -> backtrace
        self.lines: list[LogLine] = []
        self.message: list[str] = []
        self.site: Frame | None = None
        self.frames: list[Frame] = []
        self.func: str | None = None
        self.thread = ""

    @property
    def open(self) -> bool:
        return self.state != "idle"

    def feed(self, line: LogLine) -> tuple[bool, list[ErrorEvent]]:
        text = line.text
        if self.state == "idle":
            return self._start(line), []

        if _NOTE.match(text):
            append_capped(self.lines, line)
            return True, self._close()

        if self.state == "message":
            if _BT_START.match(text):
                append_capped(self.lines, line)
                self.state = "backtrace"
                return True, []
            if _PANIC.match(text) or _LOG_LINE.match(text) or len(self.message) >= _MAX_MESSAGE_LINES:
                return self._close_and_retry(line)
            append_capped(self.lines, line)
            self.message.append(text)
            return True, []

        # backtrace
        if m := _BT_FUNC.match(text):
            self.func = m["func"]
        elif m := _BT_AT.match(text):
            self.frames.append(_frame(m["file"], m["line"], m["col"], self.func))
        else:
            return self._close_and_retry(line)
        append_capped(self.lines, line)
        return True, []

    def flush(self) -> list[ErrorEvent]:
        return self._close() if self.open else []

    def _start(self, line: LogLine) -> bool:
        m = _PANIC.match(line.text)
        if not m:
            return False
        self.state = "message"
        self.thread = m["thread"]
        self.site = _frame(m["file"], m["line"], m["col"])
        if m["oldmsg"]:
            self.message.append(m["oldmsg"])
        self.lines.append(line)
        return True

    def _close_and_retry(self, line: LogLine) -> tuple[bool, list[ErrorEvent]]:
        events = self._close()
        return self._start(line), events

    def _close(self) -> list[ErrorEvent]:
        lines, site, frames, message, thread = self.lines, self.site, self.frames, self.message, self.thread
        self._reset()
        assert site is not None
        project = [f for f in frames if f.in_project]
        location = site if site.in_project else (project[0] if project else None)
        msg = next((m.strip() for m in message if m.strip()), "")
        return [ErrorEvent(
            service=self.service,
            kind="rust-panic",
            summary=f"panicked at {site}: {msg}" if msg else f"panicked at {site}",
            lines=lines,
            frames=[site, *frames],
            location=location,
            request_id=request_id_of(lines),
            details={"thread": thread, "message": "\n".join(message).strip()},
        )]


class RustCompileParser(Parser):
    """Collects every diagnostic of one failed build into a single event."""

    def __init__(self, service: str) -> None:
        super().__init__(service)
        self._reset()

    def _reset(self) -> None:
        self.active = False
        self.lines: list[LogLine] = []
        self.errors: list[tuple[str | None, str]] = []  # (code, message)
        self.frames: list[Frame] = []
        self.crate: str | None = None

    @property
    def open(self) -> bool:
        return self.active

    def feed(self, line: LogLine) -> tuple[bool, list[ErrorEvent]]:
        text = line.text
        if not self.active:
            if not _COMPILE_ERR.match(text):
                return False, []
            self.active = True
        elif not _DIAG.match(text):
            events = self._close()
            consumed, more = self.feed(line)
            return consumed, events + more

        append_capped(self.lines, line)
        if m := _COULD_NOT.match(text):
            self.crate = m["crate"]
            return True, self._close()
        if m := _COMPILE_ERR.match(text):
            self.errors.append((m["code"], m["msg"]))
        elif (m := _ARROW.match(text)) and len(self.frames) < len(self.errors):
            self.frames.append(_frame(m["file"], m["line"], m["col"]))  # the first --> of each error
        return True, []

    def flush(self) -> list[ErrorEvent]:
        return self._close() if self.active else []

    def _close(self) -> list[ErrorEvent]:
        lines, errors, frames, crate = self.lines, self.errors, self.frames, self.crate
        self._reset()
        is_rustc = bool(frames or crate or any(code for code, _ in errors))
        if not errors:  # only the "could not compile" line (e.g. a linker failure)
            errors = [(None, lines[0].text.removeprefix("error: "))]
        summary = errors[0][1] + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else "")
        project = [f for f in frames if f.in_project]
        return [ErrorEvent(
            service=self.service,
            kind="rust-compile-error" if is_rustc else "error-line",
            summary=summary,
            lines=lines,
            frames=frames,
            location=project[0] if project else None,
            request_id=None,
            details={"errors": [{"code": c, "message": m} for c, m in errors], "crate": crate},
        )]
