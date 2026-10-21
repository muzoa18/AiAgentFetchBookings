import sys
import types

import pytest

import agent
import notifier
import orders_state
from rules import Rules

CONFIG = {
    "telegram": {"bot_token": "123:ABC", "chat_id": "42"},
    "anthropic": {"api_key": "sk-test"},
    "parts_portal": {"username": "u", "password": "p"},
}
RULES = Rules(("Bosch",), ("ProMeister",), 10000, "approve")

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
def env(monkeypatch, tmp_path):
    """Fake every external edge of agent.run(); returns a recorder."""
    rec = {"sent": [], "handled": [], "saved": None, "logged": None,
           "seen": set(), "bookings": [], "send_ok": True,
           "staged": [], "order_status": "pending", "portal_opened": 0, "portal_closed": 0}

    monkeypatch.setattr(orders_state, "ORDERS_FILE", tmp_path / "orders.json")
    monkeypatch.setattr(agent, "load_rules", lambda: RULES)
    monkeypatch.setattr(agent, "make_claude_picker", lambda api_key: "picker")

    class FakePartsPortal:
        def __init__(self, config):
            pass

        def __enter__(self):
            rec["portal_opened"] += 1
            return "portal"

        def __exit__(self, *exc):
            rec["portal_closed"] += 1
            return False

    monkeypatch.setitem(sys.modules, "parts_portal",
                        types.SimpleNamespace(PartsPortal=FakePartsPortal))

    def fake_stage(config, booking, portal, rules, picker):
        rec["staged"].append((booking["id"], portal, picker))
        return rec["order_status"]

    monkeypatch.setattr(agent, "plan_and_stage", fake_stage)

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
    monkeypatch.setattr(notifier, "send_message", fake_send)
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
    assert env["logged"] == {"bookings_found": 1, "notified": 0, "notify_failed": 1,
                             "orders_staged": 0, "orders_failed": 0, "handled_failed": 0}


def test_notify_success_marks_seen_and_calls_hanterad_for_bokning_only(env):
    env["bookings"] = [_booking("B1"), _booking("F1", "Förfrågan"), _booking("O1", "Offert")]

    assert agent.run() == 0

    assert env["saved"] == {"B1", "F1", "O1"}
    assert len(env["sent"]) == 3
    assert env["handled"] == ["http://x/B1"]
    assert env["logged"] == {"bookings_found": 3, "notified": 3, "notify_failed": 0,
                             "orders_staged": 1, "orders_failed": 0, "handled_failed": 0}


def test_already_seen_booking_is_not_resent(env):
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 0

    assert env["sent"] == []
    # still on the Nya tab and no orders record -> one silent Hanterad retry, no re-notify
    assert env["handled"] == ["http://x/B1"]


def test_no_bookings_returns_zero_and_logs(env):
    assert agent.run() == 0
    assert env["logged"] == {"bookings_found": 0, "notified": 0, "notify_failed": 0,
                             "orders_staged": 0, "orders_failed": 0}


# ── Parts ordering hook ──────────────────────────────────────────────────────

def test_only_notified_bokning_with_parts_is_staged_with_one_portal(env):
    env["bookings"] = [_booking("B1"), _booking("F1", "Förfrågan"), _booking("B2")]

    assert agent.run() == 0

    assert env["staged"] == [("B1", "portal", "picker"), ("B2", "portal", "picker")]
    assert (env["portal_opened"], env["portal_closed"]) == (1, 1)
    assert env["logged"]["orders_staged"] == 2


def test_portal_never_opened_when_nothing_needs_ordering(env, monkeypatch):
    import parts_order
    monkeypatch.setattr(agent, "plan_and_stage", parts_order.plan_and_stage)
    no_parts = {**_booking("B1"), "parts": [], "fluids": []}
    fluids_only = {**_booking("B2"), "parts": [], "fluids": [{"name": "Olja 5W30", "qty": "5"}]}
    env["bookings"] = [no_parts, fluids_only, _booking("F1", "Förfrågan")]

    assert agent.run() == 0

    assert env["portal_opened"] == 0
    assert orders_state.get("B1") is None
    assert orders_state.get("B2")["status"] == "blocked"
    assert any("Olja 5W30 x5" in t for t in env["sent"])


