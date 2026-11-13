"""What happens when an incident is captured: save it, build its context, show the cards,
and ask the model in the background.

The model runs in a daemon thread, one incident at a time: CPU inference can take a minute,
logs must keep streaming meanwhile, and Ctrl-C must not wait for a half-finished answer.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from rich.console import Console

from .config import Config
from .context import build_context
from .incidents import Incident, save_incident
from .llm import Diagnosis, diagnose
from .logbus import LogBus
from .render import diagnosis_card, found_card

T = TypeVar("T")


def run_in_daemon_thread(fn: Callable[..., T], *args: Any) -> asyncio.Future[T]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def deliver(result: Any, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def work() -> None:
        try:
            result, error = fn(*args), None
        except BaseException as e:  # noqa: BLE001 - handed to the awaiting coroutine
            result, error = None, e
        try:
            loop.call_soon_threadsafe(deliver, result, error)
        except RuntimeError:
            pass  # the loop is gone: Hammock is shutting down

    threading.Thread(target=work, name="hammock-llm", daemon=True).start()
    return future


class Investigator:
    def __init__(
        self,
        config: Config,
        incidents_dir: Path,
        console: Console,
        bus: LogBus,
        healthy_since: Callable[[], float | None] = lambda: None,
        diagnose_fn: Callable[[str, Config], Diagnosis] = diagnose,
        llm: bool = True,  # False: --no-llm, never call the model
    ) -> None:
        self.config = config
        self.incidents_dir = incidents_dir
        self.console = console
        self.bus = bus
        self.healthy_since = healthy_since
        self.diagnose_fn = diagnose_fn
        self.llm = llm
        self._lock = asyncio.Lock()  # one model call at a time
        self._tasks: set[asyncio.Task[None]] = set()

    def __call__(self, incident: Incident) -> None:
        p = incident.primary
        if incident.repeats > 1:
            self.bus.info(f"🔁 incident #{incident.id} again (×{incident.repeats}): {p.service}: {p.summary}")
            save_incident(incident, self.incidents_dir)
            return

        save_incident(incident, self.incidents_dir)
        context, changed = "", []
        try:
            package = build_context(incident, self.config, self.healthy_since())
            context, changed = package.render(), package.changed_services
            if incident.saved_to:
                incident.saved_to.with_suffix(".context.md").write_text(context)
        except Exception as e:  # noqa: BLE001 - the card must still appear
            self.bus.info(f"   (could not build the context package: {e})")

        model = self.config.model if self.llm else None
        if self.config.model and not self.llm:
            status = "local model off (--no-llm): `hammock prompt` copies this incident for any model"
        elif not model:
            status = "no diagnosis: set `model:` in hammock.yaml (an Ollama tag) to have a local model explain it"
        elif not context:
            status = "no diagnosis: the context package could not be built"
        else:
            status = f"🧠 asking {model} where this started… (logs keep streaming)"
        self.console.print(found_card(incident, self.config, changed, status), soft_wrap=False)

        if model and context:
            task = asyncio.get_running_loop().create_task(self._diagnose(incident, context))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _diagnose(self, incident: Incident, context: str) -> None:
        async with self._lock:
            try:
                d = await run_in_daemon_thread(self.diagnose_fn, context, self.config)
            except Exception as e:  # noqa: BLE001 - diagnose() shouldn't raise, but never crash
                d = Diagnosis(self.config.model, error=f"diagnosis failed: {e}")
        incident.diagnosis = d.to_dict()
        save_incident(incident, self.incidents_dir)
        if d.ok:
            self.console.print(diagnosis_card(incident, self.config, d), soft_wrap=False)
        else:
            self.bus.info(f"🧠 incident #{incident.id}: no diagnosis: {d.error}")

    async def wait(self) -> None:
        """For tests: wait for pending diagnoses."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def close(self) -> None:
        for task in self._tasks:
            task.cancel()
