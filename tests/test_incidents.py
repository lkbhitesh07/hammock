import asyncio
import json

from hammock.config import Config
from hammock.correlate import build_timeline, health_paths
from hammock.detectors import ServiceDetector
from hammock.incidents import Incident, IncidentTracker, save_incident, service_exit_event
from hammock.logbus import LogLine
from hammock.runner import Runner, State
from hammock.logbus import LogBus


class Stack:
    """A fake multi-service log stream with controllable timestamps."""

    def __init__(self) -> None:
        self.lines: list[LogLine] = []
        self.detectors: dict[str, ServiceDetector] = {}
        self.events = []

    def say(self, t: float, service: str, *texts: str) -> None:
        for i, text in enumerate(texts):
            line = LogLine(service, text, text, "stderr", t + i * 0.001, 1.7e9 + t)
            self.lines.append(line)
            if service != "hammock":
                det = self.detectors.setdefault(service, ServiceDetector(service))
                self.events += det.feed(line)

    def flush(self) -> list:
        events = self.events + [e for d in self.detectors.values() for e in d.flush()]
        self.events = []
        return events


BUG1_API = [
    "2026-11-04 13:42:03,647 ERROR [django.request] request_id=web-d15af5e8 Internal Server Error: /api/orders",
    "Traceback (most recent call last):",
    '  File "/srv/demo/api/orders/views.py", line 59, in build_order',
    '    "total_cents": quote["total"],',
    "KeyError: 'total'",
    "2026-11-04 13:42:03,649 INFO [orders.requests] request_id=web-d15af5e8 POST /api/orders -> 500 (86ms)",
    '[04/Nov/2026 13:42:03] "POST /api/orders HTTP/1.1" 500 93299',
]


def bug1(stack: Stack, t: float = 100.0) -> None:
    stack.say(t - 30, "pricing", "INFO quote computed request_id=web-d15af5e8 items=2")  # long before: request ID only
    stack.say(t - 20, "pricing", "INFO unrelated line far away")
    stack.say(t - 2, "api", '[04/Nov/2026 13:42:01] "GET /health HTTP/1.1" 200 16')
    stack.say(t - 1, "hammock", "↻ restarting pricing")
    stack.say(t - 0.1, "pricing", 'INFO request request_id=web-d15af5e8 method=POST path="/quote" status=200 ms=0')
    stack.say(t, "api", *BUG1_API)


# --- correlation -----------------------------------------------------------------------------

def test_timeline_follows_request_ids_and_the_time_window():
    stack = Stack()
    bug1(stack)
    events = stack.flush()
    timeline = build_timeline(stack.lines, events, window=5.0, noise_paths={"/health"})
    got = [(t.line.service, t.why, t.line.text[:30]) for t in timeline]

    assert got[0] == ("pricing", "request", "INFO quote computed request_id")  # 30s back, matched by ID
    assert ("hammock", "window", "↻ restarting pricing") in got
    assert not any("unrelated" in text for _, _, text in got)  # 20s back, no ID: out of the window
    assert not any("/health" in text for _, _, text in got)  # health checks are noise
    assert [w for _, w, _ in got].count("error") == len(BUG1_API) - 1  # all error lines except the INFO line
    assert [t.line.mono for t in timeline] == sorted(t.line.mono for t in timeline)


def test_failing_health_checks_are_not_noise():
    stack = Stack()
    stack.say(10, "api", '[04/Nov/2026] "GET /health HTTP/1.1" 503 16', "Error: db down")
    events = stack.flush()
    timeline = build_timeline(stack.lines, events, window=5.0, noise_paths={"/health"})
    assert [t.line.text for t in timeline][0].endswith("503 16")


def test_health_paths_from_config():
    cfg = Config.model_validate({"services": {
        "a": {"cmd": "x", "health": "http://localhost:1/health"},
        "b": {"cmd": "x", "health": "http://localhost:2/"},
        "c": {"cmd": "x", "health": "tcp://localhost:3"},
    }})
    assert health_paths(cfg) == {"/health"}


# --- grouping --------------------------------------------------------------------------------

def make_tracker(stack: Stack, **kw) -> tuple[IncidentTracker, list[Incident]]:
    found: list[Incident] = []
    tracker = IncidentTracker(lambda: stack.lines, found.append, window=5.0, noise_paths={"/health"}, **kw)
    return tracker, found


def test_one_bug_becomes_one_incident_after_it_settles():
    stack = Stack()
    tracker, found = make_tracker(stack, settle=1.5)
    bug1(stack)
    for e in stack.flush():
        tracker.add(e, now=100.0)

    tracker.tick(now=101.0)
    assert found == []  # still settling
    tracker.tick(now=101.6)
    [incident] = found
    assert [e.kind for e in incident.events] == ["python-traceback", "http-5xx"]
    assert incident.primary.summary == "KeyError: 'total'"
    assert incident.request_ids == ["web-d15af5e8"]
    assert incident.request_services == ["pricing", "api"]
    assert incident.services == ["api"]


def test_errors_far_apart_are_separate_incidents():
    stack = Stack()
    tracker, found = make_tracker(stack)
    stack.say(10, "api", "Error: first")
    for e in stack.flush():
        tracker.add(e, now=10)
    tracker.tick(now=12)
    stack.say(20, "pricing", "Error: second")
    for e in stack.flush():
        tracker.add(e, now=20)
    tracker.tick(now=22)
    assert [i.primary.summary for i in found] == ["Error: first", "Error: second"]
    assert [i.id for i in found] == [1, 2]


