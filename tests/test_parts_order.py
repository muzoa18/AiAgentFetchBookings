import pytest

import notifier
import orders_state
import parts_order
from parts_order_helpers import build_summary, parse_qty
from rules import Rules

CONFIG = {"telegram": {"bot_token": "123:ABC", "chat_id": "42"}}
APPROVE = Rules(("Bosch",), ("ProMeister",), 10000, "approve")
AUTO = Rules(("Bosch",), ("ProMeister",), 10000, "auto")

PII = ("Sentinel Kund AB", "+46700000999", "sentinel@example.com", "Sentinel meddelande 42")

BOOKING = {
    "id": "B1",
    "booking_type": "Bokning",
    "booking_nr": "OLRD-3630",
    "driver_name": "Sentinel Kund AB",
    "driver_phone": "+46700000999",
    "driver_email": "sentinel@example.com",
    "booking_msg": "Sentinel meddelande 42",
    "reg_nr": "NXP168",
    "vehicle": "VOLKSWAGEN PASSAT",
    "parts": [{"name": "Oljefilter", "qty": "2"}, {"name": "Luftfilter", "qty": "1"}],
    "fluids": [{"name": "Olja 5W30", "qty": "5"}],
}

CATALOGUE = {
    "Oljefilter": [{"title": "Bosch Oljefilter P7001", "brand": "Bosch", "price_sek": 120.0,
                    "url_or_id": "p1"}],
    "Luftfilter": [{"title": "Bosch Luftfilter S0123", "brand": "Bosch", "price_sek": 250.0,
                    "url_or_id": "p2"}],
}

PARTS = [{"title": "Bosch Oljefilter P7001", "brand": "Bosch", "qty": 2, "price_sek": 120.0},
         {"title": "Bosch Luftfilter S0123", "brand": "Bosch", "qty": 1, "price_sek": 250.0}]


def confident(line, ranked):
    return 0, 0.95


class FakePortal:
    """Records every call.

    read_cart returns, in priority order: the next entry of `carts`; `initial` while
    nothing has been added; `cart` once something was added; else what was added.
    """

    def __init__(self, catalogue=CATALOGUE, cart=None, ref="ORD-123", initial=None,
                 carts=None, add_works=True):
        self.catalogue = catalogue
        self.cart = cart
        self.initial = initial
        self.carts = list(carts or [])
        self.add_works = add_works
        self.ref = ref
        self.calls = []
        self.added = []
        self.fail_on = None
        self.ceilings = []

    def _record(self, name, *args):
        self.calls.append((name, *args))
        if self.fail_on == name:
            raise RuntimeError(f"{name} broke")

    def login(self):
        self._record("login")

    def load_vehicle(self, reg_nr):
        self._record("load_vehicle", reg_nr)

    def search(self, term):
        self._record("search", term)
        return list(self.catalogue.get(term, []))

    def add_to_cart(self, candidate, qty):
        self._record("add_to_cart", candidate["url_or_id"], qty)
        if self.add_works:
            self.added.append((candidate, qty))

    def read_cart(self):
        self._record("read_cart")
        if self.carts:
            return self.carts.pop(0)
        if not self.added and self.initial is not None:
            return self.initial
        if self.added and self.cart is not None:
            return self.cart
        lines = [{"title": c["title"], "qty": q, "price_sek": c["price_sek"] * q}
                 for c, q in self.added]
        return {"lines": lines, "total_sek": sum(line["price_sek"] for line in lines)}

    def checkout(self, *, max_total_sek):
        self.ceilings.append(max_total_sek)
        self._record("checkout")
        return self.ref

    def names(self):
        return [c[0] for c in self.calls]


class UntouchablePortal:
    """Any attribute access is recorded and fails: the portal must not be used."""

    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"portal.{name} must not be used")


@pytest.fixture(autouse=True)
def orders_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "orders.json"
    monkeypatch.setattr(orders_state, "ORDERS_FILE", path)
    return path


@pytest.fixture
def tg(monkeypatch):
    """Fake Telegram: records plain messages and button messages."""
    rec = {"sent": [], "buttons": [], "message_id": 77, "send_ok": True}

    def fake_send(config, text):
        rec["sent"].append(text)
        return rec["send_ok"]

    def fake_buttons(config, text, buttons):
        rec["buttons"].append((text, buttons))
        return rec["message_id"]

    monkeypatch.setattr(notifier, "send_message", fake_send)
    monkeypatch.setattr(notifier, "send_with_buttons", fake_buttons)
    return rec


