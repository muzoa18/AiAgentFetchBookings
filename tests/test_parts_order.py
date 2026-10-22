import pytest

import notifier
import orders_state
import parts_order
from parts_order_helpers import build_summary, cart_problem, order_body, parse_qty
from rules import Rules

CONFIG = {"telegram": {"bot_token": "123:ABC", "chat_id": "42"}}
APPROVE = Rules(("Bosch",), ("ProMeister",), 10000, "approve")
AUTO = Rules(("Bosch",), ("ProMeister",), 10000, "auto")

PII = ("Sentinel Kund AB", "+46700000999", "sentinel@example.com", "Sentinel meddelande 42")

BOOKING = {
    "id": "B1",
    "booking_type": "Bokning",
    "booking_nr": "OLRD-0001",
    "driver_name": "Sentinel Kund AB",
    "driver_phone": "+46700000999",
    "driver_email": "sentinel@example.com",
    "booking_msg": "Sentinel meddelande 42",
    "reg_nr": "TST001",
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
                 carts=None, add_works=True, sent_rows=None, sent_error=None):
        self.catalogue = catalogue
        # "Skickade Beställningar" (duplicate-order guard); counted, not in `calls`.
        self.sent_rows = list(sent_rows or [])
        self.sent_error = sent_error
        self.sent_calls = 0
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

    def list_sent_orders(self):
        self.sent_calls += 1
        if self.sent_error is not None:
            raise self.sent_error
        return [dict(r) for r in self.sent_rows]

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
    """Fake Telegram: records plain messages and button messages.

    A message with only a Klar button (a resolution message) is delivered like a plain
    message: its text lands in `sent` (and in `klar` with its buttons and message id),
    and it fails when `send_ok` or `klar_ok` is False. Approval messages land in `buttons`.
    """
    rec = {"sent": [], "buttons": [], "klar": [], "cleared": [], "message_id": 77,
           "send_ok": True, "klar_ok": True, "next_klar": 900}

    def fake_send(config, text):
        rec["sent"].append(text)
        return rec["send_ok"]

    def fake_buttons(config, text, buttons):
        flat = ([b for row in buttons for b in row]
                if buttons and isinstance(buttons[0], list) else buttons)
        if flat and all(data.startswith("klar:") for _, data in flat):
            if not (rec["send_ok"] and rec["klar_ok"]):
                return None
            mid = rec["next_klar"]
            rec["next_klar"] += 1
            rec["sent"].append(text)
            rec["klar"].append((text, buttons, mid))
            return mid
        rec["buttons"].append((text, buttons))
        return rec["message_id"]

    monkeypatch.setattr(notifier, "send_message", fake_send)
    monkeypatch.setattr(notifier, "send_with_buttons", fake_buttons)
    monkeypatch.setattr(notifier, "clear_buttons",
                        lambda config, message_id: rec["cleared"].append(message_id))
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
    assert "OLRD-0001" in tg["sent"][0]
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


# ── Task 6b: search terms tried in order ─────────────────────────────────────

FRONT = {"title": "Bromsskiva Bosch fram", "brand": "", "price_sek": 500.0, "url_or_id": "sku-f",
         "article_nr": "1025-F1", "in_stock": True, "placement": "fram"}
REAR = {"title": "Bromsskiva Bosch bak", "brand": "", "price_sek": 450.0, "url_or_id": "sku-r",
        "article_nr": "1025-R1", "in_stock": True, "placement": "bak"}
DISC_BOOKING = {**BOOKING, "parts": [{"name": "Bromsskivor fram", "qty": "2"}], "fluids": []}


def _searched(portal):
    return [c[1] for c in portal.calls if c[0] == "search"]


def test_second_term_tried_when_first_has_no_candidates(tg, orders_file):
    portal = FakePortal(catalogue={"bromsskiva": [], "Bromsskivor fram": [FRONT]})

    assert parts_order.plan_and_stage(CONFIG, DISC_BOOKING, portal, APPROVE, confident) == "pending"

    assert _searched(portal) == ["bromsskiva", "Bromsskivor fram"]
    assert ("add_to_cart", "sku-f", 2) in portal.calls
    plan = orders_state.get("B1")["plan"]
    assert plan["parts"][0]["url_or_id"] == "sku-f"
    assert plan["parts"][0]["name"] == "Bromsskivor fram"
    assert plan["expected_lines"] == [{"title": "Bromsskiva Bosch fram", "qty": 2, "url_or_id": "sku-f"}]


def test_second_term_tried_when_first_candidates_are_rejected(tg):
    seen = []

    def picker(line, ranked):
        seen.append([c["url_or_id"] for c in ranked])
        return 0, 0.95

    portal = FakePortal(catalogue={"bromsskiva": [REAR], "Bromsskivor fram": [REAR, FRONT]})

    assert parts_order.plan_and_stage(CONFIG, DISC_BOOKING, portal, APPROVE, picker) == "pending"

    assert _searched(portal) == ["bromsskiva", "Bromsskivor fram"]
    assert seen == [["sku-f"]]  # the rear disc never reached the picker
    assert [c for c in portal.calls if c[0] == "add_to_cart"] == [("add_to_cart", "sku-f", 2)]


def test_first_term_match_stops_the_search(tg):
    portal = FakePortal(catalogue={"bromsskiva": [FRONT], "Bromsskivor fram": [FRONT]})

    assert parts_order.plan_and_stage(CONFIG, DISC_BOOKING, portal, APPROVE, confident) == "pending"

    assert _searched(portal) == ["bromsskiva"]


def test_unmatched_after_all_terms_blocks(tg, orders_file):
    portal = FakePortal(catalogue={"bromsskiva": [REAR], "Bromsskivor fram": [REAR]})

    assert parts_order.plan_and_stage(CONFIG, DISC_BOOKING, portal, APPROVE, confident) == "blocked"

    assert _searched(portal) == ["bromsskiva", "Bromsskivor fram"]
    assert "add_to_cart" not in portal.names()
    rec = orders_state.get("B1")
    assert rec["status"] == "blocked"
    assert rec["plan"]["reason"] == "ingen säker träff för 'Bromsskivor fram'"
    assert len(tg["sent"]) == 1
    assert "ingen säker träff för 'Bromsskivor fram'" in tg["sent"][0]
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
                          "Hantera OLRD-0001 för hand."]


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
    assert plan["reg_nr"] == "TST001"
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
    assert "OLRD-0001" in tg["sent"][0]


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
    assert "OLRD-0001" in tg["sent"][0]
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

    assert "OLRD-0001" in text
    assert "Sentinel Kund AB" in text
    assert "TST001 VOLKSWAGEN PASSAT" in text
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
    plan = {"booking_id": "B1", "booking_nr": "OLRD-0001", "reg_nr": "TST001",
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


# ── Final review: cart fluids / non-Latin lines, empty cart at execute time ──

GOOD_LINE = {"title": "Bosch Oljefilter P7001", "qty": 2, "url_or_id": "p1"}


@pytest.mark.parametrize("bad", [
    {"title": "Castrol Motorolja Edge 5W-30 4L", "qty": 1, "url_or_id": "oil"},
    {"title": "Bromsvätska DOT4", "qty": 1, "url_or_id": "bf"},
])
def test_cart_problem_rejects_fluid_lines_even_if_expected(bad):
    problem = cart_problem([GOOD_LINE, bad], [GOOD_LINE, bad], APPROVE)
    assert problem is not None
    assert bad["title"] in problem
    assert "vätska" in problem


@pytest.mark.parametrize("bad", [
    {"title": "Bosch Оljefilter", "qty": 1, "url_or_id": "cy"},
    {"title": "Oljefilter", "brand": "Bоsch", "qty": 1, "url_or_id": "cy"},
])
def test_cart_problem_rejects_non_latin_lines_even_if_expected(bad):
    problem = cart_problem([GOOD_LINE, bad], [GOOD_LINE, bad], APPROVE)
    assert problem is not None
    assert "tecken" in problem


def test_cart_problem_allows_filter_lines():
    air = {"title": "Bosch Luftfilter S0123", "qty": 1, "url_or_id": "p2"}
    assert cart_problem([GOOD_LINE, air], [GOOD_LINE, air], APPROVE) is None


def test_execute_order_empty_cart_says_order_may_already_exist(tg):
    portal = FakePortal()
    plan = _staged_plan(tg, portal)
    portal.carts = [{"lines": [], "total_sek": 0.0}]

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "failed"

    assert "checkout" not in portal.names()
    assert orders_state.get("B1")["status"] == "failed"
    assert tg["sent"] == [
        "Varukorgen på pro.meca.se är tom – ordern kan redan vara lagd. "
        "Kontrollera Orderöversikt på pro.meca.se innan du beställer för hand."]


def test_execute_order_other_mismatch_keeps_old_text(tg):
    portal = FakePortal()
    plan = _staged_plan(tg, portal)
    portal.cart = _cart(490.0, [{"title": "Bosch Oljefilter P7001", "qty": 1, "price_sek": 120.0},
                                {"title": "Bosch Luftfilter S0123", "qty": 1, "price_sek": 250.0}])

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "failed"

    (text,) = tg["sent"]
    assert text.startswith("Beställning stoppad för OLRD-0001: varukorgen stämmer inte")
    assert text.endswith("Inget beställdes – beställ för hand.")


# ── Final review M3: line names and stock in approval / ordered messages ─────

def test_order_body_shows_line_name_and_stock_note():
    parts = [{"name": "Bromsskiva fram", "title": "Bosch Bromsskiva, 300mm", "brand": "",
              "qty": 2, "price_sek": 500.0, "in_stock": False},
             {"name": "Oljefilter", "title": "Bosch Oljefilter P7001", "brand": "Bosch",
              "qty": 1, "price_sek": 120.0, "in_stock": True},
             {"name": "Luftfilter", "title": "Bosch Luftfilter S0123", "brand": "Bosch",
              "qty": 1, "price_sek": 250.0}]
    lines = order_body(parts, 1370.0, [])
    assert lines[1] == "- Bromsskiva fram -> Bosch Bromsskiva, 300mm (–) x2 à 500.00 SEK – OBS: 0 i lager"
    assert lines[2] == "- Oljefilter -> Bosch Oljefilter P7001 (Bosch) x1 à 120.00 SEK"
    assert lines[3] == "- Luftfilter -> Bosch Luftfilter S0123 (Bosch) x1 à 250.00 SEK"  # unknown stock


def test_approval_and_ordered_messages_show_line_names_and_stock(tg, orders_file):
    catalogue = {"Oljefilter": [{**CATALOGUE["Oljefilter"][0], "in_stock": False}],
                 "Luftfilter": [{**CATALOGUE["Luftfilter"][0], "in_stock": True}]}
    portal = FakePortal(catalogue=catalogue)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"

    text, _ = tg["buttons"][0]
    assert "- Oljefilter -> Bosch Oljefilter P7001 (Bosch) x2" in text
    oil_line = next(line for line in text.splitlines() if line.startswith("- Oljefilter"))
    assert oil_line.endswith("OBS: 0 i lager")
    assert text.count("OBS: 0 i lager") == 1
    assert "- Luftfilter -> Bosch Luftfilter S0123" in text
    plan = orders_state.get("B1")["plan"]
    assert [p["in_stock"] for p in plan["parts"]] == [False, True]
    assert plan["expected_lines"] == [
        {"title": "Bosch Oljefilter P7001", "qty": 2, "url_or_id": "p1"},
        {"title": "Bosch Luftfilter S0123", "qty": 1, "url_or_id": "p2"}]
    _assert_no_pii(orders_file)

    tg["sent"].clear()
    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "ordered"
    (done,) = tg["sent"]
    assert "- Oljefilter -> Bosch Oljefilter P7001 (Bosch) x2" in done
    assert "OBS: 0 i lager" in done
    assert len(text) < 4096 and len(done) < 4096
    _assert_no_pii(orders_file)


# ── Task 16a: cart_touched, detail_url, quiet re-stage of blocked orders ─────

NO_AIR = {**CATALOGUE, "Luftfilter": []}
AIR_REASON = "ingen säker träff för 'Luftfilter'"
GIVE_UP = ("Försöker inte mer automatiskt för OLRD-0001 – "
           "beställ för hand eller åtgärda orsaken.")


def _blocked_then_restaged(reason, tries=0):
    orders_state.put("B1", "blocked", plan={"reason": reason, "booking_id": "B1"})
    orders_state.update_fields("B1", cart_touched=False)
    if tries:
        orders_state.update_fields("B1", tries=tries)
    assert orders_state.restage("B1") is True


def test_cart_touched_is_persisted_before_a_later_block(tg):
    wrong = _cart(490.0, [{"title": "Okänt filter A", "qty": 2, "price_sek": 240.0},
                          {"title": "Okänt filter B", "qty": 1, "price_sek": 250.0}])
    portal = FakePortal(cart=wrong)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"

    assert orders_state.get("B1")["cart_touched"] is True


def test_cart_touched_is_persisted_before_a_later_failure(tg):
    class BreaksAfterAdd(FakePortal):
        def read_cart(self):
            if self.added:
                raise RuntimeError("cart page broke")
            return super().read_cart()

    portal = BreaksAfterAdd()

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "failed"

    rec = orders_state.get("B1")
    assert rec["status"] == "failed"
    assert rec["cart_touched"] is True


def test_cart_touched_set_when_first_add_is_called(tg):
    seen = []

    class Spy(FakePortal):
        def add_to_cart(self, candidate, qty):
            seen.append(orders_state.get("B1").get("cart_touched"))
            super().add_to_cart(candidate, qty)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, Spy(), APPROVE, confident) == "pending"
    assert seen == [True, True]
    assert orders_state.get("B1")["cart_touched"] is True


def test_cart_not_touched_when_blocked_before_adding(tg):
    portal = FakePortal(catalogue=NO_AIR)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"

    assert orders_state.get("B1")["cart_touched"] is False   # marked untouched (not legacy)


def test_detail_url_is_stored_without_customer_data(tg, orders_file):
    booking = {**BOOKING, "detail_url": "https://portal.example/Booking/Details?BookingID=123"}

    assert parts_order.plan_and_stage(CONFIG, booking, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "blocked"

    assert orders_state.get("B1")["detail_url"] == booking["detail_url"]
    _assert_no_pii(orders_file)


def test_first_block_still_messages(tg):
    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "blocked"
    assert len(tg["sent"]) == 1


def test_quiet_retry_same_reason_sends_nothing(tg, orders_file):
    _blocked_then_restaged(AIR_REASON)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "blocked"

    assert tg["sent"] == [] and tg["buttons"] == []
    rec = orders_state.get("B1")
    assert rec["status"] == "blocked"
    assert rec["tries"] == 1
    assert rec["plan"]["reason"] == AIR_REASON
    _assert_no_pii(orders_file)


def test_quiet_retry_new_reason_sends_one_blocked_message(tg):
    _blocked_then_restaged("varukorgen är inte tom")

    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "blocked"

    assert len(tg["sent"]) == 1
    assert AIR_REASON in tg["sent"][0]
    assert orders_state.get("B1")["tries"] == 1


def test_quiet_retry_that_stages_sends_the_approval_message(tg):
    _blocked_then_restaged(AIR_REASON)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(), APPROVE, confident) == "pending"

    assert tg["sent"] == []
    (text, buttons), = tg["buttons"]
    assert buttons == [("Godkänn", "approve:B1"), ("Avbryt", "cancel:B1")]
    rec = orders_state.get("B1")
    assert rec["status"] == "pending" and rec["tries"] == 1


