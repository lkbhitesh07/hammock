# Hammock demo stack

A small fake shop built from scratch to show Hammock tracing an error back to the service it started in.

```
api (Django, :8010)  ──POST /quote──▶  pricing (Rust/axum, :7001)
```

Coming next: a React shop page (`web`) in front of `api`, and Casdoor login.

## Requirements

The repo's Nix dev shell has everything: run `nix develop` at the repo root first.

Without Nix you need `cargo` (Rust stable) and `uv` on your `PATH` (uv creates `api/.venv` on first run), plus `curl`.

## Run it

```sh
cd demo
hammock up               # terminal 1
./order.sh               # terminal 2: places an order, prints the response
```

The scripts need bash: run them as `./order.sh`, not `sh order.sh`.

Every request carries an `X-Request-ID`. Both services log it, so one request can be followed across the stack.

## Planted bugs

```sh
./bug.sh 1        # pricing renames `total` to `total_amount` in its response
./bug.sh status
./bug.sh reset
```

Bugs are real source edits, not env flags, so they show up in `git diff` the way a teammate's regression would.

**Bug 1 (cross-language):** the change is in Rust, but the error shows up in Django as `KeyError: 'total'` in `orders/views.py`. Pricing keeps answering 200, so nothing in its own logs looks wrong.

Until Hammock's file watcher exists, restart pricing by hand after planting: `hammock restart pricing`.
