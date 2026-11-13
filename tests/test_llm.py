import asyncio
import json
import time
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from hammock.cli import app
from hammock.config import Config
from hammock.correlate import build_timeline
from hammock.detectors import ServiceDetector
from hammock.incidents import Incident
from hammock.investigate import Investigator, run_in_daemon_thread
from hammock.llm import INCIDENT_SCHEMA, SYSTEM_PROMPT, Diagnosis, diagnose, full_prompt, parse_answer
from hammock.logbus import LogBus, LogLine
from hammock.render import diagnosis_card, find_context_file, found_card

CONTEXT = """## Error output
File "orders/views.py", line 59, in build_order
KeyError: 'total'
## What changed (git)
diff --git a/demo/pricing/src/main.rs b/demo/pricing/src/main.rs
-    total: u64,
+    total_amount: u64,
"""

GOOD = {
    "likely_cause": "pricing renamed `total` to `total_amount`; api still reads `total`.",
    "origin_service": "pricing",
    "propagation_chain": ["pricing", "api"],
    "file": "src/main.rs",
    "line": 43,
    "suggested_fix": "Rename the field back to `total` in src/main.rs, or read `total_amount` in views.py.",
    "is_config_issue": False,
    "confidence": "high",
}


def config(**extra) -> Config:
    return Config.model_validate({"model": "test-model", "services": {
        "casdoor": {"cmd": "docker run casdoor", "external": True},
        "pricing": {"cmd": "cargo run"},
        "api": {"cmd": "python manage.py runserver", "depends_on": ["pricing", "casdoor"]},
    }, **extra})


class FakeClient:
    def __init__(self, answer=None, error: Exception | None = None):
        self.answer, self.error, self.calls = answer, error, []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        content = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return {"message": {"role": "assistant", "content": content}}


# --- the call --------------------------------------------------------------------------------