def test_quiet_retry_that_fails_is_reported(tg):
    _blocked_then_restaged(AIR_REASON)
    portal = FakePortal()
    portal.fail_on = "search"

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "failed"

    assert len(tg["sent"]) == 1
    assert "misslyckades" in tg["sent"][0]


def test_retry_reaching_the_cap_sends_one_give_up_message(tg):
    _blocked_then_restaged(AIR_REASON, tries=orders_state.MAX_TRIES - 1)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "blocked"

    assert tg["sent"] == [GIVE_UP]
    rec = orders_state.get("B1")
    assert rec["status"] == "blocked"
    assert rec["tries"] == orders_state.MAX_TRIES
    assert rec["gave_up"] is True
    assert orders_state.restage("B1") is False
    assert parts_order.give_up_if_capped(CONFIG, BOOKING) is None  # once only
    assert tg["sent"] == [GIVE_UP]


def test_retry_below_the_cap_sends_no_give_up(tg):
    _blocked_then_restaged(AIR_REASON, tries=orders_state.MAX_TRIES - 2)

    parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE, confident)

    assert tg["sent"] == []
    assert "gave_up" not in orders_state.get("B1")


def test_give_up_if_capped_sends_once_for_a_capped_record(tg):
    orders_state.put("B1", "blocked", plan={"reason": AIR_REASON})
    orders_state.update_fields("B1", tries=orders_state.MAX_TRIES)

    assert parts_order.give_up_if_capped(CONFIG, BOOKING) is True
    assert parts_order.give_up_if_capped(CONFIG, BOOKING) is None
    assert tg["sent"] == [GIVE_UP]


@pytest.mark.parametrize("fields", [{"tries": 3}, {"tries": 10, "cart_touched": True}])
def test_give_up_if_capped_ignores_other_records(tg, fields):
    orders_state.put("B1", "blocked", plan={"reason": AIR_REASON})
    orders_state.update_fields("B1", **fields)

    assert parts_order.give_up_if_capped(CONFIG, BOOKING) is None
    assert tg["sent"] == []


def test_give_up_undelivered_is_tried_again_later(tg):
    orders_state.put("B1", "blocked", plan={"reason": AIR_REASON})
    orders_state.update_fields("B1", tries=orders_state.MAX_TRIES)
    tg["send_ok"] = False

    assert parts_order.give_up_if_capped(CONFIG, BOOKING) is False
    assert "gave_up" not in orders_state.get("B1")
    tg["send_ok"] = True
    assert parts_order.give_up_if_capped(CONFIG, BOOKING) is True


# ── Review M3: an undelivered give-up message is a failure, retried later ────

def test_retry_at_cap_with_undelivered_give_up_is_failed_but_stays_blocked(tg):
    _blocked_then_restaged(AIR_REASON, tries=orders_state.MAX_TRIES - 1)
    tg["send_ok"] = False

    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "failed"

    rec = orders_state.get("B1")
    assert rec["status"] == "blocked"          # still blocked: the give-up is retried
    assert "gave_up" not in rec
    tg["send_ok"] = True
    assert parts_order.give_up_if_capped(CONFIG, BOOKING) is True


# ── Task 16b: resolution messages carry a Klar button ───────────────────────

KLAR = [("Klar", "klar:B1")]


def _klar_only(tg, text_part):
    """Exactly one Klar message was sent, containing text_part; returns its message id."""
    (text, buttons, mid), = tg["klar"]
    assert buttons == KLAR
    assert text_part in text
    assert tg["sent"] == [text]
    return mid


def test_blocked_message_carries_klar_and_stores_its_id(tg):
    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "blocked"

    mid = _klar_only(tg, "Delbeställning stoppad för OLRD-0001")
    assert orders_state.get("B1")["klar_message_id"] == mid == 900
    assert tg["buttons"] == [] and tg["cleared"] == []


def test_blocked_before_portal_carries_klar(tg):
    booking = {**BOOKING, "reg_nr": ""}
    assert parts_order.plan_and_stage(CONFIG, booking, UntouchablePortal(), APPROVE,
                                      confident) == "blocked"
    assert orders_state.get("B1")["klar_message_id"] == _klar_only(tg, "registreringsnummer")


def test_failed_staging_message_carries_klar(tg):
    portal = FakePortal()
    portal.fail_on = "search"

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "failed"

    assert orders_state.get("B1")["klar_message_id"] == _klar_only(tg, "misslyckades")


def test_record_failure_message_carries_klar(tg):
    orders_state.queue("B1")
    assert parts_order.record_failure(CONFIG, BOOKING, "portal saknas") == "failed"
    assert orders_state.get("B1")["klar_message_id"] == _klar_only(tg, "portal saknas")


def test_approval_message_has_no_klar_button(tg):
    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(), APPROVE, confident) == "pending"

    (text, buttons), = tg["buttons"]
    assert buttons == [("Godkänn", "approve:B1"), ("Avbryt", "cancel:B1")]
    assert tg["klar"] == [] and tg["sent"] == []
    assert "klar_message_id" not in orders_state.get("B1")


def test_undeliverable_approval_failure_message_carries_klar(tg):
    tg["message_id"] = None
    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(), APPROVE, confident) == "failed"
    assert orders_state.get("B1")["klar_message_id"] == _klar_only(tg, "godkännandemeddelandet")


def test_ordered_message_carries_klar(tg):
    portal = FakePortal(ref="ORD-9")
    plan = _staged_plan(tg, portal)

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "ordered"

    mid = _klar_only(tg, "Beställd: OLRD-0001")
    rec = orders_state.get("B1")
    assert rec["klar_message_id"] == mid
    assert rec["message_id"] == 77               # the approval message id is untouched
    assert parts_order.order_notify_failed("B1") is False


def test_auto_mode_ordered_message_carries_klar(tg):
    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(), AUTO, confident) == "ordered"
    assert tg["buttons"] == []
    assert orders_state.get("B1")["klar_message_id"] == _klar_only(tg, "Beställd")


@pytest.mark.parametrize("break_it,text", [
    (lambda p: setattr(p, "fail_on", "checkout"), "kan ha lagts"),
    (lambda p: setattr(p, "fail_on", "login"), "Inget beställdes"),
    (lambda p: setattr(p, "carts", [{"lines": [], "total_sek": 0.0}]), "Varukorgen på pro.meca.se är tom"),
    (lambda p: setattr(p, "cart", _cart(500.0)), "Beställning stoppad för OLRD-0001"),
])
def test_execute_order_failure_messages_carry_klar(tg, break_it, text):
    portal = FakePortal()
    plan = _staged_plan(tg, portal)
    break_it(portal)

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "failed"

    assert orders_state.get("B1")["klar_message_id"] == _klar_only(tg, text)


def test_klar_send_failure_falls_back_to_a_plain_message(tg):
    tg["klar_ok"] = False

    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "blocked"   # still delivered

    assert tg["klar"] == []
    (text,) = tg["sent"]
    assert text.startswith("Delbeställning stoppad för OLRD-0001")
    assert text.endswith("\n(Klar-knappen kunde inte skickas – markera bokningen som "
                         "Hanterad i portalen.)")
    assert "klar_message_id" not in orders_state.get("B1")


def test_klar_and_plain_both_failing_is_failed_as_before(tg):
    tg["send_ok"] = False
    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "failed"
    assert orders_state.get("B1")["status"] == "failed"
    assert "klar_message_id" not in orders_state.get("B1")


def test_ordered_with_klar_failing_but_plain_delivered_is_not_notify_failed(tg):
    portal = FakePortal(ref="ORD-9")
    plan = _staged_plan(tg, portal)
    tg["klar_ok"] = False

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "ordered"

    assert parts_order.order_notify_failed("B1") is False
    assert "Klar-knappen kunde inte skickas" in tg["sent"][-1]


def test_new_resolution_message_clears_the_old_klar_buttons(tg):
    _blocked_then_restaged("varukorgen är inte tom")
    orders_state.update_fields("B1", klar_message_id=500)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "blocked"   # new reason -> new message

    assert tg["cleared"] == [500]
    assert orders_state.get("B1")["klar_message_id"] == _klar_only(tg, AIR_REASON)


def test_quiet_same_reason_retry_keeps_the_old_klar(tg):
    _blocked_then_restaged(AIR_REASON)
    orders_state.update_fields("B1", klar_message_id=500)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "blocked"

    assert tg["sent"] == [] and tg["klar"] == [] and tg["buttons"] == [] and tg["cleared"] == []
    assert orders_state.get("B1")["klar_message_id"] == 500


def test_give_up_message_carries_klar_and_replaces_the_old_one(tg):
    orders_state.put("B1", "blocked", plan={"reason": AIR_REASON})
    orders_state.update_fields("B1", tries=orders_state.MAX_TRIES, klar_message_id=500)

    assert parts_order.give_up_if_capped(CONFIG, BOOKING) is True

    assert orders_state.get("B1")["klar_message_id"] == _klar_only(tg, "Försöker inte mer")
    assert tg["cleared"] == [500]


def test_give_up_skipped_for_a_handled_record(tg):
    orders_state.put("B1", "blocked", plan={"reason": AIR_REASON})
    orders_state.update_fields("B1", tries=orders_state.MAX_TRIES, handled=True)

    assert parts_order.give_up_if_capped(CONFIG, BOOKING) is None
    assert tg["sent"] == []


def test_send_with_klar_without_a_record_sends_plain(tg):
    assert parts_order.send_with_klar(CONFIG, "B404", "Hej") is True
    assert tg["klar"] == []
    assert tg["sent"] == ["Hej\n(Klar-knappen kunde inte skickas – markera bokningen som "
                          "Hanterad i portalen.)"]


def test_send_with_klar_survives_a_failing_clear(tg, monkeypatch):
    orders_state.put("B1", "blocked")
    orders_state.update_fields("B1", klar_message_id=500)

    def broken(config, message_id):
        raise RuntimeError("telegram hiccup")

    monkeypatch.setattr(notifier, "clear_buttons", broken)
    assert parts_order.send_with_klar(CONFIG, "B1", "Ny text") is True
    assert orders_state.get("B1")["klar_message_id"] == 900


# ── Fix wave: search_term never leaks into the plan or messages ─────────────

def test_search_term_tag_is_not_persisted_or_shown(tg, orders_file):
    tagged = {term: [{**c, "search_term": "SENTINEL-TERM"} for c in cands]
              for term, cands in CATALOGUE.items()}
    portal = FakePortal(catalogue=tagged)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"

    plan = orders_state.get("B1")["plan"]
    assert all("search_term" not in p for p in plan["parts"])
    assert all("search_term" not in x for x in plan["expected_lines"])
    assert "SENTINEL-TERM" not in orders_file.read_text(encoding="utf-8")
    assert all("SENTINEL-TERM" not in text for text, _ in tg["buttons"])
    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "ordered"
    assert all("SENTINEL-TERM" not in text for text in tg["sent"])


# ── Fix wave: parts already in the cart stay when a later part fails ─────────

from parts_order_helpers import owner_reason          # noqa: E402
from parts_portal_parse import PartsPortalError, PartsPortalPartialAdd  # noqa: E402

FIVE = ["Kupéfilter", "Oljefilter", "Oljepluggspackning", "Luftfilter", "Tändstift"]
FIVE_CATALOGUE = {name: [{"title": f"Bosch {name}", "brand": "Bosch", "price_sek": 100.0,
                          "url_or_id": f"s{i}"}] for i, name in enumerate(FIVE, 1)}
FIVE_BOOKING = {**BOOKING, "parts": [{"name": n, "qty": "2" if n == "Tändstift" else "1"}
                                     for n in FIVE]}
NOT_LINKED = PartsPortalError("part s2 not found among linked parts")
PROMISE = ("Delarna som lades till ligger kvar orörda i varukorgen på pro.meca.se – "
           "komplettera för hand.")


class NoClearPortal(FakePortal):
    """add_to_cart raises for the skus in `fail`; clear_cart must never be called."""

    def __init__(self, fail=None, **kw):
        super().__init__(catalogue=FIVE_CATALOGUE, **kw)
        self.fail = dict(fail or {})
        self.cleared = []

    def add_to_cart(self, candidate, qty):
        self._record("add_to_cart", candidate["url_or_id"], qty)
        if candidate["url_or_id"] in self.fail:
            raise self.fail[candidate["url_or_id"]]
        self.added.append((candidate, qty))

    def clear_cart(self, reg_nr=None):
        self.cleared.append(reg_nr)
        raise AssertionError("the cart must never be cleared on a failure path")


def _partial_text(in_cart, failed):
    first = (f"Delbeställning för OLRD-0001: i varukorgen: {in_cart}." if in_cart
             else "Delbeställning för OLRD-0001: Inget lades i varukorgen.")
    return "\n".join([first, f"Kunde inte läggas till: {failed}.", PROMISE,
                      "Förbered (finns på lager):", "- Olja 5W30 x5"])


def _assert_partial(tg, portal, text):
    rec = orders_state.get("B1")
    assert rec["status"] == "blocked"
    assert rec["plan"]["reason"] == "kunde inte lägga alla delar i varukorgen"
    assert rec["cart_touched"] is True
    assert orders_state.restage("B1") is False          # never retried automatically
    assert portal.cleared == []
    assert tg["buttons"] == []                          # no approval message
    assert rec["klar_message_id"] == _klar_only(tg, text)
    assert tg["sent"] == [text]


def test_part_2_of_5_fails_others_are_still_added(tg):
    portal = NoClearPortal(fail={"s2": NOT_LINKED})

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    adds = [c[1] for c in portal.calls if c[0] == "add_to_cart"]
    assert adds == ["s1", "s2", "s3", "s4", "s5"]
    assert [c["url_or_id"] for c, _ in portal.added] == ["s1", "s3", "s4", "s5"]
    assert portal.names().count("read_cart") == 1       # only the empty-cart precondition
    _assert_partial(tg, portal, _partial_text(
        "Bosch Kupéfilter x1, Bosch Oljepluggspackning x1, Bosch Luftfilter x1, Bosch Tändstift x2",
        "Oljefilter (delen finns inte bland delarna som passar bilen)"))


def test_first_part_fails_the_others_are_added(tg):
    portal = NoClearPortal(fail={"s1": PartsPortalError("part s1 not found among linked parts")})

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    assert [c["url_or_id"] for c, _ in portal.added] == ["s2", "s3", "s4", "s5"]
    _assert_partial(tg, portal, _partial_text(
        "Bosch Oljefilter x1, Bosch Oljepluggspackning x1, Bosch Luftfilter x1, Bosch Tändstift x2",
        "Kupéfilter (delen finns inte bland delarna som passar bilen)"))


def test_all_parts_fail(tg):
    errors = [PartsPortalError("part s1 not found among linked parts"),
              PartsPortalError("part s2 is already in the cart ('1 st')"),
              PartsPortalError("no cart update seen for s3 (step 1): Timeout 15000ms"),
              RuntimeError("SECRET-PAGE-TEXT boom"),
              # last: a lost vehicle stops the loop (review M3), so nothing is left untried
              PartsPortalError("vehicle ABC123 is no longer loaded after searching")]
    portal = NoClearPortal(fail={f"s{i}": e for i, e in enumerate(errors, 1)})

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    assert portal.added == []
    _assert_partial(tg, portal, _partial_text(None, "; ".join([
        "Kupéfilter (delen finns inte bland delarna som passar bilen)",
        "Oljefilter (delen låg redan i varukorgen)",
        "Oljepluggspackning (varukorgen uppdaterades inte)",
        "Luftfilter (tekniskt fel (se loggen))",
        "Tändstift (fordonet försvann från sidan)"])))


