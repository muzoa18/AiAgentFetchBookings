import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import approve
import config_loader
import notifier
import orders_state
import parts_order
import rules
from parts_portal_parse import PartsPortalError
from conftest import write_orders_skeleton
from rules import Rules

REPO = Path(__file__).resolve().parent.parent
CONFIG = {"telegram": {"bot_token": "123:ABC", "chat_id": "42"}}
APPROVE = Rules(("Bosch",), ("ProMeister",), 10000, "approve")
T0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)

EXPECTED = [{"title": "Bosch Oljefilter P7001", "qty": 2, "url_or_id": "p1"},
            {"title": "Bosch Luftfilter S0123", "qty": 1, "url_or_id": "p2"}]
CART = {"lines": [{"title": "Bosch Oljefilter P7001", "qty": 2, "url_or_id": "p1",
                   "price_sek": 240.0},
                  {"title": "Bosch Luftfilter S0123", "qty": 1, "url_or_id": "p2",
                   "price_sek": 250.0}],
        "total_sek": 490.0}


def plan_for(booking_id="B1", booking_nr="OLRD-0001"):
    return {"booking_id": booking_id, "booking_nr": booking_nr, "reg_nr": "TST001",
            "parts": [{"name": "Oljefilter", "title": "Bosch Oljefilter P7001", "brand": "Bosch",
                       "price_sek": 120.0, "qty": 2, "url_or_id": "p1"},
                      {"name": "Luftfilter", "title": "Bosch Luftfilter S0123", "brand": "Bosch",
                       "price_sek": 250.0, "qty": 1, "url_or_id": "p2"}],
            "expected_lines": EXPECTED, "total_sek": 490.0, "fluids": [],
            "created_at": T0.isoformat()}


def stage(booking_id="B1", message_id=77, updated_at=None, booking_nr="OLRD-0001"):
    orders_state.put(booking_id, "pending", plan=plan_for(booking_id, booking_nr),
                     message_id=message_id)
    if updated_at is not None:
        data = json.loads(orders_state.ORDERS_FILE.read_text(encoding="utf-8"))
        data["bookings"][booking_id]["updated_at"] = updated_at.isoformat()
        orders_state.ORDERS_FILE.write_text(json.dumps(data), encoding="utf-8")


_SAME = object()


def tap(update_id, data="approve:B1", message_id=77, chat_id="42", user_id=_SAME):
    return {"update_id": update_id, "chat_id": chat_id,
            "user_id": chat_id if user_id is _SAME else user_id,
            "callback_id": f"cb{update_id}", "data": data, "message_id": message_id}


class FakePortal:
    def __init__(self, carts=None, ref="ORD-9", clear_fails=False):
        self.carts = list(carts or [])
        self.ref = ref
        self.clear_fails = clear_fails
        self.calls = []
        self.ceilings, self.cleared_regs = [], []

    def login(self):
        self.calls.append("login")

    def load_vehicle(self, reg_nr):
        self.calls.append("load_vehicle")

    def read_cart(self):
        self.calls.append("read_cart")
        return self.carts.pop(0) if self.carts else CART

    def checkout(self, *, max_total_sek):
        self.calls.append("checkout")
        self.ceilings.append(max_total_sek)
        return self.ref

    def clear_cart(self, reg_nr=None):
        self.calls.append("clear_cart")
        self.cleared_regs.append(reg_nr)
        if self.clear_fails:
            raise RuntimeError("clear broke")


class PortalFactory:
    """Callable returning a context manager around one FakePortal; counts opens."""

    def __init__(self, portal=None, allowed=True, open_fails=False):
        self.portal = portal or FakePortal()
        self.allowed = allowed
        self.open_fails = open_fails
        self.opened = 0
        self.closed = 0

    def __call__(self):
        if not self.allowed:
            raise AssertionError("the portal must not be opened")
        factory = self

        class _Ctx:
            def __enter__(self):
                if factory.open_fails:
                    raise RuntimeError("login page unreachable")
                factory.opened += 1
                return factory.portal

            def __exit__(self, *exc):
                factory.closed += 1
                return False

        return _Ctx()


@pytest.fixture(autouse=True)
def orders_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "orders.json"
    monkeypatch.setattr(orders_state, "ORDERS_FILE", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_orders_skeleton(path)
    return path


@pytest.fixture
def tg(monkeypatch):
    """Fake Telegram: records every call; `updates` is what get_updates returns."""
    rec = {"sent": [], "answers": [], "cleared": [], "offsets": [], "updates": [],
           "send_ok": True, "klar": [], "klar_ok": True, "next_klar": 900}

    def fake_send(config, text):
        rec["sent"].append(text)
        return rec["send_ok"]

    def fake_get_updates(config, offset=None):
        rec["offsets"].append(offset)
        return list(rec["updates"])

    def fake_answer(config, callback_id, text):
        rec["answers"].append((callback_id, text))

    def fake_clear(config, message_id):
        rec["cleared"].append(message_id)

    def klar_only(config, text, buttons):
        """Resolution messages carry one Klar button; the poller sends no other buttons."""
        if not (len(buttons) == 1 and buttons[0][0] == "Klar"
                and buttons[0][1].startswith("klar:")):
            raise AssertionError("the poller only ever sends Klar buttons")
        if not (rec["send_ok"] and rec["klar_ok"]):
            return None
        mid = rec["next_klar"]
        rec["next_klar"] += 1
        rec["sent"].append(text)
        rec["klar"].append((text, buttons, mid))
        return mid

    monkeypatch.setattr(notifier, "send_message", fake_send)
    monkeypatch.setattr(notifier, "get_updates", fake_get_updates)
    monkeypatch.setattr(notifier, "answer_callback", fake_answer)
    monkeypatch.setattr(notifier, "clear_buttons", fake_clear)
    monkeypatch.setattr(notifier, "send_with_buttons", klar_only)
    return rec


@pytest.fixture(autouse=True)
def hanterad(monkeypatch):
    """Fake portal_scraper.mark_booking_handled (approve.mark_handled). Set `result` to
    True/False/None or an exception; unset means it must not be called."""
    rec = {"calls": [], "result": AssertionError("Hanterad must not be pressed")}

    def fake_mark(config, detail_url):
        rec["calls"].append(detail_url)
        if isinstance(rec["result"], BaseException):
            raise rec["result"]
        return rec["result"]

    monkeypatch.setattr(approve, "mark_handled", fake_mark, raising=False)
    return rec


@pytest.fixture
def run_main(monkeypatch):
    """main([]) with config/rules faked and the given portal factory."""

    def _run(factory, rules_obj=APPROVE):
        monkeypatch.setattr(config_loader, "load_config", lambda: CONFIG)
        if isinstance(rules_obj, Exception):
            def bad_rules(*a, **k):
                raise rules_obj
            monkeypatch.setattr(rules, "load_rules", bad_rules)
        else:
            monkeypatch.setattr(rules, "load_rules", lambda *a, **k: rules_obj)
        monkeypatch.setattr(approve, "make_portal", lambda config: factory())
        return approve.main([])

    return _run


def status(booking_id="B1"):
    return orders_state.get(booking_id)["status"]


# ── main: early exit and --check ─────────────────────────────────────────────

def test_nothing_pending_exits_without_telegram_call(monkeypatch, tmp_path):
    orders_state.put("B9", "ordered", plan={"booking_id": "B9"})
    orders_state.update_fields("B9", verified_at=T0.isoformat())   # nothing left to verify

    def forbidden(*a, **k):
        raise AssertionError("must not be called when nothing is pending")

    for name in ("send_message", "get_updates", "answer_callback", "clear_buttons"):
        monkeypatch.setattr(notifier, name, forbidden)
    monkeypatch.setattr(config_loader, "load_config", forbidden)
    monkeypatch.setattr(approve, "make_portal", forbidden)

    assert approve.main([]) == 0

    # --check, in a fresh interpreter with a pending booking: stdlib + orders_state only.
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "orders.json").write_text(json.dumps(
        {"last_update_id": None, "bookings": {"B1": {
            "status": "pending", "plan": None, "message_id": 77,
            "updated_at": T0.isoformat()}}}), encoding="utf-8")
    code = (
        "import sys, approve\n"
        "rc = approve.main(['--check'])\n"
        "heavy = [m for m in ('playwright', 'anthropic', 'yaml', 'notifier', 'rules',\n"
        "         'parts_order', 'config_loader', 'parts_portal') if m in sys.modules]\n"
        "print('HEAVY', heavy)\n"
        "sys.exit(rc)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    env.pop("GITHUB_OUTPUT", None)
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "has_pending=true" in out.stdout
    assert "HEAVY []" in out.stdout


@pytest.mark.parametrize("pending,expected", [(True, "true"), (False, "false")])
def test_check_writes_has_pending_to_github_output(monkeypatch, tmp_path, capsys,
                                                   pending, expected):
    if pending:
        stage()
    out_file = tmp_path / "gh_output"
    out_file.write_text("earlier=1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    assert approve.main(["--check"]) == 0

    assert f"has_pending={expected}" in capsys.readouterr().out
    assert out_file.read_text(encoding="utf-8") == f"earlier=1\nhas_pending={expected}\n"


def test_check_with_corrupt_state_fails_the_step(monkeypatch, tmp_path, orders_file):
    orders_file.parent.mkdir(parents=True, exist_ok=True)
    orders_file.write_text("{not json", encoding="utf-8")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert approve.main(["--check"]) == 1


def test_rules_invalid_messages_and_exits_1_without_portal(tg, run_main):
    stage()
    tg["updates"] = [tap(1)]

    assert run_main(PortalFactory(allowed=False), ValueError("bad rules")) == 1

    assert tg["sent"] == ["Regelfilen är ogiltig – inga godkännanden hanteras"]
    assert tg["offsets"] == []
    assert tg["answers"] == []
    assert status() == "pending"
    assert orders_state.last_update_id() is None


# ── handle_updates: who may approve ──────────────────────────────────────────

def test_foreign_chat_id_ignored(tg):
    stage()
    tg["updates"] = [tap(5, chat_id="999")]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert tg["answers"] == []
    assert tg["cleared"] == []
    assert tg["sent"] == []
    assert status() == "pending"
    assert orders_state.last_update_id() == 5


def test_empty_configured_chat_id_accepts_nobody(tg):
    stage()
    tg["updates"] = [tap(5, chat_id="")]
    config = {"telegram": {"bot_token": "123:ABC", "chat_id": ""}}

    assert approve.handle_updates(config, PortalFactory(allowed=False), APPROVE) == 0

    assert tg["answers"] == []
    assert status() == "pending"


@pytest.mark.parametrize("configured,chat,user", [
    ("-1001234", "-1001234", "42"),        # group configured as the chat id, member taps
    ("-1001234", "-1001234", "-1001234"),  # even if ids line up, groups are never trusted
    ("42", "42", "43"),                    # private chat, different user
    ("42", "42", ""),                      # tapping user unknown
    ("42", "42", None),
    ("abc", "abc", "abc"),                 # not a numeric private chat id
    ("+42", "+42", "+42"),
])
def test_only_owner_in_private_chat_may_tap(tg, configured, chat, user):
    stage()
    tg["updates"] = [tap(5, chat_id=chat, user_id=user)]
    config = {"telegram": {"bot_token": "123:ABC", "chat_id": configured}}

    assert approve.handle_updates(config, PortalFactory(allowed=False), APPROVE) == 0

    assert tg["answers"] == []
    assert tg["cleared"] == []
    assert tg["sent"] == []
    assert status() == "pending"
    assert orders_state.last_update_id() == 5


def test_owner_tap_with_int_configured_chat_id_works(tg):
    stage()
    factory = PortalFactory()
    tg["updates"] = [tap(5, chat_id="42", user_id="42")]
    config = {"telegram": {"bot_token": "123:ABC", "chat_id": 42}}

    assert approve.handle_updates(config, factory, APPROVE) == 1
    assert status() == "ordered"


def test_stale_message_id_ignored(tg):
    stage(message_id=77)
    tg["updates"] = [tap(5, message_id=12)]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert tg["answers"] == [("cb5", "Knappen är inte längre giltig.")]
    assert tg["cleared"] == []
    assert status() == "pending"
    assert orders_state.last_update_id() == 5


@pytest.mark.parametrize("data", [None, "", "approve", "approve:", "delete:B1", "APPROVE:B1",
                                  "approve: B1", "approve:B1 "])
def test_malformed_callback_data(tg, data):
    stage()
    tg["updates"] = [tap(5, data=data)]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert tg["answers"] == [("cb5", "Okänd åtgärd.")]
    assert status() == "pending"
    assert orders_state.last_update_id() == 5


def test_unknown_booking_id(tg):
    stage()
    tg["updates"] = [tap(5, data="approve:B404")]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert tg["answers"] == [("cb5", "Okänd bokning.")]
    assert orders_state.get("B404") is None
    assert status() == "pending"


def test_last_update_id_advanced_even_for_ignored_updates(tg):
    stage()
    orders_state.set_last_update_id(4)
    tg["updates"] = [tap(5, chat_id="999"), tap(6, data="nonsense"),
                     tap(7, data="approve:B404"), tap(8, message_id=1)]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert tg["offsets"] == [5]
    assert orders_state.last_update_id() == 8
    assert status() == "pending"

    tg["updates"] = []
    approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE)
    assert tg["offsets"] == [5, 9]


def test_offset_never_moves_backwards_or_takes_non_int(tg):
    stage()
    orders_state.set_last_update_id(10)
    tg["updates"] = [tap(3, chat_id="999"), tap("11", chat_id="999"), tap(None, chat_id="999")]

    approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE)

    assert orders_state.last_update_id() == 10