def _cart(total, lines=None):
    if lines is None:
        lines = [{"title": "Bosch Oljefilter P7001", "qty": 2, "price_sek": 240.0},
                 {"title": "Bosch Luftfilter S0123", "qty": 1, "price_sek": 250.0}]
    return {"lines": lines, "total_sek": total}


def _assert_no_pii(orders_file):
    text = orders_file.read_text(encoding="utf-8")
    for sentinel in PII:
        assert sentinel not in text


# ── plan_and_stage: blocking ─────────────────────────────────────────────────

def test_no_reg_number_is_blocked_and_messaged(tg, orders_file):
    portal = UntouchablePortal()
    booking = {**BOOKING, "reg_nr": "  "}

    assert parts_order.plan_and_stage(CONFIG, booking, portal, APPROVE, confident) == "blocked"

    assert orders_state.get("B1")["status"] == "blocked"
    assert portal.touched == []
    assert tg["buttons"] == []
    assert len(tg["sent"]) == 1
    assert "OLRD-3630" in tg["sent"][0]
    assert "registreringsnummer" in tg["sent"][0]
    _assert_no_pii(orders_file)


def test_fluids_only_booking_never_touches_cart(tg, orders_file):
    portal = UntouchablePortal()
    booking = {**BOOKING, "parts": [{"name": "Motorolja 5W-30", "qty": "5"}],
               "fluids": [{"name": "Spolarvätska", "qty": "1"}]}

    assert parts_order.plan_and_stage(CONFIG, booking, portal, APPROVE, confident) == "blocked"

    rec = orders_state.get("B1")
    assert rec["status"] == "blocked"
    assert rec["plan"]["reason"] == "inga delar att beställa"
    assert portal.touched == []
    assert tg["buttons"] == []
    assert len(tg["sent"]) == 1
    assert "Motorolja 5W-30 x5" in tg["sent"][0]
    assert "Spolarvätska x1" in tg["sent"][0]
    _assert_no_pii(orders_file)


def test_parts_line_that_is_a_fluid_is_not_searched(tg):
    portal = FakePortal()
    booking = {**BOOKING, "parts": [{"name": "Oljefilter", "qty": "2"},
                                    {"name": "Luftfilter", "qty": "1"},
                                    {"name": "Motorolja 5W-30", "qty": "4"}]}

    assert parts_order.plan_and_stage(CONFIG, booking, portal, APPROVE, confident) == "pending"

    searched = [c[1] for c in portal.calls if c[0] == "search"]
    assert searched == ["Oljefilter", "Luftfilter"]
    text, _ = tg["buttons"][0]
    assert "Motorolja 5W-30 x4" in text


def test_unmatched_part_blocks_whole_booking(tg, orders_file):
    portal = FakePortal(catalogue={**CATALOGUE, "Luftfilter": []})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"

    assert "add_to_cart" not in portal.names()  # nothing added, not even the earlier match
    assert "checkout" not in portal.names()
    assert orders_state.get("B1")["status"] == "blocked"
    assert tg["buttons"] == []
    assert len(tg["sent"]) == 1
    assert "Luftfilter" in tg["sent"][0]
    _assert_no_pii(orders_file)


def test_unconfident_pick_blocks(tg):
    portal = FakePortal()

    status = parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, lambda l, r: (0, 0.5))

    assert status == "blocked"
    assert not portal.added


def test_non_empty_cart_before_staging_is_blocked(tg):
    stale = _cart(900.0, [{"title": "Bosch Bromsskiva", "qty": 2, "price_sek": 900.0}])
    portal = FakePortal(initial=stale)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, AUTO, confident) == "blocked"

    assert "add_to_cart" not in portal.names()
    assert "checkout" not in portal.names()
    assert orders_state.get("B1")["status"] == "blocked"
    assert tg["sent"] == ["Varukorgen på pro.meca.se är inte tom (annan väntande beställning?). "
                          "Hantera OLRD-3630 för hand."]


@pytest.mark.parametrize("rules", [APPROVE, AUTO])
def test_stale_promeister_cart_with_silent_add_is_blocked(tg, rules):
    # The cart looks empty at first, add_to_cart silently does nothing, and the cart then
    # shows a ProMeister disc with the same quantity as our oil filter.
    booking = {**BOOKING, "parts": [{"name": "Oljefilter", "qty": "2"}]}
    promeister = _cart(900.0, [{"title": "ProMeister bromsskiva", "qty": 2, "price_sek": 900.0}])
    portal = FakePortal(carts=[_cart(0.0, []), promeister], add_works=False)

    assert parts_order.plan_and_stage(CONFIG, booking, portal, rules, confident) == "blocked"

    assert "checkout" not in portal.names()
    assert tg["buttons"] == []
    assert orders_state.get("B1")["status"] == "blocked"


