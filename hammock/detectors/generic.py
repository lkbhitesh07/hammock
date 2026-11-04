"""Single error lines from any language: `ERROR` log levels, `Error: …` messages, Go `[E]` lines.

Runs last, so lines already claimed by a more specific parser never reach it.
"""

from __future__ import annotations

import re

from ..logbus import LogLine
from .base import ErrorEvent, Parser, find_request_id

_LEVEL = re.compile(r"(?:^|[\s\[|])(?:ERROR|CRITICAL|FATAL)(?=$|[\s\]:|])")
_PREFIX = re.compile(r"^(?:Error|error|fatal|FATAL|panic)(?:\[\w+\])?: ")
_GO_LEVEL = re.compile(r"\[E\]")  # beego (Casdoor) style
_TIMESTAMP = re.compile(r"^\[?\d{2,4}[-/]\S+[ T]\d{2}:\d{2}:\d{2}\S*\]?\s+")
_MAX_SUMMARY = 300


class GenericErrorParser(Parser):
    def feed(self, line: LogLine) -> tuple[bool, list[ErrorEvent]]:
        text = line.text
        if not (_LEVEL.search(text) or _PREFIX.match(text) or _GO_LEVEL.search(text)):
            return False, []
        summary = _TIMESTAMP.sub("", text.strip())[:_MAX_SUMMARY]
        return True, [ErrorEvent(
            service=self.service,
            kind="error-line",
            summary=summary,
            lines=[line],
            request_id=find_request_id(text),
        )]
