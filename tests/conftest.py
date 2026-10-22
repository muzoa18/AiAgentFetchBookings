import json

import pytest

import alerts

SKELETON = {"last_update_id": None, "bookings": {}}


@pytest.fixture(autouse=True)
def _isolated_alerts(tmp_path, monkeypatch):
    """No test may touch the real config/alerts.json or message Telegram via alerts."""
    monkeypatch.setattr(alerts, "ALERTS_FILE", tmp_path / "alerts_state.json")


def write_orders_skeleton(path):
    """A valid, empty orders.json (the state guard fails closed without one)."""
    path.write_text(json.dumps(SKELETON), encoding="utf-8")