def test_cart_with_right_count_but_wrong_titles_is_blocked(tg):
    wrong = _cart(490.0, [{"title": "Okänt filter A", "qty": 2, "price_sek": 240.0},
                          {"title": "Okänt filter B", "qty": 1, "price_sek": 250.0}])
    portal = FakePortal(cart=wrong)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"
    assert tg["buttons"] == []
    assert "stämmer inte" in tg["sent"][0]


def test_cart_titles_compared_case_and_space_insensitively(tg):
    loose = _cart(490.0, [{"title": "  bosch  OLJEFILTER p7001 ", "qty": "2", "price_sek": 240.0},
                          {"title": "Bosch Luftfilter S0123", "qty": 1, "price_sek": 250.0}])
    portal = FakePortal(cart=loose)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"


@pytest.mark.parametrize("total,expected", [(10000.00, "pending"), (10000.01, "blocked")])
def test_over_ceiling_blocked(tg, total, expected):
    portal = FakePortal(cart=_cart(total))

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == expected

    assert orders_state.get("B1")["status"] == expected
    assert "checkout" not in portal.names()
    if expected == "blocked":
        assert tg["buttons"] == []
        assert "10000" in tg["sent"][0]


@pytest.mark.parametrize("total", [None, float("nan"), "1 200 kr", True, -5.0])
def test_missing_total_blocked(tg, total):
    portal = FakePortal(cart=_cart(total))

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"

    assert tg["buttons"] == []
    assert len(tg["sent"]) == 1


def test_cart_with_extra_line_is_blocked(tg):
    extra = _cart(600.0)["lines"] + [{"title": "Something else", "qty": 1, "price_sek": 110.0}]
    portal = FakePortal(cart=_cart(600.0, extra))

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"
    assert tg["buttons"] == []


@pytest.mark.parametrize("raw,expected", [
    ("x4", 4), ("X 2", 2), (" 3 ", 3), ("", 1), (None, 1), (2, 2),
    ("abc", None), ("0", None), ("-1", None), ("1.5", None), (True, None),
])
def test_parse_qty(raw, expected):
    assert parse_qty(raw) == expected


def test_bad_quantity_blocks_and_names_part(tg, orders_file):
    portal = UntouchablePortal()
    booking = {**BOOKING, "parts": [{"name": "Oljefilter", "qty": "2"},
                                    {"name": "Luftfilter", "qty": "abc"}]}

    assert parts_order.plan_and_stage(CONFIG, booking, portal, APPROVE, confident) == "blocked"

    assert portal.touched == []
    assert "Luftfilter" in tg["sent"][0]
    _assert_no_pii(orders_file)


def test_second_call_for_same_booking_does_nothing(tg):
    portal = FakePortal()
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"
    calls, sent, buttons = len(portal.calls), len(tg["sent"]), len(tg["buttons"])

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"

    assert len(portal.calls) == calls
    assert (len(tg["sent"]), len(tg["buttons"])) == (sent, buttons)


def test_queued_booking_is_staged(tg):
    orders_state.queue("B1")

    status = parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(), APPROVE, confident)
    assert status == "pending"


# ── needs_portal ─────────────────────────────────────────────────────────────

def test_needs_portal():
    assert parts_order.needs_portal(BOOKING) is True
    orders_state.queue("B1")
    assert parts_order.needs_portal(BOOKING) is True  # queued counts as not yet handled
    assert parts_order.needs_portal({**BOOKING, "reg_nr": ""}) is False
    assert parts_order.needs_portal({**BOOKING, "parts": [{"name": "Olja 5W30"}]}) is False
    assert parts_order.needs_portal(
        {**BOOKING, "parts": [{"name": "Oljefilter", "qty": "abc"}]}) is False
    assert parts_order.needs_portal({**BOOKING, "parts": [{"name": " ", "qty": "1"}]}) is False
    orders_state.put("B1", "pending")
    assert parts_order.needs_portal(BOOKING) is False


# ── plan_and_stage: staging ──────────────────────────────────────────────────

