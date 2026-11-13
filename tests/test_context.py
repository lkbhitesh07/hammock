import shutil
import subprocess
from pathlib import Path

import pytest

from hammock.config import Config
from hammock.context import (BUDGET, build_context, clip, compact_error, redact, related_services,
                             _timeline_section)
from hammock.correlate import build_timeline
from hammock.detectors import ServiceDetector
from hammock.gitinfo import git_info
from hammock.incidents import Incident
from hammock.logbus import LogLine, strip_ansi

FIXTURES = Path(__file__).parent / "fixtures"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def lines_of(service: str, texts: list[str], start: float = 100.0, step: float = 0.001) -> list[LogLine]:
    return [LogLine(service, t, strip_ansi(t), "stderr", start + i * step, 1.7e9 + start + i * step)
            for i, t in enumerate(texts)]


def events_of(lines: list[LogLine]):
    detectors: dict[str, ServiceDetector] = {}
    events = []
    for line in lines:
        events += detectors.setdefault(line.service, ServiceDetector(line.service)).feed(line)
    return events + [e for d in detectors.values() for e in d.flush()]


def incident_from(lines: list[LogLine], window: float = 5.0) -> Incident:
    events = events_of(lines)
    return Incident(1, events, build_timeline(lines, events, window), 1.7e9)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


# --- error compaction ------------------------------------------------------------------------

def test_python_traceback_drops_library_frames_and_carets():
    events = events_of(lines_of("api", (FIXTURES / "python/django_keyerror.log").read_text().splitlines()))
    text = compact_error(events[0])
    assert "site-packages" not in text
    assert "[... 2 library frames omitted ...]" in text
    assert "^^^" not in text and "~~~" not in text
    assert 'line 59, in build_order\n    "total_cents": quote["total"],\nKeyError: \'total\'' in text


def test_rust_panic_keeps_only_project_frames():
    events = events_of(lines_of("pricing", (FIXTURES / "rust/panic_backtrace.log").read_text().splitlines()))
    text = compact_error(events[0])
    assert text.splitlines()[:2] == [
        "thread 'tokio-rt-worker' (165006) panicked at src/main.rs:59:43:",
        "called `Option::unwrap()` on a `None` value",
    ]
    assert "library frames omitted" in text
    assert "src/main.rs:90:33 in pricing::request_log::{{closure}}" in text
    assert ".cargo/registry" not in text and len(text) < 600


def test_clip_keeps_head_and_tail():
    text = "HEAD" + "x" * 1000 + "TAIL"
    clipped = clip(text, 100)
    assert clipped.startswith("HEAD") and clipped.endswith("TAIL") and "omitted" in clipped
    assert clip("short", 100) == "short"


# --- timeline budget -------------------------------------------------------------------------

def test_timeline_keeps_errors_and_requests_when_over_budget():
    lines = lines_of("api", [f"INFO chatter {i}" for i in range(500)], start=96.0, step=0.008)
    lines += lines_of("pricing", ["INFO request request_id=web-1 status=200"], start=99.9)
    lines += lines_of("api", ["ERROR request_id=web-1 boom"], start=100.0)
    incident = incident_from(lines)
    text = _timeline_section(incident, 800)
    assert "[pricing] INFO request request_id=web-1 status=200" in text
    assert "<<< error output, shown above: ERROR request_id=web-1 boom >>>" in text
    assert "lines omitted" in text
    assert len(text) < 1000


def test_timeline_collapses_repeats():
    lines = lines_of("api", ["WARN retrying"] * 5 + ["Error: gave up"])
    text = _timeline_section(incident_from(lines), 5000)
    assert text.count("retrying") == 1 and "(×5)" in text


# --- git -------------------------------------------------------------------------------------

