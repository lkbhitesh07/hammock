"""Ask a local model (Ollama) where an incident started.

The model gets the context package and must answer with JSON that matches INCIDENT_SCHEMA
(Ollama structured output), at temperature 0. The answer is then checked against what
Hammock knows: unknown services are dropped from the chain, a file that appears nowhere in
the context is treated as a guess and removed, and an external service is always a config issue.

Nothing here ever raises into the caller: no model configured, Ollama not running, a model
that isn't pulled, a timeout or malformed output all become a `Diagnosis` with `error` set,
and the incident card falls back to what Hammock found deterministically.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import PurePath
from typing import Any, Protocol

from .config import Config

TIMEOUT = 300.0  # CPU-only laptops can take a minute or more on a 7B model
NUM_CTX = 8192  # Ollama's default context is smaller than our package + answer

# Field order matters: Ollama generates fields in schema order, so the cause comes first and
# the model reasons before it commits to an origin.
INCIDENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "likely_cause": {"type": "string"},
        "origin_service": {"type": "string"},
        "propagation_chain": {"type": "array", "items": {"type": "string"}},
        "file": {"type": ["string", "null"]},
        "line": {"type": ["integer", "null"]},
        "suggested_fix": {"type": "string"},
        "is_config_issue": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["likely_cause", "origin_service", "propagation_chain", "file", "line",
                 "suggested_fix", "is_config_issue", "confidence"],
}

SYSTEM_PROMPT = """\
You are Hammock's incident analyst for a local multi-service development stack.
You get a context package: the services and their dependency graph, the error Hammock \
detected, a timeline of log lines from every service around it, the code at the failing \
frames, and what changed in git.

Find the service where the problem STARTED, not just where it surfaced.
- Errors often surface downstream of their cause. A service that fails because another \
service sent it something unexpected is a symptom, not the origin.
- `a -> b` means a calls b. If a fails on data that b returned, look at b.
- Uncommitted changes (git diff) are strong evidence. A change in a dependency that matches \
the error (a renamed field, a changed type, a removed route, changed config) is usually the origin.
- Lines with the same request ID belong to the same request as it crosses services.
- Services marked external are third-party: never suggest editing their code. The fix is \
configuration (client ID/secret, redirect URI, certificates, issuer) and is_config_issue is true.
- A missing tool, wrong environment variable, port already in use or bad credentials is a \
config issue.

Answer only from the context. If you cannot tell the file or line, use null. Never invent \
file names. propagation_chain lists services from the origin to where the error was seen, \
e.g. ["pricing", "api"]. suggested_fix names the file and the concrete change. Keep \
likely_cause and suggested_fix to at most three sentences each."""

PROMPT_HEADER = "Here is the context package for the incident. Where did it start, and what is the fix?\n\n"


class ChatClient(Protocol):
    def chat(self, **kwargs: Any) -> Any: ...


@dataclass
class Diagnosis:
    model: str | None
    origin_service: str | None = None
    propagation_chain: list[str] = field(default_factory=list)
    file: str | None = None
    line: int | None = None
    likely_cause: str = ""
    suggested_fix: str = ""
    is_config_issue: bool = False
    confidence: str = "low"
    seconds: float = 0.0
    error: str | None = None  # set when there is no usable answer; the card explains it
    notes: list[str] = field(default_factory=list)  # corrections Hammock made to the answer

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def full_prompt(context: str) -> str:
    """System prompt + context in one block, for pasting into any chat model."""
    return f"{SYSTEM_PROMPT}\n\n{PROMPT_HEADER}{context}"


def diagnose(context: str, config: Config, client: ChatClient | None = None) -> Diagnosis:
    model = config.model
    if not model:
        return Diagnosis(None, error="no model configured: set `model:` in hammock.yaml (an Ollama tag)")
    started = time.monotonic()
    try:
        if client is None:
            try:
                import ollama  # optional: `pip install 'hammock[llm]'`
            except ImportError:
                return Diagnosis(model, error="the ollama package isn't installed: "
                                              "`pip install 'hammock[llm]'` (or run with --no-llm)")
            client = ollama.Client(timeout=TIMEOUT)  # honours OLLAMA_HOST
        response = client.chat(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": PROMPT_HEADER + context}],
            format=INCIDENT_SCHEMA,
            options={"temperature": 0, "seed": 7, "num_ctx": NUM_CTX},
            keep_alive="30m",  # stay loaded between incidents
        )
        raw = _content(response)
    except Exception as e:  # noqa: BLE001 - every failure becomes a fallback card
        return Diagnosis(model, error=_explain(e, model), seconds=time.monotonic() - started)
    diagnosis = parse_answer(raw, model, config, context)
    diagnosis.seconds = time.monotonic() - started
    return diagnosis


def _content(response: Any) -> str:
    message = response["message"] if isinstance(response, dict) else response.message
    return message["content"] if isinstance(message, dict) else message.content


def _explain(error: Exception, model: str) -> str:
    text = str(error)
    status = getattr(error, "status_code", None)
    if isinstance(error, ConnectionError) or "connect" in text.lower():
        return "Ollama isn't reachable (is `ollama serve` running?)"
    if status == 404 or "not found" in text.lower():
        return f"model '{model}' isn't available in Ollama: run `ollama pull {model}`"
    if "timed out" in text.lower() or "timeout" in type(error).__name__.lower():
        return f"'{model}' took longer than {TIMEOUT:.0f}s"
    return f"Ollama error: {text[:200]}"


def parse_answer(raw: str, model: str, config: Config, context: str) -> Diagnosis:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except ValueError:
        return Diagnosis(model, error=f"'{model}' did not return valid JSON")

    d = Diagnosis(model)
    d.likely_cause = str(data.get("likely_cause") or "").strip()
    d.suggested_fix = str(data.get("suggested_fix") or "").strip()
    d.is_config_issue = bool(data.get("is_config_issue"))
    d.confidence = data.get("confidence") if data.get("confidence") in ("low", "medium", "high") else "low"

    services = config.services
    origin = str(data.get("origin_service") or "").strip()
    if origin in services:
        d.origin_service = origin
    else:
        d.notes.append(f"origin '{origin}' is not a service in this stack" if origin else "no origin given")
        d.confidence = "low"
    chain = [str(s).strip() for s in data.get("propagation_chain") or []]
    d.propagation_chain = list(dict.fromkeys(s for s in chain if s in services))
    if d.origin_service and (not d.propagation_chain or d.propagation_chain[0] != d.origin_service):
        d.propagation_chain = [d.origin_service, *[s for s in d.propagation_chain if s != d.origin_service]]

    file = data.get("file")
    if isinstance(file, str) and file.strip() and file.strip().lower() not in ("unknown", "null", "none"):
        file = file.strip()
        if PurePath(file).name in context:
            d.file = file
            line = data.get("line")
            d.line = line if isinstance(line, int) and line > 0 else None
        else:
            d.notes.append(f"dropped file '{file}': it appears nowhere in the context")

    if d.origin_service and services[d.origin_service].external and not d.is_config_issue:
        d.is_config_issue = True
        d.notes.append(f"{d.origin_service} is external, so this is a config issue")
    if not d.likely_cause and not d.suggested_fix:
        d.error = f"'{model}' returned an empty answer"
    return d
