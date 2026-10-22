import json
import unicodedata
from datetime import datetime, timedelta, timezone

import pytest

import alerts
import notifier

T0 = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "ALERTS_FILE", tmp_path / "alerts.json")
    sent = []
    result = {"ok": True}

    def fake_send(config, text):
        sent.append(text)
        return result["ok"]

    monkeypatch.setattr(notifier, "send_message", fake_send)
    return sent, result, tmp_path / "alerts.json"


def test_raise_creates_alert_and_sends_once(env):
    sent, _, path = env
    assert alerts.raise_alert({}, "k1", "Något gick fel", now=T0) is True
    data = json.loads(path.read_text(encoding="utf-8"))["alerts"]["k1"]
    assert data["count"] == 1
    assert data["severity"] == "error"
    assert data["first_seen"] == "2026-10-21T08:00:00+00:00"
    assert data["last_sent"] == "2026-10-21T08:00:00+00:00"
    assert data["resolved_at"] is None
    assert sent == ["⚠️ Något gick fel"]


def test_warning_prefix(env):
    sent, _, _ = env
    alerts.raise_alert({}, "k", "Info", severity="warning", now=T0)
    assert sent == ["ℹ️ Info"]


def test_same_key_within_24h_is_not_resent(env):
    sent, _, _ = env
    alerts.raise_alert({}, "k1", "x", now=T0)
    later = T0 + timedelta(hours=1)
    assert alerts.raise_alert({}, "k1", "x", now=later) is True
    a = alerts.active()[0]
    assert a["count"] == 2
    assert a["last_seen"] == later.isoformat()
    assert len(sent) == 1


def test_resent_after_24h_while_unresolved(env):
    sent, _, _ = env
    alerts.raise_alert({}, "k1", "x", now=T0)
    alerts.raise_alert({}, "k1", "x", now=T0 + timedelta(hours=25))
    assert len(sent) == 2


def test_resolve_then_raise_again_sends_again(env):
    sent, _, _ = env
    alerts.raise_alert({}, "k1", "x", now=T0)
    assert alerts.resolve("k1", now=T0 + timedelta(hours=1)) is True
    assert alerts.resolve("k1") is False
    assert alerts.active() == []
    t2 = T0 + timedelta(hours=2)
    alerts.raise_alert({}, "k1", "x", now=t2)
    assert len(sent) == 2
    a = alerts.active()[0]
    assert a["first_seen"] == t2.isoformat()
    assert a["count"] == 1


def test_telegram_failure_still_records_alert(env):
    sent, result, _ = env
    result["ok"] = False
    assert alerts.raise_alert({}, "k1", "x", now=T0) is False
    assert [a["key"] for a in alerts.active()] == ["k1"]
    result["ok"] = True
    assert alerts.raise_alert({}, "k1", "x", now=T0 + timedelta(minutes=5)) is True
    assert len(sent) == 2


def test_send_exception_does_not_propagate(env, monkeypatch):
    def boom(config, text):
        raise RuntimeError("x")

    monkeypatch.setattr(notifier, "send_message", boom)
    assert alerts.raise_alert({}, "k1", "x", now=T0) is False
    assert len(alerts.active()) == 1


def test_corrupt_file_is_renamed_and_replaced(env):
    _, _, path = env
    path.write_text("{not json", encoding="utf-8")
    assert alerts.active() == []
    path.write_text("{not json", encoding="utf-8")
    assert alerts.raise_alert({}, "k1", "x", now=T0) is True
    assert (path.parent / "alerts.json.corrupt").exists()
    assert [a["key"] for a in alerts.active()] == ["k1"]


def test_missing_file_active_is_empty_and_resolve_is_noop(env):
    _, _, path = env
    assert alerts.active() == []
    assert alerts.resolve("nope") is False
    assert not path.exists()


def test_active_sorted_newest_first_with_fields(env):
    alerts.raise_alert({}, "old", "a", now=T0)
    alerts.raise_alert({}, "new", "b", severity="warning", now=T0 + timedelta(hours=1))
    act = alerts.active()
    assert [a["key"] for a in act] == ["new", "old"]
    assert set(act[0]) == {"key", "text", "severity", "first_seen", "last_seen", "count"}


def test_text_is_truncated_and_control_chars_removed(env):
    sent, _, _ = env
    alerts.raise_alert({}, "k1", "a\x00" * 1000, now=T0)
    stored = alerts.active()[0]["text"]
    assert len(stored) <= 500
    assert not any(unicodedata.category(c) == "Cc" for c in stored)
    assert sent[0] == "⚠️ " + stored


# ── FIX F: a hand-edited alerts.json must not silence raise_alert ────────────

def _write(path, entry):
    path.write_text(json.dumps({"alerts": {"k": entry}}), encoding="utf-8")


@pytest.mark.parametrize("bad", ["abc", None, [], 2.5, "7x"])
def test_bad_count_falls_back_to_0(env, bad):
    sent, _, path = env
    _write(path, {"text": "x", "count": bad, "first_seen": T0.isoformat(), "last_sent": None,
                  "resolved_at": None})
    assert alerts.raise_alert({}, "k", "Fel", now=T0) is True
    assert sent == ["⚠️ Fel"]
    assert json.loads(path.read_text(encoding="utf-8"))["alerts"]["k"]["count"] == 1


def test_numeric_string_count_still_counts(env):
    _, _, path = env
    _write(path, {"text": "x", "count": "4", "last_sent": None, "resolved_at": None})
    alerts.raise_alert({}, "k", "Fel", now=T0)
    assert json.loads(path.read_text(encoding="utf-8"))["alerts"]["k"]["count"] == 5


def test_naive_last_sent_is_utc(env):
    sent, _, path = env
    _write(path, {"text": "x", "count": 1, "last_sent": "2026-10-21T07:00:00",
                  "resolved_at": None})
    assert alerts.raise_alert({}, "k", "Fel", now=T0) is True   # 1 h later: suppressed
    assert sent == []
    assert alerts.raise_alert({}, "k", "Fel", now=T0 + timedelta(hours=23)) is True
    assert sent == ["⚠️ Fel"]


def test_future_last_sent_suppresses_at_most_resend_after(env):
    sent, _, path = env
    _write(path, {"text": "x", "count": 1, "last_sent": "2027-10-21T08:00:00+00:00",
                  "resolved_at": None})
    assert alerts.raise_alert({}, "k", "Fel", now=T0) is True
    assert sent == []
    assert json.loads(path.read_text(encoding="utf-8"))["alerts"]["k"]["last_sent"] == T0.isoformat()
    alerts.raise_alert({}, "k", "Fel", now=T0 + alerts.RESEND_AFTER)
    assert sent == ["⚠️ Fel"]


def test_parse_time_naive_is_utc():
    assert alerts.parse_time("2026-10-21T08:00:00") == T0
    assert alerts.parse_time("2026-10-21T10:00:00+02:00") == T0
    assert alerts.parse_time("nonsense") is None
    assert alerts.parse_time(None) is None