# ── handle_updates: approve ──────────────────────────────────────────────────

def test_approve_executes_once_and_second_tap_says_already_handled(tg):
    stage()
    factory = PortalFactory()
    tg["updates"] = [tap(5), tap(6)]

    assert approve.handle_updates(CONFIG, factory, APPROVE) == 1

    assert factory.portal.calls.count("checkout") == 1
    rec = orders_state.get("B1")
    assert rec["status"] == "ordered"
    assert rec["plan"]["confirmation"] == "ORD-9"
    assert tg["answers"] == [("cb5", "Beställer…"), ("cb6", "Redan hanterad.")]
    assert tg["cleared"] == [77]
    assert len(tg["sent"]) == 1 and tg["sent"][0].startswith("Beställd: OLRD-0001")
    assert orders_state.last_update_id() == 6

    # The same tap delivered again on a later poll changes nothing.
    tg["updates"] = [tap(7)]
    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0
    assert tg["answers"][-1] == ("cb7", "Redan hanterad.")
    assert status() == "ordered"


def test_main_approve_success_exits_0(tg, run_main):
    stage()
    factory = PortalFactory()
    tg["updates"] = [tap(5)]

    assert run_main(factory) == 0

    assert status() == "ordered"
    assert factory.opened == 1 and factory.closed == 1


def test_portal_opened_once_for_two_approvals_in_one_poll(tg):
    stage("B1", message_id=77, booking_nr="OLRD-1")
    stage("B2", message_id=78, booking_nr="OLRD-2")
    factory = PortalFactory()
    tg["updates"] = [tap(5, "approve:B1", 77), tap(6, "approve:B2", 78)]

    assert approve.handle_updates(CONFIG, factory, APPROVE) == 2

    assert factory.opened == 1
    assert factory.portal.calls.count("checkout") == 2
    assert status("B1") == status("B2") == "ordered"
    assert tg["cleared"] == [77, 78]


def test_cart_mismatch_marks_failed_and_notifies(tg, run_main):
    stage()
    wrong = {"lines": [{"title": "Bosch Oljefilter P7001", "qty": 1, "url_or_id": "p1"},
                       {"title": "Bosch Luftfilter S0123", "qty": 1, "url_or_id": "p2"}],
             "total_sek": 490.0}
    factory = PortalFactory(FakePortal(carts=[wrong]))
    tg["updates"] = [tap(5)]

    assert run_main(factory) == 1

    assert "checkout" not in factory.portal.calls
    assert status() == "failed"
    assert len(tg["sent"]) == 1 and tg["sent"][0].startswith("Beställning stoppad för OLRD-0001")
    assert tg["cleared"] == [77]
    assert orders_state.last_update_id() == 5


def test_ordered_but_notify_failed_exits_1(tg, run_main):
    stage()
    tg["send_ok"] = False
    tg["updates"] = [tap(5)]

    assert run_main(PortalFactory()) == 1

    rec = orders_state.get("B1")
    assert rec["status"] == "ordered"
    assert rec["plan"]["notify_failed"] is True


def test_approve_when_portal_cannot_open_fails_closed(tg, run_main):
    stage()
    factory = PortalFactory(open_fails=True)
    tg["updates"] = [tap(5)]

    assert run_main(factory) == 1

    assert status() == "failed"
    assert len(tg["sent"]) == 1
    assert "OLRD-0001" in tg["sent"][0] and "Inget beställdes" in tg["sent"][0]
    assert orders_state.last_update_id() == 5


def test_approve_older_than_48h_at_tap_time_is_refused(tg):
    stage(updated_at=datetime.now(timezone.utc) - timedelta(hours=49))
    tg["updates"] = [tap(5)]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert tg["answers"] == [("cb5", "Godkännandet har gått ut.")]
    assert status() == "pending"  # expire_stale reports it on the next run
    assert orders_state.last_update_id() == 5


def test_exception_leaves_offset_when_booking_still_pending(tg, monkeypatch):
    stage()
    orders_state.set_last_update_id(4)

    def broken_clear(config, message_id):
        raise RuntimeError("telegram hiccup")

    monkeypatch.setattr(notifier, "clear_buttons", broken_clear)
    tg["updates"] = [tap(5), tap(6, chat_id="999")]

    approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE)

    assert status() == "pending"
    assert orders_state.last_update_id() == 4  # retried next poll, later updates too

    monkeypatch.setattr(notifier, "clear_buttons", lambda config, mid: tg["cleared"].append(mid))
    factory = PortalFactory()
    assert approve.handle_updates(CONFIG, factory, APPROVE) == 1
    assert factory.portal.calls.count("checkout") == 1
    assert orders_state.last_update_id() == 6


def test_exception_after_order_advances_offset(tg, monkeypatch):
    stage()

    def broken(booking_id):
        raise RuntimeError("state read broke")

    monkeypatch.setattr(parts_order, "order_notify_failed", broken)
    factory = PortalFactory()
    tg["updates"] = [tap(5)]

    approve.handle_updates(CONFIG, factory, APPROVE)

    assert status() == "ordered"
    assert orders_state.last_update_id() == 5  # never re-executed


# ── handle_updates: cancel ───────────────────────────────────────────────────

@pytest.mark.parametrize("clear_fails,open_fails,tail", [
    (False, False, "Varukorgen är tömd."),
    (True, False, "töm varukorgen på pro.meca.se för hand."),
    (False, True, "töm varukorgen på pro.meca.se för hand."),
])
def test_cancel_sets_cancelled_and_clears_buttons(tg, clear_fails, open_fails, tail):
    stage()
    factory = PortalFactory(FakePortal(clear_fails=clear_fails), open_fails=open_fails)
    tg["updates"] = [tap(5, data="cancel:B1")]

    assert approve.handle_updates(CONFIG, factory, APPROVE) == 1

    rec = orders_state.get("B1")
    assert rec["status"] == "cancelled"
    assert rec["plan"] == plan_for()
    assert tg["cleared"] == [77]
    assert tg["answers"] == [("cb5", "Avbruten.")]
    assert len(tg["sent"]) == 1
    assert tg["sent"][0].startswith("Avbruten: OLRD-0001.")
    assert tg["sent"][0].endswith(tail)
    assert "checkout" not in factory.portal.calls
    if not open_fails:
        assert factory.portal.calls == ["login", "clear_cart"]
        assert factory.portal.cleared_regs == ["TST001"]  # the cart is per vehicle
    assert orders_state.last_update_id() == 5

    # Approve tapped after cancel does nothing.
    tg["updates"] = [tap(6)]
    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0
    assert tg["answers"][-1] == ("cb6", "Redan hanterad.")
    assert status() == "cancelled"


