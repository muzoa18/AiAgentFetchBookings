import sys
import types

import pytest

import agent
import notifier
import orders_state
import state
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
    monkeypatch.setattr(state, "NO_BUTTON_FILE", tmp_path / "no_button_ids.txt")
    monkeypatch.setattr(state, "NO_BUTTON_SEEN_FILE", tmp_path / "no_button_seen_once.txt")
    rec["buttons"], rec["klar"], rec["cleared"] = [], [], []

    def fake_buttons(config, text, buttons):
        # A Klar-only message is a resolution message: delivered like a plain one (Task 16b).
        if buttons and all(data.startswith("klar:") for _, data in buttons):
            if not rec["send_ok"]:
                return None
            rec["sent"].append(text)
            rec["klar"].append((text, buttons))
            return 900 + len(rec["klar"])
        rec["buttons"].append((text, buttons))
        return 77

    monkeypatch.setattr(notifier, "send_with_buttons", fake_buttons)
    monkeypatch.setattr(notifier, "clear_buttons",
                        lambda config, message_id: rec["cleared"].append(message_id))
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
                             "orders_staged": 0, "orders_failed": 0, "handled_failed": 0,
                             "login_failed": False}


def test_notify_success_marks_seen_and_calls_hanterad_for_bokning_only(env):
    env["bookings"] = [_no_parts("B1"), _booking("F1", "Förfrågan"), _booking("O1", "Offert")]

    assert agent.run() == 0

    assert env["saved"] == {"B1", "F1", "O1"}
    assert len(env["sent"]) == 3
    assert env["handled"] == ["http://x/B1"]
    assert env["logged"] == {"bookings_found": 3, "notified": 3, "notify_failed": 0,
                             "orders_staged": 0, "orders_failed": 0, "handled_failed": 0,
                             "login_failed": False}


def test_already_seen_booking_is_not_resent(env):
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 0

    assert env["sent"] == []
    # still on the Nya tab with parts and no orders record -> staged, never Hanterad here
    assert env["handled"] == []
    assert [s[0] for s in env["staged"]] == ["B1"]


def test_no_bookings_returns_zero_and_logs(env):
    assert agent.run() == 0
    assert env["logged"] == {"bookings_found": 0, "notified": 0, "notify_failed": 0,
                             "orders_staged": 0, "orders_failed": 0, "handled_failed": 0,
                             "login_failed": False}


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
    assert orders_state.get("B2") is None   # fluids only = no parts: no order record
    assert env["handled"] == ["http://x/B1", "http://x/B2"]
    assert any("Olja 5W30 x5" in t for t in env["sent"])   # listed in the notification


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
    env["bookings"] = [_no_parts("B1"), _no_parts("B2")]

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


def test_bokning_with_parts_is_queued_and_never_marked_hanterad(env, monkeypatch):
    def boom(config, url):
        raise AssertionError("Hanterad must not be pressed for a booking with parts")

    monkeypatch.setattr(agent, "mark_booking_handled", boom)
    seen = []
    monkeypatch.setattr(agent, "plan_and_stage",
                        lambda c, b, p, r, k: seen.append(orders_state.get(b["id"])) or "pending")
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 0
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

def _login_fails(env, monkeypatch,
                 reason="Fel användarnamn eller lösenord (portalen avvisade inloggningen)"):
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
    assert alerts[0].endswith(
        "Orsak: Fel användarnamn eller lösenord (portalen avvisade inloggningen)")
    assert env["logged"] == {"bookings_found": 0, "notified": 0, "notify_failed": 0,
                             "orders_staged": 0, "orders_failed": 0, "handled_failed": 0,
                             "login_failed": True}
    assert env["saved"] is None          # seen_ids untouched
    assert env["staged"] == [] and env["handled"] == []


def test_failed_login_without_reason_says_okand(env, monkeypatch):
    _login_fails(env, monkeypatch, reason="")

    assert agent.run() == 1
    assert env["sent"][0].endswith("Orsak: okänd orsak")


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
    assert env["logged"]["login_failed"] is False
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


# ── Task 16a: the portal is the truth for Bokningar ──────────────────────────

LOG_KEYS = {"bookings_found", "notified", "notify_failed", "orders_staged", "orders_failed",
            "handled_failed", "login_failed"}
GIVE_UP = "Försöker inte mer automatiskt för OLRD-3630 – beställ för hand eller åtgärda orsaken."


def test_seen_bokning_logs_already_notified_not_already_handled(env, caplog):
    caplog.set_level("INFO")
    env["seen"] = {"B1", "B2"}
    env["bookings"] = [_no_parts("B1"), _booking("B2")]

    agent.run()

    assert "Already notified: B1" in caplog.text
    assert "Already notified: B2" in caplog.text
    assert "Already handled" not in caplog.text


