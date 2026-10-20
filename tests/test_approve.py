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


def plan_for(booking_id="B1", booking_nr="OLRD-3630"):
    return {"booking_id": booking_id, "booking_nr": booking_nr, "reg_nr": "NXP168",
            "parts": [{"name": "Oljefilter", "title": "Bosch Oljefilter P7001", "brand": "Bosch",
                       "price_sek": 120.0, "qty": 2, "url_or_id": "p1"},
                      {"name": "Luftfilter", "title": "Bosch Luftfilter S0123", "brand": "Bosch",
                       "price_sek": 250.0, "qty": 1, "url_or_id": "p2"}],
            "expected_lines": EXPECTED, "total_sek": 490.0, "fluids": [],
            "created_at": T0.isoformat()}


def stage(booking_id="B1", message_id=77, updated_at=None, booking_nr="OLRD-3630"):
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

    def login(self):
        self.calls.append("login")

    def load_vehicle(self, reg_nr):
        self.calls.append("load_vehicle")

    def read_cart(self):
        self.calls.append("read_cart")
        return self.carts.pop(0) if self.carts else CART

    def checkout(self):
        self.calls.append("checkout")
        return self.ref

    def clear_cart(self):
        self.calls.append("clear_cart")
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
    return path


@pytest.fixture
def tg(monkeypatch):
    """Fake Telegram: records every call; `updates` is what get_updates returns."""
    rec = {"sent": [], "answers": [], "cleared": [], "offsets": [], "updates": [],
           "send_ok": True}

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

    def no_buttons(config, text, buttons):
        raise AssertionError("the poller never sends new buttons")

    monkeypatch.setattr(notifier, "send_message", fake_send)
    monkeypatch.setattr(notifier, "get_updates", fake_get_updates)
    monkeypatch.setattr(notifier, "answer_callback", fake_answer)
    monkeypatch.setattr(notifier, "clear_buttons", fake_clear)
    monkeypatch.setattr(notifier, "send_with_buttons", no_buttons)
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
    assert len(tg["sent"]) == 1 and tg["sent"][0].startswith("Beställd: OLRD-3630")
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
    assert len(tg["sent"]) == 1 and tg["sent"][0].startswith("Beställning stoppad för OLRD-3630")
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
    assert "OLRD-3630" in tg["sent"][0] and "Inget beställdes" in tg["sent"][0]
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
    assert tg["sent"][0].startswith("Avbruten: OLRD-3630.")
    assert tg["sent"][0].endswith(tail)
    assert "checkout" not in factory.portal.calls
    if not open_fails:
        assert factory.portal.calls == ["login", "clear_cart"]
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
        "Godkännandet för OLRD-3630 har gått ut (48 h). Beställ för hand om delarna behövs.")
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
    assert on["schedule"] == [{"cron": "*/15 * * * *"}]
    assert "workflow_dispatch" in on
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