def test_failed_order_makes_exit_code_1(env):
    env["bookings"] = [_booking("B1")]
    env["order_status"] = "failed"

    assert agent.run() == 1

    assert env["saved"] == {"B1"}
    assert env["logged"]["orders_failed"] == 1


def test_invalid_rules_file_keeps_notifying_and_exits_1(env, monkeypatch):
    def broken_rules():
        raise ValueError("mode must be one of ...")

    monkeypatch.setattr(agent, "load_rules", broken_rules)
    env["bookings"] = [_booking("B1"), _booking("B2")]

    assert agent.run() == 1

    assert env["saved"] == {"B1", "B2"}
    assert env["staged"] == []
    assert env["portal_opened"] == 0
    assert env["sent"].count("Regelfilen är ogiltig – inga beställningar") == 1
    assert env["logged"]["notified"] == 2
    # 1 for the rules file + each queued booking closed as failed (not left for the sweep)
    assert env["logged"]["orders_failed"] == 3
    assert orders_state.get("B1")["status"] == "failed"
    assert orders_state.by_status("attempted") == []


def test_mark_booking_handled_raising_does_not_skip_save(env, monkeypatch):
    def boom(config, url):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(agent, "mark_booking_handled", boom)
    env["bookings"] = [_booking("B1"), _booking("B2")]

    assert agent.run() == 1   # a failed Hanterad is reported, never silent

    assert env["saved"] == {"B1", "B2"}
    assert env["logged"]["notified"] == 2
    assert env["logged"]["handled_failed"] == 2


def test_parts_portal_import_error_fails_booking_and_run_continues(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "parts_portal", None)  # import raises ImportError
    env["bookings"] = [_booking("B1"), _booking("B2")]

    assert agent.run() == 1

    assert env["staged"] == []
    assert orders_state.get("B1")["status"] == "failed"
    assert orders_state.get("B2")["status"] == "failed"
    assert env["saved"] == {"B1", "B2"}
    failures = [t for t in env["sent"] if "misslyckades" in t]
    assert len(failures) == 2
    assert env["logged"]["orders_failed"] == 2


def test_missing_ordering_credentials_fail_closed(env, monkeypatch):
    monkeypatch.setattr(agent, "load_config",
                        lambda: {**CONFIG, "anthropic": {"api_key": ""}})
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 1

    assert env["staged"] == []
    assert env["portal_opened"] == 0
    assert orders_state.get("B1")["status"] == "failed"
    assert any("anthropic.api_key" in t for t in env["sent"])


# ── Crash recovery: queue + sweep ────────────────────────────────────────────

def test_killed_mid_order_record_is_swept_next_run(env):
    orders_state.put("B9", "attempted", plan={"booking_id": "B9", "parts": []})

    assert agent.run() == 1

    assert orders_state.get("B9")["status"] == "failed"
    swept = [t for t in env["sent"] if "B9" in t]
    assert swept == ["Delbeställning för B9 avbröts mitt i körningen: "
                     "kontrollera pro.meca.se, ordern kan ha lagts."]
    assert env["logged"]["orders_failed"] == 1


def test_queued_but_never_processed_record_is_swept(env):
    orders_state.queue("B9")
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 1

    assert orders_state.get("B9")["status"] == "failed"
    assert "Delbeställning för B9 kördes aldrig (jobbet avbröts). Beställ för hand." in env["sent"]
    assert env["logged"]["orders_failed"] == 1
    assert env["logged"]["orders_staged"] == 1


def test_sweep_message_undeliverable_still_marks_failed(env):
    orders_state.queue("B9")
    env["send_ok"] = False

    assert agent.run() == 1

    assert orders_state.get("B9")["status"] == "failed"
    assert env["logged"]["orders_failed"] == 1