def test_unseen_no_parts_bokning_notified_once_then_hanterad(env, monkeypatch):
    events = []
    monkeypatch.setattr(agent, "send_message",
                        lambda config, text: events.append(("sent", text)) or True)
    monkeypatch.setattr(agent, "mark_booking_handled",
                        lambda config, url: events.append(("handled", url)) or True)
    env["bookings"] = [_no_parts("B1")]

    assert agent.run() == 0

    assert [e[0] for e in events] == ["sent", "handled"]
    assert events[0][1].startswith("Ny Bokning - OLRD-3630")
    assert env["saved"] == {"B1"}
    assert orders_state.get("B1") is None
    assert env["staged"] == []


def test_seen_no_parts_bokning_is_retried_every_run_until_it_succeeds(env, monkeypatch):
    _handled_returns(env, monkeypatch, False)
    env["seen"] = {"B1"}
    env["bookings"] = [_no_parts("B1")]

    assert agent.run() == 1
    assert agent.run() == 1
    _handled_returns(env, monkeypatch, True)
    env["sent"].clear()
    assert agent.run() == 0

    assert env["handled"] == ["http://x/B1"] * 3
    assert env["sent"] == []          # success: no message, never a re-notification


def test_fluids_only_bokning_is_not_queued_and_gets_hanterad(env):
    env["bookings"] = [{**_booking("B1"), "parts": [{"name": "Motorolja 5W-30", "qty": "4"}],
                        "fluids": [{"name": "Spolarvätska", "qty": "1"}]}]

    assert agent.run() == 0

    assert orders_state.get("B1") is None
    assert env["staged"] == []
    assert env["handled"] == ["http://x/B1"]
    assert len(env["sent"]) == 1      # the notification only (it lists the fluids)


RECORD_STATES = ["attempted", "pending", "ordered", "failed", "cancelled", "expired", "blocked"]


@pytest.mark.parametrize("seen", [False, True])
@pytest.mark.parametrize("status", [None] + RECORD_STATES)
def test_bokning_with_parts_never_gets_hanterad(env, seen, status):
    if status:
        orders_state.put("B1", status, plan={"reason": "x"})
    if seen:
        env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    agent.run()

    assert env["handled"] == []


def test_seen_with_parts_and_no_record_is_queued_and_staged(env, monkeypatch):
    at_stage = []
    monkeypatch.setattr(agent, "plan_and_stage",
                        lambda c, b, p, r, k: at_stage.append(orders_state.get(b["id"])) or "pending")
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 0

    assert at_stage[0]["plan"] == {"queued": True}
    assert env["sent"] == []


def test_seen_with_parts_blocked_untouched_is_restaged(env, monkeypatch):
    at_stage = []
    monkeypatch.setattr(agent, "plan_and_stage",
                        lambda c, b, p, r, k: at_stage.append(orders_state.get(b["id"])) or "blocked")
    orders_state.put("B1", "blocked", plan={"reason": "ingen säker träff för 'Pollenfilter'"})
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 0

    (rec,) = at_stage
    assert orders_state.is_queued(rec)
    assert rec["tries"] == 1
    assert rec["last_reason"] == "ingen säker träff för 'Pollenfilter'"
    assert env["sent"] == [] and env["handled"] == []


def test_blocked_with_cart_touched_is_not_retried(env):
    orders_state.put("B1", "blocked", plan={"reason": "varukorgen stämmer inte"})
    orders_state.update_fields("B1", cart_touched=True)
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 0

    assert env["staged"] == [] and env["sent"] == [] and env["handled"] == []
    assert orders_state.get("B1")["status"] == "blocked"


@pytest.mark.parametrize("status", ["attempted", "pending", "ordered", "failed", "cancelled",
                                    "expired"])
def test_other_order_records_are_left_alone(env, status):
    orders_state.put("B1", status, plan={"booking_id": "B1"})
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    agent.run()

    assert env["staged"] == [] and env["handled"] == []
    # an "attempted" record is swept (reported + failed) at the start of the run, as before
    assert all("Delbeställning för B1" in t for t in env["sent"])
    if status != "attempted":
        assert env["sent"] == []
        assert orders_state.get("B1")["status"] == status


def test_blocked_out_of_tries_sends_one_give_up_message_ever(env):
    orders_state.put("B1", "blocked", plan={"reason": "x"})
    orders_state.update_fields("B1", tries=orders_state.MAX_TRIES)
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 0
    assert agent.run() == 0

    assert env["sent"] == [GIVE_UP]
    assert env["staged"] == []


