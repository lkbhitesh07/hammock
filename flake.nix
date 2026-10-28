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
          dependencies = with python.pkgs; [ typer rich pydantic pyyaml httpx ];
        };
      in
      {
        # nix run github:<owner>/hammock -- up
        packages.default = hammock;
        apps.default = flake-utils.lib.mkApp { drv = hammock; };

        # nix develop: Python + uv; uv builds .venv from uv.lock using this Python.
        devShells.default = pkgs.mkShell {
          packages = [ python pkgs.uv ];
          shellHook = ''
            export UV_PYTHON="${python}/bin/python"
            export UV_PYTHON_DOWNLOADS=never
            uv sync --quiet
            source .venv/bin/activate
          '';
        };
      });
}
