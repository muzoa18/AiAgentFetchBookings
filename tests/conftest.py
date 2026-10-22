import json

import pytest

import alerts
import notifier

SKELETON = {"last_update_id": None, "bookings": {}}


@pytest.fixture(autouse=True)
def _isolated_alerts(request, tmp_path, monkeypatch):
    """No test may touch the real config/alerts.json or reach Telegram by accident.
    A test that installs its own fake send_message simply overrides this one."""
    monkeypatch.setattr(alerts, "ALERTS_FILE", tmp_path / "alerts_state.json")
    if request.module.__name__ != "test_notifier":  # that module tests the real function
        monkeypatch.setattr(notifier, "send_message", lambda config, text: True)


def write_orders_skeleton(path):
    """A valid, empty orders.json (the state guard fails closed without one)."""
    path.write_text(json.dumps(SKELETON), encoding="utf-8")