def test_a_steady_stream_of_errors_is_cut_at_max_open():
    stack = Stack()
    tracker, found = make_tracker(stack, settle=1.5, max_open=10)
    for t in range(0, 12):
        stack.say(t, "api", f"Error: tick {t}")
        for e in stack.flush():
            tracker.add(e, now=t)
        tracker.tick(now=t + 0.5)
    assert len(found) == 1 and len(found[0].events) == 11


def test_repeats_within_cooldown_bump_the_counter():
    stack = Stack()
    tracker, found = make_tracker(stack, cooldown=30)
    for t in (100.0, 110.0, 150.0):
        bug1(stack, t)
        for e in stack.flush():
            tracker.add(e, now=t)
        tracker.tick(now=t + 2)
    assert [(i.id, i.repeats) for i in found] == [(1, 2), (1, 2), (2, 1)]
    assert found[0] is found[1]


def test_auth_failures_need_a_burst():
    stack = Stack()
    tracker, found = make_tracker(stack, auth_burst=3)
    stack.say(10, "api", '"GET /api/me HTTP/1.1" 401 0')
    stack.say(11, "api", '"GET /api/me HTTP/1.1" 401 0')
    for e in stack.flush():
        tracker.add(e, now=11)
    tracker.tick(now=20)
    assert found == []
    stack.say(30, "api", '"GET /api/me HTTP/1.1" 401 0', '"GET /api/orders HTTP/1.1" 403 0')
    stack.say(30.5, "web", '"GET /api/me HTTP/1.1" 401 0')
    for e in stack.flush():
        tracker.add(e, now=30.5)
    tracker.tick(now=32.1)
    [incident] = found
    assert len(incident.events) == 3  # the two old 401s fell out of the window
    assert incident.services == ["api", "web"]


def test_compile_error_and_exit_are_one_incident():
    stack = Stack()
    tracker, found = make_tracker(stack)
    stack.say(50, "pricing",
              "error[E0560]: struct `Quote` has no field named `total`",
              "  --> src/main.rs:71:9",
              "error: could not compile `pricing` (bin \"pricing\") due to 1 previous error")
    for e in stack.flush():
        tracker.add(e, now=50)
    tracker.add(service_exit_event("pricing", 101), now=50.1)
    tracker.tick(now=52)
    [incident] = found
    assert [e.kind for e in incident.events] == ["rust-compile-error", "service-exited"]
    assert incident.primary.kind == "rust-compile-error"


def test_save_incident(tmp_path):
    stack = Stack()
    tracker, found = make_tracker(stack)
    bug1(stack)
    for e in stack.flush():
        tracker.add(e, now=100)
    tracker.tick(now=102)
    path = save_incident(found[0], tmp_path / "incidents")
    data = json.loads(path.read_text())
    assert data["primary"]["summary"] == "KeyError: 'total'"
    assert data["primary"]["location"] == "/srv/demo/api/orders/views.py:59 in build_order"
    assert data["request_services"] == ["pricing", "api"]
    assert {t["why"] for t in data["timeline"]} == {"error", "request", "window"}


def test_save_incident_never_raises(tmp_path):
    stack = Stack()
    tracker, found = make_tracker(stack)
    stack.say(1, "api", "Error: x")
    for e in stack.flush():
        tracker.add(e, now=1)
    tracker.tick(now=5)
    blocker = tmp_path / "file"
    blocker.write_text("")
    assert save_incident(found[0], blocker / "incidents") is None


# --- runner hook -----------------------------------------------------------------------------

def test_runner_reports_exits_but_not_its_own_stops(tmp_path):
    async def go():
        cfg = Config.model_validate({"services": {
            "boom": {"cmd": "exit 7", "cwd": str(tmp_path)},
            "calm": {"cmd": "sleep 30", "cwd": str(tmp_path)},
        }})
        runner = Runner(cfg, list(cfg.services), LogBus())
        exits = []
        runner.on_exit = lambda svc: exits.append((svc.name, svc.returncode))
        await runner.up()
        while runner.services["boom"].state is not State.EXITED:
            await asyncio.sleep(0.05)
        await runner.down()
        return exits

    assert asyncio.run(go()) == [("boom", 7)]


def test_exit_event_explains_itself_with_the_last_output():
    tail = [LogLine("pricing", t, t, "stderr", 1.0 + i, 1.7e9) for i, t in
            enumerate(["Compiling pricing", "", "bash: line 1: cargo: command not found"])]
    event = service_exit_event("pricing", 127, tail)
    assert event.summary == "pricing exited on its own (code 127): bash: line 1: cargo: command not found"
    assert "command not found" in event.details["hint"]
    assert [l.text for l in event.lines] == ["Compiling pricing", "bash: line 1: cargo: command not found"]


def test_exit_event_without_output():
    event = service_exit_event("svc", 3)
    assert event.summary == "svc exited on its own (code 3)"
    assert "hint" not in event.details and len(event.lines) == 1


def test_runner_tells_you_what_waits_on_a_dead_service(tmp_path):
    async def go():
        cfg = Config.model_validate({"services": {
            # A health check keeps api waiting (without one, pricing would count as ready once spawned).
            "pricing": {"cmd": "no-such-command-xyz", "cwd": str(tmp_path), "health": "tcp://127.0.0.1:9"},
            "api": {"cmd": "sleep 30", "cwd": str(tmp_path), "depends_on": ["pricing"]},
        }})
        bus = LogBus()
        runner = Runner(cfg, list(cfg.services), bus)
        await runner.up()
        while runner.services["pricing"].state is not State.EXITED:
            await asyncio.sleep(0.05)
        await runner.down()
        return runner, bus

    runner, bus = asyncio.run(go())
    assert runner.services["pricing"].returncode == 127
    assert any(l.text == "  api is waiting on pricing: fix it, then run `hammock restart pricing`"
               for l in bus.lines("hammock"))
