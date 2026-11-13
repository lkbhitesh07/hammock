# Hammock 🦥

**One command runs your whole local stack. When something breaks, Hammock tells you which service the error actually started in.**

If you run a few services locally (an API, a worker, a Rust service, a frontend, an auth server), you probably know this routine:

- one terminal per service, each started and restarted by hand;
- an error appears in one service, but the cause is in another, and you hunt through five terminals to find it;
- you paste logs from several places into a chat window and explain the whole stack each time.

Hammock replaces the terminals with one command and does the hunting for you. It watches every service's output, catches errors, and follows a request across services. It puts the evidence on one card: what failed, where, which services the request passed through, and what changed in git. Optionally, a **local** LLM reads that card and names the service where the problem started, how it spread, and what to fix. Your logs never leave your machine.

> **Status: work in progress.** Running the stack, error detection, cross-service correlation, incident cards and the optional local model all work. Not built yet: automatic restart on file changes (use `hammock restart <service>` for now), a `hammock logs` command, and the demo's React and Casdoor services. See [What's next](#whats-next).

---

## Contents

- [Quick start (the demo)](#quick-start-the-demo)
- [Using it on your own stack](#using-it-on-your-own-stack)
- [Commands](#commands)
- [The local model (optional)](#the-local-model-optional)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [What's next](#whats-next)

---

## Quick start (the demo)

The repo has a small fake shop: a Django orders API that calls a Rust pricing service. It comes with a planted bug that starts in Rust and shows up in Python.

```
api (Django, :8010)  ──POST /quote──▶  pricing (Rust/axum, :7001)
```

### 1. Get a shell with everything installed

**With Nix (recommended):** Python, uv, Rust and Ollama in one step.

```sh
git clone https://github.com/lkbhitesh07/hammock.git hammock && cd hammock
nix develop
```

**Without Nix:** you need Python 3.11+, [uv](https://docs.astral.sh/uv/) and Rust (`cargo`).

```sh
git clone https://github.com/lkbhitesh07/hammock.git hammock && cd hammock
uv sync --all-extras
source .venv/bin/activate
```

### 2. Start the stack

```sh
cd demo
hammock up
```

Hammock starts `pricing` first and waits until its health check passes, then starts `api`. The first `cargo build` takes a few minutes. Every log line is prefixed with its service name, and each service gets its own color:

```
hammock │ ▶ pricing started (pid 41210): cargo run --quiet
hammock │ ⏸ api waiting for pricing
pricing │ INFO pricing listening on http://127.0.0.1:7001
hammock │ ✓ pricing healthy (94.2s)
hammock │ ▶ api started (pid 41388): uv run --quiet python manage.py runserver 8010
    api │ Starting development server at http://127.0.0.1:8010/
hammock │ ✓ api healthy (2.1s)
hammock │ ✓ stack ready in 96.4s
```

In a second terminal, place an order:

```sh
cd demo
./order.sh
```

### 3. Plant the bug and watch Hammock find it

```sh
./bug.sh 1                 # pricing renames the field `total` to `total_amount`
hammock restart pricing
./order.sh
```

Django now crashes with `KeyError: 'total'`. A developer would start digging in Django, because that's where the traceback is. Hammock prints this card in the `hammock up` terminal:

```
╭─ 🔎 incident #1 · KeyError: 'total' ──────────────────────────────────────────╮
│ Detected  api  python-traceback                                               │
│ Error     KeyError: 'total'                                                   │
│ Where     orders/views.py:59 in build_order                                   │
│ Request   web-fcf10ea9 · seen in pricing, api                                 │
│ Changed   uncommitted changes in pricing                                      │
│                                                                               │
│ no diagnosis: set `model:` in hammock.yaml (an Ollama tag) to have a local    │
│ model explain it                                                              │
╰─────────────────────────── copy as prompt for a bigger model: hammock prompt ─╯
```

Even without a model, the card points the right way: the failing request passed through **pricing**, and pricing is the service with **uncommitted changes**. That's where the bug is.

With a model configured, a second card follows with the model's diagnosis:

| Row | Meaning |
|---|---|
| **Origin** | The service where the problem started, e.g. `pricing (surfaced in api)` |
| **Chain** | How it spread, e.g. `pricing → api` |
| **Where** | The file and line, if the evidence shows them |
| **Cause** | What went wrong |
| **Fix** | What to change |
| **Kind** | Marked "configuration issue" when the fix is in config, not code |
| **Confidence** | low, medium or high, plus the model name and how long it took |

Clean up with `./bug.sh reset` and `hammock restart pricing`. Press Ctrl-C in the `hammock up` terminal to stop everything.

---

## Using it on your own stack

Create a `hammock.yaml` in the folder above your repos, or anywhere you like. Here is an example with the common cases:

```yaml
model: gemma3:4b                 # optional: an Ollama tag (see "The local model")
correlation_window_seconds: 5    # how far around an error to look for related lines

services:
  # Already running (e.g. via Docker or systemd). Hammock only checks it's up.
  postgres:
    health: tcp://localhost:5432

  # A third-party service: Hammock suggests config fixes for it, never code edits.
  auth:
    cwd: ../auth-server
    cmd: docker compose up
    health: http://localhost:8001
    external: true
    stop_timeout: 30

  # A Rust service with a slow first build.
  core:
    cwd: ../core-service
    cmd: cargo run
    env: { RUST_BACKTRACE: "1" }   # gives Hammock real stack frames
    health: http://localhost:8080/health
    health_timeout: 600
    depends_on: [postgres]

  # Django.
  api:
    cwd: ../api
    cmd: source .venv/bin/activate && python manage.py runserver
    health: http://localhost:8000/health
    depends_on: [postgres, auth, core]

  # A frontend.
  web:
    cwd: ../web
    cmd: npm run dev
    health: http://localhost:5173
    depends_on: [api]

  # Only started when asked: hammock up --with admin
  admin:
    cwd: ../admin
    cmd: yarn start
    env: { PORT: "3002" }
    health: http://localhost:3002
    optional: true
    depends_on: [api]
```

### Fields

| Field | Default | What it does |
|---|---|---|
| `cmd` | — | The command to run, through bash, so `source .env && …` works. **Leave it out** to make the service check-only: Hammock checks it but never starts or stops it. |
| `cwd` | config's folder | The working directory, relative to the config file. |
| `env` | `{}` | Extra environment variables. |
| `health` | — | `http(s)://…` (any non-5xx answer counts as healthy) or `tcp://host:port`. Without it, a service counts as ready as soon as it starts. |
| `health_timeout` | `120` | Seconds to wait for health. Raise it for a cold `cargo build`. |
| `stop_timeout` | `10` | Seconds between Ctrl-C and a forced kill. Raise it for `docker compose`. |
| `depends_on` | `[]` | Services that must be healthy before this one starts. |
| `external` | `false` | Third-party code: the model may only suggest config fixes. |
| `optional` | `false` | Started only with `hammock up --with <name>`. |

Check the config without starting anything:

```sh
hammock config
```

```
/path/to/hammock.yaml  model: not set
  1. pricing
       /path/to/demo/pricing$ cargo run --quiet
  2. api after pricing
       /path/to/demo/api$ uv run --quiet python manage.py runserver 8010
```

### Keep your config private

A team's config usually holds internal paths and service names. Keep it out of shared repos:

- Name it `hammock.local.yaml`. This repo's `.gitignore` already ignores `*.local.yaml`.
- Or keep it outside any repo and point Hammock at it: `hammock up -c ~/work/hammock.local.yaml`.

Incidents are saved next to the config, in `.hammock/incidents/`. They contain your logs, code snippets and git diffs, so add `.hammock/` to `.gitignore` wherever your config lives.

---

## Commands

Run every command from the config's folder, or point at the config with `-c path/to/hammock.yaml`.

| Command | What it does |
|---|---|
| `hammock up` | Starts the stack in dependency order and streams all logs. Ctrl-C stops everything Hammock started. |
| `hammock up --with admin` | Also starts an optional service. Repeatable. |
| `hammock up --no-llm` | Never calls the local model, even if the config sets one. You can also set `HAMMOCK_NO_LLM=1` in your shell profile. |
| `hammock status` | Shows each service's state, PID, uptime and health URL. |
| `hammock restart <service>` | Restarts one service and waits for its health check. Services that depend on it keep running, so they may log errors until it's back. |
| `hammock down` | Stops a running stack from another terminal. |
| `hammock prompt [N]` | Copies the latest incident (or incident N) to the clipboard as a ready-to-paste prompt for any chat model. |
| `hammock prompt --print` | Prints the prompt instead of copying it. |
| `hammock config` | Validates the config and shows what `up` would start, in order. |

---

## The local model (optional)

Hammock is useful without any model: detection, correlation and the incident card are all plain code. The model adds the final step, naming where the problem started and what to fix.

It runs through [Ollama](https://ollama.com), on your machine. **Nothing is sent to the cloud.** That matters because local logs contain internal URLs, tokens, customer data and stack traces from your own code.

### Set it up

```sh
# 1. Install Ollama. The Nix dev shell already has it; otherwise see ollama.com.
# 2. Start the server and keep the terminal open:
ollama serve

# 3. Download a model, once:
ollama pull qwen2.5-coder:7b      # about 4.7 GB
# or a smaller, faster one:
ollama pull gemma3:4b             # about 3.3 GB
```

Then set the model in `hammock.yaml`:

```yaml
model: gemma3:4b
```

Without Nix, also install the Python client: `pip install 'hammock[llm]'`, or `uv sync --all-extras`.

### Is my laptop fast enough?

Be realistic about this. The model runs on your CPU or GPU, and small laptops are slow.

- **Apple Silicon or a GPU:** a 7B model answers in seconds.
- **Desktop CPU:** expect about a minute per incident.
- **Thin-and-light laptop CPU:** use a 4B model. Measured on a 15 W Intel i5-1335U with no GPU:

  | Model | Bug 1 (`KeyError` in api, caused by pricing) | `cargo: command not found` crash |
  |---|---|---|
  | `gemma3:4b` | ✅ origin pricing, chain pricing → api, correct cause · **131s** | ✅ origin pricing, correct cause · **63s** |
  | `qwen2.5-coder:7b` | ❌ no answer within the 5-minute limit | ✅ origin pricing, correct cause · 187s |

  qwen ran while a dev stack was compiling on the same laptop, so its numbers are worse than an idle machine would give. Even so, a 7B model is too slow for this class of laptop. Both models called the `cargo` crash a code problem instead of a setup problem.

If your machine is slow, you have options:

1. **Use a smaller model,** such as `gemma3:4b`.
2. **Use a stronger machine on your network.** Run Ollama there, then `export OLLAMA_HOST=http://that-machine:11434` before `hammock up`. Your logs still stay inside your network.
3. **Skip the model.** Use `hammock up --no-llm` and read the card. When you want help, run `hammock prompt` and paste it into whichever model you're allowed to use. Hammock warns that the prompt contains your logs and code before it copies anything.

The model runs in the background, one incident at a time, so logs keep streaming while it thinks. If Ollama isn't running, the model isn't downloaded, or the answer takes too long, you get a one-line explanation and Hammock carries on.

### Handy Ollama commands

| Command | What it does |
|---|---|
| `ollama list` | Lists downloaded models. |
| `ollama ps` | Shows models loaded in memory, and whether they run on CPU or GPU. |
| `ollama run gemma3:4b` | Chats with a model in the terminal (`/bye` to quit). |
| `ollama stop <model>` | Unloads a model to free RAM. |
| `ollama rm <model>` | Deletes a model from disk (stored in `~/.ollama/models`). |

---

## How it works

Small local models give poor answers when handed 10,000 log lines. So **plain code does the heavy lifting**, and the model only reasons over a small, focused package:

1. **Run and collect.** Hammock owns every service's output and stamps each line with its own arrival time. That gives one shared clock without parsing each tool's timestamp format.
2. **Detect.** Per-language parsers catch:
   - Python and Django tracebacks;
   - Rust panics and compile errors;
   - HTTP 5xx responses and bursts of 401s or 403s;
   - generic `ERROR` lines;
   - services that exit on their own.
3. **Group.** Errors that arrive together become one incident. A repeat within 30 seconds counts the existing incident again instead of opening a new one.
4. **Correlate.** Hammock follows the request ID (`X-Request-ID`) across every service. Without one, it takes lines from all services within a few seconds of the error.
5. **Build the context package** (usually 1–3k tokens):
   - the error;
   - the cross-service timeline;
   - code around the project's stack frames, skipping library frames;
   - `git diff` for each involved service;
   - the dependency graph.

   Obvious secrets are redacted.
6. **Ask the model.** Hammock sends the package with a strict JSON schema. It then checks the answer against the evidence: unknown services and files that appear nowhere in the context are dropped, and an `external` service always counts as a config issue.

---

## Troubleshooting

**A service shows "waiting" and its page doesn't load.**
It's waiting for a dependency that isn't healthy yet. Run `hammock status`: the dependency is probably still `starting`, for example during a long first `cargo build`. Hammock starts the waiting services as soon as it's healthy. If you don't need that dependency right now, remove it from `depends_on`.

**A service shows "healthy" but doesn't work.**
Another program may be answering on the same port. Hammock can't tell the difference yet. Check with `ss -ltnp | grep :<port>` (Linux) or `lsof -i :<port>` (macOS).

**`… exited on its own (code 127)`.**
Exit code 127 means the command wasn't found. Make sure the tool (cargo, node, uv…) is on `PATH` in the shell where you run Hammock. With Nix, run `nix develop` first.

**`./order.sh` or `./bug.sh` misbehaves.**
They need bash: run `./order.sh`, not `sh order.sh`.

**No diagnosis card.**
The yellow card's last line explains why. The usual reasons:
- no `model:` set;
- Ollama isn't running (`ollama serve`);
- the model isn't downloaded (`ollama pull <tag>`);
- the answer took longer than 5 minutes (see [Is my laptop fast enough?](#is-my-laptop-fast-enough)).

---

## What's next

- Restart a service automatically when its files change, using `watch:` globs, mainly for Rust.
- `hammock logs <service>`, and a periodic "still waiting on X" message.
- A warning when a port is already taken by another program.
- Demo additions: a React shop page, Casdoor login, and bug 2 (an auth config mistake that makes every service return 401).
- Optional: a browser reporter for React runtime errors, a Mermaid diagram of a request's path, `hammock doctor`.
