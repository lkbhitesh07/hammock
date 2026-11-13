"""Command line: `hammock up/down/status/restart/config/prompt`."""

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
from .incidents import IncidentTracker, service_exit_event
from .investigate import Investigator
from .llm import full_prompt
from .logbus import LogBus
from .render import copy_to_clipboard, find_context_file
from .runner import Runner

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Hammock 🦥 — one command for your local stack.")
err = Console(stderr=True)

ConfigOpt = Annotated[Optional[Path], typer.Option("--config", "-c", help="Path to hammock.yaml (default: search upwards).")]
WithOpt = Annotated[Optional[list[str]], typer.Option("--with", "-w", help="Also start this optional service (repeatable).")]
NoLlmOpt = Annotated[bool, typer.Option("--no-llm", envvar="HAMMOCK_NO_LLM",
                                        help="Don't call the local model, even if hammock.yaml sets one (light hardware).")]


def _load(path: Path | None) -> tuple[Config, Path]:
    try:
        return load_config(path)
    except ConfigError as e:
        err.print(f"[red]✗[/] {e}")
        raise typer.Exit(2) from None


@app.command()
def up(config: ConfigOpt = None, with_: WithOpt = None, no_llm: NoLlmOpt = False) -> None:
    """Start the stack in dependency order and stream every service's logs. Ctrl-C stops it all."""
    cfg, path = _load(config)
    try:
        names = cfg.select(with_)
    except ConfigError as e:
        err.print(f"[red]✗[/] {e}")
        raise typer.Exit(2) from None
    asyncio.run(_up(cfg, path, names, llm=not no_llm))


async def _up(cfg: Config, path: Path, names: list[str], llm: bool = True) -> None:
    bus = LogBus()
    console = Console(highlight=False, soft_wrap=True)
    bus.subscribe(LogPrinter(names, console, quiet={n for n in names if cfg.services[n].check_only}))
    runner = Runner(cfg, names, bus)
    investigator = Investigator(cfg, _incidents_dir(path), console, bus, lambda: runner.first_ready_at, llm=llm)
    tracker = IncidentTracker(
        lambda: bus.window(float("-inf"), float("inf")),
        investigator,
        window=cfg.correlation_window_seconds,
        noise_paths=health_paths(cfg),
    )
    hub = DetectorHub(tracker.add, names)
    bus.subscribe(hub)
    runner.on_exit = lambda svc: tracker.add(service_exit_event(
        svc.name, svc.returncode, [l for l in bus.lines(svc.name) if l.mono >= (svc.started_at or 0)]))
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
        investigator.close()
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


def _incidents_dir(config_path: Path) -> Path:
    return config_path.parent / ".hammock" / "incidents"


@app.command()
def prompt(
    incident: Annotated[Optional[int], typer.Argument(help="Incident number (default: the latest incident).")] = None,
    config: ConfigOpt = None,
    print_only: Annotated[bool, typer.Option("--print", help="Print it; don't copy to the clipboard.")] = False,
) -> None:
    """Copy an incident's full prompt (instructions + context) for pasting into a bigger model."""
    _, path = _load(config)
    found = find_context_file(_incidents_dir(path), incident)
    if found is None:
        which = f"incident #{incident}" if incident is not None else "incidents"
        err.print(f"No saved {which} in {_incidents_dir(path)}")
        raise typer.Exit(1)
    text = full_prompt(found.read_text())
    tool = None if print_only else copy_to_clipboard(text)
    if tool:
        err.print(f"Copied {found.name} to the clipboard via {tool} (≈{len(text) // 4 / 1000:.1f}k tokens). "
                  "It contains your logs and code: paste it only where that's allowed.")
    else:
        print(text)


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
