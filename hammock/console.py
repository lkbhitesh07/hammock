"""Combined, color-prefixed log output."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from .logbus import HAMMOCK, LogLine

# Distinct hues first, so the usual 5-10 services never share a look-alike color. No red: red
# reads as "error". 256-color names degrade gracefully on 16-color terminals.
_PALETTE = ["cyan", "magenta", "green", "yellow", "dodger_blue1", "orange1", "orchid1", "spring_green2",
            "light_slate_blue", "gold3", "turquoise2", "pink1"]


class LogPrinter:
    def __init__(self, services: list[str], console: Console | None = None,
                 quiet: frozenset[str] | set[str] = frozenset()) -> None:
        """`quiet`: check-only services; they print no logs, so they don't use up a color."""
        self.console = console or Console(highlight=False, soft_wrap=True)
        self._width = max(len(n) for n in [*services, HAMMOCK])
        loud = [n for n in services if n not in quiet] + [n for n in services if n in quiet]
        self._colors = {name: _PALETTE[i % len(_PALETTE)] for i, name in enumerate(loud)}
        self._colors[HAMMOCK] = "bold white"

    def __call__(self, line: LogLine) -> None:
        color = self._colors.get(line.service, "white")
        prefix = Text(f"{line.service:>{self._width}} │ ", style=color)
        # from_ansi keeps the service's own colors but drops cursor moves / clear-screen codes.
        body = Text(line.raw, style="bold") if line.service == HAMMOCK else Text.from_ansi(line.raw)
        self.console.print(prefix + body)
