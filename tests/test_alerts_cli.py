import re
from pathlib import Path

import pytest
import yaml

import alerts
import alerts_cli
import notifier

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123456:SECRET-TOKEN-VALUE"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "777")


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notifier, "send_message",
                        lambda config, text: out.append((config, text)) or True)
    return out


def test_workflow_failed_sends_one_message_with_run_url_and_name(env, sent):
    assert alerts_cli.main(["workflow-failed", "MECA Booking Agent"]) == 0
    assert len(sent) == 1
    config, text = sent[0]
    assert "https://github.example/owner/repo/actions/runs/777" in text
    assert "MECA Booking Agent" in text
    assert "misslyckades" in text
    assert config["telegram"] == {"bot_token": TOKEN, "chat_id": "42"}


def test_workflow_failed_writes_an_active_alert(env, sent):
    alerts_cli.main(["workflow-failed", "approve"])
    active = alerts.active()
    assert [a["key"] for a in active] == ["workflow_failed_approve"]
    assert active[0]["text"] == (
        "GitHub-körningen approve misslyckades: "
        "https://github.example/owner/repo/actions/runs/777")


def test_missing_token_exits_0_and_never_sends(monkeypatch, env, sent, caplog):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert alerts_cli.main(["workflow-failed", "approve"]) == 0
    assert sent == []
    assert "TELEGRAM_BOT_TOKEN" in caplog.text


def test_never_prints_the_token(env, sent, capsys, caplog):
    alerts_cli.main(["workflow-failed", "approve"])
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err + caplog.text


def test_bad_arguments_still_exit_0(env, sent):
    assert alerts_cli.main([]) == 0
    assert alerts_cli.main(["bogus", "x"]) == 0
    assert sent == []


def test_alerts_cli_and_its_imports_are_stdlib_only():
    import sys
    for name in ("alerts_cli", "alerts", "notifier"):
        src = (ROOT / f"{name}.py").read_text(encoding="utf-8")
        for mod in re.findall(r"^(?:import|from)\s+([A-Za-z_][\w]*)", src, re.M):
            assert mod in sys.stdlib_module_names or mod in ("alerts", "notifier"), (name, mod)


def _steps(name):
    wf = yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))
    return next(iter(wf["jobs"].values()))["steps"]


@pytest.mark.parametrize("name", ["bookings.yml", "approve.yml"])
def test_workflows_list_alerts_json_in_persist(name):
    steps = _steps(name)
    names = [s["name"] for s in steps]
    persist = steps[names.index("Persist state back to repo")]
    assert "config/alerts.json" in persist["run"]
    alert = steps[names.index("Alert on failure")]
    assert names.index("Alert on failure") < names.index("Persist state back to repo")
    assert alert["if"] == "${{ failure() }}"
    assert alert["run"].strip() == 'python3 alerts_cli.py workflow-failed "$WORKFLOW_NAME"'
    assert "${{" not in alert["run"]
    assert set(alert["env"]) == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WORKFLOW_NAME"}
    assert alert["env"]["WORKFLOW_NAME"] == "${{ github.workflow }}"
