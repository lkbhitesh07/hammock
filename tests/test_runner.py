import asyncio
import os
import socket
from pathlib import Path

import httpx

from hammock.config import Config
from hammock.health import probe
from hammock.logbus import LogBus
from hammock.runner import Runner, State, _group_alive


def run(coro):
    return asyncio.run(coro)


def make_runner(tmp_path: Path, services: dict) -> tuple[Runner, LogBus]:
    for svc in services.values():
        svc.setdefault("cwd", str(tmp_path))
    cfg = Config.model_validate({"services": services})
    bus = LogBus()
    return Runner(cfg, list(cfg.services), bus), bus


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.05)


def test_output_is_streamed_per_service(tmp_path):
    async def go():
        runner, bus = make_runner(tmp_path, {
            "a": {"cmd": "echo hello; echo oops >&2; sleep 30"},
            "b": {"cmd": "printf 'x\\ry\\n'; sleep 30", "depends_on": ["a"]},
        })
        await runner.up()
        await wait_for(lambda: len(bus.lines("a")) == 2 and bus.lines("b"))
        await runner.down()
        return bus

    bus = run(go())
    assert {(l.text, l.stream) for l in bus.lines("a")} == {("hello", "stdout"), ("oops", "stderr")}
    assert bus.lines("b")[0].text == "y"  # \r progress redraws keep the final state


def test_stop_kills_grandchildren_that_ignore_signals(tmp_path):
    # The grandchild ignores SIGINT and SIGTERM, like a misbehaving wrapper's child would.
    async def go():
        runner, bus = make_runner(tmp_path, {
            "stubborn": {
                "cmd": "bash -c 'trap \"\" INT TERM; sleep 60' & echo $! > child.pid; wait",
                "stop_timeout": 0.5,
            },
        })
        await runner.up()
        await wait_for(lambda: (tmp_path / "child.pid").exists() and (tmp_path / "child.pid").read_text().strip())
        svc = runner.services["stubborn"]
        pgid = svc.pid
        await runner.down()
        return svc, pgid, bus

    svc, pgid, bus = run(go())
    assert svc.state is State.STOPPED
    assert not _group_alive(pgid)
    child = int((tmp_path / "child.pid").read_text())
    assert not os.path.exists(f"/proc/{child}")
    assert any("sending SIGKILL" in l.text for l in bus.lines("hammock"))


def test_service_that_exits_is_reported(tmp_path):
    async def go():
        runner, bus = make_runner(tmp_path, {"boom": {"cmd": "echo bye; exit 3"}})
        await runner.up()
        svc = runner.services["boom"]
        await wait_for(lambda: svc.state is State.EXITED)
        await runner.down()
        return svc, bus

    svc, bus = run(go())
    assert svc.returncode == 3
    assert any("exited on its own (code 3)" in l.text for l in bus.lines("hammock"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_server_cmd(port: int, delay: float = 0) -> str:
    return f"sleep {delay}; exec python3 -m http.server {port} --bind 127.0.0.1"


def hammock_lines(bus: LogBus) -> list[str]:
    return [l.text for l in bus.lines("hammock")]


def test_check_only_services_are_not_spawned(tmp_path):
    port = free_port()

    async def go():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", port)
        runner, bus = make_runner(tmp_path, {"db": {"health": f"tcp://127.0.0.1:{port}"}})
        await runner.up()
        db = runner.services["db"]
        await wait_for(lambda: db.state is State.HEALTHY)
        await runner.down()
        server.close()
        return db, bus

    db, bus = run(go())
    assert db.pid is None
    assert any(f"db is up (tcp://127.0.0.1:{port})" in t for t in hammock_lines(bus))


def test_dependents_wait_for_health(tmp_path):
    port = free_port()

    async def go():
        runner, bus = make_runner(tmp_path, {
            "pricing": {"cmd": http_server_cmd(port, delay=0.5), "health": f"http://127.0.0.1:{port}/"},
            "api": {"cmd": "sleep 30", "depends_on": ["pricing"]},
        })
        await runner.up()
        await wait_for(lambda: runner.services["api"].state is State.RUNNING)
        assert runner.all_ready and runner.all_ready_at
        await runner.down()
        return bus

    lines = hammock_lines(run(go()))
    healthy = next(i for i, t in enumerate(lines) if t.startswith("✓ pricing healthy"))
    started = next(i for i, t in enumerate(lines) if t.startswith("▶ api started"))
    assert any(t == "⏸ api waiting for pricing" for t in lines)
    assert healthy < started


def test_dependents_start_once_a_missing_dependency_appears(tmp_path):
    port = free_port()

    async def go():
        runner, bus = make_runner(tmp_path, {
            "db": {"health": f"tcp://127.0.0.1:{port}"},
            "api": {"cmd": "sleep 30", "depends_on": ["db"]},
        })
        await runner.up()
        await wait_for(lambda: runner.services["db"].state is State.UNHEALTHY)
        assert runner.services["api"].state is State.WAITING
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", port)
        await wait_for(lambda: runner.services["api"].state is State.RUNNING)
        await runner.down()
        server.close()
        return bus

    lines = hammock_lines(run(go()))
    assert any("db is not reachable" in t for t in lines)


def test_health_failure_and_recovery_are_reported(tmp_path):
    port = free_port()

    async def go():
        runner, bus = make_runner(tmp_path, {
            "pricing": {"cmd": http_server_cmd(port), "health": f"http://127.0.0.1:{port}/"},
        })
        await runner.up()
        svc = runner.services["pricing"]
        await wait_for(lambda: svc.state is State.HEALTHY)
        await runner.restart("pricing")  # port closes, then comes back
        await wait_for(lambda: svc.state is State.HEALTHY)
        first_pid = svc.pid
        await runner.down()
        return bus, first_pid

    bus, pid = run(go())
    lines = hammock_lines(bus)
    assert "↻ restarting pricing" in lines
    assert sum(t.startswith("✓ pricing healthy") for t in lines) == 2
    assert pid is not None


def test_http_5xx_is_unhealthy_and_4xx_is_healthy():
    async def go():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(int(request.url.path.strip("/")))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [await probe(f"http://svc:1/{code}", client) for code in (200, 401, 404, 500, 503)]

    assert run(go()) == [True, True, True, False, False]