def test_normal_run_queues_before_staging_and_sweep_skips_own_records(env, monkeypatch):
    seen_at_stage = {}

    def fake_stage(config, booking, portal, rules, picker):
        bid = booking["id"]
        seen_at_stage[bid] = orders_state.get(bid)
        assert orders_state.begin(bid) is True
        orders_state.put(bid, "pending", plan={"booking_id": bid})
        return "pending"

    monkeypatch.setattr(agent, "plan_and_stage", fake_stage)
    env["bookings"] = [_booking("B1"), _booking("B2")]

    assert agent.run() == 0

    assert seen_at_stage["B1"]["plan"] == {"queued": True}
    assert seen_at_stage["B2"]["status"] == "attempted"
    assert orders_state.get("B1")["status"] == "pending"
    assert not any("Delbeställning för" in t for t in env["sent"])


def test_queue_happens_even_if_hanterad_raises(env, monkeypatch):
    def boom(config, url):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(agent, "mark_booking_handled", boom)
    seen = []
    monkeypatch.setattr(agent, "plan_and_stage",
                        lambda c, b, p, r, k: seen.append(orders_state.get(b["id"])) or "pending")
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 1   # the Hanterad failure is reported
    assert seen[0]["plan"] == {"queued": True}


def test_ordered_but_unnotified_makes_exit_code_1(env, monkeypatch):
    def fake_stage(config, booking, portal, rules, picker):
        orders_state.put(booking["id"], "ordered",
                         plan={"confirmation": "ORD-1", "notify_failed": True})
        return "ordered"

    monkeypatch.setattr(agent, "plan_and_stage", fake_stage)
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 1
    assert env["logged"]["orders_failed"] == 1


# ── Failed MECA portal login ─────────────────────────────────────────────────

def _login_fails(env, monkeypatch, reason="Invalid user name or password."):
    from portal_scraper import PortalLoginError

    def fake_fetch(config):
        env.setdefault("events", []).append("fetch")
        raise PortalLoginError(reason)

    monkeypatch.setattr(agent, "fetch_bookings", fake_fetch)


def test_failed_login_alerts_once_and_exits_1(env, monkeypatch):
    _login_fails(env, monkeypatch)

    assert agent.run() == 1

    alerts = [t for t in env["sent"] if "MECA-inloggningen misslyckades" in t]
    assert len(alerts) == 1 and len(env["sent"]) == 1
    assert "PORTAL_USERNAME/PORTAL_PASSWORD" in alerts[0]
    assert "Orsak: Invalid user name or password." in alerts[0]
    assert env["logged"] == {"bookings_found": 0, "notified": 0, "notify_failed": 0,
                             "orders_staged": 0, "orders_failed": 0, "login_failed": True}
    assert env["saved"] is None          # seen_ids untouched
    assert env["staged"] == [] and env["handled"] == []


def test_failed_login_without_reason_says_okand(env, monkeypatch):
    _login_fails(env, monkeypatch, reason="")

    assert agent.run() == 1
    assert env["sent"][0].endswith("Orsak: okänd")


def test_failed_login_undeliverable_alert_still_exits_1(env, monkeypatch):
    _login_fails(env, monkeypatch)
    env["send_ok"] = False

    assert agent.run() == 1
    assert env["logged"]["login_failed"] is True


def test_sweep_runs_before_fetch_even_when_login_fails(env, monkeypatch):
    orders_state.queue("B9")
    _login_fails(env, monkeypatch)

    assert agent.run() == 1

    assert env["sent"][0] == "Delbeställning för B9 kördes aldrig (jobbet avbröts). Beställ för hand."
    assert "MECA-inloggningen misslyckades" in env["sent"][1]
    assert orders_state.get("B9")["status"] == "failed"
    assert env["logged"]["orders_failed"] == 1
    assert env["logged"]["login_failed"] is True


def test_empty_result_still_means_no_new_bookings(env, caplog):
    caplog.set_level("INFO")
    assert agent.run() == 0
    assert "No new bookings found" in caplog.text
    assert "login_failed" not in env["logged"]
    assert env["sent"] == []


