{
  description = "Hammock: run your local multi-service stack and explain cross-service errors with a local LLM";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python312;

        # Dependencies come from nixpkgs here; uv.lock pins them for non-Nix users.
        hammock = python.pkgs.buildPythonApplication {
          pname = "hammock";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ python.pkgs.hatchling ];
          dependencies = with python.pkgs; [ typer rich pydantic pyyaml httpx ollama ];
        };
      in
      {
        # nix run github:lkbhitesh07/hammock -- up
        packages.default = hammock;
        apps.default = flake-utils.lib.mkApp { drv = hammock; };

        # nix develop: Python + uv; uv builds .venv from uv.lock using this Python.
        devShells.default = pkgs.mkShell {
          # cargo/rustc: the demo stack's pricing service is Rust.
          # ollama: optional local model; start it with `ollama serve` when you want diagnoses.
          packages = [ python pkgs.uv pkgs.cargo pkgs.rustc pkgs.ollama ];
          shellHook = ''
            export UV_PYTHON="${python}/bin/python"
            export UV_PYTHON_DOWNLOADS=never
            uv sync --quiet --all-extras
            source .venv/bin/activate
          '';
        };
      });
}
