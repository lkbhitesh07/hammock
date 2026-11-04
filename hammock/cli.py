"""Command line: `hammock up/down/status/restart/config`."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from .config import Config, ConfigError, load_config
from .console import LogPrinter
from .control import AlreadyRunning, ControlServer, NotRunning, request, socket_path
from .correlate import health_paths
from .detectors import DetectorHub
from .incidents import Incident, IncidentTracker, save_incident, service_exit_event
from .logbus import LogBus
from .runner import Runner

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Hammock 🦥 — one command for your local stack.")
err = Console(stderr=True)

ConfigOpt = Annotated[Optional[Path], typer.Option("--config", "-c", help="Path to hammock.yaml (default: search upwards).")]
WithOpt = Annotated[Optional[list[str]], typer.Option("--with", "-w", help="Also start this optional service (repeatable).")]


def _load(path: Path | None) -> tuple[Config, Path]:
    try:
        return load_config(path)
    except ConfigError as e:
        err.print(f"[red]✗[/] {e}")
        raise typer.Exit(2) from None


@app.command()
def up(config: ConfigOpt = None, with_: WithOpt = None) -> None:
    """Start the stack in dependency order and stream every service's logs. Ctrl-C stops it all."""
    cfg, path = _load(config)
    try:
        names = cfg.select(with_)
    except ConfigError as e:
        err.print(f"[red]✗[/] {e}")
        raise typer.Exit(2) from None
    asyncio.run(_up(cfg, path, names))


def _short_path(file: str, cfg: Config, service: str) -> str:
    svc = cfg.services.get(service)
    if svc and svc.cwd:
        try:
            return str(Path(file).relative_to(svc.cwd))
        except ValueError:
            pass
    return file


def _announce(bus: LogBus, cfg: Config, incidents_dir: Path, incident: Incident) -> None:
    # A compact summary until the incident card (render.py) exists.
    p = incident.primary
    if incident.repeats > 1:
        bus.info(f"🔁 incident #{incident.id} again (×{incident.repeats}): {p.service}: {p.summary}")
        return
    save_incident(incident, incidents_dir)
    bus.info(f"🔎 incident #{incident.id} · {p.service} · {p.summary}")
    if p.location:
        loc = p.location
        where = f"{_short_path(loc.file, cfg, p.service)}:{loc.line}" + (f" in {loc.func}" if loc.func else "")
        bus.info(f"   at {where}")
    if incident.request_ids:
        others = [s for s in incident.request_services if s != p.service]
        seen = f" · also seen in {', '.join(others)}" if others else ""
        bus.info(f"   request {', '.join(incident.request_ids)}{seen}")
    saved = f" · {incident.saved_to.relative_to(incidents_dir.parent.parent)}" if incident.saved_to else ""
    bus.info(f"   {len(incident.events)} events in {', '.join(incident.services)} · "
             f"{len(incident.timeline)} timeline lines{saved}")


async def _up(cfg: Config, path: Path, names: list[str]) -> None:
    bus = LogBus()
    bus.subscribe(LogPrinter(names))
    incidents_dir = path.parent / ".hammock" / "incidents"
    tracker = IncidentTracker(
        lambda: bus.window(float("-inf"), float("inf")),
        lambda incident: _announce(bus, cfg, incidents_dir, incident),
        window=cfg.correlation_window_seconds,
        noise_paths=health_paths(cfg),
    )
    hub = DetectorHub(tracker.add, names)
    bus.subscribe(hub)
    runner = Runner(cfg, names, bus)
    runner.on_exit = lambda svc: tracker.add(service_exit_event(svc.name, svc.returncode))
    background = [asyncio.create_task(hub.run()), asyncio.create_task(tracker.run())]
    stop = asyncio.Event()
    stopped = asyncio.Event()
    forced: list[asyncio.Task[None]] = []

    def request_stop() -> None:
        if not stop.is_set():
            bus.info("stopping all services gracefully… (Ctrl-C again to force)")
            stop.set()

    def on_signal() -> None:
        if not stop.is_set():
            request_stop()
        elif not forced:
            bus.info("forcing stop")
            forced.append(asyncio.create_task(runner.down(force=True)))

    control = ControlServer(socket_path(path), path, runner, request_stop, stopped)
    try:
        await control.start()
    except AlreadyRunning:
        err.print(f"[red]✗[/] Hammock is already running for {path}. Use `hammock status` or `hammock down`.")
        raise typer.Exit(1) from None

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_signal)

    bus.info(f"using {path}")
    bus.info("start order: " + "  →  ".join(", ".join(layer) for layer in runner.layers))
    try:
        await runner.up()
        await stop.wait()
        await runner.down()
        await asyncio.gather(*forced)
        bus.info("all services stopped 🦥")
    finally:
        for task in background:
            task.cancel()
        stopped.set()
        await control.close()


