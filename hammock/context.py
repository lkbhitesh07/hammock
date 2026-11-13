"""The small, focused context package the model reasons over.

Small local models drown in 10,000 log lines. Deterministic code does the heavy lifting
here (pick the error, the related lines from other services, the code around the failing
frames, what changed in git) and the model gets a few thousand tokens to reason about.

Every section has a fixed character budget (≈ 4 chars per token), so the whole package stays
around 4–6k tokens whatever the logs look like. Obvious secrets are redacted last.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, Service
from .detectors import ErrorEvent, Frame
from .detectors.python_tb import _FRAME as PY_FRAME, _LIBRARY as PY_LIBRARY
from .gitinfo import GitInfo, git_info
from .incidents import Incident
from .logbus import HAMMOCK

BUDGET = {"error": 3500, "timeline": 5000, "code": 5000, "git": 6000}
SNIPPET_RADIUS = 15
MAX_SNIPPET_FRAMES = 3

_CARETS = re.compile(r"^\s*[\^~]+\s*$")
_LOG_TIMESTAMP = re.compile(
    r"^(?:\d{4}-\d\d-\d\d[T ][\d:.,]+Z?|\[\d\d/\w{3}/\d{4} [\d:]+\]|\d{4}/\d\d/\d\d [\d:.]+)\s+")


# --- the package -----------------------------------------------------------------------------

@dataclass(slots=True)
class ContextPackage:
    sections: list[tuple[str, str]]
    changed_services: list[str] = field(default_factory=list)  # involved services with uncommitted changes

    def render(self) -> str:
        return redact("\n\n".join(f"## {title}\n{body}".rstrip() for title, body in self.sections)) + "\n"

    @property
    def approx_tokens(self) -> int:
        return len(self.render()) // 4


def build_context(incident: Incident, config: Config, healthy_since: float | None = None) -> ContextPackage:
    primary = incident.primary
    related = related_services(incident, config)
    infos = _git_infos(related, config)
    return ContextPackage([
        ("Stack", _stack_section(config, related)),
        ("Incident", _incident_section(incident, config)),
        ("Error output", clip(_relative_paths(compact_error(primary), config, primary.service), BUDGET["error"])),
        ("Timeline across services", _timeline_section(incident, BUDGET["timeline"])),
        ("Code at the failing frames", _code_section(incident, config, BUDGET["code"])),
        ("What changed (git)", _git_section(infos, healthy_since, BUDGET["git"])),
    ], changed_services=[name for name, info in infos if info and (info.diff or info.untracked)])


# --- helpers ---------------------------------------------------------------------------------

def clip(text: str, limit: int) -> str:
    """Keep the head and (mostly) the tail: the end of an error is where the exception is."""
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.3), int(limit * 0.7)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[... {omitted} characters omitted ...]\n{text[-tail:]}"


def infer_language(svc: Service) -> str | None:
    if svc.language:
        return svc.language
    cmd = (svc.cmd or "").lower()
    for needle, language in (("cargo", "rust"), ("manage.py", "python/django"), ("python", "python"),
                             ("uvicorn", "python"), ("gunicorn", "python"), ("npm", "javascript"),
                             ("pnpm", "javascript"), ("yarn", "javascript"), ("node", "javascript"),
                             ("vite", "javascript"), ("go run", "go"), ("docker", "container")):
        if needle in cmd:
            return language
    return None


def related_services(incident: Incident, config: Config) -> list[str]:
    """Services with errors or carrying the request, plus everything they depend on."""
    involved = [s for s in (*incident.services, *incident.request_services) if s in config.services]
    seen: set[str] = set()
    stack = list(involved)
    while stack:
        name = stack.pop()
        if name not in seen:
            seen.add(name)
            stack.extend(config.services[name].depends_on)
    return [n for n in config.services if n in seen]


def _short(file: str, config: Config, service: str) -> str:
    svc = config.services.get(service)
    if svc and svc.cwd and Path(file).is_absolute():
        try:
            return str(Path(file).relative_to(svc.cwd))
        except ValueError:
            pass
    return file


def _relative_paths(text: str, config: Config, service: str) -> str:
    svc = config.services.get(service)
    return text.replace(f"{svc.cwd}/", "") if svc and svc.cwd else text


def _where(frame: Frame | None, config: Config, service: str) -> str:
    if frame is None:
        return "unknown"
    where = f"{_short(frame.file, config, service)}:{frame.line}"
    return f"{where} in {frame.func}" if frame.func else where


# --- sections --------------------------------------------------------------------------------

def _stack_section(config: Config, related: list[str]) -> str:
    rows = ["Local multi-service dev stack. `a -> b` means a depends on (calls) b."]
    for name, svc in config.services.items():
        tags = [t for t in (infer_language(svc), "external: third-party, fix via configuration only"
                            if svc.external else None) if t]
        mark = "" if name in related else " (not involved)"
        deps = f"; depends on {', '.join(svc.depends_on)}" if svc.depends_on else ""
        rows.append(f"- {name}{mark}: {', '.join(tags) or 'unknown language'}{deps}")
    edges = [f"{n} -> {d}" for n, s in config.services.items() for d in s.depends_on]
    if edges:
        rows.append("Dependency graph: " + "; ".join(edges))
    return "\n".join(rows)


def _incident_section(incident: Incident, config: Config) -> str:
    p = incident.primary
    rows = [
        f"Detected in: {p.service} ({p.kind})",
        f"Error: {p.summary}",
        f"Location in {p.service}'s code: {_where(p.location, config, p.service)}",
    ]
    if incident.request_ids:
        rows.append(f"Request IDs: {', '.join(incident.request_ids)} "
                    f"(logged by: {', '.join(incident.request_services) or p.service})")
    rows.append(f"Services that reported errors: {', '.join(incident.services)}")
    others = [e for e in incident.events if e is not p]
    if others:
        rows.append("Other events in this incident:")
        rows += [f"- {e.service} {e.kind}: {e.summary}" for e in others[:10]]
        if len(others) > 10:
            rows.append(f"- ... and {len(others) - 10} more")
    if incident.repeats > 1:
        rows.append(f"Seen {incident.repeats} times.")
    return "\n".join(rows)


def compact_error(event: ErrorEvent) -> str:
    texts = [line.text for line in event.lines]
    if event.kind == "rust-panic":
        return _compact_panic(event, texts)
    if event.kind == "python-traceback":
        return _compact_python(texts)
    return "\n".join(texts)


def _compact_python(texts: list[str]) -> str:
    """Drop library frames (and the code lines under them) and ^^^ markers."""
    out: list[str] = []
    omitted = 0
    skipping = False

    def mark() -> None:
        nonlocal omitted
        if omitted:
            out.append(f"  [... {omitted} library frame{'s' if omitted > 1 else ''} omitted ...]")
            omitted = 0

    for text in texts:
        if m := PY_FRAME.match(text):
            if PY_LIBRARY.search(m["file"]):
                omitted += 1
                skipping = True
                continue
            skipping = False
            mark()
            out.append(text)
        elif _CARETS.match(text) or (skipping and text[:1].isspace()):
            continue
        else:
            skipping = False
            mark()
            out.append(text)
    mark()
    return "\n".join(out)


def _compact_panic(event: ErrorEvent, texts: list[str]) -> str:
    out = [texts[0]]
    if message := event.details.get("message"):
        out.append(message)
    backtrace = event.frames[1:]  # frames[0] is the panic site
    if backtrace:
        project = [f for f in backtrace if f.in_project]
        out.append(f"backtrace, project frames only ({len(backtrace) - len(project)} library frames omitted):")
        out += [f"  {f}" for f in project]
    return "\n".join(out)


def _timeline_section(incident: Incident, limit: int) -> str:
    primary = incident.primary
    t0 = primary.start
    primary_lines = {id(line) for line in primary.lines}

    # (time, rank, text); rank decides what survives the budget: errors and request lines first,
    # then Hammock's own events (restarts, exits), then nearby lines, closest in time first.
    candidates: list[tuple[float, int, str]] = []
    placed = False
    for entry in incident.timeline:
        line = entry.line
        if id(line) in primary_lines:
            if not placed:
                candidates.append((line.mono, 0, f"{line.mono - t0:+.3f}s [{line.service}] "
                                   f"<<< error output, shown above: {primary.summary} >>>"))
                placed = True
            continue
        rank = 0 if entry.why == "error" else 1 if entry.why == "request" else 2 if line.service == HAMMOCK else 3
        text = _LOG_TIMESTAMP.sub("", line.text.strip())
        if _CARETS.match(text):
            continue
        candidates.append((line.mono, rank, f"{line.mono - t0:+.3f}s [{line.service}] {text}"))

    # Collapse immediate repeats of the same line from the same service.
    collapsed: list[tuple[float, int, str]] = []
    counts: list[int] = []
    for c in candidates:
        if collapsed and collapsed[-1][2].split("] ", 1)[-1] == c[2].split("] ", 1)[-1]:
            counts[-1] += 1
            continue
        collapsed.append(c)
        counts.append(1)
    rows = [(t, r, text + (f"  (×{n})" if n > 1 else "")) for (t, r, text), n in zip(collapsed, counts)]

    keep: set[int] = set()
    used = 0
    for i in sorted(range(len(rows)), key=lambda i: (rows[i][1], abs(rows[i][0] - t0))):
        cost = len(rows[i][2]) + 1
        if used + cost > limit:
            continue
        keep.add(i)
        used += cost
    out: list[str] = ["Times are relative to the first line of the error. Hammock's own lines are [hammock]."]
    skipped = 0
    for i, (_, _, text) in enumerate(rows):
        if i in keep:
            if skipped:
                out.append(f"  [... {skipped} lines omitted ...]")
                skipped = 0
            out.append(text)
        else:
            skipped += 1
    if skipped:
        out.append(f"  [... {skipped} lines omitted ...]")
    return "\n".join(out)


def _snippet_frames(incident: Incident) -> list[tuple[str, Frame]]:
    primary = incident.primary
    picked: list[tuple[str, Frame]] = []
    for event in [primary, *[e for e in incident.events if e is not primary]]:
        project = event.project_frames
        # Innermost first: Python prints it last, Rust first.
        ordered = list(reversed(project)) if event.kind == "python-traceback" else project
        for frame in ([event.location] if event.location else []) + ordered:
            key = (frame.file, frame.line)
            if all((f.file, f.line) != key for _, f in picked):
                picked.append((event.service, frame))
            if len(picked) >= MAX_SNIPPET_FRAMES:
                return picked
    return picked


def _resolve(file: str, svc: Service | None) -> Path | None:
    path = Path(file)
    if not path.is_absolute():
        if svc is None or svc.cwd is None:
            return None
        path = svc.cwd / path
    return path if path.is_file() else None


def _code_section(incident: Incident, config: Config, limit: int) -> str:
    # Group the frames per file, merge overlapping windows.
    by_file: dict[Path, tuple[str, list[int]]] = {}
    for service, frame in _snippet_frames(incident):
        path = _resolve(frame.file, config.services.get(service))
        if path:
            by_file.setdefault(path, (service, []))[1].append(frame.line)
    if not by_file:
        return "No source available for the failing frames."

    blocks: list[str] = []
    for path, (service, marks) in by_file.items():
        try:
            source = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        windows: list[list[int]] = []
        for line in sorted(marks):
            lo, hi = max(1, line - SNIPPET_RADIUS), min(len(source), line + SNIPPET_RADIUS)
            if windows and lo <= windows[-1][1] + 1:
                windows[-1][1] = max(windows[-1][1], hi)
            else:
                windows.append([lo, hi])
        for lo, hi in windows:
            rows = [f"{'>>' if n in marks else '  '}{n:>4} | {source[n - 1]}" for n in range(lo, hi + 1)]
            blocks.append(f"### {_short(str(path), config, service)} ({service}), lines {lo}-{hi}\n"
                          "```\n" + "\n".join(rows) + "\n```")
    return clip("\n".join(blocks), limit) if blocks else "No source available for the failing frames."


def _age(when: float) -> str:
    seconds = max(0, time.time() - when)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def _git_infos(related: list[str], config: Config) -> list[tuple[str, GitInfo | None]]:
    infos: list[tuple[str, GitInfo | None]] = []
    seen_paths: set[Path] = set()
    for name in related:
        svc = config.services[name]
        if svc.external or svc.check_only or svc.cwd is None or svc.cwd in seen_paths:
            continue
        seen_paths.add(svc.cwd)
        infos.append((name, git_info(svc.cwd)))
    return infos


def _git_section(infos: list[tuple[str, GitInfo | None]], healthy_since: float | None, limit: int) -> str:
    infos = list(infos)
    if not infos:
        return "No code-owning services involved."

    # Services with uncommitted changes first: that's where a fresh regression usually is.
    infos.sort(key=lambda item: not (item[1] and (item[1].diff or item[1].untracked)))
    per_service = limit // len(infos)
    blocks = ["`git diff HEAD` (uncommitted changes) and recent commits, per involved service."]
    for name, info in infos:
        if info is None:
            blocks.append(f"### {name}\nnot a git repository")
            continue
        rows = [f"### {name} ({info.relpath})"]
        if info.diff:
            rows.append("```diff\n" + clip(info.diff.rstrip(), per_service) + "\n```")
        else:
            rows.append("no uncommitted changes")
        if info.untracked:
            rows.append("untracked files: " + ", ".join(info.untracked))
        if info.commits:
            rows.append("recent commits:")
            for c in info.commits:
                fresh = " (after the stack was last healthy)" if healthy_since and c.when > healthy_since else ""
                rows.append(f"- {c.sha} {_age(c.when)}: {c.subject}{fresh}")
        blocks.append("\n".join(rows))
    return "\n".join(blocks)


# --- redaction -------------------------------------------------------------------------------

_KEYED = re.compile(
    r"(?i)\b(\w*(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token)\w*"
    r"[\"']?\s*[=:]\s*[\"']?)[^\s\"'&,;]{3,}")
_BEARER = re.compile(r"(?i)\b(bearer|basic)(\s+)[\w.~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]+")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_B64 = re.compile(r"(?<![\w/+=-])[A-Za-z0-9+/]{40,}={0,2}(?![\w/+=-])")


def _b64(m: re.Match[str]) -> str:
    s = m.group(0)
    # Long paths match the charset too; real keys mix cases and digits.
    if any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s):
        return "[REDACTED]"
    return s


def redact(text: str) -> str:
    text = _JWT.sub("[REDACTED_JWT]", text)
    text = _BEARER.sub(r"\1\2[REDACTED]", text)
    text = _KEYED.sub(r"\1[REDACTED]", text)
    text = _HEX.sub("[REDACTED_HEX]", text)
    return _B64.sub(_b64, text)