@needs_git
def test_git_info_diff_untracked_and_commits(tmp_path):
    svc = tmp_path / "svc"
    svc.mkdir()
    (svc / "main.rs").write_text("total: u64,\n")
    (svc / "Cargo.lock").write_text("v1\n")
    (tmp_path / "other.txt").write_text("outside the service\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "first commit")
    (svc / "main.rs").write_text("total_amount: u64,\n")
    (svc / "Cargo.lock").write_text("v2\n")
    (svc / "new.rs").write_text("fn x() {}\n")
    (tmp_path / "other.txt").write_text("changed too\n")

    info = git_info(svc)
    assert info is not None
    assert info.relpath == "svc"
    assert "+total_amount: u64," in info.diff
    assert "Cargo.lock" not in info.diff and "other.txt" not in info.diff
    assert info.untracked == ["new.rs"]
    assert [c.subject for c in info.commits] == ["first commit"]


@needs_git
def test_git_info_without_commits_or_repo(tmp_path):
    assert git_info(tmp_path) is None  # not a repository
    git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    info = git_info(tmp_path)
    assert info is not None and info.diff == "" and info.commits == [] and info.untracked == ["a.py"]
    assert git_info(tmp_path / "missing") is None


# --- whole package ---------------------------------------------------------------------------

@needs_git
def test_bug1_package_has_the_cross_service_evidence(tmp_path):
    api, pricing = tmp_path / "api", tmp_path / "pricing"
    (api / "orders").mkdir(parents=True)
    (pricing / "src").mkdir(parents=True)
    views = "\n".join(f"# line {n}" for n in range(1, 59)) + '\n        "total_cents": quote["total"],\n'
    (api / "orders" / "views.py").write_text(views)
    (pricing / "src" / "main.rs").write_text("struct Quote {\n    total: u64,\n}\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "demo")
    (pricing / "src" / "main.rs").write_text("struct Quote {\n    total_amount: u64,\n}\n")

    cfg = Config.model_validate({"services": {
        "pricing": {"cmd": "cargo run", "cwd": str(pricing)},
        "api": {"cmd": "python manage.py runserver", "cwd": str(api), "depends_on": ["pricing"]},
        "casdoor": {"cmd": "docker run casdoor", "cwd": str(tmp_path), "external": True},
    }})
    lines = lines_of("pricing", ['INFO request request_id=web-1 path="/quote" status=200'], start=99.9)
    lines += lines_of("api", [
        "ERROR [django.request] request_id=web-1 Internal Server Error: /api/orders",
        "Traceback (most recent call last):",
        f'  File "{api}/orders/views.py", line 59, in build_order',
        '    "total_cents": quote["total"],',
        "KeyError: 'total'",
    ])
    incident = incident_from(lines)
    assert related_services(incident, cfg) == ["pricing", "api"]

    package = build_context(incident, cfg)
    text = package.render()
    assert "Dependency graph: api -> pricing" in text
    assert "- casdoor (not involved): container, external: third-party, fix via configuration only" in text
    assert "Location in api's code: orders/views.py:59 in build_order" in text
    assert 'File "orders/views.py", line 59' in text  # paths shortened in the error output
    assert '>>  59 |         "total_cents": quote["total"],' in text
    assert "+    total_amount: u64," in text
    assert "### api (api)\nno uncommitted changes" in text
    assert text.index("### pricing") < text.index("### api (api)")  # changed services first
    assert package.approx_tokens < sum(BUDGET.values()) // 4 + 1000


def test_package_without_git_or_source_still_builds(tmp_path):
    cfg = Config.model_validate({"services": {"svc": {"cmd": "x", "cwd": str(tmp_path)}}})
    incident = incident_from(lines_of("svc", ["Error: boom"]))
    text = build_context(incident, cfg).render()
    assert "No source available" in text
    assert "not a git repository" in text or "no uncommitted changes" in text


# --- redaction -------------------------------------------------------------------------------

@pytest.mark.parametrize("secret, keeps", [
    ("Authorization: Bearer abcdef1234567890xyz", "Bearer [REDACTED]"),
    ("password=hunter22 user=bob", "password=[REDACTED] user=bob"),
    ('CASDOOR_CLIENT_SECRET: "s3cr3t-value"', 'CASDOOR_CLIENT_SECRET: "[REDACTED]"'),
    ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl", "[REDACTED_JWT]"),
    ("key 3f9a6c1e2b7d4f8a9c0e1d2b3a4f5e6d7c8b9a0f", "key [REDACTED_HEX]"),
    ("aws AKIAIOSFODNN7EXAMPLEwJalrXUtnFEMIK7MDENGbPxRfiCY", "aws [REDACTED]"),
])
def test_redact_secrets(secret, keeps):
    assert keeps in redact(secret)


@pytest.mark.parametrize("text", [
    "request_id=web-d15af5e8 index 82913ba..6b25790 100644",
    "/home/dev/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/axum-0.8.9/src/handler/mod.rs",
    "/home/dev/hammock/demo/api/orders/views.py line 59",
    'quote["total"] and the tokens=5 setting',
])
def test_redact_leaves_ordinary_text_alone(text):
    assert redact(text) == text
