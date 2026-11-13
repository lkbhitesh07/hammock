"""Incident cards (rich panels) and "copy as prompt".

Two cards per incident when a model is configured:
1. right away, what Hammock found by itself (detected service, error, location, request
   path, which involved services have uncommitted changes);
2. when the model answers, its diagnosis: origin service, propagation chain, file, cause, fix.

Without a usable answer (no model, Ollama down, model not pulled) the first card is all
there is, with a note saying why, so Hammock is useful with no LLM at all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import Config
from .incidents import Incident
from .llm import Diagnosis

_TITLE_MAX = 90
_CONFIDENCE_STYLE = {"high": "green", "medium": "yellow", "low": "red"}
PROMPT_HINT = "copy as prompt for a bigger model: hammock prompt"


def _rows() -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", no_wrap=True)
    table.add_column(overflow="fold")
    return table


def _title(incident: Incident) -> str:
    title = f"🔎 incident #{incident.id} · {incident.primary.summary}"
    return title if len(title) <= _TITLE_MAX else title[: _TITLE_MAX - 1] + "…"


def _short(file: str, config: Config, service: str) -> str:
    svc = config.services.get(service)
    if svc and svc.cwd and Path(file).is_absolute():
        try:
            return str(Path(file).relative_to(svc.cwd))
        except ValueError:
            pass
    return file


def found_card(incident: Incident, config: Config, changed: list[str], status: str) -> Panel:
    """What Hammock found deterministically. `status` explains the model's part (asking / why not)."""
    p = incident.primary
    rows = _rows()
    rows.add_row("Detected", Text.assemble((p.service, "bold cyan"), f"  {p.kind}"))
    rows.add_row("Error", p.summary)
    if p.location:
        loc = p.location
        rows.add_row("Where", f"{_short(loc.file, config, p.service)}:{loc.line}"
                              + (f" in {loc.func}" if loc.func else ""))
    if incident.request_ids:
        rows.add_row("Request", f"{', '.join(incident.request_ids)} · seen in "
                                f"{', '.join(incident.request_services) or p.service}")
    others = [s for s in incident.services if s != p.service]
    if others:
        rows.add_row("Also failing", ", ".join(others))
    if changed:
        rows.add_row("Changed", Text(f"uncommitted changes in {', '.join(changed)}", style="yellow"))
    if hint := p.details.get("hint"):
        rows.add_row("Hint", hint)
    if incident.repeats > 1:
        rows.add_row("Seen", f"{incident.repeats} times")
    return Panel(Group(rows, Text(""), Text(status, style="dim")), title=_title(incident),
                 title_align="left", subtitle=PROMPT_HINT, subtitle_align="right",
                 border_style="yellow", box=box.ROUNDED)


def diagnosis_card(incident: Incident, config: Config, d: Diagnosis) -> Panel:
    detected = incident.primary.service
    rows = _rows()
    origin = Text(d.origin_service or "unknown", style="bold red" if d.origin_service else "dim")
    if d.origin_service and d.origin_service != detected:
        origin.append(f"  (surfaced in {detected})", style="dim")
    rows.add_row("Origin", origin)
    if d.propagation_chain:
        rows.add_row("Chain", " → ".join(d.propagation_chain))
    if d.file:
        rows.add_row("Where", f"{d.file}:{d.line}" if d.line else d.file)
    rows.add_row("Cause", d.likely_cause or "—")
    rows.add_row("Fix", d.suggested_fix or "—")
    if d.is_config_issue:
        rows.add_row("Kind", Text("configuration issue, not a code change", style="magenta"))
    rows.add_row("Confidence", Text.assemble((d.confidence, _CONFIDENCE_STYLE.get(d.confidence, "")),
                                             (f" · {d.model} · {d.seconds:.0f}s", "dim")))
    parts: list = [rows]
    if d.notes:
        parts += [Text(""), Text("Hammock's checks: " + "; ".join(d.notes), style="dim")]
    return Panel(Group(*parts), title=_title(incident), title_align="left",
                 subtitle=PROMPT_HINT, subtitle_align="right", border_style="red", box=box.ROUNDED)


# --- copy as prompt --------------------------------------------------------------------------

def copy_to_clipboard(text: str) -> str | None:
    """Copy with whatever clipboard tool exists; returns its name, or None."""
    candidates = []
    if os.environ.get("WAYLAND_DISPLAY"):
        candidates.append(["wl-copy"])
    candidates += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["pbcopy"]]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, check=True, timeout=3,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                continue
            return cmd[0]
    return None


def find_context_file(incidents_dir: Path, number: int | None = None) -> Path | None:
    """The latest saved incident's context, or the latest one with that incident number."""
    files = sorted(incidents_dir.glob("*.context.md"))  # names start with a timestamp
    if number is not None:
        files = [f for f in files if f.name.endswith(f"-{number:03d}.context.md")]
    return files[-1] if files else None
