from pathlib import Path

import pytest

from hammock.config import Config, ConfigError, load_config


def make(services: dict) -> Config:
    return Config.model_validate({"services": services})


def test_layers_follow_depends_on():
    cfg = make({
        "web": {"cmd": "x", "depends_on": ["api"]},
        "api": {"cmd": "x", "depends_on": ["db", "pricing"]},
        "pricing": {"cmd": "x"},
        "db": {"health": "tcp://localhost:5432"},
    })
    assert cfg.layers(list(cfg.services)) == [["pricing", "db"], ["api"], ["web"]]


def test_cycle_is_rejected():
    with pytest.raises(ValueError, match="cycle"):
        make({"a": {"cmd": "x", "depends_on": ["b"]}, "b": {"cmd": "x", "depends_on": ["a"]}})


def test_unknown_dependency_is_rejected():
    with pytest.raises(ValueError, match="unknown service 'nope'"):
        make({"a": {"cmd": "x", "depends_on": ["nope"]}})


def test_check_only_needs_health():
    with pytest.raises(ValueError, match="check-only"):
        make({"db": {}})


@pytest.mark.parametrize("url", ["localhost:8000", "ftp://x:1", "tcp://localhost"])
def test_bad_health_urls_are_rejected(url):
    with pytest.raises(ValueError, match="health URL"):
        make({"api": {"cmd": "x", "health": url}})


def test_optional_services_are_opt_in():
    cfg = make({
        "api": {"cmd": "x"},
        "admin": {"cmd": "x", "optional": True, "depends_on": ["api"]},
    })
    assert cfg.select() == ["api"]
    assert cfg.select(["admin"]) == ["api", "admin"]
    with pytest.raises(ConfigError):
        cfg.select(["typo"])


def test_required_service_cannot_depend_on_optional():
    with pytest.raises(ValueError, match="optional"):
        make({"admin": {"cmd": "x", "optional": True}, "api": {"cmd": "x", "depends_on": ["admin"]}})


def test_env_values_become_strings():
    cfg = make({"pricing": {"cmd": "x", "env": {"RUST_BACKTRACE": 1, "DEBUG": True}}})
    assert cfg.services["pricing"].env == {"RUST_BACKTRACE": "1", "DEBUG": "True"}


def test_cwd_resolves_relative_to_config_file(tmp_path: Path):
    (tmp_path / "stack").mkdir()
    path = tmp_path / "stack" / "hammock.yaml"
    path.write_text("services:\n  api:\n    cmd: x\n    cwd: ../api\n  web:\n    cmd: x\n")
    cfg, _ = load_config(path)
    assert cfg.services["api"].cwd == tmp_path / "api"
    assert cfg.services["web"].cwd == tmp_path / "stack"


def test_unknown_field_is_a_config_error(tmp_path: Path):
    path = tmp_path / "hammock.yaml"
    path.write_text("services:\n  api:\n    cmd: x\n    helth: http://localhost\n")
    with pytest.raises(ConfigError, match="helth"):
        load_config(path)
