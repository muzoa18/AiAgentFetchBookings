"""
parts_order.py
--------------
Plans a pro.meca.se parts order for one booking, stages it for Telegram approval
(or orders directly in auto mode), and executes an approved order.

Fails closed: every path ends in a non-"attempted" status in orders_state plus a
Telegram message (ordered, blocked or failed). No exception leaves this module.
"""

import logging
import math
from collections import Counter
from datetime import datetime, timezone

import notifier
import orders_state
from parts_matcher import choose, is_fluid
from rules import Rules

log = logging.getLogger(__name__)

PREPARE_HEADER = "Förbered (finns på lager):"


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_qty(raw) -> int | None:
    """'x4' -> 4, ''/None -> 1; non-numeric or <= 0 -> None (caller blocks)."""
    if raw is None:
        return 1
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    text = str(raw).strip()
    if text[:1] in ("x", "X"):
        text = text[1:].strip()
    if not text:
        return 1
    if not text.isdigit():
        return None
    qty = int(text)
    return qty if qty > 0 else None


def split_lines(booking: dict) -> tuple[list[dict], list[dict]]:
    """(part lines to order, fluid lines to prepare). Fluids in 'parts' move to fluids."""
    parts, fluids = [], []
    for line in booking.get("parts") or []:
        (fluids if is_fluid(line.get("name", "")) else parts).append(line)
    fluids.extend(booking.get("fluids") or [])
    return parts, fluids


def needs_portal(booking: dict) -> bool:
    """True if staging this booking would use pro.meca.se (unrecorded, reg nr, real parts)."""
    if not split_lines(booking)[0] or not str(booking.get("reg_nr") or "").strip():
        return False
    try:
        return orders_state.get(booking.get("id")) is None
    except Exception:
        return False  # plan_and_stage reports the state problem without the portal


def _label(booking: dict) -> str:
    return str(booking.get("booking_nr") or booking.get("id") or "okänd bokning")