def test_already_in_cart_refusal_is_a_failed_part(tg):
    portal = NoClearPortal(fail={"s5": PartsPortalError("part s5 is already in the cart ('2 st')")})

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    assert len(portal.added) == 4
    assert "Kunde inte läggas till: Tändstift (delen låg redan i varukorgen)." in tg["sent"][0]


def test_five_parts_all_added_is_staged_as_before(tg):
    portal = NoClearPortal()

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "pending"

    assert len(portal.added) == 5 and portal.cleared == []
    (text, buttons), = tg["buttons"]
    assert buttons == [("Godkänn", "approve:B1"), ("Avbryt", "cancel:B1")]
    assert tg["sent"] == [] and tg["klar"] == []


def test_exception_after_adding_keeps_the_cart_and_says_so(tg):
    class CartBreaks(NoClearPortal):
        def read_cart(self):
            if self.added:
                raise RuntimeError("SECRET-PAGE-TEXT cart page broke")
            return super().read_cart()

    portal = CartBreaks()

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "failed"

    assert portal.cleared == []
    (text,) = tg["sent"]
    assert text == ("Delbeställning misslyckades för OLRD-0001: tekniskt fel (se loggen). "
                    "Beställ för hand. Det som redan ligger i varukorgen är orört.")


def test_exception_before_adding_has_no_cart_promise(tg):
    portal = NoClearPortal()
    portal.fail_on = "search"

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "failed"

    assert tg["sent"] == ["Delbeställning misslyckades för OLRD-0001: tekniskt fel (se loggen). "
                          "Beställ för hand."]


def test_over_ceiling_after_adding_keeps_the_cart_and_says_so(tg):
    portal = NoClearPortal(cart=_cart(20000.0))

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    assert portal.cleared == []
    assert "Det som redan ligger i varukorgen är orört." in tg["sent"][0]


# ── Fix wave: owner-facing text never carries raw exception text ─────────────

@pytest.mark.parametrize("exc,expected", [
    (PartsPortalError("part 10087157 not found among linked parts"),
     "delen finns inte bland delarna som passar bilen"),
    (PartsPortalError("part p1 is not on the current page and no search to repeat"),
     "delen finns inte bland delarna som passar bilen"),
    (PartsPortalError("part p1 is already in the cart ('1 st')"), "delen låg redan i varukorgen"),
    (PartsPortalError("no cart update seen for p1 (step 1): Timeout"), "varukorgen uppdaterades inte"),
    (PartsPortalError("cart update for p1 failed (step 2)"), "varukorgen uppdaterades inte"),
    (PartsPortalError("quantity of p1 did not reach 2"), "varukorgen uppdaterades inte"),
    (PartsPortalError("quantity of p1 is 1, expected 2"), "varukorgen uppdaterades inte"),
    (PartsPortalError("vehicle ABC123 is no longer loaded after searching"),
     "fordonet försvann från sidan"),
    (PartsPortalError("login to pro.meca.se failed (still on the login form)"),
     "inloggningen på pro.meca.se misslyckades"),
    (PartsPortalError("pro.meca.se session expired (login form shown)"),
     "inloggningen på pro.meca.se misslyckades"),
    (PartsPortalError("not logged in"), "inloggningen på pro.meca.se misslyckades"),
    (PartsPortalError("cart page is for 'X', expected Y"), "tekniskt fel (se loggen)"),
    (RuntimeError("not found among linked parts"), "tekniskt fel (se loggen)"),
    (ValueError("SECRET-PAGE-TEXT"), "tekniskt fel (se loggen)"),
    # error codes win over the text (the substring match is only a fallback)
    (PartsPortalError("SECRET-PAGE-TEXT", "not_linked"), "delen finns inte bland delarna som passar bilen"),
    (PartsPortalError("SECRET-PAGE-TEXT", "in_cart"), "delen låg redan i varukorgen"),
    (PartsPortalError("SECRET-PAGE-TEXT", "cart_update"), "varukorgen uppdaterades inte"),
    (PartsPortalError("SECRET-PAGE-TEXT", "vehicle_lost"), "fordonet försvann från sidan"),
    (PartsPortalError("SECRET-PAGE-TEXT", "login"), "inloggningen på pro.meca.se misslyckades"),
    (PartsPortalError("part p1 not found among linked parts", "in_cart"), "delen låg redan i varukorgen"),
    (PartsPortalError("SECRET-PAGE-TEXT", "surprise"), "tekniskt fel (se loggen)"),
    (PartsPortalPartialAdd("SECRET-PAGE-TEXT", steps_done=1), "varukorgen uppdaterades inte"),
])
def test_owner_reason_mapping(exc, expected):
    assert owner_reason(exc) == expected


SECRET = "SECRET-PAGE-TEXT"


def _all_text(tg):
    return [t for t in tg["sent"]] + [t for t, _ in tg["buttons"]]


@pytest.mark.parametrize("step", ["login", "load_vehicle", "search", "read_cart"])
def test_secret_exception_text_never_reaches_telegram_when_staging(tg, step, caplog):
    class Secret(FakePortal):
        def _record(self, name, *args):
            self.calls.append((name, *args))
            if name == step:
                raise PartsPortalError(f"{SECRET} at {name}")

    # wave 4: an unclassified portal error before the cart is touched is retried (blocked)
    assert parts_order.plan_and_stage(CONFIG, BOOKING, Secret(), APPROVE, confident) == "blocked"
    assert tg["sent"] and all(SECRET not in t for t in _all_text(tg))
    assert SECRET in caplog.text                          # the raw text still goes to the log


@pytest.mark.parametrize("step", ["search", "read_cart"])
def test_secret_exception_text_never_reaches_telegram_when_failing(tg, step, caplog):
    class Secret(FakePortal):
        def _record(self, name, *args):
            self.calls.append((name, *args))
            if name == step:
                raise ValueError(f"{SECRET} at {name}")

    assert parts_order.plan_and_stage(CONFIG, BOOKING, Secret(), APPROVE, confident) == "failed"
    assert tg["sent"] and all(SECRET not in t for t in _all_text(tg))
    assert SECRET in caplog.text


@pytest.mark.parametrize("step", ["login", "read_cart", "checkout"])
def test_secret_exception_text_never_reaches_telegram_when_ordering(tg, step):
    portal = FakePortal()
    plan = _staged_plan(tg, portal)

    def boom(*a, **k):
        raise RuntimeError(f"{SECRET} in {step}")

    setattr(portal, step, boom)
    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "failed"
    assert tg["sent"] and all(SECRET not in t for t in _all_text(tg))


# ── Fix wave: send_with_klar stores before it clears; klar_sent_at ──────────

from datetime import datetime, timedelta, timezone  # noqa: E402


def test_send_with_klar_stores_the_new_id_before_clearing_the_old(tg, monkeypatch):
    orders_state.put("B1", "blocked")
    orders_state.update_fields("B1", klar_message_id=500, klar_closed=True)
    seen = []
    monkeypatch.setattr(notifier, "clear_buttons", lambda config, mid: seen.append(
        (mid, orders_state.get("B1")["klar_message_id"])))

    assert parts_order.send_with_klar(CONFIG, "B1", "Ny text") is True

    assert seen == [(500, 900)]          # the new id was already stored
    rec = orders_state.get("B1")
    assert rec["klar_closed"] is False   # a new button is live again
    sent_at = datetime.fromisoformat(rec["klar_sent_at"])
    assert sent_at.tzinfo is not None
    assert abs(datetime.now(timezone.utc) - sent_at) < timedelta(minutes=1)


def test_send_with_klar_failed_store_is_a_problem_and_keeps_the_old_button(tg, monkeypatch):
    orders_state.put("B1", "blocked")
    orders_state.update_fields("B1", klar_message_id=500)

    def broken(booking_id, **fields):
        raise OSError("disk full")

    monkeypatch.setattr(orders_state, "update_fields", broken)
    assert parts_order.send_with_klar(CONFIG, "B1", "Ny text") is False
    assert tg["cleared"] == []
    assert orders_state.get("B1")["klar_message_id"] == 500


def test_block_whose_klar_id_cannot_be_stored_is_failed(tg, monkeypatch):
    real = orders_state.update_fields

    def broken(booking_id, **fields):
        if "klar_message_id" in fields:
            raise OSError("disk full")
        return real(booking_id, **fields)

    monkeypatch.setattr(orders_state, "update_fields", broken)
    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "failed"



# ── Review: truthful partial-cart message; stop when the vehicle/login is lost ─

PARTIAL_PROMISE_CHECK = ("Det som redan ligger i varukorgen är orört – kontrollera antalet "
                         "innan du lägger till mer.")


def test_partly_added_part_is_listed_as_maybe_in_the_cart(tg):
    portal = NoClearPortal(fail={"s5": PartsPortalPartialAdd(
        "SECRET-PAGE-TEXT no cart update seen for s5 (step 2)", steps_done=1)})

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    _assert_partial(tg, portal, "\n".join([
        "Delbeställning för OLRD-0001: i varukorgen: Bosch Kupéfilter x1, Bosch Oljefilter x1, "
        "Bosch Oljepluggspackning x1, Bosch Luftfilter x1.",
        "Kan ligga delvis i varukorgen – kontrollera antalet: Tändstift "
        "(lade 1 av 2, varukorgen uppdaterades inte).",
        PARTIAL_PROMISE_CHECK,
        "Förbered (finns på lager):", "- Olja 5W30 x5"]))


def test_failure_before_any_click_stays_in_the_normal_list(tg):
    portal = NoClearPortal(fail={"s5": PartsPortalPartialAdd("click intercepted", steps_done=0)})

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    (text,) = tg["sent"]
    assert "Kunde inte läggas till: Tändstift (varukorgen uppdaterades inte)." in text
    assert "Kan ligga delvis" not in text
    assert PROMISE in text


def test_normal_and_partial_failures_together(tg):
    portal = NoClearPortal(fail={
        "s1": PartsPortalError("x", "not_linked"),
        "s5": PartsPortalPartialAdd("x", steps_done=2, code="cart_update")})

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    _assert_partial(tg, portal, "\n".join([
        "Delbeställning för OLRD-0001: i varukorgen: Bosch Oljefilter x1, "
        "Bosch Oljepluggspackning x1, Bosch Luftfilter x1.",
        "Kunde inte läggas till: Kupéfilter (delen finns inte bland delarna som passar bilen).",
        "Kan ligga delvis i varukorgen – kontrollera antalet: Tändstift "
        "(lade 2 av 2, varukorgen uppdaterades inte).",
        PARTIAL_PROMISE_CHECK,
        "Förbered (finns på lager):", "- Olja 5W30 x5"]))


def test_only_a_partial_part_says_nothing_was_fully_added(tg):
    booking = {**FIVE_BOOKING, "parts": [{"name": "Tändstift", "qty": "2"}]}
    portal = NoClearPortal(fail={"s5": PartsPortalPartialAdd("x", steps_done=1)})

    assert parts_order.plan_and_stage(CONFIG, booking, portal, APPROVE, confident) == "blocked"

    (text,) = tg["sent"]
    assert text.startswith("Delbeställning för OLRD-0001: ingen del lades helt i varukorgen.\n")
    assert "Inget lades i varukorgen" not in text


@pytest.mark.parametrize("code,reason", [("vehicle_lost", "fordonet försvann från sidan"),
                                         ("login", "inloggningen på pro.meca.se misslyckades")])
def test_lost_vehicle_or_login_stops_adding(tg, code, reason):
    portal = NoClearPortal(fail={"s2": PartsPortalError("SECRET-PAGE-TEXT", code)})

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    assert [c[1] for c in portal.calls if c[0] == "add_to_cart"] == ["s1", "s2"]
    _assert_partial(tg, portal, "\n".join([
        "Delbeställning för OLRD-0001: i varukorgen: Bosch Kupéfilter x1.",
        f"Kunde inte läggas till: Oljefilter ({reason}).",
        "Inte försökt: Oljepluggspackning, Luftfilter, Tändstift.",
        PROMISE,
        "Förbered (finns på lager):", "- Olja 5W30 x5"]))


def test_lost_vehicle_detected_by_text_fallback_also_stops(tg):
    portal = NoClearPortal(fail={"s1": PartsPortalError("vehicle X is no longer loaded")})

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    assert [c[1] for c in portal.calls if c[0] == "add_to_cart"] == ["s1"]
    assert "Inte försökt: Oljefilter, Oljepluggspackning, Luftfilter, Tändstift." in tg["sent"][0]
    assert "Inget lades i varukorgen." in tg["sent"][0]



# ── Wave 4: transient pro.meca.se errors before the cart is touched are retried ─

TRANSIENT_TEXT = ("Delbeställning för OLRD-0001: pro.meca.se svarade inte som väntat – inget "
                  "lades i varukorgen. Jag försöker igen vid nästa körning.")


class PlaywrightTimeout(Exception):
    """Stands in for playwright.sync_api.TimeoutError (matched by its module)."""


PlaywrightTimeout.__module__ = "playwright._impl._errors"


class Flaky(FakePortal):
    """Raises `exc` on the n-th call of `step` (1-based)."""

    def __init__(self, step, exc, nth=1, **kw):
        super().__init__(catalogue=FIVE_CATALOGUE, **kw)
        self.step, self.exc, self.nth, self.seen = step, exc, nth, 0
        self.cleared = []

    def _record(self, name, *args):
        self.calls.append((name, *args))
        if name == self.step:
            self.seen += 1
            if self.seen == self.nth:
                raise self.exc

    def clear_cart(self, reg_nr=None):
        self.cleared.append(reg_nr)
        raise AssertionError("never cleared")


def _assert_transient_block(tg, portal):
    rec = orders_state.get("B1")
    assert rec["status"] == "blocked"
    assert rec["plan"]["reason"] == parts_order.TRANSIENT_REASON == "pro.meca.se svarade inte som väntat"
    assert rec.get("cart_touched") is False
    assert "add_to_cart" not in portal.names() and portal.cleared == []
    assert tg["buttons"] == []
    assert rec["klar_message_id"] == _klar_only(tg, TRANSIENT_TEXT)


@pytest.mark.parametrize("step,exc,nth", [
    ("login", PartsPortalError("login to pro.meca.se failed", "login"), 1),
    ("search", PartsPortalError("vehicle AJZ167 is no longer loaded after searching",
                                "vehicle_lost"), 3),               # the live case
    ("search", PlaywrightTimeout("Timeout 30000ms exceeded"), 2),
    ("load_vehicle", PartsPortalError("vehicle X could not be loaded on pro.meca.se"), 1),
    ("read_cart", PartsPortalError("SECRET-PAGE-TEXT not the cart page"), 1),
    ("search", TimeoutError("timed out"), 1),
    ("search", ConnectionError("reset"), 4),
    ("search", PartsPortalError("x", "cart_update"), 1),
])
def test_transient_error_before_the_cart_is_blocked_for_a_retry(tg, step, exc, nth):
    portal = Flaky(step, exc, nth)

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    _assert_transient_block(tg, portal)
    assert orders_state.restage("B1") is True       # retried quietly on the next run


def test_transient_retry_failing_the_same_way_is_silent_then_succeeds(tg):
    lost = PartsPortalError("vehicle lost", "vehicle_lost")
    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, Flaky("search", lost, 3), APPROVE,
                                      confident) == "blocked"
    assert len(tg["sent"]) == 1

    assert orders_state.restage("B1") is True
    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, Flaky("search", lost, 2), APPROVE,
                                      confident) == "blocked"
    assert len(tg["sent"]) == 1                      # same reason: nothing new
    assert orders_state.get("B1")["tries"] == 1

    assert orders_state.restage("B1") is True
    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, NoClearPortal(), APPROVE,
                                      confident) == "pending"
    (text, buttons), = tg["buttons"]
    assert buttons == [("Godkänn", "approve:B1"), ("Avbryt", "cancel:B1")]
    assert len(tg["sent"]) == 1


