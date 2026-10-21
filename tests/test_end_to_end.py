"""End to end with fakes: agent stages a parts order, the owner approves it on Telegram,
approve.py checks it out, the owner taps Klar and approve.py marks the booking Hanterad.
Real agent/parts_order/approve/orders_state code; only the pro.meca.se portal, the MECA
Hanterad step, the picker, Telegram and config/rules loading are faked."""
import sys
import types

import pytest

import agent
import approve
import config_loader
import notifier
import orders_state
import rules
from rules import Rules

CONFIG = {
    "telegram": {"bot_token": "123:ABC", "chat_id": "42"},
    "anthropic": {"api_key": "sk-test"},
    "parts_portal": {"username": "u", "password": "p"},
}
RULES = Rules(("Bosch",), ("ProMeister",), 10000, "approve")
PII = ("Sentinel Kund AB", "+46700000999", "sentinel@example.com", "Sentinel meddelande 42")

BOOKING = {
    "id": "B1", "booking_type": "Bokning", "booking_nr": "OLRD-0001",
    "driver_name": "Sentinel Kund AB", "driver_phone": "+46700000999",
    "driver_email": "sentinel@example.com", "booking_msg": "Sentinel meddelande 42",
    "reg_nr": "TST001", "vehicle": "VOLKSWAGEN PASSAT",
    "parts": [{"name": "Oljefilter", "qty": "1"}], "fluids": [{"name": "Olja 5W30", "qty": "5"}],
}
OIL_FILTER = {"title": "Bosch Oljefilter, Filterinsats", "brand": "", "price_sek": 189.0,
              "url_or_id": "10000001", "article_nr": "1025-L1075", "in_stock": True,
              "placement": ""}
HENGST = {**OIL_FILTER, "title": "Hengst Oljefilter", "url_or_id": "10000002"}


class FakeMecaPortal:
    """pro.meca.se with one cart per vehicle; cart lines carry the product id."""

    def __init__(self):
        self.vehicle = None
        self.carts = {}
        self.calls = []
        self.ceilings = []
        self.orders = []

    def login(self):
        self.calls.append("login")

    def load_vehicle(self, reg_nr):
        self.calls.append("load_vehicle")
        self.vehicle = reg_nr

    def search(self, term):
        self.calls.append("search")
        return [dict(HENGST), dict(OIL_FILTER)] if term == "Oljefilter" else []

    def add_to_cart(self, candidate, qty):
        self.calls.append("add_to_cart")
        # The cart shows its own title form; matching must go by id, not title.
        self.carts.setdefault(self.vehicle, []).append(
            {"title": candidate["title"].upper() + " (1025-L1075)", "brand": "",
             "qty": qty, "price_sek": candidate["price_sek"] * qty,
             "url_or_id": candidate["url_or_id"]})

    def read_cart(self):
        self.calls.append("read_cart")
        lines = [dict(line) for line in self.carts.get(self.vehicle, [])]
        return {"lines": lines, "total_sek": sum(line["price_sek"] for line in lines)}

    def checkout(self, *, max_total_sek):
        self.calls.append("checkout")
        self.ceilings.append(max_total_sek)
        self.orders.append(self.carts.pop(self.vehicle, []))
        return "ORD-555"

    def clear_cart(self, reg_nr=None):
        self.calls.append("clear_cart")
        self.carts.pop(reg_nr, None)


class Session:
    """Context manager around the shared fake portal (a fresh 'browser' per run)."""

    def __init__(self, portal):
        self.portal = portal

    def __enter__(self):
        return self.portal

    def __exit__(self, *exc):
        return False


@pytest.fixture
def world(monkeypatch, tmp_path):
    orders_file = tmp_path / "config" / "orders.json"
    monkeypatch.setattr(orders_state, "ORDERS_FILE", orders_file)
    portal = FakeMecaPortal()
    rec = {"portal": portal, "sent": [], "buttons": [], "klar": [], "answers": [], "cleared": [],
           "updates": [], "offsets": [], "picked": [], "orders_file": orders_file,
           "hanterad": []}

    def send(config, text):
        rec["sent"].append(text)
        return True

    def send_with_buttons(config, text, buttons):
        if buttons and all(data.startswith("klar:") for _, data in buttons):
            rec["sent"].append(text)  # a resolution message: delivered text, plus a Klar button
            rec["klar"].append((text, buttons))
            return 600 + len(rec["klar"])
        rec["buttons"].append((text, buttons))
        return 555

    def mark_handled(config, detail_url):
        rec["hanterad"].append(detail_url)
        return True

    def get_updates(config, offset=None):
        rec["offsets"].append(offset)
        return [u for u in rec["updates"] if offset is None or u["update_id"] >= offset]

    def picker(line, ranked):
        rec["picked"].append([c["url_or_id"] for c in ranked])
        return 0, 0.95

    monkeypatch.setattr(notifier, "send_message", send)
    monkeypatch.setattr(agent, "send_message", send)
    monkeypatch.setattr(notifier, "send_with_buttons", send_with_buttons)
    monkeypatch.setattr(notifier, "get_updates", get_updates)
    monkeypatch.setattr(notifier, "answer_callback",
                        lambda config, cb, text: rec["answers"].append((cb, text)))
    monkeypatch.setattr(notifier, "clear_buttons",
                        lambda config, mid: rec["cleared"].append(mid))
    monkeypatch.setattr(agent, "load_rules", lambda: RULES)
    monkeypatch.setattr(agent, "make_claude_picker", lambda api_key: picker)
    monkeypatch.setitem(sys.modules, "parts_portal",
                        types.SimpleNamespace(PartsPortal=lambda config: Session(portal)))
    monkeypatch.setattr(config_loader, "load_config", lambda: CONFIG)
    monkeypatch.setattr(rules, "load_rules", lambda *a, **k: RULES)
    monkeypatch.setattr(approve, "make_portal", lambda config: Session(portal))
    monkeypatch.setattr(approve, "mark_handled", mark_handled)
    return rec