def test_cancel_message_undelivered_exits_1(tg, run_main):
    stage()
    tg["send_ok"] = False
    tg["updates"] = [tap(5, data="cancel:B1")]

    assert run_main(PortalFactory()) == 1
    assert status() == "cancelled"


# ── expire_stale ─────────────────────────────────────────────────────────────

def test_expire_stale_after_48h_notifies(tg):
    stage(updated_at=T0)

    assert approve.expire_stale(CONFIG, T0 + timedelta(hours=48)) == 0
    assert status() == "pending"
    assert tg["sent"] == [] and tg["cleared"] == []

    assert approve.expire_stale(CONFIG, T0 + timedelta(hours=48, seconds=1)) == 1

    rec = orders_state.get("B1")
    assert rec["status"] == "expired"
    assert rec["plan"] == plan_for()
    assert tg["cleared"] == [77]
    assert len(tg["sent"]) == 1
    assert tg["sent"][0].startswith(
        "Godkännandet för OLRD-0001 har gått ut (48 h). Beställ för hand om delarna behövs.")
    assert "töm" in tg["sent"][0]


def test_expire_stale_unreadable_timestamp_expires(tg):
    stage()
    data = json.loads(orders_state.ORDERS_FILE.read_text(encoding="utf-8"))
    data["bookings"]["B1"]["updated_at"] = "not a date"
    orders_state.ORDERS_FILE.write_text(json.dumps(data), encoding="utf-8")

    assert approve.expire_stale(CONFIG, T0) == 1
    assert status() == "expired"


def test_approve_after_expiry_refused(tg, run_main):
    stage(updated_at=datetime.now(timezone.utc) - timedelta(hours=49))
    tg["updates"] = [tap(5)]

    assert run_main(PortalFactory(allowed=False)) == 0

    assert status() == "expired"
    assert tg["answers"] == [("cb5", "Redan hanterad.")]
    assert len(tg["sent"]) == 1 and "har gått ut" in tg["sent"][0]


def test_expiry_message_undelivered_still_expires_and_exits_1(tg, run_main):
    stage(updated_at=datetime.now(timezone.utc) - timedelta(hours=49))
    tg["send_ok"] = False

    assert run_main(PortalFactory(allowed=False)) == 1
    assert status() == "expired"


# ── Task 16b: Klar on cancelled / expired / failed messages ──────────────────

KLAR = [("Klar", "klar:B1")]
URL = "https://portal.example/Booking/Details?BookingID=B1"


def test_cancel_message_carries_klar(tg):
    stage()
    tg["updates"] = [tap(5, data="cancel:B1")]

    assert approve.handle_updates(CONFIG, PortalFactory(), APPROVE) == 1

    (text, buttons, mid), = tg["klar"]
    assert buttons == KLAR and text.startswith("Avbruten: OLRD-0001.")
    assert tg["sent"] == [text]
    assert orders_state.get("B1")["klar_message_id"] == mid
    assert tg["cleared"] == [77]  # only the approval buttons


def test_expiry_message_carries_klar(tg):
    stage(updated_at=T0)

    assert approve.expire_stale(CONFIG, T0 + timedelta(hours=49)) == 1

    (text, buttons, mid), = tg["klar"]
    assert buttons == KLAR and "har gått ut" in text
    assert orders_state.get("B1")["klar_message_id"] == mid


def test_expiry_klar_failure_falls_back_to_plain_and_is_still_delivered(tg, run_main):
    stage(updated_at=datetime.now(timezone.utc) - timedelta(hours=49))
    tg["klar_ok"] = False

    assert run_main(PortalFactory(allowed=False)) == 0

    assert tg["klar"] == []
    (text,) = tg["sent"]
    assert "har gått ut" in text
    assert text.endswith("(Klar-knappen kunde inte skickas – markera bokningen som "
                         "Hanterad i portalen.)")
    assert "klar_message_id" not in orders_state.get("B1")


def test_portal_open_failure_message_carries_klar(tg, run_main):
    stage()
    tg["updates"] = [tap(5)]

    assert run_main(PortalFactory(open_fails=True)) == 1

    (text, buttons, mid), = tg["klar"]
    assert buttons == KLAR and "Inget beställdes" in text
    assert orders_state.get("B1")["klar_message_id"] == mid


def test_ordered_message_from_the_poller_carries_klar(tg):
    stage()
    tg["updates"] = [tap(5)]

    assert approve.handle_updates(CONFIG, PortalFactory(), APPROVE) == 1

    (text, buttons, mid), = tg["klar"]
    assert buttons == KLAR and text.startswith("Beställd: OLRD-0001")
    assert orders_state.get("B1")["klar_message_id"] == mid


# ── Task 16b: Klar taps ──────────────────────────────────────────────────────

def resolved(booking_id="B1", status_="ordered", klar=900, detail_url=URL, **fields):
    """A resolved record with an outstanding Klar button (message id `klar`)."""
    stage(booking_id)
    orders_state.put(booking_id, status_)
    extra = {"klar_message_id": klar, **fields}
    if detail_url is not None:
        extra["detail_url"] = detail_url
    orders_state.update_fields(booking_id, **extra)


def klar_tap(update_id, booking_id="B1", message_id=900, **kw):
    return tap(update_id, data=f"klar:{booking_id}", message_id=message_id, **kw)


DONE = "Klar: bokning OLRD-0001 är markerad som Hanterad."
NOT_DONE = f"Kunde inte markera bokning OLRD-0001 som Hanterad – gör det för hand: {URL}"


@pytest.mark.parametrize("status_", ["ordered", "blocked", "failed", "cancelled", "expired"])
def test_klar_tap_marks_hanterad(tg, hanterad, run_main, status_):
    resolved(status_=status_)
    hanterad["result"] = True
    tg["updates"] = [klar_tap(5)]

    assert run_main(PortalFactory(allowed=False)) == 0

    assert hanterad["calls"] == [URL]
    rec = orders_state.get("B1")
    assert rec["handled"] is True and rec["status"] == status_
    assert tg["answers"] == [("cb5", "Markerar som Hanterad…")]
    assert tg["cleared"] == [900]
    assert tg["sent"] == [DONE] and tg["klar"] == []
    assert orders_state.outstanding_klar() == []
    assert orders_state.last_update_id() == 5


@pytest.mark.parametrize("result", [False, RuntimeError("browser died")])
def test_klar_hanterad_failure_keeps_the_button_and_exits_1(tg, hanterad, run_main, result):
    resolved()
    hanterad["result"] = result
    tg["updates"] = [klar_tap(5)]

    assert run_main(PortalFactory(allowed=False)) == 1

    assert hanterad["calls"] == [URL]
    assert tg["sent"] == [NOT_DONE]
    assert tg["cleared"] == []
    rec = orders_state.get("B1")
    assert rec.get("handled") is not True and rec["klar_message_id"] == 900
    assert [b for b, _ in orders_state.outstanding_klar()] == ["B1"]
    assert orders_state.last_update_id() == 5

    # The owner taps the same button again; this time it works.
    hanterad["result"] = True
    tg["updates"] = [klar_tap(6)]
    assert run_main(PortalFactory(allowed=False)) == 0
    assert orders_state.get("B1")["handled"] is True
    assert tg["sent"][-1] == DONE


def test_klar_confirmation_undelivered_exits_1_but_stays_handled(tg, hanterad, run_main):
    resolved()
    hanterad["result"] = True
    tg["send_ok"] = False
    tg["updates"] = [klar_tap(5)]

    assert run_main(PortalFactory(allowed=False)) == 1
    assert orders_state.get("B1")["handled"] is True


@pytest.mark.parametrize("status_,answer", [
    ("pending", "Godkänn eller avbryt beställningen först."),
    ("attempted", "Beställningen pågår – vänta."),
])
def test_klar_refused_while_the_order_is_open(tg, hanterad, status_, answer):
    resolved(status_=status_)
    tg["updates"] = [klar_tap(5)]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert hanterad["calls"] == []
    assert tg["answers"] == [("cb5", answer)]
    assert tg["sent"] == [] and tg["cleared"] == []
    assert orders_state.get("B1")["status"] == status_
    assert "handled" not in orders_state.get("B1")


def test_klar_on_already_handled_booking(tg, hanterad):
    resolved(handled=True)
    tg["updates"] = [klar_tap(5)]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert hanterad["calls"] == []
    assert tg["answers"] == [("cb5", "Redan hanterad.")]
    assert tg["cleared"] == [900]
    assert tg["sent"] == []


def test_klar_without_detail_url_asks_for_manual_handling(tg, hanterad, run_main):
    resolved(detail_url=None)
    tg["updates"] = [klar_tap(5)]

    assert run_main(PortalFactory(allowed=False)) == 1

    assert hanterad["calls"] == []
    assert tg["answers"] == [("cb5", "Saknar länk – markera bokningen som Hanterad i portalen.")]
    (text,) = tg["sent"]
    assert "OLRD-0001" in text and "Hanterad i portalen" in text
    assert orders_state.get("B1").get("handled") is not True


@pytest.mark.parametrize("message_id", [77, 12, None, "900"])
def test_klar_stale_message_id_is_refused(tg, hanterad, message_id):
    resolved()  # approval message 77, Klar message 900
    tg["updates"] = [klar_tap(5, message_id=message_id)]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert hanterad["calls"] == []
    assert tg["answers"] == [("cb5", "Knappen är inte längre giltig.")]
    assert orders_state.last_update_id() == 5


def test_klar_without_a_stored_klar_message_is_refused(tg, hanterad):
    stage()
    orders_state.put("B1", "ordered")
    tg["updates"] = [klar_tap(5, message_id=77)]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert hanterad["calls"] == []
    assert tg["answers"] == [("cb5", "Knappen är inte längre giltig.")]


def test_approve_tap_on_the_klar_message_is_refused(tg, hanterad):
    stage()
    orders_state.update_fields("B1", klar_message_id=900)
    tg["updates"] = [tap(5, data="approve:B1", message_id=900)]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0
    assert tg["answers"] == [("cb5", "Knappen är inte längre giltig.")]
    assert status() == "pending"