def test_transient_retries_stop_at_the_cap_with_one_give_up(tg):
    lost = PartsPortalError("vehicle lost", "vehicle_lost")
    parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, Flaky("login", lost), APPROVE, confident)
    for _ in range(orders_state.MAX_TRIES):
        assert orders_state.restage("B1") is True
        parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, Flaky("login", lost), APPROVE, confident)

    assert orders_state.restage("B1") is False
    assert tg["sent"][0] == TRANSIENT_TEXT
    assert tg["sent"][1:] == [GIVE_UP]


def test_error_after_the_first_add_keeps_the_partial_cart_behaviour(tg):
    portal = Flaky("read_cart", PartsPortalError("vehicle lost", "vehicle_lost"), nth=2)

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "failed"

    (text,) = tg["sent"]
    assert text.startswith("Delbeställning misslyckades för OLRD-0001: fordonet försvann")
    assert "Det som redan ligger i varukorgen är orört." in text
    assert portal.cleared == []


@pytest.mark.parametrize("exc", [ValueError("SECRET-PAGE-TEXT bad"),
                                 PartsPortalError("parts_portal username/password not configured",
                                                  "config"),
                                 PartsPortalError("invalid registration number 'x'", "config"),
                                 KeyError("plan")])
def test_non_transient_error_before_the_cart_still_fails(tg, exc):
    portal = Flaky("search", exc)

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "failed"

    assert orders_state.get("B1")["status"] == "failed"
    assert tg["sent"][0].startswith("Delbeställning misslyckades för OLRD-0001")


def test_matching_problems_are_not_transient(tg):
    portal = FakePortal(catalogue=NO_AIR)
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"
    assert orders_state.get("B1")["plan"]["reason"] == AIR_REASON


def test_is_transient_classification():
    from parts_order_helpers import is_transient
    assert is_transient(PartsPortalError("x", "vehicle_lost"))
    assert is_transient(PartsPortalError("x", "login"))
    assert is_transient(PartsPortalError("x", "cart_update"))
    assert is_transient(PartsPortalError("unclassified"))
    assert is_transient(PlaywrightTimeout("t"))
    assert is_transient(TimeoutError()) and is_transient(ConnectionResetError())
    for exc in (PartsPortalError("x", "config"), PartsPortalError("x", "not_linked"),
                PartsPortalError("x", "in_cart"), ValueError("x"), RuntimeError("x"),
                OSError("disk"), KeyError("k")):
        assert not is_transient(exc), exc


# ── Wave 4 review: vehicle_unknown, search_timeout, per-booking time budget ──

def test_unknown_plate_is_not_retried(tg):
    portal = Flaky("load_vehicle", PartsPortalError("vehicle X could not be loaded on pro.meca.se",
                                                    "vehicle_unknown"))

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "failed"

    assert tg["sent"] == ["Delbeställning misslyckades för OLRD-0001: pro.meca.se känner inte igen "
                          "registreringsnumret. Beställ för hand."]
    assert orders_state.restage("B1") is False


def test_search_timeout_before_the_cart_is_retried(tg):
    portal = Flaky("search", PartsPortalError("search results for 'x' did not appear",
                                              "search_timeout"), nth=2)
    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"
    _assert_transient_block(tg, portal)


@pytest.mark.parametrize("code,reason", [
    ("vehicle_unknown", "pro.meca.se känner inte igen registreringsnumret"),
    ("search_timeout", "sökresultatet visades inte i tid")])
def test_new_codes_have_swedish_reasons(code, reason):
    assert owner_reason(PartsPortalError("SECRET-PAGE-TEXT", code)) == reason


class Clocked(NoClearPortal):
    """Every search / add advances the injected stage clock."""

    def __init__(self, clock, per_search=0, per_add=0, **kw):
        super().__init__(**kw)
        self.clock, self.per_search, self.per_add = clock, per_search, per_add

    def search(self, term):
        self.clock["t"] += self.per_search
        return super().search(term)

    def add_to_cart(self, candidate, qty):
        self.clock["t"] += self.per_add
        return super().add_to_cart(candidate, qty)


@pytest.fixture
def stage_clock(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(parts_order, "_clock", lambda: clock["t"])
    return clock


def test_stage_budget_exceeded_before_the_cart_is_a_transient_block(tg, stage_clock):
    portal = Clocked(stage_clock, per_search=200)   # searches at 0, 200, 400; then 600 > 480

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    assert portal.names().count("search") == 3
    _assert_transient_block(tg, portal)
    assert parts_order.STAGE_BUDGET_S == 480


def test_stage_budget_exceeded_while_adding_stops_and_lists_the_rest(tg, stage_clock):
    portal = Clocked(stage_clock, per_add=300)      # adds at 0 and 300; then 600 > 480

    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "blocked"

    assert [c[1] for c in portal.calls if c[0] == "add_to_cart"] == ["s1", "s2"]
    _assert_partial(tg, portal, "\n".join([
        "Delbeställning för OLRD-0001: i varukorgen: Bosch Kupéfilter x1, Bosch Oljefilter x1.",
        "Inte försökt: Oljepluggspackning, Luftfilter, Tändstift.",
        PROMISE,
        "Förbered (finns på lager):", "- Olja 5W30 x5"]))


def test_stage_within_budget_is_unchanged(tg, stage_clock):
    portal = Clocked(stage_clock, per_search=10, per_add=10)
    assert parts_order.plan_and_stage(CONFIG, FIVE_BOOKING, portal, APPROVE, confident) == "pending"


# ── Task 14: vehicle model for the order checker; UNVERIFIED reference text ──

def test_vehicle_model_is_stored_when_staging(tg, orders_file):
    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(), APPROVE,
                                      confident) == "pending"
    assert orders_state.get("B1")["vehicle_model"] == "VOLKSWAGEN PASSAT"
    _assert_no_pii(orders_file)


def test_vehicle_model_is_sanitised_and_kept_when_blocked(tg, orders_file):
    booking = {**BOOKING, "vehicle": " KIARIO\x00 (III)‎(2014)\n" + "x" * 100}
    assert parts_order.plan_and_stage(CONFIG, booking, FakePortal(catalogue=NO_AIR), APPROVE,
                                      confident) == "blocked"
    model = orders_state.get("B1")["vehicle_model"]
    assert model.startswith("KIARIO (III)(2014) x") and len(model) == 80


@pytest.mark.parametrize("vehicle", [None, "", "   ", 42])
def test_no_vehicle_model_stores_nothing(tg, vehicle):
    booking = {**BOOKING, "vehicle": vehicle}
    parts_order.plan_and_stage(CONFIG, booking, FakePortal(), APPROVE, confident)
    assert "vehicle_model" not in orders_state.get("B1")


def test_unverified_reference_is_not_shown_as_an_order_number(tg):
    portal = FakePortal(ref="UNVERIFIED-20261021T123728Z")
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, AUTO, confident) == "ordered"
    (text,) = tg["sent"]
    assert "UNVERIFIED" not in text and "20261021T123728Z" not in text
    assert ("Ordernummer: bekräftas senare (kolla Skickade Beställningar i pro.meca.se)"
            in text.splitlines())
    assert orders_state.get("B1")["plan"]["confirmation"] == "UNVERIFIED-20261021T123728Z"


def test_real_reference_is_shown_as_before(tg):
    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(ref="4500012345"), AUTO,
                                      confident) == "ordered"
    assert tg["sent"][0].splitlines()[:2] == ["Beställd: OLRD-0001 (TST001)",
                                              "Ordernummer: 4500012345"]


# ── Duplicate-order guard: a recent sent order for the vehicle blocks staging ──

from order_match import numbers_text  # noqa: E402


def _days_ago(n):
    return (datetime.now(timezone.utc).date() - timedelta(days=n)).isoformat()


def _sent_row(days_ago=1, reg_nr="TST001", model="VOLKSWAGEN, Passat Variant (B8), 14-",
              supplier_no="S-90001", order_numbers="70001/70002"):
    return {"supplier_no": supplier_no, "status": "Skickad", "date": _days_ago(days_ago),
            "reg_nr": reg_nr, "model": model, "order_numbers": order_numbers,
            "order_status": ""}


DUP_TEXT = ("Bokning OLRD-0001: en skickad order för fordonet finns redan i pro.meca.se "
            "(S-90001 · 70001/70002). Jag har inte förberett någon ny order. "
            "Beställ för hand om delarna ändå behövs.")


def _assert_duplicate_block(tg, portal, row=None):
    rec = orders_state.get("B1")
    numbers = numbers_text(row or _sent_row())
    assert rec["status"] == "blocked"
    assert rec["plan"]["reason"] == f"order finns redan i pro.meca.se ({numbers})"
    assert not rec["plan"]["reason"].startswith(orders_state.PRE_CART_REASONS)
    assert rec.get("cart_touched") is False
    assert portal.calls == [] and portal.added == []      # no login, vehicle or cart action
    assert portal.sent_calls == 1
    assert tg["buttons"] == []
    assert orders_state.restage("B1") is False            # never retried automatically
    assert orders_state.get("B1")["status"] == "blocked"


def test_recent_sent_order_for_the_plate_blocks_staging(tg, orders_file):
    portal = FakePortal(sent_rows=[_sent_row()])

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"

    _assert_duplicate_block(tg, portal)
    assert tg["sent"] == [DUP_TEXT]
    assert orders_state.get("B1")["klar_message_id"] == _klar_only(tg, DUP_TEXT)
    _assert_no_pii(orders_file)


def test_duplicate_numbers_come_from_the_row_only(tg):
    row = _sent_row(supplier_no="S-12345", order_numbers="1 / 2,3")
    portal = FakePortal(sent_rows=[row])

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"

    _assert_duplicate_block(tg, portal, row)
    assert "(S-12345 · 1/2/3)" in tg["sent"][0]


def test_sent_order_without_a_registration_column_matches_by_model(tg):
    portal = FakePortal(sent_rows=[_sent_row(reg_nr=None)])
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"
    _assert_duplicate_block(tg, portal)


def test_sent_order_for_another_model_without_registration_column_proceeds(tg):
    portal = FakePortal(sent_rows=[_sent_row(reg_nr=None, model="KIA, Rio Kombi-sedan (III)")])
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"
    assert portal.sent_calls == 1


def test_sent_order_exactly_seven_days_old_still_blocks(tg):
    assert parts_order.DUPLICATE_WINDOW_DAYS == 7
    portal = FakePortal(sent_rows=[_sent_row(days_ago=7)])
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"
    _assert_duplicate_block(tg, portal)


def test_sent_order_older_than_seven_days_does_not_block(tg):
    portal = FakePortal(sent_rows=[_sent_row(days_ago=8)])

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"

    assert portal.sent_calls == 1
    assert "add_to_cart" in portal.names()
    assert len(tg["buttons"]) == 1


def test_sent_order_for_another_plate_does_not_block(tg):
    portal = FakePortal(sent_rows=[_sent_row(reg_nr="OTH999")])
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"
    assert portal.sent_calls == 1


def test_sent_order_claimed_by_another_record_does_not_block(tg):
    orders_state.put("B0", "ordered", plan={"booking_id": "B0"})
    orders_state.update_fields("B0", verified_numbers=numbers_text(_sent_row()),
                               verified_at=datetime.now(timezone.utc).isoformat())
    portal = FakePortal(sent_rows=[_sent_row()])

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"

    assert portal.sent_calls == 1


def test_unclaimed_row_blocks_even_when_another_row_is_claimed(tg):
    orders_state.put("B0", "ordered", plan={"booking_id": "B0"})
    orders_state.update_fields("B0", verified_numbers="S-80000 · 60001")
    claimed = _sent_row(supplier_no="S-80000", order_numbers="60001")
    portal = FakePortal(sent_rows=[claimed, _sent_row()])

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"

    _assert_duplicate_block(tg, portal)


def test_sent_orders_timeout_is_a_transient_block_with_no_cart_action(tg):
    portal = FakePortal(sent_rows=[_sent_row(reg_nr="OTH999")],
                        sent_error=PartsPortalError("sent orders did not appear", "search_timeout"))

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"

    rec = orders_state.get("B1")
    assert rec["plan"]["reason"] == parts_order.TRANSIENT_REASON
    assert rec.get("cart_touched") is False
    assert portal.calls == [] and portal.sent_calls == 1
    assert tg["sent"] == [TRANSIENT_TEXT] and tg["buttons"] == []
    assert orders_state.restage("B1") is True             # retried on the next run


def test_sent_orders_parse_error_fails_closed_with_no_cart_action(tg):
    portal = FakePortal(sent_error=PartsPortalError("SECRET-PAGE-TEXT table", "parse"))

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "failed"

    assert portal.calls == [] and portal.added == []
    assert tg["buttons"] == []
    assert "SECRET-PAGE-TEXT" not in "".join(tg["sent"])


def test_restaged_record_is_checked_again_and_blocked_when_ordered_by_hand(tg):
    _blocked_then_restaged(AIR_REASON)
    portal = FakePortal(sent_rows=[_sent_row()])

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"

    _assert_duplicate_block(tg, portal)
    assert tg["sent"] == [DUP_TEXT]                       # a new reason: the owner is told


def test_sent_orders_are_read_once_per_staging_attempt(tg):
    portal = FakePortal(sent_rows=[_sent_row(days_ago=30), _sent_row(reg_nr="OTH999")])
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"
    assert portal.sent_calls == 1


def test_guard_runs_before_the_first_portal_step(tg):
    class Ordered(FakePortal):
        def list_sent_orders(self):
            self.calls.append(("list_sent_orders",))
            return super().list_sent_orders()

    portal = Ordered()
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"
    assert portal.names()[:2] == ["list_sent_orders", "login"]


def test_guard_skipped_without_a_portal(tg):
    booking = {**BOOKING, "parts": [], "fluids": [{"name": "Olja 5W30", "qty": "5"}]}
    assert parts_order.plan_and_stage(CONFIG, booking, None, APPROVE, confident) == "blocked"
    assert orders_state.get("B1")["plan"]["reason"] == "inga delar att beställa"


def test_existing_sent_order_helper():
    now = datetime.now(timezone.utc)
    row = _sent_row()
    assert parts_order._existing_sent_order(FakePortal(sent_rows=[row]), BOOKING, now) == row
    assert parts_order._existing_sent_order(FakePortal(), BOOKING, now) is None
    naive = now.replace(tzinfo=None)                      # treated as UTC
    assert parts_order._existing_sent_order(FakePortal(sent_rows=[row]), BOOKING, naive) == row
    with pytest.raises(PartsPortalError):
        parts_order._existing_sent_order(
            FakePortal(sent_error=PartsPortalError("x", "login")), BOOKING, now)


# ── Duplicate guard fail-closed: a non-empty list that cannot be matched blocks ──

UNCHECKED_TEXT = ("Bokning OLRD-0001: jag kunde inte kontrollera om en order för fordonet redan "
                  "finns i pro.meca.se. Jag har inte förberett någon order – kontrollera "
                  "Skickade Beställningar för hand. Jag försöker igen vid nästa körning.")


def _assert_unchecked_block(tg, portal):
    rec = orders_state.get("B1")
    assert rec["status"] == "blocked"
    assert rec["plan"]["reason"] == parts_order.UNCHECKED_REASON == "kan inte kontrollera tidigare order"
    assert not rec["plan"]["reason"].startswith(orders_state.NO_RESTAGE_REASONS)
    assert rec.get("cart_touched") is False
    assert portal.calls == [] and portal.added == [] and portal.sent_calls == 1
    assert tg["buttons"] == []
    assert tg["sent"] == [UNCHECKED_TEXT]
    assert orders_state.restage("B1") is True             # re-stageable, like a transient block


