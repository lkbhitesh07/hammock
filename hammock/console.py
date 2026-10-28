"""Combined, color-prefixed log output."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from .logbus import HAMMOCK, LogLine

_PALETTE = ["cyan", "magenta", "green", "yellow", "blue", "bright_red", "bright_cyan", "bright_magenta",
            "bright_green", "bright_yellow", "bright_blue"]


class LogPrinter:
    def __init__(self, services: list[str], console: Console | None = None) -> None:
        self.console = console or Console(highlight=False, soft_wrap=True)
        self._width = max(len(n) for n in [*services, HAMMOCK])
        self._colors = {name: _PALETTE[i % len(_PALETTE)] for i, name in enumerate(services)}
        self._colors[HAMMOCK] = "bold white"

    def __call__(self, line: LogLine) -> None:
        color = self._colors.get(line.service, "white")
        prefix = Text(f"{line.service:>{self._width}} │ ", style=color)
        # from_ansi keeps the service's own colors but drops cursor moves / clear-screen codes.
        body = Text(line.raw, style="bold") if line.service == HAMMOCK else Text.from_ansi(line.raw)
        self.console.print(prefix + body)