@pytest.mark.parametrize("configured,chat,user", [
    ("-1001234", "-1001234", "42"),
    ("-1001234", "-1001234", "-1001234"),
    ("42", "42", "43"),
    ("42", "42", ""),
    ("42", "42", None),
    ("42", "999", "42"),
    ("abc", "abc", "abc"),
])
def test_klar_only_from_the_owner_in_private_chat(tg, hanterad, configured, chat, user):
    resolved()
    tg["updates"] = [klar_tap(5, chat_id=chat, user_id=user)]
    config = {"telegram": {"bot_token": "123:ABC", "chat_id": configured}}

    assert approve.handle_updates(config, PortalFactory(allowed=False), APPROVE) == 0

    assert hanterad["calls"] == []
    assert tg["answers"] == [] and tg["sent"] == [] and tg["cleared"] == []
    assert orders_state.last_update_id() == 5


@pytest.mark.parametrize("data", ["klar", "klar:", "KLAR:B1", "Klar:B1", "klar: B1", "klar:B1 ",
                                  "klar:B 1"])
def test_klar_malformed_data(tg, hanterad, data):
    resolved()
    tg["updates"] = [tap(5, data=data, message_id=900)]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert hanterad["calls"] == []
    assert tg["answers"] == [("cb5", "Okänd åtgärd.")]


def test_klar_unknown_booking(tg, hanterad):
    resolved()
    tg["updates"] = [klar_tap(5, booking_id="B404")]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0

    assert hanterad["calls"] == []
    assert tg["answers"] == [("cb5", "Okänd bokning.")]


def test_klar_double_tap_and_replay(tg, hanterad, run_main):
    resolved()
    hanterad["result"] = True
    tg["updates"] = [klar_tap(5), klar_tap(6)]

    assert run_main(PortalFactory(allowed=False)) == 0

    assert hanterad["calls"] == [URL]
    assert tg["answers"] == [("cb5", "Markerar som Hanterad…"), ("cb6", "Redan hanterad.")]
    assert tg["sent"] == [DONE]

    # The same tap delivered again later: handled, nothing pressed.
    tg["updates"] = [klar_tap(7)]
    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 0
    assert hanterad["calls"] == [URL]
    assert tg["answers"][-1] == ("cb7", "Redan hanterad.")


def test_klar_for_a_blocked_record_due_for_quiet_restage(tg, hanterad):
    resolved(status_="blocked", cart_touched=False)
    orders_state.put("B1", "blocked", plan={"reason": "ingen säker träff för 'Luftfilter'",
                                            "booking_id": "B1", "booking_nr": "OLRD-0001"})
    hanterad["result"] = True
    tg["updates"] = [klar_tap(5)]

    assert approve.handle_updates(CONFIG, PortalFactory(allowed=False), APPROVE) == 1

    assert hanterad["calls"] == [URL]
    assert orders_state.get("B1")["handled"] is True
    assert tg["sent"] == [DONE]
    assert orders_state.restage("B1") is False  # a handled booking is never re-staged


def test_klar_and_approve_in_one_poll(tg, hanterad):
    resolved("B1")
    stage("B2", message_id=78, booking_nr="OLRD-2")
    hanterad["result"] = True
    factory = PortalFactory()
    tg["updates"] = [klar_tap(5), tap(6, data="approve:B2", message_id=78)]

    assert approve.handle_updates(CONFIG, factory, APPROVE) == 2

    assert orders_state.get("B1")["handled"] is True
    assert status("B2") == "ordered"
    assert factory.opened == 1  # the parts portal is opened only for the approval
    assert orders_state.last_update_id() == 6


def test_klar_exception_is_isolated_and_offset_advances(tg, hanterad, monkeypatch):
    resolved("B1")
    stage("B2", message_id=78, booking_nr="OLRD-2")
    hanterad["result"] = True
    real_update = orders_state.update_fields

    def broken(booking_id, **fields):
        if fields.get("handled"):
            raise RuntimeError("disk full")
        return real_update(booking_id, **fields)

    monkeypatch.setattr(orders_state, "update_fields", broken)
    tg["updates"] = [klar_tap(5), tap(6, data="cancel:B2", message_id=78)]

    actions, problem = approve._handle(CONFIG, PortalFactory(), APPROVE)

    assert problem is True
    assert status("B2") == "cancelled"  # the later update was still handled
    assert orders_state.last_update_id() == 6


# ── Task 16b: --check and the early exit with an outstanding Klar ────────────

def test_check_true_for_outstanding_klar_false_after_handled(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    resolved(status_="cancelled")             # an 'ordered' one would also await the checker

    assert approve.main(["--check"]) == 0
    assert "has_pending=true" in capsys.readouterr().out

    orders_state.update_fields("B1", handled=True)
    assert approve.main(["--check"]) == 0
    assert "has_pending=false" in capsys.readouterr().out


def test_check_with_only_an_outstanding_klar_is_stdlib_only(tmp_path):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "orders.json").write_text(json.dumps(
        {"last_update_id": None, "bookings": {"B1": {
            "status": "ordered", "plan": None, "message_id": 77, "klar_message_id": 900,
            "updated_at": T0.isoformat()}}}), encoding="utf-8")
    code = (
        "import sys, approve\n"
        "rc = approve.main(['--check'])\n"
        "heavy = [m for m in ('playwright', 'anthropic', 'yaml', 'notifier', 'rules',\n"
        "         'parts_order', 'config_loader', 'parts_portal', 'portal_scraper')\n"
        "         if m in sys.modules]\n"
        "print('HEAVY', heavy)\n"
        "sys.exit(rc)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    env.pop("GITHUB_OUTPUT", None)
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "has_pending=true" in out.stdout
    assert "HEAVY []" in out.stdout


def test_main_exits_early_when_only_handled_klar_records_exist(monkeypatch):
    resolved(handled=True, verified_at=T0.isoformat())   # and the order checker is done

    def forbidden(*a, **k):
        raise AssertionError("must not be called")

    for name in ("send_message", "get_updates", "answer_callback", "clear_buttons"):
        monkeypatch.setattr(notifier, name, forbidden)
    monkeypatch.setattr(config_loader, "load_config", forbidden)

    assert approve.main([]) == 0


def test_main_polls_when_only_a_klar_is_outstanding(tg, run_main):
    resolved()
    tg["updates"] = []

    assert run_main(PortalFactory(allowed=False)) == 0
    assert tg["offsets"] == [None]


def test_approve_module_does_not_import_portal_scraper_at_top_level():
    text = (REPO / "approve.py").read_text(encoding="utf-8")
    top = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
    assert top == ["import argparse", "import logging", "import os", "import sys",
                   "from contextlib import ExitStack",
                   "from datetime import datetime, timedelta, timezone",
                   "import orders_state  # stdlib only: everything heavy is imported lazily below"]


# ── workflows ────────────────────────────────────────────────────────────────

def _workflow(name):
    wf = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    assert isinstance(wf, dict)
    return wf


def _steps(wf):
    (job,) = wf["jobs"].values()
    return job["steps"]


def test_approve_workflow_shape():
    wf = _workflow("approve.yml")
    on = wf.get("on", wf.get(True))  # PyYAML reads a bare `on` key as True
    # Once a day at 06:00 UTC, every day (Telegram keeps updates only 24 h).
    assert on["schedule"] == [{"cron": "0 6 * * *"}]
    assert "workflow_dispatch" in on
    (job,) = wf["jobs"].values()
    assert job["timeout-minutes"] == 15
    assert wf["concurrency"] == {"group": "meca-bookings", "cancel-in-progress": False}
    assert _workflow("bookings.yml")["concurrency"]["group"] == "meca-bookings"

    steps = _steps(wf)
    runs = [s.get("run", "") for s in steps]
    check_i = next(i for i, r in enumerate(runs) if "approve.py --check" in r)
    assert steps[check_i]["id"] == "check"
    install_i = next(i for i, r in enumerate(runs) if "pip install" in r)
    assert check_i < install_i
    gate = "steps.check.outputs.has_pending == 'true'"
    for step in steps:
        run = step.get("run", "")
        if "pip install" in run or "playwright install" in run or run.strip() == "python approve.py":
            assert gate in step.get("if", "")
    run_step = next(s for s in steps if s.get("run", "").strip() == "python approve.py")
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "PARTS_PORTAL_USERNAME",
                "PARTS_PORTAL_PASSWORD", "PORTAL_USERNAME", "PORTAL_PASSWORD"):
        assert var in run_step["env"]


@pytest.mark.parametrize("name", ["approve.yml", "bookings.yml"])
def test_persist_step_rebases_and_retries(name):
    persist = next(s for s in _steps(_workflow(name)) if "git push" in s.get("run", ""))
    assert "always()" in persist["if"]
    assert "config/orders.json" in persist["run"]
    assert "git pull --rebase --autostash origin main" in persist["run"]
    assert "exit 1" in persist["run"]
    assert "git rebase --abort 2>/dev/null || true" in persist["run"]


def test_bookings_persist_step_commits_every_local_state_file():
    persist = next(s for s in _steps(_workflow("bookings.yml")) if "git push" in s.get("run", ""))
    run = persist["run"]
    # each file is added on its own: one missing pathspec makes a multi-file git add stage nothing
    loop = run[run.index("for f in"):run.index("done")]
    assert 'git add "$f" 2>/dev/null || true' in loop
    for name in ("config/seen_ids.txt", "config/orders.json", "logs/run_history.jsonl",
                 "config/no_button_ids.txt", "config/no_button_seen_once.txt"):
        assert name in loop.replace(";", " ").split()


def test_daily_run_is_clear_of_the_approval_poll():
    wf = _workflow("bookings.yml")
    on = wf.get("on", wf.get(True))
    assert on["schedule"] == [{"cron": "0 15 * * 1-6"}]
    approve_on = _workflow("approve.yml")
    approve_on = approve_on.get("on", approve_on.get(True))
    assert approve_on["schedule"] == [{"cron": "0 6 * * *"}]  # 9 h away from the daily run
    text = (REPO / ".github" / "workflows" / "bookings.yml").read_text(encoding="utf-8")
    assert "15:00 UTC" in text and "17:00" in text and "16:00" in text


# ── Fix wave: raw exception text never reaches the owner ─────────────────────