def test_rows_without_registration_column_and_no_vehicle_model_block(tg):
    booking = {**BOOKING, "vehicle": ""}
    portal = FakePortal(sent_rows=[_sent_row(reg_nr=None)])

    assert parts_order.plan_and_stage(CONFIG, booking, portal, APPROVE, confident) == "blocked"

    _assert_unchecked_block(tg, portal)


def test_empty_sent_list_without_vehicle_model_proceeds(tg):
    booking = {**BOOKING, "vehicle": None}
    portal = FakePortal(sent_rows=[])
    assert parts_order.plan_and_stage(CONFIG, booking, portal, APPROVE, confident) == "pending"
    assert portal.sent_calls == 1


@pytest.mark.parametrize("dates", [["21.10.2026"], ["", None, "2026-13-40"]])
def test_rows_whose_dates_all_cannot_be_read_block(tg, dates):
    rows = [{**_sent_row(reg_nr="OTH999"), "date": d} for d in dates]
    portal = FakePortal(sent_rows=rows)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "blocked"

    _assert_unchecked_block(tg, portal)


def test_one_readable_date_is_enough_to_check(tg):
    rows = [{**_sent_row(reg_nr="OTH999"), "date": "21.10.2026"}, _sent_row(reg_nr="OTH999")]
    portal = FakePortal(sent_rows=rows)
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE, confident) == "pending"


def test_unchecked_retry_blocking_the_same_way_is_quiet(tg):
    booking = {**BOOKING, "vehicle": ""}
    rows = [_sent_row(reg_nr=None)]
    assert parts_order.plan_and_stage(CONFIG, booking, FakePortal(sent_rows=rows), APPROVE,
                                      confident) == "blocked"
    assert orders_state.restage("B1") is True
    assert parts_order.plan_and_stage(CONFIG, booking, FakePortal(sent_rows=rows), APPROVE,
                                      confident) == "blocked"
    assert tg["sent"] == [UNCHECKED_TEXT]                 # no second message


def test_existing_sent_order_raises_when_it_cannot_check():
    now = datetime.now(timezone.utc)
    with pytest.raises(parts_order.SentOrdersUnchecked):
        parts_order._existing_sent_order(FakePortal(sent_rows=[_sent_row(reg_nr=None)]),
                                         {**BOOKING, "vehicle": ""}, now)
    with pytest.raises(parts_order.SentOrdersUnchecked):
        parts_order._existing_sent_order(FakePortal(sent_rows=[{**_sent_row(), "date": "x"}]),
                                         BOOKING, now)


# ── Package 2 task 3: alternatives at staging, swap buttons ──────────────────

OOS = {"title": "Bosch Oljefilter P7001", "brand": "Bosch", "price_sek": 120.0,
       "url_or_id": "p1", "in_stock": False, "search_term": "Oljefilter",
       "page_text": "SECRET-PAGE-TEXT"}
MANN = {"title": "Mann Oljefilter W712", "brand": "Mann", "price_sek": 200.0,
        "url_or_id": "p3", "in_stock": True, "article_nr": "W712", "search_term": "Oljefilter",
        "page_text": "SECRET-PAGE-TEXT"}
DEAR = {"title": "Bosch Oljefilter P9000", "brand": "Bosch", "price_sek": 500.0,
        "url_or_id": "p4", "in_stock": True, "search_term": "Oljefilter"}
LUFT = CATALOGUE["Luftfilter"]
NO_SWITCH = Rules(("Bosch",), ("ProMeister",), 10000, "approve", prefer_in_stock=False)
APPROVE_BUTTONS = [("Godkänn", "approve:B1"), ("Avbryt", "cancel:B1")]
MANN_ALT = {"title": "Mann Oljefilter W712", "brand": "Mann", "price_sek": 200.0,
            "url_or_id": "p3", "in_stock": True, "article_nr": "W712",
            "search_term": "Oljefilter"}


def by_sku(*skus):
    """A confident picker that takes the first of `skus` present in the list it is shown."""
    def picker(line, ranked):
        ids = [c.get("url_or_id") for c in ranked]
        for sku in skus:
            if sku in ids:
                return ids.index(sku), 0.95
        return 0, 0.1
    return picker


def _added(portal):
    return [c.get("url_or_id") for c, _ in portal.added]


def test_out_of_stock_line_stores_alternative_and_sends_a_swap_row(tg, orders_file):
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, NO_SWITCH,
                                      by_sku("p1", "p3", "p2")) == "pending"

    plan = orders_state.get("B1")["plan"]
    assert plan["parts"][0]["url_or_id"] == "p1"
    assert plan["parts"][0]["alternative"] == {**MANN_ALT, "alt_reason": "slut", "saving_sek": 0}
    assert "alternative" not in plan["parts"][1]
    # The alternative is never in the cart at staging: only the chosen parts were added,
    # and the cart was verified against the chosen parts.
    assert _added(portal) == ["p1", "p2"]
    assert [x["url_or_id"] for x in plan["expected_lines"]] == ["p1", "p2"]
    assert "checkout" not in portal.names()
    text, buttons = tg["buttons"][0]
    assert buttons == [APPROVE_BUTTONS, [("Byt Oljefilter", "swap:B1:0")]]
    assert ("Alternativ för Oljefilter: Mann Oljefilter W712 (Mann) à 200.00 SEK – i lager "
            "(slut)") in text
    assert text.endswith("Godkänn för att beställa.")
    assert orders_state.get("B1")["message_id"] == 77
    assert "SECRET-PAGE-TEXT" not in orders_file.read_text(encoding="utf-8")
    assert "SECRET-PAGE-TEXT" not in text
    _assert_no_pii(orders_file)


def test_expensive_line_stores_a_cheaper_alternative_with_its_saving(tg):
    portal = FakePortal(catalogue={"Oljefilter": [DEAR, MANN], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE,
                                      by_sku("p4", "p3", "p2")) == "pending"

    part = orders_state.get("B1")["plan"]["parts"][0]
    assert part["url_or_id"] == "p4"
    assert part["alternative"] == {**MANN_ALT, "alt_reason": "dyr", "saving_sek": 300.0}
    assert _added(portal) == ["p4", "p2"]
    text, buttons = tg["buttons"][0]
    assert ("Alternativ för Oljefilter: Mann Oljefilter W712 (Mann) à 200.00 SEK – i lager "
            "(dyr: sparar 300.00 SEK/st)") in text
    assert buttons[1] == [("Byt Oljefilter", "swap:B1:0")]


def test_no_flagged_line_sends_exactly_todays_message_and_one_row(tg):
    assert parts_order.plan_and_stage(CONFIG, BOOKING, FakePortal(), APPROVE,
                                      confident) == "pending"

    text, buttons = tg["buttons"][0]
    assert buttons == APPROVE_BUTTONS
    plan = orders_state.get("B1")["plan"]
    expected = build_summary(BOOKING, _cart(490.0), BOOKING["fluids"], plan["parts"])
    assert text == expected + "\n\nGodkänn för att beställa."
    assert "Alternativ" not in text and "Bytt" not in text
    assert all("alternative" not in p and "replaced_from" not in p for p in plan["parts"])


def test_in_stock_replacement_is_shown_and_stored(tg, orders_file):
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE,
                                      by_sku("p1", "p3", "p2")) == "pending"

    part = orders_state.get("B1")["plan"]["parts"][0]
    assert part["url_or_id"] == "p3"
    assert part["replaced_from"] == {"title": "Bosch Oljefilter P7001", "brand": "Bosch",
                                     "price_sek": 120.0}
    assert "alternative" not in part
    assert _added(portal) == ["p3", "p2"]          # the replacement, never the original
    text, buttons = tg["buttons"][0]
    oil = next(line for line in text.splitlines() if line.startswith("- Oljefilter"))
    assert oil.startswith("- Oljefilter -> Mann Oljefilter W712 (Mann) x2")
    assert oil.endswith("Bytt från Bosch Oljefilter P7001 (slut)")
    assert buttons == APPROVE_BUTTONS              # nothing to swap: one row as today
    assert "checkout" not in portal.names()        # still waits for the owner
    assert "SECRET-PAGE-TEXT" not in orders_file.read_text(encoding="utf-8")


def test_replacement_and_alternative_are_both_shown(tg):
    portal = FakePortal(catalogue={"Oljefilter": [OOS, DEAR, MANN], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE,
                                      by_sku("p1", "p4", "p3", "p2")) == "pending"

    part = orders_state.get("B1")["plan"]["parts"][0]
    assert part["url_or_id"] == "p4"
    assert part["replaced_from"]["title"] == "Bosch Oljefilter P7001"
    assert part["alternative"]["url_or_id"] == "p3"
    assert part["alternative"]["alt_reason"] == "dyr"
    assert _added(portal) == ["p4", "p2"]
    text, buttons = tg["buttons"][0]
    assert "Bytt från Bosch Oljefilter P7001 (slut)" in text
    assert "Alternativ för Oljefilter: Mann Oljefilter W712 (Mann)" in text
    assert buttons == [APPROVE_BUTTONS, [("Byt Oljefilter", "swap:B1:0")]]


def test_replacement_over_the_ceiling_blocks(tg):
    rules = Rules(("Bosch",), ("ProMeister",), 1000, "approve")
    portal = FakePortal(catalogue={"Oljefilter": [OOS, DEAR], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, rules,
                                      by_sku("p1", "p4", "p2")) == "blocked"

    assert "taket" in orders_state.get("B1")["plan"]["reason"]
    assert tg["buttons"] == [] and "checkout" not in portal.names()


def test_replacement_is_verified_against_the_cart(tg):
    wrong = _cart(490.0, [{"title": "Bosch Oljefilter P7001", "qty": 2, "url_or_id": "p1"},
                          {"title": "Bosch Luftfilter S0123", "qty": 1, "url_or_id": "p2"}])
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT}, cart=wrong)

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE,
                                      by_sku("p1", "p3", "p2")) == "blocked"

    assert "stämmer inte" in orders_state.get("B1")["plan"]["reason"]
    assert tg["buttons"] == [] and "checkout" not in portal.names()


def test_forbidden_brand_is_never_a_replacement_or_alternative(tg):
    forbidden = {"title": "ProMeister Oljefilter", "brand": "ProMeister", "price_sek": 50.0,
                 "url_or_id": "p9", "in_stock": True}
    portal = FakePortal(catalogue={"Oljefilter": [OOS, forbidden], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, APPROVE,
                                      by_sku("p9", "p1", "p2")) == "pending"

    part = orders_state.get("B1")["plan"]["parts"][0]
    assert part["url_or_id"] == "p1"
    assert "alternative" not in part and "replaced_from" not in part
    assert _added(portal) == ["p1", "p2"]
    text, buttons = tg["buttons"][0]
    assert "ProMeister" not in text and buttons == APPROVE_BUTTONS


def test_swap_button_over_64_bytes_is_skipped_but_the_text_stays(tg):
    long_id = "X" * 58                        # swap:<58>:0 is 65 bytes
    booking = {**BOOKING, "id": long_id}
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, booking, portal, NO_SWITCH,
                                      by_sku("p1", "p3", "p2")) == "pending"

    text, buttons = tg["buttons"][0]
    assert buttons == [("Godkänn", f"approve:{long_id}"), ("Avbryt", f"cancel:{long_id}")]
    assert "Alternativ för Oljefilter: Mann Oljefilter W712" in text
    assert orders_state.get(long_id)["plan"]["parts"][0]["alternative"]["url_or_id"] == "p3"


def test_swap_button_data_fits_telegrams_limit():
    buttons = parts_order._swap_buttons("B1", [{"name": "Oljefilter med ett mycket långt namn",
                                                "alternative": {"title": "x"}},
                                               {"name": "Luftfilter"}], swaps=0)
    assert buttons == [[("Byt Oljefilter med ett m", "swap:B1:0")]]
    assert all(len(d.encode()) <= 64 for row in buttons for _, d in row)
    assert parts_order._swap_buttons("Å" * 29, [{"name": "a", "alternative": {}}], 0) == []
    assert parts_order._swap_buttons("Å" * 28, [{"name": "a", "alternative": {}}], 0) != []


def test_no_swap_buttons_after_two_swaps(tg, monkeypatch):
    real_get = orders_state.get
    monkeypatch.setattr(orders_state, "get",
                        lambda bid: (lambda r: r if r is None else {**r, "swaps": 2})(real_get(bid)))
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, NO_SWITCH,
                                      by_sku("p1", "p3", "p2")) == "pending"

    text, buttons = tg["buttons"][0]
    assert buttons == APPROVE_BUTTONS
    assert "Alternativ för Oljefilter" in text


@pytest.mark.parametrize("swaps", [0, 1])
def test_swap_buttons_under_two_swaps(swaps):
    parts = [{"name": "Oljefilter", "alternative": {"title": "x"}}]
    assert parts_order._swap_buttons("B1", parts, swaps) == [[("Byt Oljefilter", "swap:B1:0")]]


@pytest.mark.parametrize("swaps", [2, 3, True, "1", None, 1.0])
def test_swap_buttons_need_a_valid_count_under_two(swaps):
    parts = [{"name": "Oljefilter", "alternative": {"title": "x"}}]
    expected = [[("Byt Oljefilter", "swap:B1:0")]] if swaps is None else []
    assert parts_order._swap_buttons("B1", parts, swaps) == expected


def test_auto_mode_orders_the_chosen_part_and_sends_no_buttons(tg):
    rules = Rules(("Bosch",), ("ProMeister",), 10000, "auto", prefer_in_stock=False)
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, rules,
                                      by_sku("p1", "p3", "p2")) == "ordered"

    assert tg["buttons"] == []
    assert _added(portal) == ["p1", "p2"]
    assert portal.names().count("checkout") == 1
    assert all("Alternativ" not in text for text in tg["sent"])


def test_max_alternatives_three_still_stores_one_alternative(tg):
    rules = Rules(("Bosch",), ("ProMeister",), 10000, "approve", prefer_in_stock=False,
                  max_alternatives=3)
    other = {**MANN, "title": "Mahle Oljefilter OC1", "brand": "Mahle", "url_or_id": "p5"}
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN, other], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, rules,
                                      by_sku("p1", "p3", "p2")) == "pending"

    part = orders_state.get("B1")["plan"]["parts"][0]
    assert isinstance(part["alternative"], dict) and part["alternative"]["url_or_id"] == "p3"
    text, buttons = tg["buttons"][0]
    assert text.count("Alternativ för") == 1
    assert buttons == [APPROVE_BUTTONS, [("Byt Oljefilter", "swap:B1:0")]]


def test_ordered_message_does_not_repeat_alternatives(tg):
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, NO_SWITCH,
                                      by_sku("p1", "p3", "p2")) == "pending"
    plan = orders_state.get("B1")["plan"]

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, NO_SWITCH) == "ordered"

    (done,) = tg["sent"]
    assert "Alternativ" not in done and "Mann" not in done