def test_diagnose_sends_schema_low_temperature_and_context():
    client = FakeClient(GOOD)
    d = diagnose(CONTEXT, config(), client)
    assert d.ok and d.origin_service == "pricing" and d.propagation_chain == ["pricing", "api"]
    assert (d.file, d.line, d.confidence) == ("src/main.rs", 43, "high")
    [call] = client.calls
    assert call["model"] == "test-model"
    assert call["format"] == INCIDENT_SCHEMA
    assert call["options"]["temperature"] == 0 and call["options"]["num_ctx"] >= 8192
    assert call["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert CONTEXT in call["messages"][1]["content"]


def test_schema_asks_for_the_cause_before_the_origin():
    assert list(INCIDENT_SCHEMA["properties"])[:2] == ["likely_cause", "origin_service"]
    assert set(INCIDENT_SCHEMA["required"]) == set(INCIDENT_SCHEMA["properties"])


def test_no_model_configured():
    no_model = Config.model_validate({"services": {"a": {"cmd": "x"}}})
    d = diagnose(CONTEXT, no_model, FakeClient(GOOD))
    assert not d.ok and "set `model:`" in d.error


@pytest.mark.parametrize("error, says", [
    (ConnectionError("Failed to connect to Ollama"), "ollama serve"),
    (type("ResponseError", (Exception,), {"status_code": 404})("model 'test-model' not found"),
     "ollama pull test-model"),
    (TimeoutError("timed out"), "longer than"),
    (RuntimeError("boom"), "Ollama error: boom"),
])
def test_failures_become_explanations(error, says):
    d = diagnose(CONTEXT, config(), FakeClient(error=error))
    assert not d.ok and says in d.error


# --- checking the answer ---------------------------------------------------------------------

def test_invalid_json():
    assert parse_answer("not json", "m", config(), CONTEXT).error == "'m' did not return valid JSON"
    assert parse_answer("[1, 2]", "m", config(), CONTEXT).error


def test_unknown_services_are_dropped_and_confidence_lowered():
    d = parse_answer(json.dumps({**GOOD, "origin_service": "billing", "propagation_chain": ["billing", "api"]}),
                     "m", config(), CONTEXT)
    assert d.origin_service is None and d.propagation_chain == ["api"] and d.confidence == "low"
    assert "origin 'billing' is not a service in this stack" in d.notes


def test_chain_starts_at_the_origin():
    d = parse_answer(json.dumps({**GOOD, "propagation_chain": ["api"]}), "m", config(), CONTEXT)
    assert d.propagation_chain == ["pricing", "api"]


def test_invented_files_are_dropped():
    d = parse_answer(json.dumps({**GOOD, "file": "src/quote.rs", "line": 12}), "m", config(), CONTEXT)
    assert d.file is None and d.line is None
    assert any("src/quote.rs" in n for n in d.notes)


@pytest.mark.parametrize("file", [None, "", "unknown", "null"])
def test_unknown_file_is_none(file):
    d = parse_answer(json.dumps({**GOOD, "file": file}), "m", config(), CONTEXT)
    assert d.file is None and not d.notes


def test_external_origin_is_always_a_config_issue():
    d = parse_answer(json.dumps({**GOOD, "origin_service": "casdoor", "propagation_chain": ["casdoor", "api"],
                                 "file": None, "is_config_issue": False}), "m", config(), CONTEXT)
    assert d.is_config_issue and "casdoor is external, so this is a config issue" in d.notes


def test_bad_confidence_and_line_values():
    d = parse_answer(json.dumps({**GOOD, "confidence": "certain", "line": -3}), "m", config(), CONTEXT)
    assert d.confidence == "low" and d.line is None


def test_full_prompt_is_self_contained():
    text = full_prompt(CONTEXT)
    assert text.startswith(SYSTEM_PROMPT) and text.endswith(CONTEXT)


# --- cards -----------------------------------------------------------------------------------

def bug1_incident() -> Incident:
    texts = [
        "ERROR [django.request] request_id=web-1 Internal Server Error: /api/orders",
        "Traceback (most recent call last):",
        '  File "/srv/api/orders/views.py", line 59, in build_order',
        "KeyError: 'total'",
    ]
    lines = [LogLine("pricing", t, t, "stderr", 99.0, 1.7e9) for t in ['INFO request request_id=web-1 status=200']]
    lines += [LogLine("api", t, t, "stderr", 100 + i / 1000, 1.7e9) for i, t in enumerate(texts)]
    det = {s: ServiceDetector(s) for s in ("api", "pricing")}
    events = [e for l in lines for e in det[l.service].feed(l)] + [e for d in det.values() for e in d.flush()]
    return Incident(1, events, build_timeline(lines, events, 5.0), 1.7e9)


def render(panel) -> str:
    console = Console(record=True, width=110, color_system=None)
    console.print(panel)
    return console.export_text()


def test_found_card_shows_what_hammock_knows():
    text = render(found_card(bug1_incident(), config(), ["pricing"], "🧠 asking test-model…"))
    for expected in ("incident #1 · KeyError: 'total'", "Detected", "api", "python-traceback",
                     "/srv/api/orders/views.py:59 in build_order", "web-1 · seen in pricing, api",
                     "uncommitted changes in pricing", "asking test-model", "hammock prompt"):
        assert expected in text, expected


def test_diagnosis_card():
    d = parse_answer(json.dumps(GOOD), "test-model", config(), CONTEXT)
    d.seconds = 41
    text = render(diagnosis_card(bug1_incident(), config(), d))
    for expected in ("Origin", "pricing  (surfaced in api)", "pricing → api", "src/main.rs:43",
                     "renamed `total`", "Rename the field back", "high · test-model · 41s"):
        assert expected in text, expected
    assert "configuration issue" not in text


# --- the whole flow --------------------------------------------------------------------------

def test_investigator_shows_both_cards_and_saves_the_diagnosis(tmp_path):
    console = Console(record=True, width=110, color_system=None)
    seen_threads = []

    def fake_diagnose(context: str, cfg: Config) -> Diagnosis:
        import threading
        seen_threads.append(threading.current_thread().name)
        assert "KeyError: 'total'" in context
        return parse_answer(json.dumps(GOOD), "test-model", cfg, context + CONTEXT)

    async def go():
        inv = Investigator(config(), tmp_path / "incidents", console, LogBus(), diagnose_fn=fake_diagnose)
        incident = bug1_incident()
        inv(incident)
        await inv.wait()
        return incident

    incident = asyncio.run(go())
    text = console.export_text()
    assert text.index("asking test-model") < text.index("surfaced in api")
    assert seen_threads == ["hammock-llm"]
    saved = json.loads(incident.saved_to.read_text())
    assert saved["diagnosis"]["origin_service"] == "pricing"
    assert incident.saved_to.with_suffix(".context.md").exists()


def test_investigator_without_model_never_calls_one(tmp_path):
    console = Console(record=True, width=110, color_system=None)

    def explode(*_):
        raise AssertionError("must not be called")

    async def go():
        cfg = Config.model_validate({"services": {"api": {"cmd": "x"}, "pricing": {"cmd": "y"}}})
        inv = Investigator(cfg, tmp_path, console, LogBus(), diagnose_fn=explode)
        inv(bug1_incident())
        await inv.wait()

    asyncio.run(go())
    assert "set `model:` in hammock.yaml" in console.export_text()


def test_failed_diagnosis_is_one_line(tmp_path):
    bus = LogBus()

    async def go():
        inv = Investigator(config(), tmp_path, Console(record=True), bus,
                           diagnose_fn=lambda c, cfg: Diagnosis("test-model", error="Ollama isn't reachable"))
        inv(bug1_incident())
        await inv.wait()

    asyncio.run(go())
    assert any("no diagnosis: Ollama isn't reachable" in l.text for l in bus.lines("hammock"))


def test_daemon_thread_does_not_block_loop_shutdown():
    async def go():
        run_in_daemon_thread(time.sleep, 30)  # abandoned, like a model mid-answer at Ctrl-C
        await asyncio.sleep(0)

    started = time.monotonic()
    asyncio.run(go())
    assert time.monotonic() - started < 2


# --- hammock prompt --------------------------------------------------------------------------

def test_prompt_command_prints_latest_or_numbered(tmp_path, monkeypatch):
    (tmp_path / "hammock.yaml").write_text("services:\n  api:\n    cmd: x\n")
    incidents = tmp_path / ".hammock" / "incidents"
    incidents.mkdir(parents=True)
    (incidents / "20261113-100000-001.context.md").write_text("## old\n")
    (incidents / "20261113-110000-002.context.md").write_text("## two\n")
    (incidents / "20261113-120000-001.context.md").write_text("## newest\n")

    runner = CliRunner()
    args = ["prompt", "--print", "-c", str(tmp_path / "hammock.yaml")]
    latest = runner.invoke(app, args)
    assert latest.exit_code == 0 and "## newest" in latest.stdout and SYSTEM_PROMPT[:40] in latest.stdout
    assert "## two" in runner.invoke(app, [*args, "2"]).stdout
    missing = runner.invoke(app, [*args, "9"])
    assert missing.exit_code == 1

    assert find_context_file(tmp_path / "nothing") is None


# --- the model is optional -------------------------------------------------------------------

def test_no_llm_flag_skips_the_model_and_says_so(tmp_path):
    console = Console(record=True, width=110, color_system=None)

    def explode(*_):
        raise AssertionError("must not be called")

    async def go():
        inv = Investigator(config(), tmp_path, console, LogBus(), diagnose_fn=explode, llm=False)
        inv(bug1_incident())
        await inv.wait()

    asyncio.run(go())
    assert "local model off (--no-llm)" in console.export_text()


def test_missing_ollama_package_is_explained(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_ollama(name, *args, **kwargs):
        if name == "ollama":
            raise ImportError("No module named 'ollama'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ollama)
    d = diagnose(CONTEXT, config())
    assert not d.ok and "hammock[llm]" in d.error