def _why(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


def _fluid_text(line: dict) -> str:
    name = str(line.get("name", "")).strip()
    qty = str(line.get("qty") or "").strip().lstrip("xX").strip()
    return f"{name} x{qty}" if qty else name


def _prepare_section(fluids: list[dict]) -> list[str]:
    if not fluids:
        return []
    return [PREPARE_HEADER] + [f"- {_fluid_text(f)}" for f in fluids]


def _sek(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.2f} SEK"
    return "pris saknas"


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _total_problem(total, ceiling: int) -> str | None:
    if isinstance(total, bool) or not isinstance(total, (int, float)) \
            or not math.isfinite(total) or total < 0:
        return "varukorgens totalsumma saknas eller kan inte läsas"
    if total > ceiling:
        return f"totalen {total:.2f} SEK överstiger taket {ceiling} SEK"
    return None


def _line_key(line: dict) -> tuple[str, int | None]:
    return " ".join(str(line.get("title", "")).casefold().split()), _as_int(line.get("qty"))


# ── Summary ──────────────────────────────────────────────────────────────────

def build_summary(booking: dict, cart: dict, fluids: list[dict]) -> str:
    reg = str(booking.get("reg_nr") or "–").strip()
    vehicle = f"{reg} {booking.get('vehicle') or ''}".strip()
    lines = [
        f"Delbeställning {_label(booking)}",
        f"Kund: {booking.get('driver_name') or '–'}",
        f"Fordon: {vehicle}",
        "Delar:",
    ]
    for line in cart.get("lines") or []:
        lines.append(f"- {line.get('title')} x{line.get('qty')}: {_sek(line.get('price_sek'))}")
    lines.append(f"Totalt: {_sek(cart.get('total_sek'))}")
    lines.extend(_prepare_section(fluids))
    return "\n".join(lines)


# ── Status endings (each sends a message) ────────────────────────────────────

def _fail(config: dict, booking_id, label: str, reason: str) -> str:
    try:
        orders_state.put(booking_id, "failed")
    except Exception:
        log.exception("Could not record 'failed' for booking %r", booking_id)
    notifier.send_message(
        config, f"Delbeställning misslyckades för {label}: {reason}. Beställ för hand.")
    return "failed"


def _block(config: dict, booking: dict, reason: str, fluids: list[dict],
           cart_touched: bool = False) -> str:
    orders_state.put(booking["id"], "blocked", plan={
        "reason": reason, "booking_id": booking["id"], "booking_nr": booking.get("booking_nr"),
        "reg_nr": booking.get("reg_nr"), "fluids": fluids,
    })
    lines = [f"Delbeställning stoppad för {_label(booking)}: {reason}. "
             "Inget beställdes – beställ för hand."]
    if cart_touched:
        lines.append("Varukorgen på pro.meca.se innehåller delarna – "
                     "töm den eller beställ därifrån.")
    lines.extend(_prepare_section(fluids))
    if not notifier.send_message(config, "\n".join(lines)):
        log.error("Blocked-order message for %s was not delivered.", booking["id"])
    return "blocked"


def record_failure(config: dict, booking: dict, reason: str) -> str:
    """Mark an unrecorded booking 'failed' with a message (setup errors before staging)."""
    booking_id = booking.get("id")
    try:
        if not orders_state.claim(booking_id):
            return orders_state.get(booking_id)["status"]
    except Exception:
        log.exception("Order state unavailable for booking %r", booking_id)
    return _fail(config, booking_id, _label(booking), reason)


# ── Planning ─────────────────────────────────────────────────────────────────

def plan_and_stage(config: dict, booking: dict, portal, rules: Rules, picker) -> str:
    """Stage one booking's parts order. Returns pending | ordered | blocked | failed."""
    booking_id, label = booking.get("id"), _label(booking)
    try:
        claimed = orders_state.claim(booking_id)
    except Exception as e:
        log.exception("Cannot claim booking %r", booking_id)
        return _fail(config, booking_id, label, _why(e))  # put fails the same way; logged
    if not claimed:
        try:
            return orders_state.get(booking_id)["status"]
        except Exception:
            log.exception("Cannot read order state for %r", booking_id)
            return "failed"
    try:
        return _stage(config, booking, portal, rules, picker)
    except Exception as e:
        log.exception("Parts ordering failed for %s", booking_id)
        return _fail(config, booking_id, label, _why(e))


def _stage(config: dict, booking: dict, portal, rules: Rules, picker) -> str:
    booking_id = booking["id"]
    order_lines, fluids = split_lines(booking)
    if not order_lines:
        orders_state.put(booking_id, "blocked", plan={"reason": "inga delar att beställa",
                                                      "fluids": fluids})
        text = "\n".join([f"Inga delar att beställa för {_label(booking)}."]
                         + _prepare_section(fluids))
        if not notifier.send_message(config, text):
            log.error("Fluids message for %s was not delivered.", booking_id)
        return "blocked"

    reg_nr = str(booking.get("reg_nr") or "").strip()
    if not reg_nr:
        return _block(config, booking, "registreringsnummer saknas", fluids)

    wanted = []
    for line in order_lines:
        name = str(line.get("name") or "").strip()
        if not name:
            return _block(config, booking, "en del saknar namn", fluids)
        qty = parse_qty(line.get("qty"))
        if qty is None:
            return _block(config, booking, f"ogiltigt antal för '{name}' ({line.get('qty')!r})",
                          fluids)
        wanted.append((line, name, qty))

    portal.login()
    portal.load_vehicle(reg_nr)
    matched = []
    for line, name, qty in wanted:  # match everything before adding anything
        candidate = choose(line, portal.search(name), rules, picker)
        if candidate is None:
            return _block(config, booking, f"ingen säker träff för '{name}'", fluids)
        matched.append((name, candidate, qty))
    for _, candidate, qty in matched:
        portal.add_to_cart(candidate, qty)

    cart = portal.read_cart()
    problem = _total_problem(cart.get("total_sek"), rules.ceiling_sek)
    lines = cart.get("lines") or []
    if problem is None and sorted((_as_int(l.get("qty")) or 0) for l in lines) \
            != sorted(q for _, _, q in matched):
        problem = "varukorgen stämmer inte med de tillagda delarna"
    if problem:
        return _block(config, booking, problem, fluids, cart_touched=True)

    summary = build_summary(booking, cart, fluids)
    plan = {
        "booking_id": booking_id,
        "booking_nr": booking.get("booking_nr"),
        "reg_nr": reg_nr,
        "parts": [{"name": name, "title": c.get("title"), "brand": c.get("brand"),
                   "price_sek": c.get("price_sek"), "qty": qty, "url_or_id": c.get("url_or_id")}
                  for name, c, qty in matched],
        "cart_lines": [{"title": l.get("title"), "qty": l.get("qty"),
                        "price_sek": l.get("price_sek")} for l in lines],
        "total_sek": cart.get("total_sek"),
        "fluids": fluids,
        "summary": summary,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    orders_state.put(booking_id, "pending", plan=plan)

    if rules.mode == "auto":
        return execute_order(config, portal, booking_id, plan, rules)
    if rules.mode != "approve":
        raise ValueError(f"unknown rules mode {rules.mode!r}")

    message_id = notifier.send_with_buttons(
        config, f"{summary}\n\nGodkänn för att beställa.",
        [("Godkänn", f"approve:{booking_id}"), ("Avbryt", f"cancel:{booking_id}")])
    if message_id is None:
        return _fail(config, booking_id, _label(booking),
                     "godkännandemeddelandet kunde inte skickas (varukorgen är fylld)")
    orders_state.put(booking_id, "pending", message_id=message_id)
    return "pending"


# ── Execution (auto mode here, approve mode from the poller) ─────────────────

def execute_order(config: dict, portal, booking_id: str, plan: dict, rules: Rules) -> str:
    """Re-check the cart against the plan, then check out. Caller guarantees status 'pending'."""
    label = str(plan.get("booking_nr") or booking_id)
    checkout_started, ref = False, None
    try:
        orders_state.put(booking_id, "attempted", plan=plan)
        portal.login()
        portal.load_vehicle(plan["reg_nr"])
        cart = portal.read_cart()
        total = cart.get("total_sek")
        problem = _total_problem(total, rules.ceiling_sek)
        if problem is None and abs(total - plan["total_sek"]) > 0.005:
            problem = f"totalen har ändrats ({plan['total_sek']:.2f} → {total:.2f} SEK)"
        if problem is None and Counter(map(_line_key, cart.get("lines") or [])) \
                != Counter(map(_line_key, plan["cart_lines"])):
            problem = "varukorgen stämmer inte med den godkända planen"
        if problem:
            orders_state.put(booking_id, "failed")
            notifier.send_message(config, f"Beställning stoppad för {label}: {problem}. "
                                          "Inget beställdes – beställ för hand.")
            return "failed"

        checkout_started = True
        ref = portal.checkout()
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("ingen orderbekräftelse")
        orders_state.put(booking_id, "ordered", plan={**plan, "confirmation": ref})
        notifier.send_message(
            config, f"Beställd: {label}\nOrdernummer: {ref}\n\n{plan.get('summary', '')}".rstrip())
        return "ordered"
    except Exception as e:
        log.exception("Order execution failed for %s", booking_id)
        try:
            orders_state.put(booking_id, "failed")
        except Exception:
            log.exception("Could not record 'failed' for booking %r", booking_id)
        if checkout_started:
            tail = f" (ordernummer {ref})" if ref else ""
            text = (f"Beställning misslyckades för {label}{tail}: {_why(e)}. "
                    "Kontrollera pro.meca.se: ordern kan ha lagts.")
        else:
            text = (f"Beställning misslyckades för {label}: {_why(e)}. "
                    "Inget beställdes – beställ för hand.")
        notifier.send_message(config, text)
        return "failed"