def test_approve_mode_sends_buttons_and_sets_pending(tg, orders_file):
    portal = FakePortal()

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"

    assert portal.names() == [
        "login", "load_vehicle", "read_cart", "search", "search", "add_to_cart", "add_to_cart",
        "read_cart"]
    assert ("add_to_cart", "p1", 2) in portal.calls
    assert len(tg["buttons"]) == 1
    text, buttons = tg["buttons"][0]
    assert buttons == [("Godkänn", "approve:B1"), ("Avbryt", "cancel:B1")]
    assert "490.00 SEK" in text
    assert "Bosch Oljefilter P7001 (Bosch) x2" in text

    rec = orders_state.get("B1")
    assert rec["status"] == "pending"
    assert rec["message_id"] == 77
    plan = rec["plan"]
    assert plan["booking_id"] == "B1"
    assert plan["reg_nr"] == "NXP168"
    assert plan["total_sek"] == 490.0
    assert [(p["title"], p["qty"], p["url_or_id"]) for p in plan["parts"]] == [
        ("Bosch Oljefilter P7001", 2, "p1"), ("Bosch Luftfilter S0123", 1, "p2")]
    assert plan["expected_lines"] == [
        {"title": "Bosch Oljefilter P7001", "qty": 2, "url_or_id": "p1"},
        {"title": "Bosch Luftfilter S0123", "qty": 1, "url_or_id": "p2"}]
    assert plan["fluids"] == [{"name": "Olja 5W30", "qty": "5"}]
    assert plan["created_at"]
    assert "summary" not in plan
    _assert_no_pii(orders_file)


def test_approval_message_undeliverable_is_failed(tg):
    tg["message_id"] = None

    status = parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(), APPROVE, confident)

    assert status == "failed"
    assert orders_state.get("B1")["status"] == "failed"
    assert len(tg["sent"]) == 1
    assert "OLRD-3630" in tg["sent"][0]


def test_block_message_undeliverable_is_failed(tg):
    tg["send_ok"] = False

    status = parts_order.plan_and_stage(CONFIG, {**BOOKING, "reg_nr": ""}, UntouchablePortal(),
                                        APPROVE, confident)

    assert status == "failed"
    assert orders_state.get("B1")["status"] == "failed"


def test_fluids_message_undeliverable_is_failed(tg):
    tg["send_ok"] = False
    booking = {**BOOKING, "parts": []}

    assert parts_order.plan_and_stage(CONFIG, booking, UntouchablePortal(), APPROVE,
                                      confident) == "failed"
    assert orders_state.get("B1")["status"] == "failed"


def test_auto_mode_orders_without_buttons(tg, orders_file):
    portal = FakePortal()

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, AUTO, confident) == "ordered"

    assert tg["buttons"] == []
    assert portal.names().count("checkout") == 1
    rec = orders_state.get("B1")
    assert rec["status"] == "ordered"
    assert rec["plan"]["confirmation"] == "ORD-123"
    assert parts_order.order_notify_failed("B1") is False
    assert len(tg["sent"]) == 1
    assert "Beställd" in tg["sent"][0]
    assert "ORD-123" in tg["sent"][0]
    _assert_no_pii(orders_file)


def test_auto_mode_cart_changed_before_checkout_fails(tg):
    good = _cart(490.0)
    changed = _cart(490.0, [{"title": "Bosch Oljefilter P7001", "qty": 2, "price_sek": 240.0},
                            {"title": "ProMeister luftfilter", "qty": 1, "price_sek": 250.0}])
    portal = FakePortal(carts=[_cart(0.0, []), good, changed])

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, AUTO, confident) == "failed"

    assert "checkout" not in portal.names()
    assert orders_state.get("B1")["status"] == "failed"


def test_portal_exception_is_failed_and_messaged(tg, orders_file):
    portal = FakePortal()
    portal.fail_on = "search"

    status = parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident)

    assert status == "failed"
    assert orders_state.get("B1")["status"] == "failed"  # never left "attempted"
    assert len(tg["sent"]) == 1
    assert "misslyckades" in tg["sent"][0]
    assert "OLRD-3630" in tg["sent"][0]
    _assert_no_pii(orders_file)


def test_missing_booking_id_fails_without_crashing(tg):
    booking = {k: v for k, v in BOOKING.items() if k != "id"}

    assert parts_order.plan_and_stage(CONFIG, booking, FakePortal(), APPROVE, confident) == "failed"
    assert len(tg["sent"]) == 1


def test_record_failure_only_once(tg):
    orders_state.queue("B1")
    assert parts_order.record_failure(CONFIG, BOOKING, "portal saknas") == "failed"
    assert orders_state.get("B1")["status"] == "failed"
    assert parts_order.record_failure(CONFIG, BOOKING, "portal saknas") == "failed"
    assert len(tg["sent"]) == 1


# ── summary ──────────────────────────────────────────────────────────────────