def test_failed_login_message_carries_portal_reason(env, monkeypatch):
    _login_fails(env, monkeypatch,
                 reason="Fel användarnamn eller lösenord (portalen avvisade inloggningen)")

    assert agent.run() == 1
    assert env["sent"] == [agent.LOGIN_FAILED_TEXT
                           + "Fel användarnamn eller lösenord (portalen avvisade inloggningen)"]



# ── Hanterad failures are reported; seen Bokningar still listed are retried ──

HANDLED_FAILED = "Kunde inte markera bokning OLRD-3630 som Hanterad i portalen – gör det för hand: http://x/{}"


def _handled_returns(env, monkeypatch, result):
    def fake_handled(config, url):
        env["handled"].append(url)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(agent, "mark_booking_handled", fake_handled)


def _no_parts(bid, btype="Bokning"):
    return {**_booking(bid, btype), "parts": [], "fluids": []}


def test_hanterad_true_sends_no_extra_message(env):
    env["bookings"] = [_no_parts("B1")]

    assert agent.run() == 0
    assert len(env["sent"]) == 1
    assert env["logged"]["handled_failed"] == 0


@pytest.mark.parametrize("result", [None, False, RuntimeError("browser crashed")])
def test_hanterad_not_true_is_reported_once_and_exits_1(env, monkeypatch, result):
    _handled_returns(env, monkeypatch, result)
    env["bookings"] = [_no_parts("B1")]

    assert agent.run() == 1

    assert env["sent"][1:] == [HANDLED_FAILED.format("B1")]
    assert env["logged"]["handled_failed"] == 1
    assert env["saved"] == {"B1"}


def test_hanterad_failure_message_uses_id_without_booking_nr(env, monkeypatch):
    _handled_returns(env, monkeypatch, False)
    env["bookings"] = [{**_no_parts("B1"), "booking_nr": ""}]

    assert agent.run() == 1
    assert env["sent"][1] == ("Kunde inte markera bokning B1 som Hanterad i portalen – "
                              "gör det för hand: http://x/B1")


def test_hanterad_failure_message_undeliverable_still_exits_1(env, monkeypatch):
    _handled_returns(env, monkeypatch, None)
    env["seen"] = {"B1"}
    env["bookings"] = [_no_parts("B1")]
    env["send_ok"] = False

    assert agent.run() == 1
    assert env["logged"]["handled_failed"] == 1


def test_forfragan_offert_have_no_hanterad_attempt_or_message(env):
    env["bookings"] = [_no_parts("F1", "Förfrågan"), _no_parts("O1", "Offert")]

    assert agent.run() == 0
    assert env["handled"] == []
    assert len(env["sent"]) == 2


def test_seen_bokning_still_listed_is_retried_once_silently(env, caplog):
    caplog.set_level("INFO")
    env["seen"] = {"B1"}
    env["bookings"] = [_no_parts("B1")]

    assert agent.run() == 0

    assert env["handled"] == ["http://x/B1"]
    assert env["sent"] == []
    assert env["staged"] == []
    assert orders_state.get("B1") is None
    assert "Booking B1 CONFIRMED as Hanterad (retry)" in caplog.text


def test_seen_bokning_retry_failure_is_reported(env, monkeypatch):
    _handled_returns(env, monkeypatch, None)
    env["seen"] = {"B1"}
    env["bookings"] = [_no_parts("B1")]

    assert agent.run() == 1

    assert env["handled"] == ["http://x/B1"]
    assert env["sent"] == [HANDLED_FAILED.format("B1")]
    assert env["logged"]["handled_failed"] == 1


def test_seen_bokning_with_orders_record_is_left_to_ordering(env):
    orders_state.put("B1", "pending", plan={"booking_id": "B1"})
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 0
    assert env["handled"] == []
    assert env["sent"] == []


def test_seen_forfragan_offert_are_not_retried(env):
    env["seen"] = {"F1", "O1"}
    env["bookings"] = [_no_parts("F1", "Förfrågan"), _no_parts("O1", "Offert")]

    assert agent.run() == 0
    assert env["handled"] == [] and env["sent"] == []
