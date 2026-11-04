"""HTTP responses that matter: 5xx, and 401/403 (auth trouble often starts in another service)."""

from __future__ import annotations

import re

from ..logbus import LogLine
from .base import ErrorEvent, Parser, find_request_id

# Common/combined log format, as printed by Django runserver, gunicorn, nginx, Vite proxies.
_ACCESS = re.compile(r'"(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[\d.]+" (?P<status>\d{3})\b')
# key=value structured logs (Rust tracing, tower-http, logfmt).
_KV_STATUS = re.compile(r"\b(?:status|status_code|http\.status_code)[=:]\s*\"?(?P<status>\d{3})\b")
_KV_METHOD = re.compile(r"\bmethod[=:]\s*\"?(?P<method>[A-Z]+)\b")
_KV_PATH = re.compile(r"\b(?:path|uri|url)[=:]\s*\"?(?P<path>/[^\s\"]*)")

AUTH_STATUSES = {401, 403}


class HttpParser(Parser):
    def feed(self, line: LogLine) -> tuple[bool, list[ErrorEvent]]:
        text = line.text
        if m := _ACCESS.search(text):
            method, path, status = m["method"], m["path"], int(m["status"])
        elif m := _KV_STATUS.search(text):
            status = int(m["status"])
            mm, pm = _KV_METHOD.search(text), _KV_PATH.search(text)
            method, path = (mm["method"] if mm else None), (pm["path"] if pm else None)
        else:
            return False, []

        if status >= 500:
            kind, severity = "http-5xx", "error"
        elif status in AUTH_STATUSES:
            kind, severity = "http-auth", "warning"
        else:
            return False, []
        request = " ".join(p for p in (method, path) if p) or "request"
        return True, [ErrorEvent(
            service=self.service,
            kind=kind,
            summary=f"{request} → {status}",
            lines=[line],
            request_id=find_request_id(text),
            severity=severity,  # type: ignore[arg-type]
            details={"method": method, "path": path, "status": status},
        )]