def tap(update_id, message_id=555, data="approve:B1"):
    return {"update_id": update_id, "chat_id": "42", "user_id": "42",
            "callback_id": f"cb{update_id}", "data": data, "message_id": message_id}


DETAIL_URL = "https://portal.example/Booking/Details?BookingID=B1"


def test_stage_approve_and_order_end_to_end(world):
    portal = world["portal"]

    # ── agent: stage ──
    orders_state.queue("B1")
    assert agent._order_parts(CONFIG, [{**BOOKING, "detail_url": DETAIL_URL}]) == (1, 0)

    record = orders_state.get("B1")
    assert record["status"] == "pending"
    assert record["message_id"] == 555
    assert record["detail_url"] == DETAIL_URL
    assert "klar_message_id" not in record  # waiting for approval: not resolved, no Klar
    assert world["picked"] == [["10000001"]]  # Bosch offered first, alone
    plan = record["plan"]
    assert plan["expected_lines"] == [
        {"title": "Bosch Oljefilter, Filterinsats", "qty": 1, "url_or_id": "10000001"}]
    assert plan["total_sek"] == 189.0
    (text, buttons), = world["buttons"]
    assert buttons == [("Godkänn", "approve:B1"), ("Avbryt", "cancel:B1")]
    assert "- Oljefilter -> Bosch Oljefilter, Filterinsats (–) x1 à 189.00 SEK" in text
    assert "- Olja 5W30 x5" in text
    assert portal.carts["TST001"][0]["url_or_id"] == "10000001"
    assert "checkout" not in portal.calls
    for sentinel in PII:
        assert sentinel not in world["orders_file"].read_text(encoding="utf-8")

    # ── approve.py: the owner taps Godkänn ──
    world["updates"] = [tap(1001)]
    assert approve.main([]) == 0

    record = orders_state.get("B1")
    assert record["status"] == "ordered"
    assert record["plan"]["confirmation"] == "ORD-555"
    assert record["message_id"] == 555
    assert portal.calls.count("checkout") == 1
    assert portal.ceilings == [10000.0]
    assert [line["url_or_id"] for line in portal.orders[0]] == ["10000001"]
    assert world["cleared"] == [555]
    assert world["answers"] == [("cb1001", "Beställer…")]
    (done,) = world["sent"]
    assert done.startswith("Beställd: OLRD-0001 (TST001)\nOrdernummer: ORD-555")
    assert "- Oljefilter -> Bosch Oljefilter, Filterinsats" in done
    assert orders_state.last_update_id() == 1001

    # The "Beställd" message carries the Klar button; the booking is not Hanterad yet.
    (klar_text, klar_buttons), = world["klar"]
    assert klar_text == done and klar_buttons == [("Klar", "klar:B1")]
    assert record["klar_message_id"] == 601
    assert world["hanterad"] == []
    assert orders_state.outstanding_klar() == [("B1", record)]

    # ── a second identical Godkänn tap (the Klar is outstanding, so the poller runs) ──
    world["updates"] = [tap(1001), tap(1002)]
    assert approve.main([]) == 0
    assert world["offsets"][-1] == 1002
    assert world["answers"][-1] == ("cb1002", "Redan hanterad.")
    assert portal.calls.count("checkout") == 1
    assert len(world["sent"]) == 1
    assert orders_state.get("B1")["status"] == "ordered"
    assert orders_state.last_update_id() == 1002

    # ── the owner taps Klar: the next poll marks the booking Hanterad ──
    world["updates"] = [tap(1003, message_id=601, data="klar:B1")]
    assert approve.main([]) == 0
    assert world["hanterad"] == [DETAIL_URL]
    record = orders_state.get("B1")
    assert record["handled"] is True and record["status"] == "ordered"
    assert world["answers"][-1] == ("cb1003", "Markerar som Hanterad…")
    assert world["cleared"] == [555, 601]
    assert world["sent"][-1] == "Klar: bokning OLRD-0001 är markerad som Hanterad."
    assert orders_state.outstanding_klar() == []
    assert portal.calls.count("checkout") == 1

    # ── a second Klar tap: already handled, nothing pressed ──
    assert approve.handle_updates(CONFIG, lambda: Session(portal), RULES) == 0  # replay
    world["updates"] = [tap(1004, message_id=601, data="klar:B1")]
    assert approve.handle_updates(CONFIG, lambda: Session(portal), RULES) == 0
    assert world["answers"][-1] == ("cb1004", "Redan hanterad.")
    assert world["hanterad"] == [DETAIL_URL]
    assert len(world["sent"]) == 2
    for sentinel in PII:
        assert sentinel not in world["orders_file"].read_text(encoding="utf-8")

    # Nothing pending and no Klar outstanding: the next poll exits before Telegram.
    offsets = list(world["offsets"])
    assert approve.main([]) == 0
    assert world["offsets"] == offsets
