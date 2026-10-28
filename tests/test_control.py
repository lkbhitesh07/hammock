import asyncio
from pathlib import Path

import pytest

from hammock.config import Config
from hammock.control import AlreadyRunning, ControlServer, NotRunning, request
from hammock.logbus import LogBus
from hammock.runner import Runner, State


def test_status_restart_down_round_trip(tmp_path: Path):
    sock = tmp_path / "c.sock"

    async def go():
        cfg = Config.model_validate({"services": {
            "api": {"cmd": "sleep 30", "cwd": str(tmp_path)},
            "db": {"health": "tcp://127.0.0.1:1"},
        }})
        runner = Runner(cfg, list(cfg.services), LogBus())
        stop, stopped = asyncio.Event(), asyncio.Event()
        server = ControlServer(sock, tmp_path / "hammock.yaml", runner, stop.set, stopped)
        await server.start()
        await runner.up()
        while runner.services["api"].state is not State.RUNNING:
            await asyncio.sleep(0.05)

        with pytest.raises(AlreadyRunning):
            await ControlServer(sock, tmp_path, runner, stop.set, stopped).start()

        status = await request(sock, {"cmd": "status"})
        old_pid = runner.services["api"].pid
        restarted = await request(sock, {"cmd": "restart", "service": "api"})
        bad = await request(sock, {"cmd": "restart", "service": "db"})
        unknown = await request(sock, {"cmd": "restart", "service": "nope"})

        async def serve_down():  # what `hammock up` does when asked to stop
            await stop.wait()
            await runner.down()
            stopped.set()
            await server.close()

        downer = asyncio.create_task(serve_down())
        down = await request(sock, {"cmd": "down"})
        await downer
        with pytest.raises(NotRunning):
            await request(sock, {"cmd": "status"})
        return status, old_pid, runner.services["api"], restarted, bad, unknown, down

    status, old_pid, api, restarted, bad, unknown, down = asyncio.run(go())
    by_name = {s["name"]: s for s in status["services"]}
    assert by_name["api"]["state"] == "running" and by_name["api"]["pid"] == old_pid
    assert by_name["db"]["check_only"] is True
    assert restarted == {"ok": True, "state": "running"}
    assert not bad["ok"] and "check-only" in bad["error"]
    assert not unknown["ok"]
    assert down == {"ok": True}
    assert api.state is State.STOPPED
    assert not sock.exists()


def test_stale_socket_is_replaced(tmp_path: Path):
    sock = tmp_path / "c.sock"
    sock.touch()  # left behind by a crashed run

    async def go():
        cfg = Config.model_validate({"services": {"db": {"health": "tcp://127.0.0.1:1"}}})
        server = ControlServer(sock, tmp_path, Runner(cfg, ["db"], LogBus()), lambda: None, asyncio.Event())
        await server.start()
        response = await request(sock, {"cmd": "ping"})
        await server.close()
        return response

    assert asyncio.run(go()) == {"ok": True}
