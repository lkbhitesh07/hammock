"""Request IDs: accept the caller's X-Request-ID (or make one), log it, pass it on to pricing."""

import logging
import time
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
log = logging.getLogger("orders.requests")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get("X-Request-ID") or f"api-{uuid.uuid4().hex[:8]}"
        token = request_id_var.set(request_id)
        started = time.monotonic()
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            if request.path != "/health":
                ms = (time.monotonic() - started) * 1000
                log.info("%s %s -> %s (%.0fms)", request.method, request.path, response.status_code, ms)
            return response
        finally:
            request_id_var.reset(token)
