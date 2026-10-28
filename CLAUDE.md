# Hammock 🦥 — Project Context

> Handover file. Place this at the repo root as `CLAUDE.md` so Claude Code loads it automatically.
> It captures every decision made during planning. Treat it as the source of truth; ask before deviating.

---

## 1. Hackathon context

- **Event:** DEV.to *Hacktoberfest Open-Source AI Challenge — Week 3: "Slow and Steady"*
  https://dev.to/devteam/join-the-hacktoberfest-open-source-ai-challenge-slow-and-steady-2850-in-prizes-across-21-3iee
- **Theme:** Build something with open-weight models or open-source AI that *rewards laziness*. The open pieces must be what makes the project work.
- **Deadline:** **Sunday, October 25, 2026, 11:59 PM PDT.** Submission is a DEV post using the official template.
- **Judging, in order of weight:**
  1. Writing quality (heaviest): what it is, who it's for, why open innovation matters.
  2. Relevance to theme: open AI at the core, genuinely saves effort.
  3. Creativity.
  4. Technical execution: it works and is well built.
  5. Partner tech use (optional).
- **Target categories:** Overall, **Best Use of Gemma** (featured, if a Gemma model is used), possibly **Best Use of Sentry Agent Tracing**. Only claim a category if the tech is used meaningfully.
- **Optional but valued:** save the agent session with DevRelay and embed it in the post.
- **Already taken by others (avoid overlap):** git-commit timesheet/standup generators (e.g. "SlothLog").

---

## 2. The problem (author's real pain)

The author's company runs several services locally:

- **Backend:** Python (Django), Rust, Casdoor (auth, third-party Go service, run via Docker)
- **Frontend:** React

Daily friction:

1. Opening a terminal per service, starting/stopping each by hand, restarting after changes.
2. When an error appears, manually collecting logs from several terminals and explaining the whole flow to an LLM.
3. Manually passing data from one service to another while debugging.
4. **The symptom appears in one service, the cause lives in another.** New developers can't tell where an error starts or where to fix it.

Local logs contain internal URLs, tokens, customer data, and proprietary stack traces. Sending them to a cloud LLM is a security/compliance problem. **This is the "why open matters" argument: a local open-weight model makes the automation allowed, offline, and free.**

---

## 3. The solution

**Hammock** — one command runs the whole local stack, and when something breaks, a local LLM tells you *which service the error actually started in*, how it propagated, and what to fix.

### Positioning
docker-compose, Tilt, overmind, mprocs, process-compose solve *running* services. None of them *explain cross-service errors*. Hammock's value is the investigation layer.

### Key technical insight (put this in the post)
Small local models fail when handed 10,000 log lines. **Deterministic code does the heavy lifting** (detect errors, align logs by time / request ID, extract code snippets, collect git diff). The model receives a small, focused context package and only does the reasoning. That's why a ~7B model on a laptop gives useful answers.

---

## 4. Scope & priorities

### MVP (must ship, in this order)
1. **Config + runner:** `hammock.yaml`, `hammock up` / `hammock down`, dependency-ordered startup with health checks, combined color-prefixed logs.
2. **Smart restart:** file watcher restarts only the changed service (mainly matters for **Rust**, since Django `runserver` and the React dev server already auto-reload). Wait for health before marking dependents ready.
3. **Error detection:** parsers for Python/Django, Rust (panics + compile errors), HTTP 5xx access lines.
4. **Context package:** error + surrounding lines + other services' lines in the same time window (or same request ID) + code snippets from stack frames + git diff + dependency graph.
5. **Incident card:** local LLM via Ollama with structured JSON output, rendered in the terminal, plus "copy as prompt".
6. **Demo stack** with two planted bugs (Section 9).

### Nice to have (only after MVP works end to end)
- `hammock explain`: Mermaid diagram of request flow across services (great post screenshot).
- Browser error reporter for React runtime errors (Section 6.4).
- Textual TUI with per-service panes.
- `hammock doctor`: preflight for missing env vars, busy ports, DB not running, wrong runtime versions.
- `hammock init`: model reads repos and drafts `hammock.yaml`.
- Sentry Agent Tracing spans around LLM calls.

### Non-goals
- Request replay between services (stretch only).
- Production use, remote hosts, Kubernetes.
- Any cloud LLM dependency. The cloud is only reachable via the user's manual "copy as prompt".

### ⚠️ Hard rule
**Never use the author's company code, logs, or config in the repo, demo, or post.** The demo uses a fake stack built from scratch.

---

## 5. Tech decisions

- **Language:** Python 3.11+ (fastest to ship; the team knows it).
- **Libraries:**
  - `asyncio` subprocesses: spawn and stream stdout/stderr.
  - `watchfiles`: file watching.
  - `rich`: colored, prefixed logs and incident card rendering (MVP UI).
  - `textual`: optional TUI upgrade.
  - `pydantic` + `PyYAML`: config.
  - `typer`: CLI.
  - `httpx`: health checks.
  - `ollama`: the official Python client.
  - `aiohttp` (or the stdlib): tiny local endpoint for browser errors.
