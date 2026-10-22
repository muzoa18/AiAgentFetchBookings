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
    # preflight (DISPLAY_NAMES) pulls in orders_state and config_loader; config_loader
    # imports yaml only inside try/except ImportError (indented, so not matched here).
    ours = ("alerts", "notifier", "preflight", "orders_state", "config_loader")
    for name in ("alerts_cli",) + ours:
        src = (ROOT / f"{name}.py").read_text(encoding="utf-8")
        for mod in re.findall(r"^(?:import|from)\s+([A-Za-z_][\w]*)", src, re.M):
            assert mod in sys.stdlib_module_names or mod in ours, (name, mod)


def _steps(name):
    wf = yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))
    return next(iter(wf["jobs"].values()))["steps"]


@pytest.mark.parametrize("name", ["bookings.yml", "approve.yml"])
def test_workflows_list_alerts_json_in_persist(name):
    steps = _steps(name)
    names = [s["name"] for s in steps]
    persist = steps[names.index("Persist state back to repo")]
    assert "config/alerts.json" in persist["run"]
    assert "watchdog_alerts" not in persist["run"]     # the watchdog's own file
    alert = steps[names.index("Alert on failure")]
    assert names.index("Alert on failure") < names.index("Persist state back to repo")
    assert alert["if"] == "${{ failure() }}"
    assert alert["run"].strip() == 'python3 alerts_cli.py workflow-failed "$WORKFLOW_NAME"'
    assert "${{" not in alert["run"]
    assert set(alert["env"]) == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WORKFLOW_NAME"}
    assert alert["env"]["WORKFLOW_NAME"] == "${{ github.workflow }}"


# ── workflow-ok (FIX B) and the workflow-failed suppression (FIX C) ──────────────

from datetime import datetime, timedelta, timezone  # noqa: E402

import subprocess  # noqa: E402
import sys  # noqa: E402


def _keys():
    return sorted(a["key"] for a in alerts.active())


@pytest.mark.parametrize("display,short", [("MECA Booking Agent", "booking_agent"),
                                           ("MECA Approval Poller", "approve")])
def test_workflow_ok_resolves_the_workflows_alerts(sent, display, short):
    for key in (f"workflow_failed_{display}", f"run_failed_{short}", f"crash_{short}",
                "workflow_failed_Other", "run_failed_other", "state_unreadable"):
        alerts.raise_alert({}, key, "x")
    assert alerts_cli.main(["workflow-ok", display]) == 0
    assert _keys() == sorted(["workflow_failed_Other", "run_failed_other",
                              "state_unreadable"])


def test_workflow_ok_unknown_name_resolves_only_its_workflow_failed(sent):
    for key in ("workflow_failed_Nope", "run_failed_approve", "crash_booking_agent"):
        alerts.raise_alert({}, key, "x")
    assert alerts_cli.main(["workflow-ok", "Nope"]) == 0
    assert _keys() == ["crash_booking_agent", "run_failed_approve"]


def test_workflow_ok_needs_no_token_and_sends_nothing(monkeypatch, sent):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    alerts.raise_alert({}, "workflow_failed_MECA Approval Poller", "x")
    sent.clear()
    assert alerts_cli.main(["workflow-ok", "MECA Approval Poller"]) == 0
    assert alerts.active() == []
    assert sent == []


def test_workflow_ok_never_raises(monkeypatch, sent):
    monkeypatch.setattr(alerts, "resolve", lambda key: 1 / 0)
    assert alerts_cli.main(["workflow-ok", "MECA Approval Poller"]) == 0


@pytest.mark.parametrize("key", ["state_unreadable", "crash_approve", "run_failed_approve"])
def test_workflow_failed_suppressed_by_a_fresh_specific_alert(env, sent, key, caplog):
    caplog.set_level("INFO")
    alerts.raise_alert({}, key, "specific")
    sent.clear()
    assert alerts_cli.main(["workflow-failed", "MECA Approval Poller"]) == 0
    assert sent == []
    assert "workflow_failed_MECA Approval Poller" not in _keys()
    assert "suppressed" in caplog.text


def test_workflow_failed_not_suppressed_by_an_old_specific_alert(env, sent):
    old = datetime.now(timezone.utc) - timedelta(minutes=61)
    alerts.raise_alert({}, "run_failed_approve", "specific", now=old)
    sent.clear()
    alerts_cli.main(["workflow-failed", "MECA Approval Poller"])
    assert len(sent) == 1
    assert "workflow_failed_MECA Approval Poller" in _keys()


def test_workflow_failed_not_suppressed_by_a_resolved_alert(env, sent):
    alerts.raise_alert({}, "crash_approve", "specific")
    alerts.resolve("crash_approve")
    sent.clear()
    alerts_cli.main(["workflow-failed", "MECA Approval Poller"])
    assert len(sent) == 1


def test_workflow_failed_not_suppressed_by_the_other_workflows_alert(env, sent):
    alerts.raise_alert({}, "run_failed_booking_agent", "specific")
    sent.clear()
    alerts_cli.main(["workflow-failed", "MECA Approval Poller"])
    assert len(sent) == 1


def test_workflow_failed_install_failure_raises(env, sent):
    # A pip/Chromium install failure: our code never ran, no specific alert exists.
    alerts_cli.main(["workflow-failed", "MECA Booking Agent"])
    assert len(sent) == 1
    assert _keys() == ["workflow_failed_MECA Booking Agent"]


def test_workflow_failed_unknown_name_is_never_suppressed(env, sent):
    alerts.raise_alert({}, "state_unreadable", "specific")
    sent.clear()
    alerts_cli.main(["workflow-failed", "Other"])
    assert len(sent) == 1


@pytest.mark.parametrize("args", [["workflow-ok", "MECA Booking Agent"],
                                  ["workflow-failed", "MECA Booking Agent"]])
def test_alerts_cli_runs_without_site_packages(tmp_path, args):
    # -S: no site-packages (nothing pip-installed), as on the runner before the install.
    proc = subprocess.run([sys.executable, "-S", str(ROOT / "alerts_cli.py"), *args],
                          cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "ModuleNotFoundError" not in proc.stderr
    assert "alerts_cli failed" not in proc.stderr


@pytest.mark.parametrize("name,run_step", [("bookings.yml", "Run booking agent"),
                                           ("approve.yml", "Handle approvals")])
def test_workflows_mark_ok_after_the_run_and_before_persist(name, run_step):
    steps = _steps(name)
    names = [s["name"] for s in steps]
    ok = steps[names.index("Mark OK")]
    assert names.index(run_step) < names.index("Mark OK") < names.index(
        "Persist state back to repo")
    assert ok["if"] == "${{ success() }}"
    assert ok["run"].strip() == 'python3 alerts_cli.py workflow-ok "$WORKFLOW_NAME"'
    assert set(ok["env"]) == {"WORKFLOW_NAME"}
    assert ok["env"]["WORKFLOW_NAME"] == "${{ github.workflow }}"
