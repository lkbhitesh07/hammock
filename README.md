# Hammock 🦥

One command runs your whole local stack. When something breaks, a local LLM tells you which service the error actually started in.

```sh
nix run github:<owner>/hammock -- up          # with Nix
uvx --from git+https://github.com/<owner>/hammock hammock up   # without Nix
```

Work in progress.