def test_portal_open_failure_message_has_no_raw_exception_text(tg, run_main):
    stage()

    def factory():
        raise RuntimeError("SECRET-PAGE-TEXT Chromium exploded")

    tg["updates"] = [tap(5)]
    assert run_main(factory) == 1

    (text,) = tg["sent"]
    assert "SECRET-PAGE-TEXT" not in text
    assert text.startswith("Beställning misslyckades för OLRD-0001: kunde inte öppna pro.meca.se.")
    assert status() == "failed"



# ── Fix wave: Klar follow-ups ────────────────────────────────────────────────

def test_klar_with_hanterad_button_absent_counts_as_already_handled(tg, hanterad, run_main):
    resolved()
    hanterad["result"] = None
    tg["updates"] = [klar_tap(5)]

    assert run_main(PortalFactory(allowed=False)) == 0

    assert hanterad["calls"] == [URL]
    assert tg["sent"] == ["Hanterad-knappen finns inte – bokningen verkar redan vara hanterad "
                          f"(kontrollera i portalen: {URL})"]
    assert tg["cleared"] == [900]
    assert orders_state.get("B1")["handled"] is True
    assert orders_state.outstanding_klar() == []


def test_klar_without_detail_url_closes_the_button_once(tg, hanterad, run_main):
    resolved(detail_url=None)
    tg["updates"] = [klar_tap(5)]

    assert run_main(PortalFactory(allowed=False)) == 1

    assert tg["cleared"] == [900]
    rec = orders_state.get("B1")
    assert rec["klar_closed"] is True and rec.get("handled") is not True
    assert orders_state.outstanding_klar() == []
    assert len(tg["sent"]) == 1

    # A replayed tap on the closed button changes nothing and is not a failure.
    tg["updates"] = [klar_tap(6)]
    assert approve._handle(CONFIG, PortalFactory(allowed=False), APPROVE) == (0, False)
    assert tg["answers"][-1] == ("cb6", "Knappen är inte längre giltig.")
    assert len(tg["sent"]) == 1
    assert hanterad["calls"] == []


def _klar_sent(days_ago, booking_id="B1", **fields):
    resolved(booking_id, **fields)
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    orders_state.update_fields(booking_id, klar_sent_at=when.isoformat())


def test_expired_klar_is_closed_once_with_a_notice(tg, hanterad, run_main, monkeypatch, capsys):
    _klar_sent(15, verified_at=T0.isoformat())       # the order checker is already done
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert approve.main(["--check"]) == 0            # the run must happen once to close it
    assert "has_pending=true" in capsys.readouterr().out

    assert run_main(PortalFactory(allowed=False)) == 0

    assert tg["cleared"] == [900]
    assert tg["sent"] == ["Klar-knappen för OLRD-0001 har gått ut – markera bokningen som "
                          "Hanterad i portalen."]
    assert orders_state.get("B1")["klar_closed"] is True
    assert hanterad["calls"] == []

    assert approve.main(["--check"]) == 0
    assert "has_pending=false" in capsys.readouterr().out
    assert approve.main([]) == 0                     # early exit: nothing more is sent
    assert len(tg["sent"]) == 1


def test_expired_klar_tap_in_the_same_run_is_refused(tg, hanterad, run_main):
    _klar_sent(15)
    tg["updates"] = [klar_tap(5)]

    assert run_main(PortalFactory(allowed=False)) == 0

    assert hanterad["calls"] == []
    assert tg["answers"] == [("cb5", "Knappen är inte längre giltig.")]


def test_expired_klar_notice_undelivered_is_retried(tg, hanterad, run_main):
    _klar_sent(15)
    tg["send_ok"] = False

    assert run_main(PortalFactory(allowed=False)) == 1
    assert orders_state.get("B1").get("klar_closed") is not True
    assert [b for b, _ in orders_state.expired_klar()] == ["B1"]


def test_recent_klar_is_not_expired(tg, hanterad, run_main):
    _klar_sent(13)
    assert run_main(PortalFactory(allowed=False)) == 0
    assert tg["sent"] == [] and tg["cleared"] == []
    assert [b for b, _ in orders_state.outstanding_klar()] == ["B1"]


def test_handled_or_closed_records_get_no_expiry_notice(tg, run_main):
    _klar_sent(30, "B1", handled=True)
    _klar_sent(30, "B2", klar_closed=True)
    stage("B3", message_id=78)  # something pending so the run goes on

    assert run_main(PortalFactory(allowed=False)) == 0
    assert tg["sent"] == [] and tg["cleared"] == []


# ── Task 14: the order checker (Skickade Beställningar) ──────────────────────

RIO = "KIA, Rio Kombi-sedan (III), 12-16"
ASTRA = "OPEL, Astra Sports Tourer (J), 10-16"


def today() -> str:
    return approve._local_date(datetime.now(timezone.utc)).isoformat()


def sent_row(supplier_no, model, numbers, day=None, reg=None):
    """reg=None: as if the table had no registration column (model matching)."""
    return {"supplier_no": supplier_no, "status": "", "date": day or today(), "reg_nr": reg,
            "model": model, "order_numbers": numbers, "order_status": "Bekräftad"}


ROWS = [sent_row("90000001", "", ""),
        sent_row("90000002", RIO, "01000011/01000012"),
        sent_row("90000004", ASTRA, "01000031/01000032"),
        sent_row("90000005", ASTRA, "01000041/01000042")]


class SentPortal:
    """Only list_sent_orders: the checker must never touch carts, vehicles or checkout."""

    def __init__(self, rows=ROWS, error=None):
        self.rows, self.error, self.calls = rows, error, []

    def login(self):
        self.calls.append("login")

    def list_sent_orders(self):
        self.calls.append("list_sent_orders")
        if self.error is not None:
            raise self.error
        return [dict(r) for r in self.rows]

    def __getattr__(self, name):
        raise AssertionError(f"the order checker must not use portal.{name}")


def placed(booking_id="B1", model="KIARIO (III)(2014)", age=timedelta(hours=1),
           booking_nr="OLRD-0001", confirmation="UNVERIFIED-20261021T123728Z", **fields):
    """An 'ordered' record as execute_order leaves it (Klar already handled)."""
    orders_state.put(booking_id, "ordered",
                     plan={**plan_for(booking_id, booking_nr), "confirmation": confirmation})
    extra = {"handled": True, **fields}
    if model is not None:
        extra["vehicle_model"] = model
    orders_state.update_fields(booking_id, **extra)
    data = json.loads(orders_state.ORDERS_FILE.read_text(encoding="utf-8"))
    data["bookings"][booking_id]["updated_at"] = (datetime.now(timezone.utc) - age).isoformat()
    orders_state.ORDERS_FILE.write_text(json.dumps(data), encoding="utf-8")


def check_output(capsys) -> str:
    assert approve.main(["--check"]) == 0
    return capsys.readouterr().out.strip()