- **Model runtime:** Ollama at `localhost:11434`. The model is configurable in `hammock.yaml`. Candidates: a code-focused ~7B model (Qwen coder family) and a Gemma model (for the Gemma category). Run `ollama list` / check the Ollama library for current tags, and pick whichever correctly identifies root cause on both demo bugs.
- **Fallback:** if Ollama isn't reachable, still show a deterministic incident summary (detected error, frames, correlated lines). Never crash.

---

## 6. Architecture

### 6.1 Repo layout

```
hammock/
  CLAUDE.md
  README.md
  pyproject.toml
  hammock/
    cli.py           # typer: up, down, restart <svc>, logs, explain
    config.py        # pydantic models for hammock.yaml
    runner.py        # spawn processes, dependency order, health checks, restarts
    logbus.py        # receives every line, stamps arrival time, per-service ring buffers
    watcher.py       # watchfiles -> restart affected service
    detectors/
      python_tb.py   # Python/Django tracebacks + Django error lines
      rust.py        # panics, backtrace frames, compile errors, tracing ERROR lines
      http.py        # access-log 5xx/4xx lines
      browser.py     # events POSTed from the React reporter
    correlate.py     # time-window + request-id matching across services
    context.py       # builds the context package (budgeted)
    gitinfo.py       # git diff / recent commits per service repo
    llm.py           # Ollama call with JSON schema, prompt, fallback
    render.py        # rich incident card, copy-as-prompt
    server.py        # local endpoint for browser errors (nice-to-have)
  browser/
    hammock-reporter.js
  demo/
    hammock.yaml
    web/             # React (Vite)
    api/             # Django orders API
    pricing/         # Rust (axum) pricing service
  tests/
    fixtures/        # captured real-format log samples per language
```

### 6.2 Log bus
Hammock owns every service's stdout/stderr, so it **stamps each line with its own monotonic + wall-clock timestamp on arrival**. No need to parse four different log timestamp formats. Keep a ring buffer per service (e.g. last 2,000 lines).

### 6.3 Error detection (regex starting points; verify against real output in `tests/fixtures`)

**Python / Django**
- Traceback start: `^Traceback \(most recent call last\):`
- Frame: `^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)$`
- Exception line: first non-indented line after the frames, e.g. `^(?P<exc>[A-Za-z_][\w.]*)(: (?P<msg>.*))?$`
- Django: `Internal Server Error: (?P<path>\S+)`
- Access log: `"(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[\d.]+" (?P<status>\d{3})`
- Prefer frames inside the project directory over site-packages when choosing the "likely" location.

**Rust** (run with `RUST_BACKTRACE=1`)
- Panic: `thread '(?P<thread>[^']+)' panicked at (?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)`. In newer Rust the message is on the next line.
- Backtrace frame: `at (?P<file>[^:\s]+):(?P<line>\d+):(?P<col>\d+)`. Prefer `src/` paths over `.cargo/registry`.
- Compile error: `^error(\[E\d{4}\])?: (?P<msg>.+)$`, then `-->\s+(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)`
- `tracing` logs: lines containing `ERROR`.
- A failed rebuild after an edit is a first-class incident ("service didn't come back up because…").

**Casdoor** (`external: true`)
- Don't parse code. Watch for error lines, failed health checks, and 401/403 bursts in *other* services.
- The model should suggest **configuration** fixes (client ID/secret, redirect URI, certificate/JWT issuer), never code edits in Casdoor.

**React**
- Dev-server compile errors appear in the terminal; detect them like other lines.
- Runtime errors are in the browser, which Hammock can't see. See 6.4.

### 6.4 Browser reporter (nice-to-have)
A dev-only snippet (~20 lines) in `browser/hammock-reporter.js`:
- catches `window.onerror` and `unhandledrejection`;
- wraps `fetch` to add an `X-Request-ID` header and report non-2xx responses (URL, status, request ID);
- POSTs events to `http://localhost:<hammock_port>/browser-event`.

### 6.5 Correlation
1. **Request ID (preferred):** React fetch wrapper creates `X-Request-ID`; Django middleware logs and forwards it; the Rust service logs it (`tower-http` request-id layer for axum). Hammock follows one request across every service.
2. **Time window (fallback):** collect lines from all services within ±N seconds of the error (default 5s), ordered by Hammock's arrival timestamp.

### 6.6 Context package (keep it small, roughly 4–6k tokens max)
- Failing service name, language, `external` flag.
- Error block (traceback/panic/compile error) and ~30 lines before it.
- Correlated lines from other services (deduplicated, trimmed).
- Code snippets: ±15 lines around the top 2–3 project frames.
- `git diff` (working tree vs HEAD) per affected repo, plus commit subjects since the stack was last fully healthy. Record a timestamp whenever all health checks pass.
- Dependency graph from config, e.g. `web -> api -> pricing; api -> casdoor`.
- Optional: redact obvious secrets (bearer tokens, `password=`, long hex/base64 strings) before building the prompt.

### 6.7 LLM call
Use Ollama structured output: pass a JSON schema in `format`, use a low temperature. Incident schema:

