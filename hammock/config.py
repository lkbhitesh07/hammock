"""hammock.yaml: pydantic models, validation, and dependency ordering."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .health import validate_url

DEFAULT_CONFIG_NAMES = ("hammock.yaml", "hammock.yml", "hammock.local.yaml")


class ConfigError(Exception):
    pass


class Service(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: str | None = None  # omitted = check-only (already running; Hammock only health-checks it)
    cwd: Path | None = None
    env: dict[str, str] = Field(default_factory=dict)
    health: str | None = None  # http(s)://... or tcp://host:port
    health_timeout: float = 120.0
    stop_timeout: float = 10.0
    depends_on: list[str] = Field(default_factory=list)
    watch: list[str] = Field(default_factory=list)
    external: bool = False
    optional: bool = False
    language: str | None = None

    @field_validator("env", mode="before")
    @classmethod
    def _stringify_env(cls, v: object) -> object:
        # YAML turns `RUST_BACKTRACE: 1` into an int; the environment needs strings.
        if isinstance(v, dict):
            return {str(k): "" if val is None else str(val) for k, val in v.items()}
        return v

    @field_validator("health")
    @classmethod
    def _valid_health_url(cls, v: str | None) -> str | None:
        if v is not None:
            validate_url(v)
        return v

    @model_validator(mode="after")
    def _check_only_needs_health(self) -> Service:
        if self.cmd is None and self.health is None:
            raise ValueError("a service without `cmd` is check-only and needs a `health` URL")
        return self

    @property
    def check_only(self) -> bool:
        return self.cmd is None


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    correlation_window_seconds: float = 5.0
    services: dict[str, Service]

    @model_validator(mode="after")
    def _check_graph(self) -> Config:
        for name, svc in self.services.items():
            for dep in svc.depends_on:
                if dep not in self.services:
                    raise ValueError(f"service '{name}' depends on unknown service '{dep}'")
                if self.services[dep].optional and not svc.optional:
                    raise ValueError(f"service '{name}' is required but depends on optional service '{dep}'")
        _layers(self.services, set(self.services))  # raises on cycles
        return self

    def select(self, with_optional: list[str] | None = None) -> list[str]:
        """Required services plus the requested optional ones (and their dependencies)."""
        with_optional = with_optional or []
        for name in with_optional:
            if name not in self.services:
                raise ConfigError(f"unknown service '{name}' in --with")
        chosen = {n for n, s in self.services.items() if not s.optional} | set(with_optional)
        stack = list(chosen)
        while stack:
            for dep in self.services[stack.pop()].depends_on:
                if dep not in chosen:
                    chosen.add(dep)
                    stack.append(dep)
        return [n for n in self.services if n in chosen]

    def layers(self, names: list[str]) -> list[list[str]]:
        """Startup layers: every service's dependencies sit in an earlier layer."""
        return _layers(self.services, set(names))


def _layers(services: dict[str, Service], names: set[str]) -> list[list[str]]:
    remaining = {n: {d for d in services[n].depends_on if d in names} for n in names}
    order = [n for n in services if n in names]  # keep yaml order inside a layer
    layers: list[list[str]] = []
    while remaining:
        ready = [n for n in order if n in remaining and not remaining[n]]
        if not ready:
            raise ValueError(f"dependency cycle between: {', '.join(sorted(remaining))}")
        layers.append(ready)
        for n in ready:
            del remaining[n]
        for deps in remaining.values():
            deps.difference_update(ready)
    return layers


def find_config(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        for name in DEFAULT_CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    raise ConfigError(f"no {' / '.join(DEFAULT_CONFIG_NAMES)} found in {start} or its parents")


def load_config(path: Path | None = None) -> tuple[Config, Path]:
    """Load and validate a config; relative `cwd`s resolve against the config file's directory."""
    path = (path or find_config()).resolve()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        config = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid {path.name}:\n{e}") from e
    for svc in config.services.values():
        svc.cwd = (path.parent / (svc.cwd or ".")).resolve()
    return config, path