NO_BUTTON_NOTICE = ("Bokning OLRD-3630 verkar sakna Hanterad-knapp (typen är osäker) – "
                    "kontrollera den i portalen.")


def test_guessed_type_without_button_twice_becomes_offert_like(env, monkeypatch, caplog):
    caplog.set_level("INFO")
    _handled_returns(env, monkeypatch, None)
    env["bookings"] = [{**_no_parts("B1"), "booking_type_guess": True}]

    # first None: only logged and remembered, no message, no failure
    assert agent.run() == 0
    assert len(env["sent"]) == 1      # the notification only
    assert env["logged"]["handled_failed"] == 0
    assert "B1" not in state.load_no_button_ids()

    # second None in a row: added to the list, ONE notice
    env["seen"] = env["saved"]
    env["sent"].clear()
    assert agent.run() == 0
    assert env["sent"] == [NO_BUTTON_NOTICE]
    assert "B1" in state.load_no_button_ids()
    assert env["handled"] == ["http://x/B1"] * 2

    # from then on: Offert/Förfrågan handling (PENDING only, no attempt, no message)
    env["sent"].clear()
    assert agent.run() == 0
    assert env["handled"] == ["http://x/B1"] * 2
    assert env["sent"] == []
    assert "PENDING Bokning B1" in caplog.text


def test_guessed_none_then_other_result_resets_the_count(env, monkeypatch):
    env["bookings"] = [{**_no_parts("B1"), "booking_type_guess": True}]
    _handled_returns(env, monkeypatch, None)
    agent.run()
    env["seen"] = env["saved"]
    _handled_returns(env, monkeypatch, False)
    assert agent.run() == 1            # a real failure in between
    _handled_returns(env, monkeypatch, None)
    env["sent"].clear()
    assert agent.run() == 0            # a first None again: no notice yet
    assert env["sent"] == []
    assert "B1" not in state.load_no_button_ids()


def test_undelivered_no_button_notice_is_a_failure_and_retried(env, monkeypatch):
    _handled_returns(env, monkeypatch, None)
    env["bookings"] = [{**_no_parts("B1"), "booking_type_guess": True}]
    agent.run()
    env["seen"] = env["saved"]
    env["send_ok"] = False

    assert agent.run() == 1
    assert env["logged"]["handled_failed"] == 1
    assert "B1" not in state.load_no_button_ids()

    env["send_ok"] = True
    env["sent"].clear()
    assert agent.run() == 0
    assert env["sent"] == [NO_BUTTON_NOTICE]
    assert "B1" in state.load_no_button_ids()


def test_not_guessed_type_without_button_is_still_a_failure(env, monkeypatch):
    _handled_returns(env, monkeypatch, None)
    env["bookings"] = [{**_no_parts("B1"), "booking_type_guess": False}]

    assert agent.run() == 1

    assert env["sent"][1:] == [HANDLED_FAILED.format("B1")]
    assert env["logged"]["handled_failed"] == 1
    assert "B1" not in state.load_no_button_ids()


@pytest.mark.parametrize("result", [False, RuntimeError("browser crashed")])
def test_guessed_type_real_failure_is_still_reported(env, monkeypatch, result):
    _handled_returns(env, monkeypatch, result)
    env["bookings"] = [{**_no_parts("B1"), "booking_type_guess": True}]

    assert agent.run() == 1
    assert env["sent"][1:] == [HANDLED_FAILED.format("B1")]
    assert "B1" not in state.load_no_button_ids()


def test_one_broken_booking_does_not_stop_the_others(env):
    env["bookings"] = [{**_booking("B0"), "parts": 5}, _no_parts("B1")]

    assert agent.run() == 1      # the broken booking is not silent

    assert env["handled"] == ["http://x/B1"]
    assert env["saved"] == {"B1"}   # B0 broke while building its message: not notified


def test_log_run_keys_are_identical_in_every_path(env, monkeypatch):
    keys = []
    env["bookings"] = []
    agent.run()
    keys.append(set(env["logged"]))
    env["bookings"] = [_booking("B1"), _no_parts("B2"), _booking("F1", "Förfrågan")]
    agent.run()
    keys.append(set(env["logged"]))
    _login_fails(env, monkeypatch)
    agent.run()
    keys.append(set(env["logged"]))
    assert keys == [LOG_KEYS] * 3


# ── Task 16a: quiet retry end to end through the real plan_and_stage ─────────

