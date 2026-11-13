"""Spawn services, stream their output into the log bus, watch their health, stop them gracefully.

Each service runs as `bash -c <cmd>` in its own session (= its own process group), so a
stop reaches the whole tree: `nix develop -> cargo watch -> binary`, Vite's node workers,
Django's autoreloader child. Stopping escalates SIGINT (what Ctrl-C would send) ->
SIGTERM -> SIGKILL, and only gives up once the whole group is gone.

Startup: every service waits until all of its `depends_on` are ready (healthy, or running
if they have no health check), so independent branches start in parallel. Waiting never
times out: a dependency that is late (cold cargo build) or broken (compile error) is
reported, and its dependents start as soon as it recovers.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Callable
from enum import Enum

import httpx

from .config import Config, Service
from .health import probe
from .logbus import LogBus

_READ_LIMIT = 1 << 20  # webpack/vite can print very long lines
_POLL = 0.1
_PROBE_STARTING = 1.0  # seconds between probes until healthy
_PROBE_HEALTHY = 5.0  # once healthy, a cheap TCP connect every few seconds


class State(str, Enum):
    WAITING = "waiting"  # for dependencies
    STARTING = "starting"  # spawned, health check not passing yet
    RUNNING = "running"  # spawned, no health check configured
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"  # health check failing
    CHECKING = "checking"  # check-only, first probe pending
    STOPPING = "stopping"
    STOPPED = "stopped"
    EXITED = "exited"  # ended on its own
    FAILED = "failed"  # could not be spawned


READY_STATES = frozenset({State.RUNNING, State.HEALTHY})


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


class ServiceProcess:
    def __init__(
        self,
        name: str,
        spec: Service,
        bus: LogBus,
        client: httpx.AsyncClient,
        on_change: Callable[[ServiceProcess], None] = lambda _: None,
    ) -> None:
        self.name = name
        self.spec = spec
        self.bus = bus
        self.state = State.CHECKING if spec.check_only else State.WAITING
        self.ready = asyncio.Event()
        self.returncode: int | None = None
        self.started_at: float | None = None  # monotonic
        self._client = client
        self._on_change = on_change
        self._proc: asyncio.subprocess.Process | None = None
        self._pumps: list[asyncio.Task[None]] = []
        self._waiter: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def _set(self, state: State) -> None:
        self.state = state
        if state in READY_STATES:
            self.ready.set()
        else:
            self.ready.clear()
        self._on_change(self)

    async def start(self) -> None:
        if self.spec.check_only:
            if self._monitor_task is None:
                self._monitor_task = asyncio.create_task(self._monitor())
            return
        if self._proc is not None:
            return
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "BROWSER": "none", **self.spec.env}
        self._stopping = False
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "bash", "-c", self.spec.cmd,  # type: ignore[arg-type]  # not check-only
                cwd=self.spec.cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=_READ_LIMIT,
            )
        except OSError as e:
            self.bus.info(f"✗ {self.name}: could not start: {e}")
            self._set(State.FAILED)
            return
        self.started_at = time.monotonic()
        self.returncode = None
        self.bus.info(f"▶ {self.name} started (pid {self._proc.pid}): {self.spec.cmd}")
        self._set(State.STARTING if self.spec.health else State.RUNNING)
        self._pumps = [
            asyncio.create_task(self._pump(self._proc.stdout, "stdout")),  # type: ignore[arg-type]
            asyncio.create_task(self._pump(self._proc.stderr, "stderr")),  # type: ignore[arg-type]
        ]
        self._waiter = asyncio.create_task(self._wait(self._proc))
        if self.spec.health:
            self._monitor_task = asyncio.create_task(self._monitor())

    async def _pump(self, stream: asyncio.StreamReader, kind: str) -> None:
        while True:
            try:
                chunk = await stream.readline()
            except ValueError:  # line longer than _READ_LIMIT: take what's buffered
                chunk = await stream.read(_READ_LIMIT)
            if not chunk:
                return
            text = chunk.decode(errors="replace").rstrip("\r\n")
            # Progress bars redraw with \r; keep only the final state of the line.
            text = text.rsplit("\r", 1)[-1]
            self.bus.publish(self.name, text, kind)  # type: ignore[arg-type]

    async def _wait(self, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        await asyncio.gather(*self._pumps, return_exceptions=True)
        self.returncode = code
        if not self._stopping:
            await self._cancel_monitor()
            self.bus.info(f"■ {self.name} exited on its own (code {code})")
            # Leftover children (e.g. a binary whose wrapper died) would hold ports.
            _signal_group(proc.pid, signal.SIGTERM)
            self._proc = None
            self._set(State.EXITED)

    async def _monitor(self) -> None:
        url = self.spec.health
        assert url is not None
        # Check-only services are supposed to be up already: complain on the first failure.
        limit = 0.0 if self.spec.check_only else self.spec.health_timeout
        began = time.monotonic()
        was_healthy = warned = False
        while True:
            healthy = self.state is State.HEALTHY
            ok = await probe(url, self._client, light=healthy)
            if ok and not healthy:
                if was_healthy:
                    self.bus.info(f"✓ {self.name} healthy again")
                elif self.spec.check_only:
                    self.bus.info(f"✓ {self.name} is up ({url})")
                else:
                    self.bus.info(f"✓ {self.name} healthy ({time.monotonic() - began:.1f}s)")
                was_healthy, warned = True, False
                self._set(State.HEALTHY)
            elif not ok and healthy:
                self.bus.info(f"✗ {self.name} health check failing ({url})")
                began = time.monotonic()
                self._set(State.UNHEALTHY)
            elif not ok and not warned and time.monotonic() - began >= limit:
                warned = True
                if self.spec.check_only:
                    self.bus.info(f"✗ {self.name} is not reachable at {url}; start it and its dependents will continue")
                else:
                    self.bus.info(f"… {self.name} not healthy after {limit:.0f}s ({url}); its dependents keep waiting")
                self._set(State.UNHEALTHY)
            await asyncio.sleep(_PROBE_HEALTHY if self.state is State.HEALTHY else _PROBE_STARTING)

    async def _cancel_monitor(self) -> None:
        task, self._monitor_task = self._monitor_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stop(self, force: bool = False) -> None:
        proc = self._proc
        if proc is None:
            await self._cancel_monitor()
            if self.state is State.WAITING:
                self._set(State.STOPPED)
            return
        pgid = proc.pid  # session leader: pgid == pid
        if self._stopping:
            # A graceful stop is already running; a forced stop just cuts it short.
            if force:
                _signal_group(pgid, signal.SIGKILL)
            return
        self._stopping = True
        await self._cancel_monitor()
        self._set(State.STOPPING)
        steps = [(signal.SIGKILL, 2.0)] if force else [
            (signal.SIGINT, self.spec.stop_timeout),
            (signal.SIGTERM, 5.0),
            (signal.SIGKILL, 2.0),
        ]
        previous: signal.Signals | None = None
        for sig, timeout in steps:
            if not _group_alive(pgid):
                break
            if previous is not None:
                self.bus.info(f"  {self.name} still running after {previous.name}; sending {sig.name}")
            _signal_group(pgid, sig)
            deadline = time.monotonic() + timeout
            while _group_alive(pgid) and time.monotonic() < deadline:
                await asyncio.sleep(_POLL)
            previous = sig
        if _group_alive(pgid):
            self.bus.info(f"✗ {self.name}: processes in group {pgid} survived SIGKILL")
        if self._waiter:
            await self._waiter
        self._proc = None
        self._stopping = False
        self._set(State.STOPPED)
        self.bus.info(f"■ {self.name} stopped")


class Runner:
    def __init__(self, config: Config, names: list[str], bus: LogBus) -> None:
        self.config = config
        self.bus = bus
        self.layers = config.layers(names)
        self.client = httpx.AsyncClient(follow_redirects=False)
        self.services = {
            n: ServiceProcess(n, config.services[n], bus, self.client, self._on_change)
            for layer in self.layers
            for n in layer
        }
        self.stopping = False
        # Called when a service exits on its own or can't be spawned (not on Hammock's own stops).
        self.on_exit: Callable[[ServiceProcess], None] | None = None
        self.all_ready = False
        self.all_ready_at: float | None = None  # wall clock of the last time the whole stack became ready
        self.first_ready_at: float | None = None  # wall clock of the first time this session
        self._t0 = time.monotonic()
        self._launches: list[asyncio.Task[None]] = []

    def _on_change(self, svc: ServiceProcess) -> None:
        if svc.state in (State.EXITED, State.FAILED) and not self.stopping:
            waiting = [n for n, s in self.services.items()
                       if svc.name in s.spec.depends_on and s.state is State.WAITING]
            if waiting:
                self.bus.info(f"  {', '.join(waiting)} {'is' if len(waiting) == 1 else 'are'} waiting on "
                              f"{svc.name}: fix it, then run `hammock restart {svc.name}`")
            if self.on_exit:
                self.on_exit(svc)
        ready = all(s.ready.is_set() for s in self.services.values())
        if ready and not self.all_ready:
            if self.all_ready_at is None:
                self.bus.info(f"✓ stack ready in {time.monotonic() - self._t0:.1f}s")
            else:
                self.bus.info("✓ stack ready again")
            self.all_ready_at = time.time()
            if self.first_ready_at is None:
                self.first_ready_at = self.all_ready_at
        self.all_ready = ready

    async def up(self) -> None:
        """Launch everything; returns at once while services wait on their dependencies."""
        self._t0 = time.monotonic()
        self._launches = [asyncio.create_task(self._launch(s)) for s in self.services.values()]

    async def _launch(self, svc: ServiceProcess) -> None:
        deps = [self.services[d] for d in svc.spec.depends_on]
        pending = [d.name for d in deps if not d.ready.is_set()]
        if pending and not svc.spec.check_only:
            self.bus.info(f"⏸ {svc.name} waiting for {', '.join(pending)}")
        await asyncio.gather(*(d.ready.wait() for d in deps))
        await svc.start()

    async def restart(self, name: str) -> None:
        svc = self.services[name]
        self.bus.info(f"↻ restarting {name}")
        await svc.stop()
        await svc.start()
        dependents = [n for n, s in self.services.items() if name in s.spec.depends_on and s.pid]
        if dependents and svc.spec.health:
            self.bus.info(f"  {', '.join(dependents)} may see errors until {name} is healthy")

    async def down(self, force: bool = False) -> None:
        self.stopping = True
        for task in self._launches:
            task.cancel()
        for layer in reversed(self.layers):
            await asyncio.gather(*(self.services[n].stop(force) for n in layer))
        if not force:
            await self.client.aclose()