def test_build_summary_renders_alternatives_and_replacements():
    parts = [{"name": "Oljefilter", "title": "Mann Oljefilter W712", "brand": "Mann", "qty": 2,
              "price_sek": 200.0, "in_stock": True,
              "replaced_from": {"title": "Bosch Oljefilter P7001", "brand": "Bosch",
                                "price_sek": 120.0},
              "alternative": {"title": "Knecht OC1", "brand": None, "price_sek": 90.0,
                              "in_stock": True, "alt_reason": "dyr", "saving_sek": 110.0}},
             {"name": "", "title": "Bosch Luftfilter S0123", "brand": "Bosch", "qty": 1,
              "price_sek": 250.0,
              "alternative": {"title": "Mann C1", "brand": "Mann", "price_sek": None,
                              "in_stock": None, "alt_reason": "slut", "saving_sek": 0}}]
    lines = build_summary(BOOKING, _cart(650.0), [], parts).splitlines()
    assert lines[4] == ("- Oljefilter -> Mann Oljefilter W712 (Mann) x2 à 200.00 SEK – "
                        "Bytt från Bosch Oljefilter P7001 (slut)")
    assert lines[5] == "- Bosch Luftfilter S0123 (Bosch) x1 à 250.00 SEK"
    assert lines[6] == "Totalt: 650.00 SEK"
    # Task 4: a blank line sets the alternatives apart (also from a Förbered list), and the
    # saving is per unit ("/st").
    assert lines[7:] == [
        "",
        "Alternativ för Oljefilter: Knecht OC1 (–) à 90.00 SEK – i lager "
        "(dyr: sparar 110.00 SEK/st)",
        "Alternativ för Bosch Luftfilter S0123: Mann C1 (Mann) à pris saknas (slut)"]


def test_auto_mode_never_replaces_an_out_of_stock_pick(tg, orders_file):
    # Task 4 (was: auto mode told the owner about a replacement). The in-stock replacement
    # is approve mode only: auto orders exactly what it ordered before package 2, and it
    # stores no alternative either (nobody can swap an order that is already placed).
    rules = Rules(("Bosch",), ("ProMeister",), 10000, "auto")
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, rules,
                                      by_sku("p1", "p3", "p2")) == "ordered"

    assert tg["buttons"] == []
    assert _added(portal) == ["p1", "p2"]
    (done,) = tg["sent"]
    assert "Bytt från" not in done and "Mann" not in done
    assert "OBS: 0 i lager" in done
    parts = orders_state.get("B1")["plan"]["parts"]
    assert all("alternative" not in p and "replaced_from" not in p for p in parts)


# ── Package 2 task 4: approval text guards ───────────────────────────────────

def test_alternatives_are_set_apart_by_a_blank_line_in_the_approval_text(tg):
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, NO_SWITCH,
                                      by_sku("p1", "p3", "p2")) == "pending"

    text, _ = tg["buttons"][0]
    assert "- Olja 5W30 x5\n\nAlternativ för Oljefilter:" in text


def _long_booking(n=12, title_len=180):
    parts = [{"name": f"Del nummer {i} med ett ganska långt namn", "qty": "1"} for i in range(n)]
    catalogue, picks = {}, []
    for i in range(n):
        base = f"Bosch Reservdel {i} " + "X" * title_len
        catalogue[parts[i]["name"]] = [
            {"title": base, "brand": "Bosch", "price_sek": 100.0, "url_or_id": f"o{i}",
             "in_stock": False},
            {"title": f"Mann Reservdel {i} " + "Y" * title_len, "brand": "Mann",
             "price_sek": 110.0, "url_or_id": f"a{i}", "in_stock": True}]
        picks += [f"o{i}", f"a{i}"]
    return {**BOOKING, "parts": parts, "fluids": []}, catalogue, picks


def test_too_long_approval_text_drops_the_alternatives_and_swap_rows(tg):
    booking, catalogue, picks = _long_booking()
    portal = FakePortal(catalogue=catalogue)

    assert parts_order.plan_and_stage(CONFIG, booking, portal, NO_SWITCH,
                                      by_sku(*picks)) == "pending"

    plan = orders_state.get("B1")["plan"]
    assert all(isinstance(p.get("alternative"), dict) for p in plan["parts"])  # still stored
    full = build_summary(booking, {"total_sek": plan["total_sek"]}, [], plan["parts"])
    assert len(full) > 4000                       # the test really is over the limit
    text, buttons = tg["buttons"][0]
    assert len(text) <= 4000
    assert "Alternativ för" not in text
    assert text.count("OBS: 0 i lager") == 12     # the stock notes still fit
    assert text.endswith("Godkänn för att beställa.")
    assert buttons == APPROVE_BUTTONS             # Godkänn/Avbryt only, no swap rows
    assert orders_state.get("B1")["message_id"] == 77


def test_too_long_even_without_alternatives_falls_back_to_the_plain_summary(tg):
    booking, catalogue, picks = _long_booking(title_len=200)
    portal = FakePortal(catalogue=catalogue)

    # prefer_in_stock: every line is replaced, so every line also carries "Bytt från <title>".
    assert parts_order.plan_and_stage(CONFIG, booking, portal, APPROVE,
                                      by_sku(*[p for p in picks if p.startswith("o")]
                                             + [p for p in picks if p.startswith("a")])
                                      ) == "pending"

    plan = orders_state.get("B1")["plan"]
    assert all(isinstance(p.get("replaced_from"), dict) for p in plan["parts"])
    text, buttons = tg["buttons"][0]
    assert len(text) <= 4000
    assert "Bytt från" not in text and "Alternativ för" not in text
    expected = build_summary(booking, {"total_sek": plan["total_sek"]}, [], plan["parts"],
                             alternatives=False, notes=False)
    assert text == expected + "\n\nGodkänn för att beställa."
    assert buttons == APPROVE_BUTTONS


def test_short_text_keeps_alternatives_and_swap_rows(tg):
    booking, catalogue, picks = _long_booking(n=2, title_len=20)
    portal = FakePortal(catalogue=catalogue)

    assert parts_order.plan_and_stage(CONFIG, booking, portal, NO_SWITCH,
                                      by_sku(*picks)) == "pending"

    text, buttons = tg["buttons"][0]
    assert text.count("Alternativ för") == 2
    assert buttons[0] == APPROVE_BUTTONS and len(buttons) == 3


def test_swaps_count_is_read_from_the_real_state_file(tg, orders_file):
    orders_state.queue("B1")
    orders_state.update_fields("B1", swaps=2)
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, NO_SWITCH,
                                      by_sku("p1", "p3", "p2")) == "pending"

    text, buttons = tg["buttons"][0]
    assert buttons == APPROVE_BUTTONS
    assert "Alternativ för Oljefilter" in text
    assert orders_state.get("B1")["swaps"] == 2


# ── Package 2 task 4: restage_with_pins ──────────────────────────────────────

def _staged_for_swap(tg, catalogue=None, rules=NO_SWITCH, picks=("p1", "p3", "p2")):
    """Stage B1 (Oljefilter p1 out of stock with alternative p3, Luftfilter p2), then put
    the record where approve._swap leaves it before restaging (attempted, swaps + 1,
    cart touched). Returns the record as it was when pending."""
    portal = FakePortal(catalogue=catalogue or {"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})
    assert parts_order.plan_and_stage(CONFIG, {**BOOKING, "detail_url": "https://m/b/B1"},
                                      portal, rules, by_sku(*picks)) == "pending"
    record = orders_state.get("B1")
    orders_state.put("B1", "attempted")
    orders_state.update_fields("B1", swaps=1, cart_touched=True)
    tg["buttons"].clear()
    tg["message_id"] = 78
    return record


class UnitPortal(FakePortal):
    """read_cart as pro.meca.se shows it: price_sek is the unit price ("Pris per enhet"),
    with url_or_id; `live` overrides a sku's unit price."""

    def __init__(self, live=None, **kw):
        super().__init__(**kw)
        self.live = dict(live or {})

    def read_cart(self):
        self._record("read_cart")
        if self.carts:
            return self.carts.pop(0)
        if not self.added and self.initial is not None:
            return self.initial
        lines = [{"title": c["title"], "qty": q, "url_or_id": c["url_or_id"],
                  "price_sek": self.live.get(c["url_or_id"], c["price_sek"])}
                 for c, q in self.added]
        return {"lines": lines, "total_sek": sum(x["price_sek"] * x["qty"] for x in lines)}


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, line, ranked):
        self.calls.append(line.get("name"))
        return 0, 0.95


def _restage(record, portal, rules=NO_SWITCH, index=0, pin=None, picker=None):
    booking = parts_order.swap_booking("B1", record)
    parts = record["plan"]["parts"]
    pins = {index: parts[index]["alternative"] if pin is None else pin}
    keep = {i: p for i, p in enumerate(parts) if i != index}
    return parts_order.restage_with_pins(CONFIG, portal, booking, pins, rules,
                                         picker or Recorder(), keep=keep)


def test_swap_booking_is_rebuilt_from_the_stored_plan_only(tg):
    record = _staged_for_swap(tg)
    booking = parts_order.swap_booking("B1", record)
    assert booking == {"id": "B1", "booking_nr": "OLRD-0001", "reg_nr": "TST001",
                       "parts": [{"name": "Oljefilter", "qty": 2},
                                 {"name": "Luftfilter", "qty": 1}],
                       "fluids": [{"name": "Olja 5W30", "qty": "5"}],
                       "detail_url": "https://m/b/B1", "vehicle": "VOLKSWAGEN PASSAT",
                       "customer_line": "se bokning OLRD-0001"}  # fix round 1, M-5


def test_restage_with_pins_stages_the_alternative_and_keeps_the_other_lines(tg, orders_file):
    record = _staged_for_swap(tg)
    old_parts = record["plan"]["parts"]
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})
    picker = Recorder()

    assert _restage(record, portal, picker=picker) == "pending"

    new = orders_state.get("B1")
    assert new["status"] == "pending" and new["message_id"] == 78
    assert new["swaps"] == 1 and new["cart_touched"] is True
    parts = new["plan"]["parts"]
    assert parts[0] == {"name": "Oljefilter", "title": "Mann Oljefilter W712", "brand": "Mann",
                        "price_sek": 200.0, "qty": 2, "url_or_id": "p3", "in_stock": True}
    assert parts[1] == old_parts[1]                       # the other line stays as staged
    assert [(c.get("url_or_id"), q) for c, q in portal.added] == [("p3", 2), ("p2", 1)]
    assert [x["url_or_id"] for x in new["plan"]["expected_lines"]] == ["p3", "p2"]
    assert new["plan"]["total_sek"] == 650.0
    searched = [c[1] for c in portal.calls if c[0] == "search"]
    assert searched and all("olje" not in t.casefold() for t in searched)  # no search for the pin
    assert picker.calls == []                             # nothing re-chosen
    assert "checkout" not in portal.names()
    text, buttons = tg["buttons"][0]
    assert "- Oljefilter -> Mann Oljefilter W712 (Mann) x2 à 200.00 SEK" in text
    assert "Alternativ" not in text                       # no further alternative for it
    assert buttons == APPROVE_BUTTONS
    assert "SECRET-PAGE-TEXT" not in orders_file.read_text(encoding="utf-8")
    _assert_no_pii(orders_file)


def test_restage_keeps_the_other_lines_alternative_and_its_swap_button(tg):
    dear_luft = {"title": "Bosch Luftfilter S9999", "brand": "Bosch", "price_sek": 500.0,
                 "url_or_id": "p6", "in_stock": True, "search_term": "Luftfilter"}
    cheap_luft = {"title": "Mann Luftfilter C1", "brand": "Mann", "price_sek": 150.0,
                  "url_or_id": "p7", "in_stock": True, "search_term": "Luftfilter"}
    catalogue = {"Oljefilter": [OOS, MANN], "Luftfilter": [dear_luft, cheap_luft]}
    record = _staged_for_swap(tg, catalogue=catalogue, picks=("p1", "p3", "p6", "p7"))
    assert record["plan"]["parts"][1]["alternative"]["url_or_id"] == "p7"

    assert _restage(record, FakePortal(catalogue=catalogue)) == "pending"

    parts = orders_state.get("B1")["plan"]["parts"]
    assert parts[1] == record["plan"]["parts"][1]
    _, buttons = tg["buttons"][0]
    assert buttons == [APPROVE_BUTTONS, [("Byt Luftfilter", "swap:B1:1")]]


def test_restage_after_the_second_swap_has_no_swap_buttons(tg):
    dear_luft = {"title": "Bosch Luftfilter S9999", "brand": "Bosch", "price_sek": 500.0,
                 "url_or_id": "p6", "in_stock": True}
    cheap_luft = {"title": "Mann Luftfilter C1", "brand": "Mann", "price_sek": 150.0,
                  "url_or_id": "p7", "in_stock": True}
    catalogue = {"Oljefilter": [OOS, MANN], "Luftfilter": [dear_luft, cheap_luft]}
    record = _staged_for_swap(tg, catalogue=catalogue, picks=("p1", "p3", "p6", "p7"))
    orders_state.update_fields("B1", swaps=2)

    assert _restage(record, FakePortal(catalogue=catalogue)) == "pending"

    text, buttons = tg["buttons"][0]
    assert buttons == APPROVE_BUTTONS
    assert "Alternativ för Luftfilter" in text


def _assert_swap_failed(tg, portal, reason_part=None, cart_text=None):
    """cart_text: what the message must say about the cart (fix round 1, M-1): by default
    the emptied-or-unknown text; SWAP_FAILED_FULL after every add succeeded."""
    rec = orders_state.get("B1")
    assert rec["status"] in ("blocked", "failed") and rec["cart_touched"] is True
    assert tg["buttons"] == []
    assert "checkout" not in portal.names()
    (text,) = tg["sent"]
    assert (cart_text or parts_order.SWAP_FAILED) in text and "OLRD-0001" in text
    if reason_part:
        assert reason_part in rec["plan"]["reason"]
    assert orders_state.restage("B1") is False            # never retried automatically


@pytest.mark.parametrize("pin", [
    {"title": "ProMeister Oljefilter", "brand": "ProMeister", "price_sek": 50.0,
     "url_or_id": "p9", "in_stock": True, "alt_reason": "dyr", "saving_sek": 70.0},
    {"title": "Motorolja 5W30 Castrol", "brand": "Castrol", "price_sek": 50.0,
     "url_or_id": "p9", "in_stock": True, "alt_reason": "dyr", "saving_sek": 70.0},
    {"title": "Ölfilter Ж", "brand": "Mann", "price_sek": 50.0,
     "url_or_id": "p9", "in_stock": True, "alt_reason": "dyr", "saving_sek": 70.0},
])
def test_restage_refuses_a_stored_alternative_the_filters_reject(tg, pin):
    record = _staged_for_swap(tg)
    plan = record["plan"]
    plan["parts"][0]["alternative"] = pin                 # as if stored by an older version
    orders_state.put("B1", "attempted", plan=plan)
    record = {**record, "plan": plan}
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert _restage(record, portal) in ("blocked", "failed")

    assert portal.added == []
    _assert_swap_failed(tg, portal, "inte tillåtet")


def test_restage_refuses_a_placement_that_does_not_fit_the_line(tg):
    fram = {"title": "Bosch Bromsskiva fram", "brand": "Bosch", "price_sek": 900.0,
            "url_or_id": "b1", "in_stock": False, "placement": "Framaxel"}
    bak = {"title": "Mann Bromsskiva bak", "brand": "Mann", "price_sek": 300.0,
           "url_or_id": "b2", "in_stock": True, "placement": "Bakaxel"}
    booking = {**BOOKING, "parts": [{"name": "Bromsskiva fram", "qty": "2"}]}
    portal = FakePortal(catalogue={"Bromsskiva fram": [fram], "bromsskiva fram": [fram]})
    assert parts_order.plan_and_stage(CONFIG, booking, portal, NO_SWITCH,
                                      by_sku("b1")) == "pending"
    record = orders_state.get("B1")
    record["plan"]["parts"][0]["alternative"] = {**bak, "alt_reason": "slut", "saving_sek": 0}
    orders_state.put("B1", "attempted", plan=record["plan"])
    orders_state.update_fields("B1", swaps=1, cart_touched=True)
    tg["buttons"].clear()
    portal = FakePortal(catalogue={"Bromsskiva fram": [fram, bak]})

    assert _restage(record, portal) in ("blocked", "failed")

    assert portal.added == []
    _assert_swap_failed(tg, portal, "inte tillåtet")


