from pathlib import Path

import pytest

from hammock.detectors import DetectorHub, ErrorEvent, ServiceDetector
from hammock.logbus import LogLine, strip_ansi

FIXTURES = Path(__file__).parent / "fixtures"


def make_lines(service: str, texts: list[str], start: float = 100.0, step: float = 0.001) -> list[LogLine]:
    return [LogLine(service, t, strip_ansi(t), "stderr", start + i * step, 1e9 + i * step)
            for i, t in enumerate(texts)]


def detect(service: str, texts: list[str]) -> list[ErrorEvent]:
    detector = ServiceDetector(service)
    events = [e for line in make_lines(service, texts) for e in detector.feed(line)]
    return events + detector.flush()


def detect_fixture(service: str, name: str) -> list[ErrorEvent]:
    return detect(service, (FIXTURES / name).read_text().splitlines())


# --- Python / Django -------------------------------------------------------------------------

def test_django_keyerror_bug1():
    events = detect_fixture("api", "python/django_keyerror.log")
    assert [e.kind for e in events] == ["python-traceback", "http-5xx"]
    tb, access = events

    assert tb.summary == "KeyError: 'total'"
    assert tb.details["exception"] == "KeyError"
    assert tb.details["path"] == "/api/orders"
    assert tb.request_id == "web-d15af5e8"
    assert tb.location is not None
    assert tb.location.file.endswith("demo/api/orders/views.py")
    assert (tb.location.line, tb.location.func) == (59, "build_order")
    assert [f.func for f in tb.project_frames] == ["orders", "build_order"]
    assert len(tb.frames) == 4  # the two Django frames are kept, marked as library code
    assert tb.lines[0].text.endswith("Internal Server Error: /api/orders")
    assert tb.lines[-1].text == "KeyError: 'total'"

    assert access.summary == "POST /api/orders → 500"
    assert access.details["status"] == 500


def test_django_chained_exception_reports_last_and_keeps_chain():
    events = detect_fixture("api", "python/django_chained.log")
    assert [e.kind for e in events] == ["python-traceback", "http-5xx"]
    tb = events[0]
    assert tb.summary == "urllib.error.URLError: <urlopen error [Errno 111] Connection refused>"
    assert tb.details["chain"][0] == "ConnectionRefusedError: [Errno 111] Connection refused"
    assert tb.request_id == "web-5e6f7a8b"
    # stdlib frames under /nix/store/.../lib/python3.12/ are not project code
    assert [(f.func, f.line) for f in tb.project_frames] == [("orders", 42), ("get_quote", 27)]
    assert tb.location is not None and tb.location.func == "get_quote"


def test_django_startup_syntax_error():
    events = detect_fixture("api", "python/django_syntax_error.log")
    assert len(events) == 1
    tb = events[0]
    assert tb.summary == "SyntaxError: expected ':'"
    assert tb.details["thread"] == "django-main-thread"
    assert tb.lines[0].text == "Exception in thread django-main-thread:"
    assert tb.location is not None
    assert tb.location.file.endswith("orders/views.py")
    assert (tb.location.line, tb.location.func) == (52, None)  # SyntaxError frames have no function


def test_traceback_does_not_swallow_the_next_log_line():
    events = detect("api", [
        "Traceback (most recent call last):",
        '  File "/srv/app/views.py", line 3, in index',
        "    1 / 0",
        "ZeroDivisionError: division by zero",
        "ERROR something else went wrong",
    ])
    assert [e.summary for e in events] == ["ZeroDivisionError: division by zero", "ERROR something else went wrong"]


def test_django_header_without_traceback():
    events = detect("api", ["ERROR Internal Server Error: /checkout", "INFO all good"])
    assert [(e.kind, e.details.get("path")) for e in events] == [("django-500", "/checkout")]


# --- Rust ------------------------------------------------------------------------------------

def test_rust_panic_with_backtrace():
    events = detect_fixture("pricing", "rust/panic_backtrace.log")
    assert len(events) == 1
    panic = events[0]
    assert panic.kind == "rust-panic"
    assert panic.details["thread"] == "tokio-rt-worker"
    assert panic.details["message"] == "called `Option::unwrap()` on a `None` value"
    assert str(panic.location) == "src/main.rs:59:43"
    assert panic.summary == "panicked at src/main.rs:59:43: called `Option::unwrap()` on a `None` value"
    assert [(f.func, f.line) for f in panic.project_frames] == [
        (None, 59), ("pricing::quote::{{closure}}", 59), ("pricing::request_log::{{closure}}", 90)]
    assert panic.lines[-1].text.startswith("note: Some details are omitted")