def test_summary_lists_fluids_to_prepare():
    text = build_summary(BOOKING, _cart(490.0), BOOKING["fluids"], PARTS)

    assert "OLRD-3630" in text
    assert "Sentinel Kund AB" in text
    assert "NXP168 VOLKSWAGEN PASSAT" in text
    assert "Bosch Oljefilter P7001 (Bosch) x2" in text
    assert "120.00 SEK" in text
    assert "Totalt: 490.00 SEK" in text
    assert "Förbered (finns på lager):" in text
    assert "- Olja 5W30 x5" in text


def test_summary_without_fluids_has_no_prepare_section():
    assert "Förbered" not in build_summary(BOOKING, _cart(490.0), [], PARTS)


# ── execute_order ────────────────────────────────────────────────────────────

def _staged_plan(tg, portal):
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"
    tg["sent"].clear()
    portal.calls.clear()
    return orders_state.get("B1")["plan"]


def test_execute_order_success_sets_ordered_with_confirmation(tg, orders_file):
    portal = FakePortal(ref="ORD-9")
    plan = _staged_plan(tg, portal)

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "ordered"

    assert portal.names() == ["login", "load_vehicle", "read_cart", "checkout"]
    rec = orders_state.get("B1")
    assert rec["status"] == "ordered"
    assert rec["plan"]["confirmation"] == "ORD-9"
    assert rec["message_id"] == 77
    assert len(tg["sent"]) == 1
    assert "ORD-9" in tg["sent"][0]
    assert "Förbered (finns på lager):" in tg["sent"][0]
    _assert_no_pii(orders_file)


def test_execute_order_ordered_message_undeliverable_keeps_ordered(tg):
    portal = FakePortal(ref="ORD-9")
    plan = _staged_plan(tg, portal)
    tg["send_ok"] = False

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "ordered"

    rec = orders_state.get("B1")
    assert rec["status"] == "ordered"
    assert rec["plan"]["confirmation"] == "ORD-9"
    assert rec["plan"]["notify_failed"] is True
    assert parts_order.order_notify_failed("B1") is True


def test_execute_order_passes_the_ceiling_to_checkout(tg, orders_file):
    portal = FakePortal(ref="ORD-9")
    plan = _staged_plan(tg, portal)

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "ordered"

    assert portal.ceilings == [10000.0]


def test_execute_order_cart_mismatch_no_checkout(tg):
    portal = FakePortal()
    plan = _staged_plan(tg, portal)
    portal.cart = _cart(490.0, [{"title": "Bosch Oljefilter P7001", "qty": 1, "price_sek": 120.0},
                                {"title": "Bosch Luftfilter S0123", "qty": 1, "price_sek": 250.0}])

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "failed"

    assert "checkout" not in portal.names()
    assert orders_state.get("B1")["status"] == "failed"
    assert len(tg["sent"]) == 1


def test_execute_order_promeister_line_fails_closed(tg):
    # Even if the expected lines somehow contain a ProMeister item, never check it out.
    line = {"title": "ProMeister bromsskiva", "qty": 1, "url_or_id": "pm1"}
    plan = {"booking_id": "B1", "booking_nr": "OLRD-3630", "reg_nr": "NXP168",
            "parts": [], "expected_lines": [line], "total_sek": 500.0, "fluids": []}
    orders_state.put("B1", "pending", plan=plan)
    portal = FakePortal(carts=[_cart(500.0, [{"title": "ProMeister bromsskiva", "qty": 1,
                                              "price_sek": 500.0}])])

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "failed"

    assert "checkout" not in portal.names()
    assert orders_state.get("B1")["status"] == "failed"
    assert "ProMeister" in tg["sent"][0]


@pytest.mark.parametrize("total", [None, 10000.01, 500.0])
def test_execute_order_bad_or_changed_total_no_checkout(tg, total):
    portal = FakePortal()
    plan = _staged_plan(tg, portal)
    portal.cart = _cart(total)

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "failed"
    assert "checkout" not in portal.names()


def test_execute_order_checkout_exception_warns_order_may_exist(tg):
    portal = FakePortal()
    plan = _staged_plan(tg, portal)
    portal.fail_on = "checkout"

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "failed"

    assert orders_state.get("B1")["status"] == "failed"
    assert "kan ha lagts" in tg["sent"][0]


def test_execute_order_login_exception_says_nothing_ordered(tg):
    portal = FakePortal()
    plan = _staged_plan(tg, portal)
    portal.fail_on = "login"

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "failed"

    assert orders_state.get("B1")["status"] == "failed"
    assert "Inget beställdes" in tg["sent"][0]
