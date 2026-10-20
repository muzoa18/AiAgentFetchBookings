import pytest

import agent

CONFIG = {"telegram": {"bot_token": "123:ABC", "chat_id": "42"}}

BOOKING = {
    "booking_type": "Bokning",
    "booking_nr": "OLRD-3630",
    "driver_name": "Mamadou Sowe",
    "driver_phone": "+46739133124",
    "reg_nr": "NXP168",
    "vehicle": "VOLKSWAGEN PASSAT",
    "workshop_dt": "2026-04-01 (3 Dagar)",
    "services": [{"name": "Servicekontroll 150 000 KM"}],
    "parts": [{"name": "Oljefilter", "qty": "2"}, {"name": "Luftfilter", "qty": "1"}],
    "fluids": [{"name": "Olja 5W30", "qty": "5"}],
    "total_price": "1 500 kr",
}


def test_build_message_matches_existing_sms_format():
    assert agent.build_message(BOOKING) == "\n".join([
        "Ny Bokning - OLRD-3630",
        "Kund: Mamadou Sowe",
        "Tel: +46739133124",
        "Fordon: NXP168 VOLKSWAGEN PASSAT",
        "Datum: 2026-04-01 (3 Dagar)",
        "---",
        "Tjänster:",
        "- Servicekontroll 150 000 KM",
        "Delar:",
        "- Oljefilter x2",
        "- Luftfilter",
        "Vätskor/Oljor:",
        "- Olja 5W30 x5",
        "---",
        "Totalt: 1 500 kr",
    ])


@pytest.fixture
def env(monkeypatch):
    """Fake every external edge of agent.run(); returns a recorder."""
    rec = {"sent": [], "handled": [], "saved": None, "logged": None,
           "seen": set(), "bookings": [], "send_ok": True}

    monkeypatch.setattr(agent, "load_config", lambda: CONFIG)
    monkeypatch.setattr(agent, "load_seen_ids", lambda: set(rec["seen"]))
    monkeypatch.setattr(agent, "save_seen_ids", lambda ids: rec.__setitem__("saved", set(ids)))
    monkeypatch.setattr(agent, "fetch_bookings", lambda config: rec["bookings"])

    def fake_send(config, text):
        rec["sent"].append(text)
        return rec["send_ok"]

    def fake_handled(config, url):
        rec["handled"].append(url)
        return True

    monkeypatch.setattr(agent, "send_message", fake_send)
    monkeypatch.setattr(agent, "mark_booking_handled", fake_handled)
    monkeypatch.setattr(agent, "log_run", lambda name, summary: rec.__setitem__("logged", summary))
    return rec


def _booking(bid, btype="Bokning"):
    return {**BOOKING, "id": bid, "booking_type": btype, "detail_url": f"http://x/{bid}"}


def test_notify_failure_does_not_mark_seen(env):
    env["bookings"] = [_booking("B1")]
    env["send_ok"] = False

    assert agent.run() == 1

    assert "B1" not in (env["saved"] or set())
    assert env["handled"] == []
    assert env["logged"] == {"bookings_found": 1, "notified": 0, "notify_failed": 1}


def test_notify_success_marks_seen_and_calls_hanterad_for_bokning_only(env):
    env["bookings"] = [_booking("B1"), _booking("F1", "Förfrågan"), _booking("O1", "Offert")]

    assert agent.run() == 0

    assert env["saved"] == {"B1", "F1", "O1"}
    assert len(env["sent"]) == 3
    assert env["handled"] == ["http://x/B1"]
    assert env["logged"] == {"bookings_found": 3, "notified": 3, "notify_failed": 0}


def test_already_seen_booking_is_not_resent(env):
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 0

    assert env["sent"] == []
    assert env["handled"] == []


def test_no_bookings_returns_zero_and_logs(env):
    assert agent.run() == 0
    assert env["logged"] == {"bookings_found": 0, "notified": 0, "notify_failed": 0}