```json
{
  "origin_service": "string",
  "propagation_chain": ["string"],
  "file": "string | null",
  "line": "integer | null",
  "likely_cause": "string",
  "suggested_fix": "string",
  "is_config_issue": "boolean",
  "confidence": "low | medium | high"
}
```

Prompt guidance:
- The system prompt explains that this is a multi-service local dev stack, that symptoms often appear downstream of the cause, and that the git diff is strong evidence.
- It must say that `external` services are not to be code-edited; suggest config fixes instead.
- The model must answer only from the provided context and say "unknown" rather than guess files.

### 6.8 Incident card (rich panel)
Shows: origin service (highlighted), propagation chain as `pricing → api → web`, file:line, cause, fix, confidence. Also offers a key/command to copy the full context package as a prompt for a bigger model.

---

## 7. Config schema (`hammock.yaml`)

```yaml
model: <ollama-model-tag>      # e.g. a Qwen coder ~7B or a Gemma tag
correlation_window_seconds: 5
services:
  casdoor:
    cmd: docker run --rm -p 8000:8000 casbin/casdoor-all-in-one
    health: http://localhost:8000
    external: true
  pricing:                     # Rust
    cwd: ./pricing
    cmd: cargo run
    env: { RUST_BACKTRACE: "1" }
    watch: ["src/**/*.rs", "Cargo.toml"]
    health: http://localhost:7001/health
  api:                         # Django
    cwd: ./api
    cmd: python manage.py runserver 8001
    health: http://localhost:8001/health
    depends_on: [casdoor, pricing]
  web:                         # React
    cwd: ./web
    cmd: npm run dev
    depends_on: [api]
```

Fields: `cmd` (required), `cwd`, `env`, `health` (URL), `depends_on`, `watch` (globs; if omitted, rely on the service's own reload), `external` (bool, default false), `language` (optional; inferred from cmd/cwd).

---

## 8. Restart behaviour
- On a change matching a service's `watch` globs: stop it (SIGTERM, then SIGKILL after a timeout), start it, wait for health.
- Dependents are marked "waiting" until the restarted service is healthy again.
- Don't restart Django/React by default; they reload themselves.
- `hammock restart <svc>` for manual restarts.

---

## 9. Demo stack (fake, built from scratch)

React shop page → Django orders API → Rust pricing service, with login through Casdoor.

**Planted bug 1: cross-language (the demo's highlight).**
Rename a field in the Rust pricing response (`total` → `total_amount`). Django raises `KeyError: 'total'`; React shows `undefined`. A developer would start in Django, but Hammock uses the timeline and the git diff to report: *origin = pricing; chain = pricing → api → web; fix = update the field name or the Django consumer.*

**Planted bug 2: auth config.**
Change the redirect URI (or client secret) in Casdoor's app config. Every service returns 401s. Hammock reports `is_config_issue: true`, origin = casdoor, and suggests a config fix, not code.

Keep each bug toggleable (e.g. a branch or env flag) so the demo can be recorded repeatably. Target: a demo video under 2 minutes showing both.

---

## 10. Timeline (deadline Oct 25, 11:59 PM PDT)
- **Thu Oct 22:** config, runner, log bus, health checks, Rust restart on change; scaffold the demo services.
- **Fri Oct 23:** detectors (Python, Rust, HTTP) with fixture tests; correlation; context package; git info.
- **Sat Oct 24:** Ollama integration with JSON schema; incident card; demo bugs; pick a model by testing both bugs; record the demo. Add `explain` if time allows.
- **Sun Oct 25:** README, write the DEV post, submit with margin.

Rule: **no nice-to-haves until bug 1 works end to end.**

---

## 11. Measure the "lazy payoff" (needed for the post)
Time both demo bugs manually (find terminals, read logs, build an LLM prompt by hand, find the root cause) vs with Hammock. Also record terminals and commands needed to start the stack before and after (e.g. "5 terminals and 5 commands" → "1 command"). Use real numbers.

---

## 12. DEV post outline (matches the official template)
1. **What I Built:** open with the real pain (juggling terminals; the error shows in Django but starts in Rust; new devs are lost). Then the one-line pitch.
2. **Demo:** video plus incident card screenshots; Mermaid flow diagram if built.
3. **Code:** embed the GitHub repo.
4. **How I Built It:** the architecture, the "code does the heavy lifting, the model reasons on a small package" insight, parsers per language, correlation, structured JSON output, model choice and why.
5. **Why Does Open Innovation Matter?:** logs hold secrets, customer data, and proprietary stack traces, so a local model makes this allowed; it works offline; costs nothing to run per error; and the weights don't change under you.
6. **My Agent Session:** DevRelay link, if saved.
7. **The Lazy Payoff:** the measured numbers from Section 11.
8. **Prize Categories:** only those genuinely used.

---

## 13. Working conventions for Claude
- Build the smallest working vertical slice first, then widen.
- Add `tests/fixtures` with realistic captured log output for each detector, and test the regexes against them.
- Keep dependencies minimal. No LangChain/LlamaIndex; plain Ollama calls are enough.
- The app must degrade gracefully: no Ollama, no git repo, or a service without health check should never crash Hammock.
- Ask the author before adding new scope.