def _send(config: Path | None, payload: dict[str, Any]) -> dict[str, Any]:
    _, path = _load(config)
    try:
        response = asyncio.run(request(socket_path(path), payload))
    except NotRunning:
        err.print(f"Hammock isn't running for {path}")
        raise typer.Exit(1) from None
    if not response.get("ok"):
        err.print(f"[red]✗[/] {escape(response.get('error', 'unknown error'))}")
        raise typer.Exit(1)
    return response


_STATE_STYLE = {"healthy": "green", "running": "green", "starting": "yellow", "waiting": "yellow",
                "checking": "yellow", "unhealthy": "red", "exited": "red", "failed": "red"}


@app.command()
def status(config: ConfigOpt = None) -> None:
    """Show every service's state in the running stack."""
    response = _send(config, {"cmd": "status"})
    table = Table(box=box.SIMPLE, title=response["config"], title_justify="left", title_style="dim")
    for column in ("service", "state", "pid", "uptime", "health"):
        table.add_column(column)
    for s in response["services"]:
        state = s["state"] + (" (check-only)" if s["check_only"] else "")
        uptime = _duration(s["uptime"]) if s["uptime"] is not None else ""
        table.add_row(s["name"], Text(state, style=_STATE_STYLE.get(s["state"], "dim")),
                      str(s["pid"] or ""), uptime, s["health"] or "")
    Console().print(table)


@app.command()
def restart(service: str, config: ConfigOpt = None) -> None:
    """Restart one service in the running stack."""
    response = _send(config, {"cmd": "restart", "service": service})
    Console().print(f"↻ {service} restarted ({response['state']})")


@app.command()
def down(config: ConfigOpt = None) -> None:
    """Stop the running stack gracefully (same as Ctrl-C in the `up` terminal)."""
    _send(config, {"cmd": "down"})
    Console().print("all services stopped 🦥")


def _duration(seconds: int) -> str:
    minutes, s = divmod(seconds, 60)
    hours, m = divmod(minutes, 60)
    return f"{hours}h{m:02d}m" if hours else f"{m}m{s:02d}s" if m else f"{s}s"


@app.command("config")
def show_config(config: ConfigOpt = None, with_: WithOpt = None) -> None:
    """Validate the config and print what `up` would start, in order."""
    cfg, path = _load(config)
    out = Console()
    out.print(f"[bold]{path}[/]  model: {cfg.model or '[dim]not set[/]'}")
    try:
        names = cfg.select(with_)
    except ConfigError as e:
        err.print(f"[red]✗[/] {e}")
        raise typer.Exit(2) from None
    for i, layer in enumerate(cfg.layers(names), 1):
        for name in layer:
            svc = cfg.services[name]
            tags = [t for t, on in (("check-only", svc.check_only), ("external", svc.external)) if on]
            deps = f" [dim]after {', '.join(svc.depends_on)}[/]" if svc.depends_on else ""
            tag_text = escape(f" [{', '.join(tags)}]") if tags else ""
            out.print(f"  {i}. [cyan]{name}[/]{tag_text}{deps}")
            if svc.cmd:
                out.print(f"       [dim]{escape(f'{svc.cwd}$ {svc.cmd}')}[/]")
    skipped = [n for n, s in cfg.services.items() if s.optional and n not in names]
    if skipped:
        out.print(f"  [dim]optional, not selected: {', '.join(skipped)} (use --with)[/]")


if __name__ == "__main__":
    app()
