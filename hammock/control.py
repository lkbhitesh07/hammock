"""Unix-socket control channel so `hammock status/down/restart` work from another terminal.

One JSON request line in, one JSON response line out. The socket is keyed by the config
file's path, so several stacks can run side by side.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .runner import Runner


class NotRunning(Exception):
    pass


class AlreadyRunning(Exception):
    pass


def socket_path(config_path: Path) -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / f"hammock-{os.getuid()}"
    digest = hashlib.sha1(str(config_path.resolve()).encode()).hexdigest()[:12]
    return base / f"{digest}.sock"


async def request(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.open_unix_connection(path)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise NotRunning from e
    writer.write(json.dumps(payload).encode() + b"\n")
    await writer.drain()
    line = await reader.readline()
    writer.close()
    if not line:
        raise NotRunning
    return json.loads(line)


class ControlServer:
    def __init__(self, path: Path, config_path: Path, runner: Runner,
                 request_stop: Callable[[], None], stopped: asyncio.Event) -> None:
        self.path = path
        self.config_path = config_path
        self.runner = runner
        self.request_stop = request_stop
        self.stopped = stopped
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists():
            try:
                await request(self.path, {"cmd": "ping"})
            except (NotRunning, OSError, ValueError):
                self.path.unlink(missing_ok=True)  # stale socket from a crashed run
            else:
                raise AlreadyRunning
        self._server = await asyncio.start_unix_server(self._handle, self.path)
        os.chmod(self.path, 0o600)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        if self._handlers:  # let `hammock down` hear back before we exit
            await asyncio.wait(self._handlers, timeout=2)
        self.path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            try:
                req = json.loads(await reader.readline())
                response = await self._dispatch(req)
            except Exception as e:  # never let a bad request take the stack down
                response = {"ok": False, "error": str(e)}
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
            writer.close()
        except OSError:
            pass  # client went away
        finally:
            self._handlers.discard(task)

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True}
        if cmd == "status":
            return {"ok": True, "config": str(self.config_path), "services": self._status()}
        if cmd == "down":
            self.request_stop()
            await self.stopped.wait()
            return {"ok": True}
        if cmd == "restart":
            name = req.get("service")
            svc = self.runner.services.get(name)  # type: ignore[arg-type]
            if svc is None:
                return {"ok": False, "error": f"'{name}' is not part of the running stack"}
            if svc.spec.check_only:
                return {"ok": False, "error": f"'{name}' is check-only; Hammock doesn't manage its process"}
            if self.stopped.is_set() or self.runner.stopping:
                return {"ok": False, "error": "the stack is stopping"}
            await self.runner.restart(name)
            return {"ok": True, "state": svc.state.value}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def _status(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [
            {
                "name": name,
                "state": svc.state.value,
                "pid": svc.pid,
                "uptime": round(now - svc.started_at) if svc.started_at and svc.pid else None,
                "health": svc.spec.health,
                "check_only": svc.spec.check_only,
            }
            for name, svc in self.runner.services.items()
        ]