def test_restage_refuses_a_pin_that_is_not_the_stored_alternative(tg):
    record = _staged_for_swap(tg)
    other = {**MANN_ALT, "url_or_id": "p8", "alt_reason": "slut", "saving_sek": 0}
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert _restage(record, portal, pin=other) in ("blocked", "failed")

    assert portal.added == [] and portal.calls == []
    _assert_swap_failed(tg, portal)


def test_restage_over_the_ceiling_blocks_with_the_swap_text(tg):
    rules = Rules(("Bosch",), ("ProMeister",), 600, "approve", prefer_in_stock=False)
    record = _staged_for_swap(tg, rules=rules)            # 490 SEK staged
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert _restage(record, portal, rules=rules) in ("blocked", "failed")  # 650 SEK > 600

    _assert_swap_failed(tg, portal, "taket", cart_text=parts_order.SWAP_FAILED_FULL)
    assert parts_order.SWAP_FAILED not in tg["sent"][0]   # the cart is full, not emptied


def test_restage_verifies_the_cart_against_the_new_expected_lines(tg):
    record = _staged_for_swap(tg)
    old_cart = _cart(490.0, [{"title": "Bosch Oljefilter P7001", "qty": 2, "url_or_id": "p1"},
                             {"title": "Bosch Luftfilter S0123", "qty": 1, "url_or_id": "p2"}])
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT},
                        cart=old_cart)

    assert _restage(record, portal) in ("blocked", "failed")

    _assert_swap_failed(tg, portal, "stämmer inte", cart_text=parts_order.SWAP_FAILED_FULL)


def test_restage_fails_when_a_kept_part_is_gone(tg):
    record = _staged_for_swap(tg)
    other_luft = {**LUFT[0], "url_or_id": "p5", "title": "Mann Luftfilter X"}
    picker = Recorder()
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": [other_luft]})

    assert _restage(record, portal, picker=picker) in ("blocked", "failed")

    assert portal.added == [] and picker.calls == []      # never silently re-chosen
    _assert_swap_failed(tg, portal, "Luftfilter")


def test_restage_blocks_when_the_cart_is_not_empty(tg):
    record = _staged_for_swap(tg)
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT},
                        initial=_cart(240.0, [{"title": "x", "qty": 1}]))

    assert _restage(record, portal) in ("blocked", "failed")

    assert portal.added == []
    _assert_swap_failed(tg, portal)


def test_restage_add_failure_blocks_with_the_swap_text(tg):
    record = _staged_for_swap(tg)
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})
    portal.fail_on = "add_to_cart"

    assert _restage(record, portal) in ("blocked", "failed")

    _assert_swap_failed(tg, portal, cart_text="Inget lades i varukorgen.")
    assert parts_order.SWAP_FAILED not in tg["sent"][0]


def test_restage_portal_error_blocks_with_the_swap_text(tg):
    record = _staged_for_swap(tg)
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})
    portal.fail_on = "login"

    assert _restage(record, portal) in ("blocked", "failed")

    _assert_swap_failed(tg, portal)


def test_restage_approval_message_not_sent_is_not_pending(tg, monkeypatch):
    record = _staged_for_swap(tg)
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})
    tg["message_id"] = None

    assert _restage(record, portal) in ("blocked", "failed")

    rec = orders_state.get("B1")
    assert rec["status"] != "pending" and rec["cart_touched"] is True
    assert "checkout" not in portal.names()


def test_restage_never_orders_even_in_auto_mode(tg):
    record = _staged_for_swap(tg)
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert _restage(record, portal, rules=AUTO) == "pending"

    assert "checkout" not in portal.names()
    assert len(tg["buttons"]) == 1


@pytest.mark.parametrize("change,why", [
    ({"swaps": 2}, "byten"),
    ({"index": 1}, "alternativ"),
    ({"index": 2}, "rad"),
    ({"index": -1}, "rad"),
    ({"index": True}, "rad"),
])
def test_swap_problem_refuses(tg, change, why):
    record = _staged_for_swap(tg)
    record = {**record, "swaps": change.get("swaps", 0)}
    assert why in parts_order.swap_problem(record, change.get("index", 0), NO_SWITCH)


def test_swap_problem_accepts_a_valid_swap(tg):
    record = _staged_for_swap(tg)
    assert parts_order.swap_problem(record, 0, NO_SWITCH) is None
    assert parts_order.swap_problem({**record, "swaps": 1}, 0, NO_SWITCH) is None


def test_swap_problem_checks_the_ceiling_estimate(tg):
    record = _staged_for_swap(tg)                       # 490 -> about 650 SEK after the swap
    low = Rules(("Bosch",), ("ProMeister",), 600, "approve", prefer_in_stock=False)
    assert "taket" in parts_order.swap_problem(record, 0, low)


@pytest.mark.parametrize("alt", [
    {"title": "ProMeister Oljefilter", "brand": "ProMeister", "url_or_id": "p9"},
    {"title": "Motorolja 5W30 Castrol", "brand": "Castrol", "url_or_id": "p9"},
    {"title": "Mann Oljefilter", "brand": "Mann", "url_or_id": ""},
])
def test_swap_problem_runs_the_matcher_filters(tg, alt):
    record = _staged_for_swap(tg)
    record["plan"]["parts"][0]["alternative"] = {**alt, "price_sek": 50.0, "in_stock": True,
                                                 "alt_reason": "dyr", "saving_sek": 70.0}
    assert "inte tillåtet" in parts_order.swap_problem(record, 0, NO_SWITCH)


# ── Task 4 fix round 1 ───────────────────────────────────────────────────────

KLOSS_FRAM = {"title": "Bosch Bromsklossar", "brand": "Bosch", "price_sek": 900.0,
              "url_or_id": "k1", "in_stock": False, "placement": "Framaxel",
              "search_term": "Bromsklossar fram"}
KLOSS_ALT = {"title": "Mann Bromsklossar", "brand": "Mann", "price_sek": 400.0,
             "url_or_id": "k2", "in_stock": True, "placement": "Framaxel",
             "search_term": "Bromsklossar fram"}
KLOSS_BOOKING = {**BOOKING, "parts": [{"name": "Bromsklossar fram", "qty": "1"}], "fluids": []}


def _kloss_catalogue():
    return {term: [KLOSS_FRAM, KLOSS_ALT] for term in
            ("Bromsklossar fram", "bromsklossar fram", "bromsklossar", "Bromsklossar")}


def test_alternative_keeps_its_placement_and_the_swap_works_end_to_end(tg):
    # I-1: the axle is only in the shop's placement field, not in the title.
    portal = FakePortal(catalogue=_kloss_catalogue())
    assert parts_order.plan_and_stage(CONFIG, KLOSS_BOOKING, portal, NO_SWITCH,
                                      by_sku("k1", "k2")) == "pending"
    record = orders_state.get("B1")
    alt = record["plan"]["parts"][0]["alternative"]
    assert alt["placement"] == "Framaxel"
    assert parts_order.pin_candidate(alt)["placement"] == "Framaxel"
    _, buttons = tg["buttons"][0]
    assert buttons == [APPROVE_BUTTONS, [("Byt Bromsklossar fram", "swap:B1:0")]]
    assert parts_order.swap_problem(record, 0, NO_SWITCH) is None
    orders_state.put("B1", "attempted")
    orders_state.update_fields("B1", swaps=1, cart_touched=True)
    tg["buttons"].clear()

    portal = UnitPortal(catalogue=_kloss_catalogue())
    booking = parts_order.swap_booking("B1", record)
    assert parts_order.restage_with_pins(CONFIG, portal, booking, {0: alt}, NO_SWITCH,
                                         Recorder()) == "pending"
    assert _added(portal) == ["k2"]
    assert orders_state.get("B1")["plan"]["parts"][0]["url_or_id"] == "k2"


def test_alternative_that_cannot_be_pinned_shows_text_but_no_swap_button(tg):
    # A candidate the matcher may choose but a swap could never add (no url_or_id).
    no_id = {**MANN, "url_or_id": ""}
    portal = FakePortal(catalogue={"Oljefilter": [OOS, no_id], "Luftfilter": LUFT})

    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, NO_SWITCH,
                                      lambda line, ranked: (len(ranked) - 1, 0.95)
                                      if line["name"] == "Oljefilter" and len(ranked) == 1
                                      else (0, 0.95)) == "pending"

    part = orders_state.get("B1")["plan"]["parts"][0]
    assert isinstance(part.get("alternative"), dict)
    text, buttons = tg["buttons"][0]
    assert "Alternativ för Oljefilter: Mann Oljefilter W712" in text
    assert buttons == APPROVE_BUTTONS


def test_swap_buttons_skip_an_alternative_the_pin_check_rejects():
    parts = [{"name": "Oljefilter", "alternative": {"title": "ProMeister X", "brand": "ProMeister",
                                                    "url_or_id": "p9"}},
             {"name": "Luftfilter", "alternative": {"title": "Mann C1", "brand": "Mann",
                                                    "url_or_id": "p7"}}]
    assert parts_order._swap_buttons("B1", parts, 0, NO_SWITCH) == [
        [("Byt Luftfilter", "swap:B1:1")]]


def test_restage_with_a_past_deadline_fails_with_the_swap_text(tg, stage_clock):
    # I-2: the swap's own budget, counted by the caller from the start of the swap.
    record = _staged_for_swap(tg)
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    booking = parts_order.swap_booking("B1", record)
    parts = record["plan"]["parts"]
    status = parts_order.restage_with_pins(CONFIG, portal, booking,
                                           {0: parts[0]["alternative"]}, NO_SWITCH,
                                           Recorder(), keep={1: parts[1]},
                                           deadline=stage_clock["t"] - 1)

    assert status in ("blocked", "failed")
    assert portal.added == [] and portal.calls == []
    _assert_swap_failed(tg, portal)


def test_restage_deadline_while_adding_lists_what_is_in_the_cart(tg, stage_clock):
    record = _staged_for_swap(tg)

    class Slow(FakePortal):
        def add_to_cart(self, candidate, qty):
            super().add_to_cart(candidate, qty)
            stage_clock["t"] += 100

    portal = Slow(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})
    booking = parts_order.swap_booking("B1", record)
    parts = record["plan"]["parts"]
    status = parts_order.restage_with_pins(CONFIG, portal, booking,
                                           {0: parts[0]["alternative"]}, NO_SWITCH,
                                           Recorder(), keep={1: parts[1]},
                                           deadline=stage_clock["t"] + 50)

    assert status in ("blocked", "failed")
    assert _added(portal) == ["p3"]
    _assert_swap_failed(tg, portal, cart_text="i varukorgen: Mann Oljefilter W712 x2")
    assert "Inte försökt: Luftfilter" in tg["sent"][0]


def test_restage_partial_add_says_which_parts_are_in_the_cart(tg):
    record = _staged_for_swap(tg)

    class OneFails(FakePortal):
        def add_to_cart(self, candidate, qty):
            if candidate["url_or_id"] == "p2":
                self._record("add_to_cart", "p2", qty)
                raise RuntimeError("add broke")
            super().add_to_cart(candidate, qty)

    portal = OneFails(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert _restage(record, portal) in ("blocked", "failed")

    _assert_swap_failed(tg, portal, cart_text="i varukorgen: Mann Oljefilter W712 x2")
    text = tg["sent"][0]
    assert "Bytet misslyckades" in text and "Kunde inte läggas till: Luftfilter" in text
    assert parts_order.SWAP_FAILED not in text


def test_swapped_line_shows_the_live_cart_unit_price(tg):
    # M-4: the stored alternative said 200.00; the cart says 210.00 per unit.
    record = _staged_for_swap(tg)
    portal = UnitPortal(live={"p3": 210.0},
                        catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert _restage(record, portal) == "pending"

    rec = orders_state.get("B1")
    assert rec["plan"]["parts"][0]["price_sek"] == 210.0
    assert rec["plan"]["total_sek"] == 670.0
    text, _ = tg["buttons"][0]
    assert "- Oljefilter -> Mann Oljefilter W712 (Mann) x2 à 210.00 SEK\n" in text


def test_swapped_line_falls_back_to_the_stored_price_without_a_live_line(tg):
    record = _staged_for_swap(tg)
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT},
                        cart=_cart(650.0, [{"title": "Mann Oljefilter W712", "qty": 2},
                                           {"title": "Bosch Luftfilter S0123", "qty": 1}]))

    assert _restage(record, portal) == "pending"

    assert orders_state.get("B1")["plan"]["parts"][0]["price_sek"] == 200.0


def test_swapped_line_stock_note_follows_live_stock_info(tg):
    record = _staged_for_swap(tg)
    plan = record["plan"]
    plan["parts"][0]["alternative"]["in_stock"] = False
    orders_state.put("B1", "attempted", plan=plan)
    lines = [{"title": "Mann Oljefilter W712", "qty": 2, "url_or_id": "p3", "price_sek": 200.0,
              "in_stock": True},
             {"title": "Bosch Luftfilter S0123", "qty": 1, "url_or_id": "p2",
              "price_sek": 250.0}]
    portal = FakePortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT},
                        cart=_cart(650.0, lines))

    assert _restage(record, portal) == "pending"

    text, _ = tg["buttons"][0]
    assert "OBS: 0 i lager" not in text
    assert orders_state.get("B1")["plan"]["parts"][0]["in_stock"] is True


def test_swapped_line_keeps_the_stored_out_of_stock_note_without_live_info(tg):
    record = _staged_for_swap(tg)
    plan = record["plan"]
    plan["parts"][0]["alternative"]["in_stock"] = False
    orders_state.put("B1", "attempted", plan=plan)
    portal = UnitPortal(catalogue={"Oljefilter": [OOS, MANN], "Luftfilter": LUFT})

    assert _restage(record, portal) == "pending"

    text, _ = tg["buttons"][0]
    assert "- Oljefilter -> Mann Oljefilter W712 (Mann) x2 à 200.00 SEK – OBS: 0 i lager" in text


def test_swap_message_points_to_the_booking_for_the_customer(tg):
    # M-5: the customer name is not stored, so the line points to the booking.
    record = _staged_for_swap(tg)

    assert _restage(record, UnitPortal(catalogue={"Oljefilter": [OOS, MANN],
                                                  "Luftfilter": LUFT})) == "pending"

    text, _ = tg["buttons"][0]
    assert "\nKund: se bokning OLRD-0001\n" in text
    assert "Kund: –" not in text


def test_alternative_placement_is_stored_clean_and_only_when_present():
    from parts_order_helpers import alternative_entry
    base = {"title": "Mann X", "brand": "Mann", "price_sek": 1.0, "url_or_id": "x"}
    assert "placement" not in alternative_entry(base)
    assert "placement" not in alternative_entry({**base, "placement": "  "})
    assert alternative_entry({**base, "placement": " Fram\naxel​ "})["placement"] == "Fram axel"
    assert len(alternative_entry({**base, "placement": "F" * 500})["placement"]) == 80


# ── Task 5: immediate verification of the placed order ───────────────────────

UNVERIFIED_REF = "UNVERIFIED-20261022T101010Z"
LATER_LINE = "Ordernummer: bekräftas senare (kolla Skickade Beställningar i pro.meca.se)"


def _today_se():
    from zoneinfo import ZoneInfo
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Stockholm")).date().isoformat()