class _Portal:
    """Minimal pro.meca.se fake: one search result per known term, cart = what was added."""

    def __init__(self, catalogue):
        self.catalogue, self.added = catalogue, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self):
        pass

    def load_vehicle(self, reg_nr):
        pass

    def search(self, term):
        return list(self.catalogue.get(term, []))

    def add_to_cart(self, candidate, qty):
        self.added.append((candidate, qty))

    def read_cart(self):
        lines = [{"title": c["title"], "qty": q, "price_sek": c["price_sek"] * q,
                  "url_or_id": c["url_or_id"]} for c, q in self.added]
        return {"lines": lines, "total_sek": sum(x["price_sek"] for x in lines)}


OIL = {"title": "Bosch Oljefilter P7001", "brand": "Bosch", "price_sek": 120.0, "url_or_id": "p1"}
AIR = {"title": "Bosch Luftfilter S0123", "brand": "Bosch", "price_sek": 250.0, "url_or_id": "p2"}


@pytest.fixture
def real_stage(env, monkeypatch):
    import parts_order
    monkeypatch.setattr(agent, "plan_and_stage", parts_order.plan_and_stage)
    monkeypatch.setattr(agent, "make_claude_picker", lambda api_key: lambda line, ranked: (0, 0.95))

    def use(catalogue):
        monkeypatch.setitem(sys.modules, "parts_portal",
                            types.SimpleNamespace(PartsPortal=lambda config: _Portal(catalogue)))
    return use


@pytest.mark.parametrize("old_reason,catalogue,messages,buttons", [
    ("ingen säker träff för 'Luftfilter'", {"Oljefilter": [OIL]}, 0, 0),
    ("varukorgen är inte tom", {"Oljefilter": [OIL]}, 1, 0),
    ("ingen säker träff för 'Luftfilter'", {"Oljefilter": [OIL], "Luftfilter": [AIR]}, 0, 1),
])
def test_blocked_order_quiet_retry(env, real_stage, old_reason, catalogue, messages, buttons):
    real_stage(catalogue)
    orders_state.put("B1", "blocked", plan={"reason": old_reason, "booking_id": "B1"})
    orders_state.update_fields("B1", cart_touched=False)
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 0

    assert len(env["sent"]) == messages
    assert len(env["buttons"]) == buttons
    assert env["handled"] == []
    rec = orders_state.get("B1")
    assert rec["tries"] == 1
    assert rec["status"] == ("pending" if buttons else "blocked")
    assert rec["detail_url"] == "http://x/B1"
    text = orders_state.ORDERS_FILE.read_text(encoding="utf-8")
    for sentinel in ("Mamadou Sowe", "+46739133124"):
        assert sentinel not in text


# ── Review I1: never press Hanterad on an incompletely read specification ───

SPEC_INCOMPLETE = ("Bokning OLRD-3630: specifikationen kunde inte läsas fullständigt – jag har "
                   "inte markerat den som Hanterad. Kontrollera delarna i portalen.")


def _no_hanterad(monkeypatch):
    def boom(config, url):
        raise AssertionError("Hanterad must not be pressed")
    monkeypatch.setattr(agent, "mark_booking_handled", boom)


@pytest.mark.parametrize("flags", [{"spec_fallback": True},
                                   {"spec_dropped": 2},
                                   {"spec_fallback": True, "spec_dropped": 1}])
@pytest.mark.parametrize("seen", [False, True])
def test_incomplete_spec_without_parts_is_not_marked_hanterad(env, monkeypatch, flags, seen):
    _no_hanterad(monkeypatch)
    if seen:
        env["seen"] = {"B1"}
    env["bookings"] = [{**_no_parts("B1"), **flags}]

    assert agent.run() == 1

    assert env["sent"][-1] == SPEC_INCOMPLETE
    assert env["sent"].count(SPEC_INCOMPLETE) == 1
    assert len(env["sent"]) == (1 if seen else 2)
    if not seen:
        assert "Obs:" in env["sent"][0]          # the notification keeps its warning
    assert env["logged"]["handled_failed"] == 1
    assert orders_state.get("B1") is None


def test_incomplete_spec_warning_logs_no_row_text(env, monkeypatch, caplog):
    caplog.set_level("INFO")
    _no_hanterad(monkeypatch)
    env["bookings"] = [{**_no_parts("B1"), "spec_fallback": True,
                        "fluids": [{"name": "Sentinel Vätska", "qty": "1"}]}]

    agent.run()

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("B1" in w and "specification" in w.lower() for w in warnings)
    assert not any("Sentinel Vätska" in w for w in warnings)


