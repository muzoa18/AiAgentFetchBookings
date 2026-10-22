import json

import pytest

import alerts
import notifier
import orders_state
import preflight
from conftest import write_orders_skeleton


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "orders.json"
    monkeypatch.setattr(orders_state, "ORDERS_FILE", path)
    monkeypatch.setattr(alerts, "ALERTS_FILE", tmp_path / "alerts.json")
    sent = []
    monkeypatch.setattr(notifier, "send_message", lambda config, text: sent.append(text) or True)
    return path, sent


def _active_texts():
    return [a["text"] for a in alerts.active()]


def test_state_problems_missing_file(env):
    assert preflight.state_problems() == ["config/orders.json saknas"]


def test_state_problems_corrupt_file(env):
    path, _ = env
    path.write_text("{not json", encoding="utf-8")
    assert preflight.state_problems() == ["config/orders.json kan inte läsas"]


def test_state_ok_for_valid_skeleton(env):
    path, _ = env
    write_orders_skeleton(path)
    assert preflight.state_problems() == []


def test_guarded_exception_raises_crash_alert_and_returns_1(env):
    def boom():
        raise RuntimeError("SECRET-PAGE-TEXT")

    assert preflight.guarded("booking_agent", boom, config_loader=lambda: {}) == 1
    texts = _active_texts()
    assert len(texts) == 1
    assert "RuntimeError" in texts[0]
    assert "SECRET-PAGE-TEXT" not in texts[0]
    assert texts[0] == ("Körningen av booking_agent kraschade (RuntimeError) – "
                        "se GitHub Actions-loggen.")


def test_guarded_success_resolves_alerts(env):
    preflight.guarded("approve", lambda: 1, config_loader=lambda: {})
    assert len(alerts.active()) == 1
    assert preflight.guarded("approve", lambda: 0, config_loader=lambda: {}) == 0
    assert alerts.active() == []


def test_guarded_nonzero_raises_run_failed_alert_and_keeps_code(env):
    assert preflight.guarded("approve", lambda: 3, config_loader=lambda: {}) == 3
    assert _active_texts() == ["Körningen av approve slutade med fel – se GitHub Actions-loggen."]


def test_guarded_config_failure_only_logs(env):
    def bad_config():
        raise OSError("no config")

    assert preflight.guarded("approve", lambda: 7, config_loader=bad_config) == 7
    assert preflight.guarded("approve", lambda: (_ for _ in ()).throw(KeyError("x")),
                             config_loader=bad_config) == 1
    assert alerts.active() == []


def test_check_state_or_alert_blocks_and_alerts(env):
    _, sent = env
    assert preflight.check_state_or_alert({}) is False
    assert _active_texts() == ["config/orders.json saknas"]
    assert len(sent) == 1


def test_check_state_or_alert_resolves_when_fixed(env):
    path, _ = env
    assert preflight.check_state_or_alert({}) is False
    write_orders_skeleton(path)
    assert preflight.check_state_or_alert({}) is True
    assert alerts.active() == []


@pytest.mark.parametrize("name,display", [("booking_agent", "MECA Booking Agent"),
                                          ("approve", "MECA Approval Poller")])
def test_guarded_success_resolves_the_workflow_failed_alert(env, name, display):
    alerts.raise_alert({}, f"workflow_failed_{display}", "x")
    alerts.raise_alert({}, "workflow_failed_Other", "y")
    assert preflight.guarded(name, lambda: 0, config_loader=lambda: {}) == 0
    assert [a["key"] for a in alerts.active()] == ["workflow_failed_Other"]


def test_guarded_failure_keeps_the_workflow_failed_alert(env):
    alerts.raise_alert({}, "workflow_failed_MECA Approval Poller", "x")
    assert preflight.guarded("approve", lambda: 2, config_loader=lambda: {}) == 2
    assert "workflow_failed_MECA Approval Poller" in [a["key"] for a in alerts.active()]


def test_display_names_are_the_workflow_names():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    names = {f: yaml.safe_load((root / f).read_text(encoding="utf-8"))["name"]
             for f in ("bookings.yml", "approve.yml")}
    assert preflight.DISPLAY_NAMES == {"booking_agent": names["bookings.yml"],
                                       "approve": names["approve.yml"]}