def _today_row(supplier_no="S-91001", order_numbers="71001/71002", reg_nr="TST001", **kw):
    return {**_sent_row(supplier_no=supplier_no, order_numbers=order_numbers, reg_nr=reg_nr,
                        **kw), "date": _today_se()}


def _verified_text(row=None, label="OLRD-0001"):
    from order_match import VERIFIED_TEXT
    row = row or _today_row()
    return VERIFIED_TEXT.format(label=label, model=row["model"],
                                        numbers=f"{row['supplier_no']} ({row['order_numbers']})")


class Polled(FakePortal):
    """list_sent_orders returns the next entry of `polls` (rows, or an exception to raise);
    the duplicate-order guard during staging sees no rows."""

    def __init__(self, polls=(), **kw):
        kw.setdefault("ref", UNVERIFIED_REF)
        super().__init__(**kw)
        self.polls = list(polls)
        self.staged = False

    def list_sent_orders(self):
        self.sent_calls += 1
        if not self.staged:
            return []
        item = self.polls.pop(0) if self.polls else []
        if isinstance(item, BaseException):
            raise item
        return [dict(r) for r in item]


def _polled(polls):
    portal = Polled(polls=polls)
    portal.staged = True
    return portal


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(parts_order, "_sleep", lambda s: calls.append(s))
    return calls


def _place(tg, portal, rules=APPROVE, **kw):
    plan = _staged_plan(tg, portal)
    portal.staged = True
    portal.sent_calls = 0
    return parts_order.execute_order(CONFIG, portal, "B1", plan, rules, **kw)


def _ordered_record(**kw):
    return {"status": "ordered", "plan": {"reg_nr": "TST001"},
            "vehicle_model": "VOLKSWAGEN PASSAT",
            "updated_at": datetime.now(timezone.utc).isoformat(), **kw}


def _verify(portal, record=None, others=(), **kw):
    kw.setdefault("sleep", lambda s: None)
    return parts_order.verify_now(portal, "B1", record or _ordered_record(), list(others), **kw)


def test_unique_plate_match_on_the_second_poll_is_stored_and_sent_separately(tg, sleeps):
    row = _today_row()
    portal = Polled(polls=[[], [row]])

    assert _place(tg, portal) == "ordered"

    assert portal.sent_calls == 2
    assert sleeps == [10]
    rec = orders_state.get("B1")
    assert rec["status"] == "ordered"
    assert rec["verified_numbers"] == numbers_text(row) == "S-91001 · 71001/71002"
    assert datetime.fromisoformat(rec["verified_at"]).tzinfo is not None
    # the Beställd message is exactly the pre-verification text; the check is its own message
    assert len(tg["sent"]) == 2
    assert tg["sent"][0].splitlines()[:2] == ["Beställd: OLRD-0001 (TST001)", LATER_LINE]
    assert "verifierad" not in tg["sent"][0]
    assert tg["sent"][1] == _verified_text(row)
    assert parts_order.order_notify_failed("B1") is False


def test_no_match_after_three_polls_sends_only_the_ordered_message(tg, sleeps):
    portal = Polled(polls=[[], [], []])

    assert _place(tg, portal) == "ordered"

    assert portal.sent_calls == 3
    assert sleeps == [10, 10]                       # no sleep after the last try
    rec = orders_state.get("B1")
    assert "verified_at" not in rec and "verified_numbers" not in rec
    assert len(tg["sent"]) == 1 and LATER_LINE in tg["sent"][0].splitlines()


def test_verify_now_returns_none_after_all_tries():
    calls = []
    portal = _polled([[], [], []])
    assert _verify(portal, sleep=calls.append) is None
    assert portal.sent_calls == 3 and calls == [10, 10]


def test_verify_now_without_sleep_uses_the_module_sleep(sleeps):
    portal = _polled([[], [], []])
    assert parts_order.verify_now(portal, "B1", _ordered_record(), []) is None
    assert sleeps == [10, 10]


def test_two_candidate_rows_are_ambiguous(tg, sleeps):
    rows = [_today_row(), _today_row(supplier_no="S-91002", order_numbers="71003")]
    portal = Polled(polls=[rows, rows, rows])

    assert _place(tg, portal) == "ordered"

    assert "verified_at" not in orders_state.get("B1")
    assert len(tg["sent"]) == 1


def test_another_records_claimed_numbers_are_excluded(tg, sleeps):
    orders_state.put("B2", "ordered", plan={"reg_nr": "TST001"})
    orders_state.update_fields("B2", verified_at=datetime.now(timezone.utc).isoformat(),
                               verified_numbers="S-91001 · 71001/71002")
    rows = [_today_row(), _today_row(supplier_no="S-91002", order_numbers="71003")]
    portal = Polled(polls=[rows])

    assert _place(tg, portal) == "ordered"

    assert orders_state.get("B1")["verified_numbers"] == "S-91002 · 71003"
    assert sleeps == []


def test_verify_now_excludes_claimed_numbers_directly():
    rows = [_today_row(), _today_row(supplier_no="S-91002", order_numbers="71003")]
    others = [("B2", _ordered_record(verified_at=datetime.now(timezone.utc).isoformat(),
                                     verified_numbers="S-91001 · 71001/71002"))]
    assert _verify(_polled([rows]), others=others) == "S-91002 · 71003"


def test_same_plate_row_before_the_order_date_is_excluded():
    assert _verify(_polled([[_sent_row(days_ago=1)]] * 3)) is None


def test_without_the_registration_column_nothing_is_verified():
    # Reviewer probe: a foreign hand-placed order for the same model, no registration
    # column, this order's own row not listed yet -> never taken as this order's number.
    foreign = _today_row(reg_nr=None, supplier_no="S-1", order_numbers="0168",
                         model="VOLKSWAGEN, Passat Variant")
    portal = _polled([[foreign]] * 3)
    assert _verify(portal) is None
    assert portal.sent_calls == 1                   # no column: no point in reading again


def test_without_the_registration_column_nothing_is_stored(tg, sleeps):
    portal = Polled(polls=[[_today_row(reg_nr=None)]] * 3)

    assert _place(tg, portal) == "ordered"

    assert "verified_at" not in orders_state.get("B1")
    assert len(tg["sent"]) == 1


def test_a_record_without_a_plate_is_not_verified():
    portal = _polled([[_today_row()]])
    assert _verify(portal, _ordered_record(plan={"reg_nr": "  "})) is None
    assert portal.sent_calls == 0


def test_another_plate_does_not_match():
    assert _verify(_polled([[_today_row(reg_nr="XYZ999")]] * 3)) is None


def test_a_real_reference_must_be_in_the_row():
    row = _today_row()
    assert _verify(_polled([[row]] * 3), ref="4500012345") is None
    assert _verify(_polled([[row]]), ref="71002") == numbers_text(row)
    assert _verify(_polled([[row]]), ref="S-91001") == numbers_text(row)


def test_an_unverified_placeholder_needs_no_cross_check():
    row = _today_row()
    assert _verify(_polled([[row]]), ref=UNVERIFIED_REF) == numbers_text(row)
    assert _verify(_polled([[row]]), ref=None) == numbers_text(row)


def test_ref_mismatch_stores_nothing(tg, sleeps):
    portal = Polled(polls=[[_today_row()]] * 3, ref="4500012345")

    assert _place(tg, portal) == "ordered"

    assert "verified_at" not in orders_state.get("B1")
    assert tg["sent"][0].splitlines()[1] == "Ordernummer: 4500012345"
    assert len(tg["sent"]) == 1


def test_ref_in_the_row_is_stored(tg, sleeps):
    portal = Polled(polls=[[_today_row()]], ref="71001")
    assert _place(tg, portal) == "ordered"
    assert orders_state.get("B1")["verified_numbers"] == "S-91001 · 71001/71002"
    assert tg["sent"][0].splitlines()[1] == "Ordernummer: 71001"


def test_verify_now_another_unverified_order_for_the_car_is_not_guessed():
    # The checker resolves all unverified orders together: one row, two orders -> waiting.
    assert _verify(_polled([[_today_row()]] * 3), others=[("B2", _ordered_record())]) is None


@pytest.mark.parametrize("error", [PartsPortalError("SECRET-PAGE-TEXT", "parse"),
                                   AttributeError("no list_sent_orders")])
def test_list_sent_orders_raising_keeps_the_order_and_message(tg, sleeps, error, caplog):
    portal = Polled(polls=[error], ref="ORD-9")

    assert _place(tg, portal) == "ordered"

    rec = orders_state.get("B1")
    assert rec["status"] == "ordered" and "verified_at" not in rec
    assert rec["plan"]["confirmation"] == "ORD-9"
    assert tg["sent"] == [tg["sent"][0]]
    assert tg["sent"][0].splitlines()[:2] == ["Beställd: OLRD-0001 (TST001)",
                                              "Ordernummer: ORD-9"]
    assert parts_order.order_notify_failed("B1") is False
    assert "SECRET-PAGE-TEXT" not in caplog.text


def test_portal_without_list_sent_orders_is_swallowed():
    class Bare:
        pass
    assert _verify(Bare()) is None


def test_verify_budget_exceeded_stops_polling(stage_clock):
    class Slow(Polled):
        def list_sent_orders(self):
            stage_clock["t"] += 50
            return super().list_sent_orders()
    portal = Slow(polls=[[], [], []])
    portal.staged = True
    calls = []
    assert _verify(portal, sleep=calls.append) is None
    # polls at 0 (-> 50) and 60 (-> 110): past the 90 s budget, no third poll
    assert portal.sent_calls == 2 and calls == [10]
    assert parts_order.VERIFY_BUDGET_S == 90


def test_exhausted_run_deadline_skips_verification(tg, sleeps, stage_clock):
    portal = Polled(polls=[[_today_row()]])

    assert _place(tg, portal, deadline=stage_clock["t"] - 1) == "ordered"

    assert portal.sent_calls == 0                   # never read after the order
    assert orders_state.get("B1")["status"] == "ordered"
    assert len(tg["sent"]) == 1 and tg["sent"][0].startswith("Beställd: OLRD-0001")
    assert _verify(_polled([[_today_row()]]), deadline=stage_clock["t"] - 1) is None


def test_the_checker_skips_a_record_verified_right_after_checkout(tg, sleeps):
    import approve
    assert _place(tg, Polled(polls=[[_today_row()]])) == "ordered"
    sent = len(tg["sent"])

    class Untouchable:
        def get(self):
            raise AssertionError("the checker must not open the portal")

    assert approve._to_verify() == []
    assert approve._verify_orders(CONFIG, Untouchable(), datetime.now(timezone.utc)) is False
    assert len(tg["sent"]) == sent


def test_ordered_message_not_delivered_is_notify_failed_and_still_verified(tg, sleeps):
    portal = Polled(polls=[[_today_row()]])
    plan = _staged_plan(tg, portal)
    portal.staged = True
    tg["send_ok"] = False

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "ordered"

    rec = orders_state.get("B1")
    assert rec["plan"]["notify_failed"] is True
    assert rec["verified_numbers"] == "S-91001 · 71001/71002"
    assert parts_order.order_notify_failed("B1") is True


def test_verified_message_not_delivered_is_kept_and_not_resent(tg, sleeps, monkeypatch):
    plain = []

    def failing(config, text):
        plain.append(text)
        return False

    monkeypatch.setattr(notifier, "send_message", failing)
    assert _place(tg, Polled(polls=[[_today_row()]])) == "ordered"

    rec = orders_state.get("B1")
    assert rec["verified_numbers"] == "S-91001 · 71001/71002"
    assert plain == [_verified_text()]                # tried once
    assert parts_order.order_notify_failed("B1") is False   # Beställd was delivered
    assert tg["sent"][0].startswith("Beställd: OLRD-0001")


def test_ordered_state_and_message_come_before_verification(tg, sleeps, monkeypatch):
    events = []
    real_put = orders_state.put
    real_buttons = notifier.send_with_buttons

    def put(booking_id, status, *a, **kw):
        events.append(("put", status))
        return real_put(booking_id, status, *a, **kw)

    def buttons(config, text, rows):
        events.append(("message", text.splitlines()[0]))
        return real_buttons(config, text, rows)

    class Recording(Polled):
        def list_sent_orders(self):
            if self.staged:
                events.append(("list_sent_orders", orders_state.get("B1")["status"]))
            return super().list_sent_orders()

    portal = Recording(polls=[[_today_row()]])
    plan = _staged_plan(tg, portal)
    portal.staged = True
    monkeypatch.setattr(orders_state, "put", put)
    monkeypatch.setattr(notifier, "send_with_buttons", buttons)

    assert parts_order.execute_order(CONFIG, portal, "B1", plan, APPROVE) == "ordered"

    assert events == [("put", "attempted"), ("put", "ordered"),
                      ("message", "Beställd: OLRD-0001 (TST001)"),
                      ("list_sent_orders", "ordered")]


def test_auto_mode_verifies_too(tg, sleeps):
    class AutoPolled(Polled):
        def checkout(self, *, max_total_sek):
            self.staged = True
            return super().checkout(max_total_sek=max_total_sek)

    portal = AutoPolled(polls=[[_today_row()]])
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, AUTO, confident) == "ordered"
    assert orders_state.get("B1")["verified_numbers"] == "S-91001 · 71001/71002"
    assert tg["sent"][-1] == _verified_text()


def test_auto_mode_past_the_staging_deadline_does_not_verify(tg, sleeps, stage_clock):
    class AutoPolled(Polled):
        def checkout(self, *, max_total_sek):
            self.staged = True
            stage_clock["t"] += parts_order.STAGE_BUDGET_S + 1
            return super().checkout(max_total_sek=max_total_sek)

    portal = AutoPolled(polls=[[_today_row()]])
    assert parts_order.plan_and_stage(CONFIG, BOOKING, portal, AUTO, confident) == "ordered"
    assert portal.sent_calls == 1                   # the duplicate guard only
    assert "verified_at" not in orders_state.get("B1")


def test_approve_passes_the_run_deadline(tg, monkeypatch, stage_clock):
    import approve
    seen = {}

    def fake_execute(config, portal, booking_id, plan, rules, deadline=None):
        seen["deadline"] = deadline
        return "ordered"

    class Session:
        def get(self):
            return object()

    monkeypatch.setattr(parts_order, "execute_order", fake_execute)
    monkeypatch.setattr(notifier, "answer_callback", lambda *a, **k: None)
    orders_state.put("B1", "pending", plan={"reg_nr": "TST001"}, message_id=5)
    record = orders_state.get("B1")
    monkeypatch.setattr(approve, "_run_start", stage_clock["t"] - 100)

    approve._approve(CONFIG, Session(), "B1", record, APPROVE, "cb")

    assert seen["deadline"] == stage_clock["t"] - 100 + approve.VERIFY_START_S
    assert approve.VERIFY_START_S == 270
    monkeypatch.setattr(approve, "_run_start", None)   # not set: counted from the tap
    approve._approve(CONFIG, Session(), "B1", record, APPROVE, "cb")
    assert seen["deadline"] == stage_clock["t"] + approve.VERIFY_START_S


def test_out_of_time_error_names_the_budget_in_use(tg, stage_clock):
    orders_state.put("B1", "attempted")
    with pytest.raises(PartsPortalError, match="240 s"):
        parts_order._stage(CONFIG, BOOKING, FakePortal(), APPROVE, confident, pins={},
                           keep={}, deadline=stage_clock["t"] - 1)
    with pytest.raises(PartsPortalError, match="480 s"):
        parts_order._stage(CONFIG, BOOKING, Clocked(stage_clock, per_search=500), APPROVE,
                           confident)