def test_verified_order_is_stored_and_reported_once(tg, run_main, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    placed()
    assert check_output(capsys) == "has_pending=true"
    factory = PortalFactory(SentPortal())

    assert run_main(factory) == 0

    record = orders_state.get("B1")
    assert record["verified_numbers"] == "90000002 · 01000011/01000012"
    assert orders_state._parse_time(record["verified_at"]) is not None
    assert record["status"] == "ordered"
    assert record["plan"]["confirmation"] == "UNVERIFIED-20261021T123728Z"   # untouched
    assert tg["sent"] == [f"Beställning verifierad: OLRD-0001 – {RIO}, ordernummer 90000002 "
                          "(01000011/01000012)."]
    assert tg["klar"] == []                       # a plain message, no Klar button
    assert factory.portal.calls == ["list_sent_orders"]
    assert orders_state.unverified_orders() == []
    assert check_output(capsys) == "has_pending=false"

    assert run_main(PortalFactory(allowed=False)) == 0      # nothing left: early exit
    assert len(tg["sent"]) == 1


def test_verified_message_undelivered_exits_1_but_the_state_is_saved(tg, run_main):
    placed()
    tg["send_ok"] = False
    assert run_main(PortalFactory(SentPortal())) == 1
    assert orders_state.get("B1")["verified_numbers"] == "90000002 · 01000011/01000012"


def test_ambiguous_rows_are_reported_once_and_never_picked(tg, run_main, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    placed(model="OPELASTRA (J)(2012)")
    assert run_main(PortalFactory(SentPortal())) == 0

    record = orders_state.get("B1")
    assert "verified_at" not in record and record["verify_warned"] is True
    assert tg["sent"] == [f"Flera ordrar för {ASTRA} i Skickade Beställningar – kontrollera "
                          "vilken som är OLRD-0001: 90000004 (01000031/01000032), "
                          "90000005 (01000041/01000042)."]
    assert check_output(capsys) == "has_pending=false"
    assert run_main(PortalFactory(SentPortal())) == 0
    assert len(tg["sent"]) == 1


def test_ambiguous_list_is_capped_at_five(tg, run_main):
    rows = [sent_row(f"9000001{i}", ASTRA, f"0100{i}") for i in range(7)]
    placed(model="OPELASTRA (J)")
    run_main(PortalFactory(SentPortal(rows)))
    (text,) = tg["sent"]
    assert text.count("9000001") == 5 and "90000015" not in text


def test_ambiguous_message_undelivered_is_retried(tg, run_main):
    placed(model="OPELASTRA (J)")
    tg["send_ok"] = False
    assert run_main(PortalFactory(SentPortal())) == 1
    assert "verify_warned" not in orders_state.get("B1")


def test_not_found_is_silent_for_three_days(tg, run_main, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    placed(model="KIANIRO (I)", age=timedelta(days=2, hours=23))
    assert run_main(PortalFactory(SentPortal())) == 0
    assert tg["sent"] == []
    assert "verify_warned" not in orders_state.get("B1")
    assert check_output(capsys) == "has_pending=true"       # polled again on the next run


def test_not_found_after_three_days_warns_once(tg, run_main, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    placed(model="KIANIRO (I)", age=timedelta(days=3, minutes=1))
    assert run_main(PortalFactory(SentPortal())) == 0
    assert tg["sent"] == ["Kunde inte verifiera beställningen för OLRD-0001 i Skickade "
                          "Beställningar efter 3 dagar – kontrollera i portalen."]
    assert orders_state.get("B1")["verify_warned"] is True
    assert check_output(capsys) == "has_pending=false"
    assert run_main(PortalFactory(SentPortal())) == 0
    assert len(tg["sent"]) == 1


def test_warning_undelivered_exits_1_and_is_retried(tg, run_main):
    placed(model="KIANIRO (I)", age=timedelta(days=4))
    tg["send_ok"] = False
    assert run_main(PortalFactory(SentPortal())) == 1
    assert "verify_warned" not in orders_state.get("B1")


def test_no_model_and_no_registration_never_opens_the_portal(tg, run_main, monkeypatch):
    placed(model=None)
    data = json.loads(orders_state.ORDERS_FILE.read_text(encoding="utf-8"))
    data["bookings"]["B1"]["plan"]["reg_nr"] = None
    orders_state.ORDERS_FILE.write_text(json.dumps(data), encoding="utf-8")
    assert run_main(PortalFactory(allowed=False)) == 0
    assert tg["sent"] == []


def test_no_model_record_warns_after_three_days(tg, run_main):
    placed(model=None, age=timedelta(days=5))
    assert run_main(PortalFactory(SentPortal())) == 0
    assert tg["sent"] == ["Kunde inte verifiera beställningen för OLRD-0001 i Skickade "
                          "Beställningar efter 3 dagar – kontrollera i portalen."]


def test_no_model_record_is_verified_by_its_registration(tg, run_main):
    rows = ROWS + [sent_row("90000009", "KIA, Rio (III), 12-16", "01000091/01000092",
                            reg="TST001")]
    placed(model=None)                     # the live record from before vehicle_model existed
    assert run_main(PortalFactory(SentPortal(rows))) == 0
    assert orders_state.get("B1")["verified_numbers"] == "90000009 · 01000091/01000092"


def test_numbers_verified_for_another_record_are_not_claimed_twice(tg, run_main):
    placed("B0", booking_nr="OLRD-0000",
           verified_at="2026-10-21T15:00:00+00:00",
           verified_numbers="90000002 · 01000011/01000012")
    placed("B1")
    assert run_main(PortalFactory(SentPortal())) == 0
    assert "verified_at" not in orders_state.get("B1") and tg["sent"] == []


def test_two_orders_share_one_portal_session(tg, run_main):
    placed("B1")
    placed("B2", booking_nr="OLRD-0002", model="KIANIRO (I)")
    factory = PortalFactory(SentPortal())
    assert run_main(factory) == 0
    assert factory.opened == 1 and factory.portal.calls == ["list_sent_orders"]
    assert len(tg["sent"]) == 1 and "OLRD-0001" in tg["sent"][0]


def test_rows_older_than_the_order_are_ignored(tg, run_main):
    old = [sent_row("90000002", RIO, "01000011/01000012", day="2020-01-01")]
    placed()
    assert run_main(PortalFactory(SentPortal(old))) == 0
    assert tg["sent"] == []


@pytest.mark.parametrize("error", [
    PartsPortalError("SECRET-PAGE-TEXT 0701234567 kund@example.com", "search_timeout"),
    RuntimeError("SECRET-PAGE-TEXT 0701234567 kund@example.com"),
])
def test_portal_errors_are_logged_masked_and_retried_silently(tg, run_main, caplog, error):
    placed()
    with caplog.at_level("INFO"):
        assert run_main(PortalFactory(SentPortal(error=error))) == 0
    assert tg["sent"] == []
    assert "verified_at" not in orders_state.get("B1")
    assert "SECRET-PAGE-TEXT" not in caplog.text and "0701234567" not in caplog.text
    assert "kund@example.com" not in caplog.text


def test_portal_that_cannot_open_is_retried_silently(tg, run_main):
    placed()
    assert run_main(PortalFactory(open_fails=True)) == 0
    assert tg["sent"] == []


def test_portal_error_still_gives_the_three_day_warning(tg, run_main):
    placed(age=timedelta(days=3, hours=1))
    assert run_main(PortalFactory(SentPortal(error=RuntimeError("down")))) == 0
    assert len(tg["sent"]) == 1 and tg["sent"][0].startswith("Kunde inte verifiera")


def test_checker_crash_never_breaks_the_run(tg, run_main, monkeypatch):
    placed()

    def broken(*a, **k):
        raise RuntimeError("checker broke")

    monkeypatch.setattr(approve, "_verify_orders", broken)
    tg["updates"] = []
    assert run_main(PortalFactory(allowed=False)) == 0
    assert tg["offsets"] == [None]                    # the taps were still polled


def test_checker_runs_after_an_approval_in_the_same_session(tg, run_main):
    stage()
    tg["updates"] = [tap(1)]

    class Both(FakePortal):
        def list_sent_orders(self):
            self.calls.append("list_sent_orders")
            return []

    factory = PortalFactory(Both())
    assert run_main(factory) == 0
    assert status() == "ordered" and factory.opened == 1
    assert factory.portal.calls[-1] == "list_sent_orders"


def test_check_is_true_for_an_unverified_order_only_until_warned(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    placed()
    assert check_output(capsys) == "has_pending=true"
    orders_state.update_fields("B1", verify_warned=True)
    assert check_output(capsys) == "has_pending=false"


def test_check_with_only_an_unverified_order_is_stdlib_only(tmp_path):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "orders.json").write_text(json.dumps(
        {"last_update_id": None, "bookings": {"B1": {
            "status": "ordered", "plan": {"confirmation": "UNVERIFIED-20261021T123728Z"},
            "message_id": 77, "handled": True, "updated_at": T0.isoformat()}}}),
        encoding="utf-8")
    code = (
        "import sys, approve\n"
        "rc = approve.main(['--check'])\n"
        "heavy = [m for m in ('playwright', 'anthropic', 'yaml', 'notifier', 'rules',\n"
        "         'parts_order', 'config_loader', 'parts_portal', 'portal_scraper',\n"
        "         'order_match') if m in sys.modules]\n"
        "print('HEAVY', heavy)\n"
        "sys.exit(rc)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    env.pop("GITHUB_OUTPUT", None)
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "has_pending=true" in out.stdout
    assert "HEAVY []" in out.stdout


def test_main_polls_when_only_an_unverified_order_is_open(tg, run_main):
    placed()
    factory = PortalFactory(SentPortal([]))
    assert run_main(factory) == 0
    assert factory.portal.calls == ["list_sent_orders"]


# ── Review round 1: several records per run, plates, stored numbers ──────────

def _set_reg(booking_id, reg):
    data = json.loads(orders_state.ORDERS_FILE.read_text(encoding="utf-8"))
    data["bookings"][booking_id]["plan"]["reg_nr"] = reg
    orders_state.ORDERS_FILE.write_text(json.dumps(data), encoding="utf-8")


def test_two_orders_for_one_car_and_one_row_verify_nothing_yet(tg, run_main):
    placed("B1")
    placed("B2", booking_nr="OLRD-0002")
    rows = [sent_row("90000002", RIO, "01000011/01000012", reg="TST001")]
    assert run_main(PortalFactory(SentPortal(rows))) == 0
    assert tg["sent"] == []
    for bid in ("B1", "B2"):
        assert "verified_at" not in orders_state.get(bid)
        assert "verify_warned" not in orders_state.get(bid)


def test_two_orders_for_one_car_and_two_rows_are_ambiguous_for_both(tg, run_main):
    placed("B1")
    placed("B2", booking_nr="OLRD-0002")
    rows = [sent_row("90000002", RIO, "01000011/01000012", reg="TST001"),
            sent_row("90000003", RIO, "01000021/01000022", reg="TST001")]
    assert run_main(PortalFactory(SentPortal(rows))) == 0
    assert len(tg["sent"]) == 2
    for text, nr in zip(tg["sent"], ("OLRD-0001", "OLRD-0002")):
        assert text.startswith("Flera ordrar för") and f"som är {nr}:" in text
        assert "90000002 (01000011/01000012), 90000003 (01000021/01000022)" in text
    assert all(orders_state.get(b)["verify_warned"] is True for b in ("B1", "B2"))
    assert all("verified_at" not in orders_state.get(b) for b in ("B1", "B2"))


def test_two_cars_in_one_run_are_each_verified(tg, run_main):
    placed("B1")
    placed("B2", booking_nr="OLRD-0002", model="OPELASTRA (J)")
    _set_reg("B2", "TST002")
    rows = [sent_row("90000002", RIO, "01000011/01000012", reg="TST001"),
            sent_row("90000004", ASTRA, "01000031/01000032", reg="TST002")]
    assert run_main(PortalFactory(SentPortal(rows))) == 0
    assert orders_state.get("B1")["verified_numbers"].startswith("90000002")
    assert orders_state.get("B2")["verified_numbers"].startswith("90000004")


def test_with_a_plate_an_empty_registration_row_is_not_our_order(tg, run_main):
    placed()
    rows = [sent_row("90000002", RIO, "01000011/01000012", reg=""),
            sent_row("90000003", ASTRA, "01000021/01000022", reg="TST009")]
    assert run_main(PortalFactory(SentPortal(rows))) == 0
    assert tg["sent"] == [] and "verified_at" not in orders_state.get("B1")


def test_a_plate_match_verifies_whatever_the_model_naming(tg, run_main):
    placed(model="MERCEDESC220 (W205)")
    rows = [sent_row("90000002", "MERCEDES-BENZ, C-Klass (W205), 14-21", "01000011",
                     reg="TST001")]
    assert run_main(PortalFactory(SentPortal(rows))) == 0
    assert tg["sent"] == ["Beställning verifierad: OLRD-0001 – MERCEDES-BENZ, C-Klass (W205), "
                          "14-21, ordernummer 90000002 (01000011)."]


def test_a_plate_match_without_a_model_in_the_row_names_the_booking_model(tg, run_main):
    placed()
    rows = [sent_row("90000002", "", "01000011", reg="TST001")]
    assert run_main(PortalFactory(SentPortal(rows))) == 0
    assert tg["sent"] == ["Beställning verifierad: OLRD-0001 – KIARIO (III)(2014), "
                          "ordernummer 90000002 (01000011)."]


def test_long_numbers_are_stored_cut_and_claimed_the_same_way_next_run(tg, run_main):
    long = "/".join(f"0100{i:04d}" for i in range(17))             # 152 characters
    rows = [sent_row("90000002", RIO, long, reg="TST001")]
    placed("B1")
    assert run_main(PortalFactory(SentPortal(rows))) == 0
    stored = orders_state.get("B1")["verified_numbers"]
    assert len(stored) <= orders_state.NUMBERS_MAX and stored.startswith("90000002 · ")
    assert not stored.endswith(("/", "·", " "))
    assert approve._claimed() == set(stored.replace("·", " ").replace("/", " ").split())
    # Next run: a second order for the same car must not get the already used row.
    placed("B2", booking_nr="OLRD-0002")
    assert run_main(PortalFactory(SentPortal(rows))) == 0
    assert "verified_at" not in orders_state.get("B2") and len(tg["sent"]) == 1



def test_main_stops_before_telegram_when_orders_json_missing(monkeypatch, run_main, orders_file):
    import alerts
    orders_file.unlink()

    def forbidden(*a, **k):
        raise AssertionError("no Telegram polling when the order state is unreadable")

    monkeypatch.setattr(notifier, "get_updates", forbidden)
    monkeypatch.setattr(notifier, "send_message", lambda config, text: True)
    monkeypatch.setattr(approve, "make_portal", forbidden)
    monkeypatch.setattr(config_loader, "load_config", lambda: CONFIG)

    assert approve.main([]) == 1
    assert [a["text"] for a in alerts.active()] == ["config/orders.json saknas"]


def test_main_resolves_the_state_alert_when_the_state_is_readable(monkeypatch, run_main, orders_file, tmp_path):
    import alerts
    import preflight
    monkeypatch.setattr(alerts, "ALERTS_FILE", tmp_path / "a.json")
    monkeypatch.setattr(notifier, "send_message", lambda config, text: True)
    alerts.raise_alert({"telegram": {}}, preflight.STATE_ALERT_KEY, "trasig")
    assert [a["key"] for a in alerts.active()] == [preflight.STATE_ALERT_KEY]
    monkeypatch.setattr(config_loader, "load_config", lambda: CONFIG)

    assert approve.main([]) == 0   # nothing pending: stops early, after the guard
    assert alerts.active() == []


# ── Package 2 task 4: one-tap swap ───────────────────────────────────────────

P1 = {"title": "Bosch Oljefilter P7001", "brand": "Bosch", "price_sek": 120.0,
      "url_or_id": "p1", "in_stock": False, "search_term": "Oljefilter"}
P2 = {"title": "Bosch Luftfilter S0123", "brand": "Bosch", "price_sek": 250.0,
      "url_or_id": "p2", "in_stock": True, "search_term": "Luftfilter"}
P3 = {"title": "Mann Oljefilter W712", "brand": "Mann", "price_sek": 200.0,
      "url_or_id": "p3", "in_stock": True, "search_term": "Oljefilter"}
SWAP_CATALOGUE = {"Oljefilter": [P1, P3], "Luftfilter": [P2]}
P3_ALT = {**P3, "alt_reason": "slut", "saving_sek": 0}


def swap_plan(booking_id="B1", alternative=P3_ALT, total=490.0):
    oil = {"name": "Oljefilter", "title": P1["title"], "brand": "Bosch", "price_sek": 120.0,
           "qty": 2, "url_or_id": "p1", "in_stock": False}
    if alternative is not None:
        oil["alternative"] = dict(alternative)
    return {"booking_id": booking_id, "booking_nr": "OLRD-0001", "reg_nr": "TST001",
            "parts": [oil, {"name": "Luftfilter", "title": P2["title"], "brand": "Bosch",
                            "price_sek": 250.0, "qty": 1, "url_or_id": "p2", "in_stock": True}],
            "expected_lines": [{"title": P1["title"], "qty": 2, "url_or_id": "p1"},
                               {"title": P2["title"], "qty": 1, "url_or_id": "p2"}],
            "total_sek": total, "fluids": [], "created_at": T0.isoformat()}


def stage_swap(booking_id="B1", message_id=77, swaps=None, updated_at=None, **plan_kw):
    orders_state.put(booking_id, "pending", plan=swap_plan(booking_id, **plan_kw),
                     message_id=message_id)
    fields = {"cart_touched": True, "detail_url": "https://meca.example/b/1",
              "vehicle_model": "VOLKSWAGEN PASSAT"}
    if swaps is not None:
        fields["swaps"] = swaps
    orders_state.update_fields(booking_id, **fields)
    if updated_at is not None:
        data = json.loads(orders_state.ORDERS_FILE.read_text(encoding="utf-8"))
        data["bookings"][booking_id]["updated_at"] = updated_at.isoformat()
        orders_state.ORDERS_FILE.write_text(json.dumps(data), encoding="utf-8")


class SwapPortal:
    """pro.meca.se for a swap: a cart (filled as staged), search, add, clear. Records calls."""

    def __init__(self, catalogue=SWAP_CATALOGUE, clear_fails=False, add_fails=()):
        self.catalogue = catalogue
        self.clear_fails = clear_fails
        self.add_fails = set(add_fails)
        self.cart = [(P1, 2), (P2, 1)]       # what the original staging left in the cart
        self.calls, self.added = [], []

    def login(self):
        self.calls.append("login")

    def list_sent_orders(self):
        self.calls.append("list_sent_orders")
        return []

    def load_vehicle(self, reg_nr):
        self.calls.append("load_vehicle")

    def clear_cart(self, reg_nr=None):
        self.calls.append("clear_cart")
        if self.clear_fails:
            raise RuntimeError("clear broke")
        self.cart = []

    def search(self, term):
        self.calls.append(("search", term))
        return [dict(c) for c in self.catalogue.get(term, [])]

    def add_to_cart(self, candidate, qty):
        self.calls.append(("add_to_cart", candidate["url_or_id"], qty))
        if candidate["url_or_id"] in self.add_fails:
            raise RuntimeError("add broke")
        self.cart.append((candidate, qty))
        self.added.append((candidate["url_or_id"], qty))

    def read_cart(self):
        self.calls.append("read_cart")
        lines = [{"title": c["title"], "qty": q, "url_or_id": c["url_or_id"],
                  "price_sek": c["price_sek"] * q} for c, q in self.cart]
        return {"lines": lines, "total_sek": sum(x["price_sek"] for x in lines)}

    def checkout(self, *, max_total_sek):
        raise AssertionError("a swap must never check out")


@pytest.fixture
def tgs(tg, monkeypatch):
    """tg, plus approval messages (any non-Klar buttons) recorded in `approvals` with ids
    from 500 up; `approval_ok` False makes them fail."""
    klar = notifier.send_with_buttons
    tg.update(approvals=[], next_approval=500, approval_ok=True)

    def buttons(config, text, rows):
        flat = [b for row in rows for b in row] if isinstance(rows[0], list) else rows
        if all(data.startswith("klar:") for _, data in flat):
            return klar(config, text, rows)
        if not tg["approval_ok"]:
            return None
        mid = tg["next_approval"]
        tg["next_approval"] += 1
        tg["approvals"].append((text, rows, mid))
        return mid

    monkeypatch.setattr(notifier, "send_with_buttons", buttons)
    monkeypatch.setattr(parts_order, "execute_order",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no approve path")))
    return tg


def run_swap(tgs, updates, portal=None, rules_obj=APPROVE, allowed=True):
    tgs["updates"] = updates
    factory = PortalFactory(portal or SwapPortal(), allowed=allowed)
    actions, problem = approve._handle(CONFIG, factory, rules_obj)
    return actions, problem, factory


def test_swap_restages_the_alternative_with_a_new_approval_message(tgs, orders_file):
    stage_swap()
    portal = SwapPortal()

    actions, problem, factory = run_swap(tgs, [tap(1, "swap:B1:0")], portal)

    assert (actions, problem) == (1, False)
    rec = orders_state.get("B1")
    assert rec["status"] == "pending"
    assert rec["message_id"] == 500                     # the new approval message
    assert rec["swaps"] == 1 and rec["cart_touched"] is True
    parts = rec["plan"]["parts"]
    assert [p["url_or_id"] for p in parts] == ["p3", "p2"]
    assert "alternative" not in parts[0]
    assert parts[1] == swap_plan()["parts"][1]          # the other line is as staged
    assert rec["plan"]["expected_lines"] == [
        {"title": P3["title"], "qty": 2, "url_or_id": "p3"},
        {"title": P2["title"], "qty": 1, "url_or_id": "p2"}]
    assert rec["plan"]["total_sek"] == 650.0            # read again from the cart
    assert portal.calls.index("clear_cart") < portal.calls.index(("add_to_cart", "p3", 2))
    assert portal.added == [("p3", 2), ("p2", 1)]
    assert not any(c[0] == "search" and "olje" in c[1].casefold()
                   for c in portal.calls if isinstance(c, tuple))
    assert tgs["cleared"] == [77]                       # old buttons gone
    assert tgs["answers"] == [("cb1", "Byter…")]
    (text, rows, mid), = tgs["approvals"]
    assert mid == 500
    assert "- Oljefilter -> Mann Oljefilter W712 (Mann) x2 à 200.00 SEK" in text
    assert "Totalt: 650.00 SEK" in text
    assert "Alternativ" not in text
    assert rows == [("Godkänn", "approve:B1"), ("Avbryt", "cancel:B1")]
    assert tgs["sent"] == []
    assert factory.opened == 1
    assert "SECRET" not in orders_file.read_text(encoding="utf-8")


def test_swap_rechecks_the_ceiling(tgs):
    stage_swap()
    rules_low = Rules(("Bosch",), ("ProMeister",), 600, "approve")
    portal = SwapPortal()
    # The estimate (490 - 240 + 400 = 650) is over 600: refused before the cart is touched.
    actions, problem, _ = run_swap(tgs, [tap(1, "swap:B1:0")], portal, rules_low)
    assert (actions, problem) == (0, False)
    assert portal.calls == [] and orders_state.get("B1")["status"] == "pending"
    assert tgs["answers"] == [("cb1", approve.SWAP_REFUSED)]


def test_swap_rechecks_the_ceiling_on_the_real_cart(tgs):
    stage_swap(total=100.0)                            # a wrong estimate: 100 - 240 + 400
    rules_low = Rules(("Bosch",), ("ProMeister",), 600, "approve")
    portal = SwapPortal()

    actions, problem, _ = run_swap(tgs, [tap(1, "swap:B1:0")], portal, rules_low)

    assert (actions, problem) == (1, True)
    rec = orders_state.get("B1")
    assert rec["status"] in ("blocked", "failed") and rec["cart_touched"] is True
    assert "taket" in rec["plan"]["reason"]
    assert tgs["approvals"] == []
    assert any(parts_order.SWAP_FAILED in t for t in tgs["sent"])


def test_swap_with_a_booking_id_containing_colons(tgs):
    stage_swap("A:B:1")

    actions, problem, _ = run_swap(tgs, [tap(1, "swap:A:B:1:0")])

    assert (actions, problem) == (1, False)
    rec = orders_state.get("A:B:1")
    assert rec["swaps"] == 1 and rec["plan"]["parts"][0]["url_or_id"] == "p3"
    (_, rows, _), = tgs["approvals"]
    assert rows == [("Godkänn", "approve:A:B:1"), ("Avbryt", "cancel:A:B:1")]


def _setup_status(status):
    def setup():
        stage_swap()
        orders_state.put("B1", status)
    return setup


REFUSALS = [
    ("wrong chat", stage_swap, {"chat_id": "99"}, None),
    ("wrong user", stage_swap, {"user_id": "7"}, None),
    ("stale message", stage_swap, {"message_id": 76}, "Knappen är inte längre giltig."),
    ("blocked", _setup_status("blocked"), {}, "Redan hanterad."),
    ("ordered", _setup_status("ordered"), {}, "Redan hanterad."),
    ("cancelled", _setup_status("cancelled"), {}, "Redan hanterad."),
    ("two swaps", lambda: stage_swap(swaps=2), {}, "REFUSED"),
    ("no alternative", stage_swap, {"data": "swap:B1:1"}, "REFUSED"),
    ("index out of range", stage_swap, {"data": "swap:B1:2"}, "REFUSED"),
    ("expired", lambda: stage_swap(updated_at=datetime.now(timezone.utc) - timedelta(hours=49)),
     {}, "Godkännandet har gått ut."),
    ("unknown booking", stage_swap, {"data": "swap:B2:0"}, "Okänd bokning."),
] + [(f"malformed {d!r}", stage_swap, {"data": d}, "Okänd åtgärd.") for d in (
    "swap:B1", "swap:B1:", "swap:B1:x", "swap:B1:-1", "swap:B1:+1", "swap:B1: 0", "swap:B1:0 ",
    "swap:B1:01", "swap:B1:1.0", "swap:B1:0:", "swap::0", "swap:B1:9999", "swap:B 1:0",
    "swap:B1:٠", "swap", "swapx:B1:0")]


@pytest.mark.parametrize("why,setup,kw,answer", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_swap_refusals_change_nothing(tgs, why, setup, kw, answer):
    setup()
    before = orders_state.get("B1")
    kw = {"data": "swap:B1:0", **kw}
    answer = approve.SWAP_REFUSED if answer == "REFUSED" else answer

    actions, problem, _ = run_swap(tgs, [tap(1, **kw)], allowed=False)

    assert (actions, problem) == (0, False)
    assert orders_state.get("B1") == before
    assert tgs["answers"] == ([] if answer is None else [("cb1", answer)])
    assert tgs["cleared"] == [] and tgs["sent"] == [] and tgs["approvals"] == []


@pytest.mark.parametrize("alt", [
    {"title": "ProMeister Oljefilter", "brand": "ProMeister"},
    {"title": "Castrol Motorolja 5W30", "brand": "Castrol"},
    {"title": "Oljefilter Ж", "brand": "Mann"},
])
def test_swap_to_a_forbidden_or_fluid_alternative_is_refused(tgs, alt):
    stage_swap(alternative={**P3_ALT, **alt})
    before = orders_state.get("B1")

    actions, problem, _ = run_swap(tgs, [tap(1, "swap:B1:0")], allowed=False)

    assert (actions, problem) == (0, False)
    assert orders_state.get("B1") == before
    assert tgs["answers"] == [("cb1", approve.SWAP_REFUSED)]
    assert tgs["cleared"] == [] and tgs["approvals"] == []


def test_swap_to_an_alternative_for_the_wrong_axle_is_refused(tgs):
    plan = swap_plan()
    plan["parts"][0]["name"] = "Bromsskiva fram"
    plan["parts"][0]["alternative"] = {**P3_ALT, "title": "Mann Bromsskiva bak",
                                       "placement": "Bakaxel"}
    orders_state.put("B1", "pending", plan=plan, message_id=77)
    before = orders_state.get("B1")

    actions, problem, _ = run_swap(tgs, [tap(1, "swap:B1:0")], allowed=False)

    assert (actions, problem) == (0, False)
    assert orders_state.get("B1") == before
    assert tgs["answers"] == [("cb1", approve.SWAP_REFUSED)]


def test_double_tap_on_the_old_message_does_nothing(tgs):
    stage_swap()
    portal = SwapPortal()

    actions, problem, _ = run_swap(tgs, [tap(1, "swap:B1:0"), tap(2, "swap:B1:0"),
                                         tap(3, "approve:B1")], portal)

    assert (actions, problem) == (1, False)
    rec = orders_state.get("B1")
    assert rec["swaps"] == 1 and rec["message_id"] == 500 and rec["status"] == "pending"
    assert portal.calls.count("clear_cart") == 1
    assert len(tgs["approvals"]) == 1
    assert tgs["answers"] == [("cb1", "Byter…"), ("cb2", "Knappen är inte längre giltig."),
                              ("cb3", "Knappen är inte längre giltig.")]


def test_second_swap_on_the_new_message_is_the_last(tgs):
    plan = swap_plan()
    plan["parts"][1]["alternative"] = {"title": "Mann Luftfilter C1", "brand": "Mann",
                                       "price_sek": 150.0, "url_or_id": "p7", "in_stock": True,
                                       "search_term": "Luftfilter", "alt_reason": "dyr",
                                       "saving_sek": 100.0}
    orders_state.put("B1", "pending", plan=plan, message_id=77)
    orders_state.update_fields("B1", cart_touched=True)
    catalogue = {**SWAP_CATALOGUE, "Luftfilter": [P2]}
    portal = SwapPortal(catalogue=catalogue)

    run_swap(tgs, [tap(1, "swap:B1:0")], portal)
    (_, rows, mid), = tgs["approvals"]
    assert rows == [[("Godkänn", "approve:B1"), ("Avbryt", "cancel:B1")],
                    [("Byt Luftfilter", "swap:B1:1")]]
    assert orders_state.get("B1")["plan"]["parts"][1]["alternative"]["url_or_id"] == "p7"

    run_swap(tgs, [tap(2, "swap:B1:1", message_id=mid)], portal)

    rec = orders_state.get("B1")
    assert rec["swaps"] == 2 and rec["status"] == "pending"
    assert [p["url_or_id"] for p in rec["plan"]["parts"]] == ["p3", "p7"]
    assert tgs["approvals"][1][1] == [("Godkänn", "approve:B1"), ("Avbryt", "cancel:B1")]
    assert tgs["cleared"] == [77, 500]


def test_cart_clear_failure_keeps_the_old_record(tgs):
    stage_swap()
    before = orders_state.get("B1")
    portal = SwapPortal(clear_fails=True)

    actions, problem, _ = run_swap(tgs, [tap(1, "swap:B1:0")], portal)

    assert problem is True
    assert orders_state.get("B1") == before              # still pending, old message valid
    assert portal.added == []
    assert tgs["cleared"] == [] and tgs["approvals"] == []
    (text,) = tgs["sent"]
    assert approve.CART_MANUAL in text and "OLRD-0001" in text


def test_portal_unavailable_is_a_cart_clear_failure(tgs):
    stage_swap()
    before = orders_state.get("B1")
    tgs["updates"] = [tap(1, "swap:B1:0")]

    actions, problem = approve._handle(CONFIG, PortalFactory(open_fails=True), APPROVE)

    assert problem is True and orders_state.get("B1") == before
    assert approve.CART_MANUAL in tgs["sent"][0]


def test_staging_failure_after_the_clear_is_truthful_and_not_pending(tgs):
    stage_swap()
    portal = SwapPortal(add_fails={"p3"})

    actions, problem, _ = run_swap(tgs, [tap(1, "swap:B1:0"), tap(2, "approve:B1")], portal)

    assert problem is True
    rec = orders_state.get("B1")
    assert rec["status"] in ("blocked", "failed")
    assert rec["cart_touched"] is True and rec["swaps"] == 1
    assert orders_state.restage("B1") is False
    assert tgs["cleared"][0] == 77                       # old buttons removed
    assert tgs["approvals"] == []
    assert any(parts_order.SWAP_FAILED in t and "OLRD-0001" in t for t in tgs["sent"])
    assert tgs["answers"][1] == ("cb2", "Redan hanterad.")


def test_new_approval_message_not_sent_is_not_pending(tgs):
    stage_swap()
    tgs["approval_ok"] = False

    actions, problem, _ = run_swap(tgs, [tap(1, "swap:B1:0")])

    assert problem is True
    rec = orders_state.get("B1")
    assert rec["status"] in ("blocked", "failed") and rec["cart_touched"] is True
    assert tgs["cleared"] == [77]


def test_swap_never_orders_even_in_auto_mode(tgs):
    stage_swap()
    portal = SwapPortal()

    actions, problem, _ = run_swap(tgs, [tap(1, "swap:B1:0")], portal,
                                   Rules(("Bosch",), ("ProMeister",), 10000, "auto"))

    assert (actions, problem) == (1, False)
    assert orders_state.get("B1")["status"] == "pending"
    assert len(tgs["approvals"]) == 1


def test_parse_swap_data():
    assert approve._parse("swap:B1:0") == ("swap", "B1", 0)
    assert approve._parse("swap:a:b:12") == ("swap", "a:b", 12)
    assert approve._parse("approve:a:b") == ("approve", "a:b")
    for bad in ("swap:B1:x", "swap:B1", "swap::1", "swap:B1:-1", "swap:B1:1e3"):
        assert approve._parse(bad) is None
