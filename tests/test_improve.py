"""improve.py: real file lists, current model, truthful prompt, protected paths. Offline."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import improve

REPO = Path(__file__).resolve().parent.parent

EXPECTED_SOURCES = [
    "agent.py", "approve.py", "notifier.py", "orders_state.py", "parts_order.py",
    "parts_order_helpers.py", "parts_matcher.py", "parts_portal.py", "parts_portal_parse.py",
    "portal_scraper.py", "rules.py", "parts_rules.yaml", "config_loader.py", "state.py",
    "run_logger.py",
]


class FakeClient:
    """Stands in for anthropic.Anthropic; records the call, returns canned proposals."""

    def __init__(self, proposals, calls):
        self._proposals = proposals
        self._calls = calls
        self.messages = self

    def create(self, **kwargs):
        self._calls.append(kwargs)
        text = json.dumps({"proposals": self._proposals})
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=text)])


@pytest.fixture
def fake_claude(monkeypatch):
    calls = []
    state = {"proposals": []}

    def factory(api_key=None):
        return FakeClient(state["proposals"], calls)

    monkeypatch.setattr(improve.anthropic, "Anthropic", factory)
    return state, calls


def proposal(file, find="", replacement="", change_type="replace_function", title=None):
    return {"priority": 1, "title": title or f"change {file}", "reason": "because",
            "file": file, "change_type": change_type, "find": find,
            "replacement": replacement, "impact": "low", "auto_apply": True}


# ── (a) (b) (c) file lists and model ──────────────────────────────────────────

def test_source_files_are_the_real_current_modules():
    assert improve.SOURCE_FILES == EXPECTED_SOURCES
    missing = [f for f in improve.SOURCE_FILES if not (REPO / f).exists()]
    assert missing == []


def test_read_sources_has_no_missing_files(monkeypatch):
    monkeypatch.chdir(REPO)
    assert "FILE NOT FOUND" not in improve.read_sources()


def test_log_files_are_only_logs_that_can_exist():
    assert [path for path, _ in improve.LOG_FILES] == ["logs/agent.log", "logs/improve_auto.log"]


def test_model_is_current_and_call_style_is_structured_output(fake_claude):
    state, calls = fake_claude
    state["proposals"].append(proposal("agent.py"))
    result = improve.ask_claude("key", "SOURCES", "LOGS")
    assert improve.IMPROVE_MODEL == "claude-opus-5-5"
    assert calls[0]["model"] == "claude-opus-5-5"
    assert calls[0]["system"] == improve.SYSTEM_PROMPT
    assert calls[0]["output_config"] == {
        "format": {"type": "json_schema", "schema": improve.PROPOSALS_SCHEMA}}
    assert result == state["proposals"]


# ── (d) prompt describes the current system ──────────────────────────────────

def test_prompt_describes_the_current_system():
    prompt = improve.SYSTEM_PROMPT
    for needle in ("Telegram", "Klar", "Hanterad", "10,000 SEK", "parts_rules.yaml",
                   "ProMeister", "Kopplade reservdelar", "approve.py", "Approve"):
        assert needle in prompt, needle
    for stale in ("SMS", "46elks", "Gmail", "email_agent", "parts_agent", "Offertnotering"):
        assert stale not in prompt, stale


def test_prompt_states_the_protected_areas():
    prompt = improve.SYSTEM_PROMPT
    for needle in (".github/", "parts_rules.yaml", "config/", "orders_state",
                   "skipped (protected)"):
        assert needle in prompt, needle


# ── (e) protected paths, enforced in code ────────────────────────────────────

@pytest.mark.parametrize("path", [
    ".github/workflows/bookings.yml", ".github", "./.github/workflows/approve.yml",
    ".GitHub/workflows/x.yml", ".github\\workflows\\bookings.yml",
    "parts_rules.yaml", "./parts_rules.yaml", "PARTS_RULES.YAML",
    "config/config.yaml", "config/orders.json", "config", "Config/seen_ids.txt",
    "approve.py", "parts_matcher.py", "parts_order.py", "parts_order_helpers.py",
    "orders_state.py", "rules.py", "improve.py", ".claude/skills/meca-agent/SKILL.md",
    "../outside.py", "sub/../../outside.py", "/etc/passwd", "C:\\Windows\\x.py", "",
    "tests/../config/config.yaml", "parts_rules.yaml.", "parts_rules.yaml ",
    "parts_rules.yaml::$DATA", "config./config.yaml",
])
def test_protected_paths(path):
    assert improve.is_protected(path)


@pytest.mark.parametrize("path", [
    "portal_scraper.py", "parts_portal.py", "parts_portal_parse.py", "agent.py",
    "notifier.py", "state.py", "run_logger.py", "config_loader.py",
    "tests/test_parts_portal_parse.py", "./portal_scraper.py",
])
def test_unprotected_paths(path):
    assert not improve.is_protected(path)


def test_symlink_into_a_protected_path_is_protected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("x", encoding="utf-8")
    try:
        (tmp_path / "innocent.yaml").symlink_to(tmp_path / "config" / "config.yaml")
    except OSError:
        pytest.skip("symlinks not supported here")
    assert improve.is_protected("innocent.yaml")


def test_apply_proposal_refuses_a_protected_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "parts_rules.yaml").write_text("ceiling_sek: 10000\n", encoding="utf-8")
    ok = improve.apply_proposal(proposal("parts_rules.yaml", "ceiling_sek: 10000",
                                         "ceiling_sek: 99999"))
    assert ok is False
    assert (tmp_path / "parts_rules.yaml").read_text(encoding="utf-8") == "ceiling_sek: 10000\n"


def _write_tree(root: Path):
    files = {
        "portal_scraper.py": "TIMEOUT = 1\n",
        "approve.py": "OWNER_ONLY = True\n",
        "parts_rules.yaml": "ceiling_sek: 10000\n",
        "config/config.yaml": "portal: {}\n",
        ".github/workflows/bookings.yml": "on: push\n",
        "notifier.py": "X = 1\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return files


def test_apply_all_skips_protected_and_lists_them(tmp_path, monkeypatch, fake_claude):
    monkeypatch.chdir(tmp_path)
    files = _write_tree(tmp_path)
    state, _ = fake_claude
    state["proposals"].extend([
        proposal("portal_scraper.py", "TIMEOUT = 1", "TIMEOUT = 2", title="longer timeout"),
        proposal(".github/workflows/bookings.yml", "on: push", "on: pull_request",
                 title="change the workflow"),
        proposal("parts_rules.yaml", "ceiling_sek: 10000", "ceiling_sek: 50000",
                 title="raise the ceiling"),
        proposal("approve.py", "", "OWNER_ONLY = False\n", change_type="replace_file",
                 title="drop owner check"),
        proposal("./Config/config.yaml", "portal: {}", "portal: {debug: true}",
                 title="edit config"),
        proposal("../escape.py", "", "print('x')\n", change_type="replace_file",
                 title="escape the repo"),
    ])

    improve.run_apply_all("key")

    assert (tmp_path / "portal_scraper.py").read_text(encoding="utf-8") == "TIMEOUT = 2\n"
    for name, text in files.items():
        if name != "portal_scraper.py":
            assert (tmp_path / name).read_text(encoding="utf-8") == text, name
    assert not (tmp_path.parent / "escape.py").exists()

    report = (tmp_path / "improve_reports" / "latest.md").read_text(encoding="utf-8")
    assert "longer timeout" in report
    for title in ("change the workflow", "raise the ceiling", "drop owner check",
                  "edit config", "escape the repo"):
        line = next(l for l in report.splitlines() if title in l)
        assert "skipped (protected)" in line, line
    applied_section = report.split("## ✅ Applied")[1].split("##")[0]
    assert "raise the ceiling" not in applied_section
    assert "longer timeout" in applied_section


def test_apply_all_still_reverts_a_file_that_does_not_compile(tmp_path, monkeypatch, fake_claude):
    monkeypatch.chdir(tmp_path)
    _write_tree(tmp_path)
    state, _ = fake_claude
    state["proposals"].append(proposal("portal_scraper.py", "TIMEOUT = 1", "TIMEOUT = (",
                                       title="broken"))
    improve.run_apply_all("key")
    assert (tmp_path / "portal_scraper.py").read_text(encoding="utf-8") == "TIMEOUT = 1\n"
    report = (tmp_path / "improve_reports" / "latest.md").read_text(encoding="utf-8")
    assert "Reverted" in report and "portal_scraper.py" in report


def test_apply_all_with_no_proposals_writes_the_healthy_report(tmp_path, monkeypatch, fake_claude):
    monkeypatch.chdir(tmp_path)
    improve.run_apply_all("key")
    report = (tmp_path / "improve_reports" / "latest.md").read_text(encoding="utf-8")
    assert "No improvements proposed" in report


def test_auto_mode_never_writes_a_protected_path(tmp_path, monkeypatch, fake_claude):
    monkeypatch.chdir(tmp_path)
    files = _write_tree(tmp_path)
    state, _ = fake_claude
    state["proposals"].append(proposal("parts_rules.yaml", "ceiling_sek: 10000",
                                       "ceiling_sek: 50000"))
    improve.run_auto("key")
    assert (tmp_path / "parts_rules.yaml").read_text(encoding="utf-8") == files["parts_rules.yaml"]