def test_incomplete_spec_with_parts_is_still_queued_and_staged(env, monkeypatch):
    _no_hanterad(monkeypatch)
    env["bookings"] = [{**_booking("B1"), "spec_fallback": True, "spec_dropped": 1}]

    assert agent.run() == 0

    assert [s[0] for s in env["staged"]] == ["B1"]
    assert SPEC_INCOMPLETE not in env["sent"]


@pytest.mark.parametrize("flags", [{}, {"spec_fallback": False, "spec_dropped": 0}])
def test_clean_spec_without_parts_is_marked_hanterad(env, flags):
    env["bookings"] = [{**_no_parts("B1"), **flags}]

    assert agent.run() == 0
    assert env["handled"] == ["http://x/B1"]
    assert len(env["sent"]) == 1


# ── Review M3: an undelivered give-up message fails the run ─────────────────

def test_undelivered_give_up_fails_the_run_and_is_retried(env):
    orders_state.put("B1", "blocked", plan={"reason": "x"})
    orders_state.update_fields("B1", tries=orders_state.MAX_TRIES)
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]
    env["send_ok"] = False

    assert agent.run() == 1
    assert env["logged"]["orders_failed"] == 1
    assert "gave_up" not in orders_state.get("B1")

    env["send_ok"] = True
    env["sent"].clear()
    assert agent.run() == 0
    assert env["sent"] == [GIVE_UP]


# ── Review M1: legacy blocked records through the agent ─────────────────────

@pytest.mark.parametrize("reason,restaged", [
    ("ingen säker träff för 'Pollenfilter'", True),
    ("inga delar att beställa", True),
    ("varukorgen stämmer inte med de valda delarna", False),
])
def test_legacy_blocked_record_restaged_only_for_pre_cart_reasons(env, reason, restaged):
    orders_state.put("B1", "blocked", plan={"reason": reason})   # no cart_touched key
    env["seen"] = {"B1"}
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 0

    assert bool(env["staged"]) is restaged


# ── Review I2: no raw exception text from agent.py ───────────────────────────

def test_ordering_setup_error_text_never_reaches_telegram(env, monkeypatch, caplog):
    class Exploding:
        def __init__(self, config):
            raise RuntimeError("SECRET-PAGE-TEXT chromium crashed")

    monkeypatch.setitem(sys.modules, "parts_portal", types.SimpleNamespace(PartsPortal=Exploding))
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 1

    assert orders_state.get("B1")["status"] == "failed"
    failures = [t for t in env["sent"] if "misslyckades" in t]
    assert failures == ["Delbeställning misslyckades för OLRD-3630: kunde inte starta "
                        "beställningen. Beställ för hand."]
    assert all("SECRET-PAGE-TEXT" not in t and "RuntimeError" not in t for t in env["sent"])
    assert "SECRET-PAGE-TEXT" in caplog.text


def test_missing_settings_keep_their_fixed_swedish_reason(env, monkeypatch):
    monkeypatch.setattr(agent, "load_config",
                        lambda: {**CONFIG, "anthropic": {"api_key": ""}})
    env["bookings"] = [_booking("B1")]

    assert agent.run() == 1

    (failure,) = [t for t in env["sent"] if "misslyckades" in t]
    assert "kunde inte starta beställningen (inställning saknas: anthropic.api_key)" in failure
    assert "RuntimeError" not in failure



# ── Re-review: the login alert forwards only the fixed Swedish reasons ───────

@pytest.mark.parametrize("reason", [
    "SECRET-PAGE-TEXT Invalid user name or password.",
    "Portalen avvisade inloggningen (SECRET-PAGE-TEXT)",
    "login form not found",
    "okänd orsak",
])
def test_login_alert_never_forwards_page_text(env, monkeypatch, caplog, reason):
    _login_fails(env, monkeypatch, reason=reason)

    assert agent.run() == 1

    (alert,) = env["sent"]
    assert alert.endswith("Orsak: okänd orsak")
    assert "SECRET-PAGE-TEXT" not in alert
    assert reason in caplog.text                       # the raw reason is logged


@pytest.mark.parametrize("reason", [
    "Fel användarnamn eller lösenord (portalen avvisade inloggningen)",
    "För många inloggningsförsök i rad – vänta en stund och försök igen",
    "Lösenordet har gått ut – byt lösenord i portalen",
    "Kontot är tillfälligt avstängt",
    "Portalen avvisade inloggningen",
    "Portalen avvisade inloggningen (AccountLocked)",
])
def test_login_alert_forwards_the_fixed_auth_reasons(env, monkeypatch, reason):
    _login_fails(env, monkeypatch, reason=reason)

    assert agent.run() == 1
    assert env["sent"][0].endswith(f"Orsak: {reason}")