def test_rust_panic_old_format_and_no_backtrace():
    events = detect("svc", [
        "thread 'main' panicked at 'index out of bounds', src/lib.rs:10:5",
        "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace",
        "2026-11-04T19:42:03Z  INFO next",
    ])
    assert len(events) == 1
    assert events[0].details["message"] == "index out of bounds"
    assert str(events[0].location) == "src/lib.rs:10:5"


def test_rust_panic_without_note_ends_at_next_log_line():
    events = detect("svc", [
        "thread 'tokio-rt-worker' (99) panicked at src/main.rs:5:9:",
        "boom",
        "2026-11-04T19:42:03.565494Z ERROR request failed request_id=web-1",
    ])
    assert [e.kind for e in events] == ["rust-panic", "error-line"]
    assert events[0].details["message"] == "boom"
    assert events[1].request_id == "web-1"


def test_rust_compile_error():
    events = detect_fixture("pricing", "rust/compile_error.log")
    assert len(events) == 1
    err = events[0]
    assert err.kind == "rust-compile-error"
    assert err.summary == "struct `Quote` has no field named `total`"
    assert err.details["errors"] == [{"code": "E0560", "message": "struct `Quote` has no field named `total`"}]
    assert err.details["crate"] == "pricing"
    assert str(err.location) == "src/main.rs:71:9"
    assert err.lines[-1].text.startswith("error: could not compile")


def test_rust_compile_counts_multiple_errors():
    events = detect("pricing", [
        "error[E0425]: cannot find value `x` in this scope",
        " --> src/a.rs:1:1",
        "",
        "error[E0308]: mismatched types",
        " --> src/b.rs:2:2",
        "",
        "error: could not compile `pricing` (bin \"pricing\") due to 2 previous errors",
    ])
    assert len(events) == 1
    assert events[0].summary == "cannot find value `x` in this scope (+1 more)"
    assert [str(f) for f in events[0].frames] == ["src/a.rs:1:1", "src/b.rs:2:2"]


def test_healthy_pricing_output_has_no_events():
    assert detect_fixture("pricing", "rust/pricing_ok.log") == []


# --- HTTP and generic lines ------------------------------------------------------------------

@pytest.mark.parametrize("line, kind, summary", [
    ('[04/Nov/2026 13:42:03] "POST /api/orders HTTP/1.1" 500 93299', "http-5xx", "POST /api/orders → 500"),
    ('INFO request request_id=web-1 method=POST path="/quote" status=502 ms=3', "http-5xx", "POST /quote → 502"),
    ('[04/Nov/2026 13:42:03] "GET /api/me HTTP/1.1" 401 12', "http-auth", "GET /api/me → 401"),
])
def test_http_lines(line, kind, summary):
    [event] = detect("api", [line])
    assert (event.kind, event.summary) == (kind, summary)


@pytest.mark.parametrize("line", [
    '[04/Nov/2026 13:42:03] "GET /health HTTP/1.1" 200 16',
    '[04/Nov/2026 13:42:03] "GET /missing HTTP/1.1" 404 179',
    'INFO request request_id=web-1 method=POST path="/quote" status=200 ms=0',
])
def test_http_lines_that_are_fine(line):
    assert detect("api", [line]) == []


@pytest.mark.parametrize("line, summary", [
    ("Error: That port is already in use.", "Error: That port is already in use."),
    ("2026-11-04T19:42:03Z ERROR pricing: db pool exhausted", "ERROR pricing: db pool exhausted"),
    ("2026/11/04 19:42:03.000 [E] [auth.go:42] invalid client secret", "[E] [auth.go:42] invalid client secret"),
])
def test_generic_error_lines(line, summary):
    [event] = detect("svc", [line])
    assert (event.kind, event.summary) == ("error-line", summary)


@pytest.mark.parametrize("line", [
    "WARNING: This is a development server.",
    "INFO computed 0 errors",
    "Performing system checks...",
])
def test_lines_that_are_not_errors(line):
    assert detect("svc", [line]) == []


# --- hub -------------------------------------------------------------------------------------

def test_hub_flushes_quiet_blocks_and_skips_hammock_lines():
    found: list[ErrorEvent] = []
    hub = DetectorHub(found.append, idle=0.5)
    hub(make_lines("hammock", ["ERROR this is Hammock talking"])[0])
    for line in make_lines("pricing", ["thread 'main' panicked at src/main.rs:1:1:", "boom"], start=10.0):
        hub(line)
    hub.flush(now=10.2)
    assert found == []  # still within the idle window: more backtrace may follow
    hub.flush(now=11.0)
    assert [(e.service, e.kind) for e in found] == [("pricing", "rust-panic")]


def test_hub_survives_a_broken_consumer():
    def explode(_: ErrorEvent) -> None:
        raise RuntimeError("consumer bug")

    hub = DetectorHub(explode)
    hub(make_lines("api", ["Error: boom"])[0])  # must not raise
