"""
parts_order.py
--------------
Plans a pro.meca.se parts order for one booking, stages it for Telegram approval
(or orders directly in auto mode), and executes an approved order.

Fails closed: every path ends in a non-"attempted" status in orders_state plus a
Telegram message (ordered, blocked or failed). A message that cannot be delivered
turns blocked into "failed" so the run goes red. No exception leaves this module.

The pro.meca.se account has ONE cart, so staging requires it to be empty: a second
booking while another is pending is blocked rather than mixed into the same cart.

Customer data (name, phone, email, booking message) is never persisted: the plan
only holds what approval needs (orders.json is committed to git).
"""

import logging
from datetime import datetime, timezone

import notifier
import orders_state
from parts_matcher import choose, is_fluid
from parts_order_helpers import (build_summary, cart_problem, clean_fluids, order_body,
                                 parse_qty, prepare_section, total_problem)
from rules import Rules

log = logging.getLogger(__name__)


def split_lines(booking: dict) -> tuple[list[dict], list[dict]]:
    """(part lines to order, fluid lines to prepare). Fluids in 'parts' move to fluids."""
    parts, fluids = [], []
    for line in booking.get("parts") or []:
        (fluids if is_fluid(line.get("name", "")) else parts).append(line)
    fluids.extend(booking.get("fluids") or [])
    return parts, clean_fluids(fluids)


def _wanted(order_lines: list[dict]) -> tuple[list[tuple], str | None]:
    """[(line, name, qty)], or a block reason for a blank name / bad quantity."""
    wanted = []
    for line in order_lines:
        name = str(line.get("name") or "").strip()
        if not name:
            return [], "en del saknar namn"
        qty = parse_qty(line.get("qty"))
        if qty is None:
            return [], f"ogiltigt antal för '{name}' ({line.get('qty')!r})"
        wanted.append((line, name, qty))
    return wanted, None


def needs_portal(booking: dict) -> bool:
    """True if staging this booking would use pro.meca.se (not yet handled, would not block)."""
    order_lines, _ = split_lines(booking)
    if not order_lines or not str(booking.get("reg_nr") or "").strip() or _wanted(order_lines)[1]:
        return False
    try:
        record = orders_state.get(booking.get("id"))
    except Exception:
        return False  # plan_and_stage reports the state problem without the portal
    return record is None or orders_state.is_queued(record)


def order_notify_failed(booking_id: str) -> bool:
    """True if an 'ordered' booking's result message was not delivered (or state unreadable)."""
    try:
        record = orders_state.get(booking_id)
    except Exception:
        return True
    return not record or bool((record.get("plan") or {}).get("notify_failed"))


def _label(booking: dict) -> str:
    return str(booking.get("booking_nr") or booking.get("id") or "okänd bokning")


def _why(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


# ── Status endings (each sends a message) ────────────────────────────────────

def _fail(config: dict, booking_id, label: str, reason: str) -> str:
    try:
        orders_state.put(booking_id, "failed")
    except Exception:
        log.exception("Could not record 'failed' for booking %r", booking_id)
    if not notifier.send_message(
            config, f"Delbeställning misslyckades för {label}: {reason}. Beställ för hand."):
        log.error("Failure message for %r was not delivered.", booking_id)
    return "failed"


def _block(config: dict, booking: dict, reason: str, fluids: list[dict],
           cart_touched: bool = False, text: str | None = None) -> str:
    booking_id = booking["id"]
    orders_state.put(booking_id, "blocked", plan={
        "reason": reason, "booking_id": booking_id, "booking_nr": booking.get("booking_nr"),
        "reg_nr": booking.get("reg_nr"), "fluids": fluids,
    })
    if text is None:
        lines = [f"Delbeställning stoppad för {_label(booking)}: {reason}. "
                 "Inget beställdes – beställ för hand."]
        if cart_touched:
            lines.append("Varukorgen på pro.meca.se innehåller delarna – "
                         "töm den eller beställ därifrån.")
        text = "\n".join(lines + prepare_section(fluids))
    if notifier.send_message(config, text):
        return "blocked"
    log.error("Blocked-order message for %s was not delivered — marking failed.", booking_id)
    orders_state.put(booking_id, "failed")
    return "failed"


def record_failure(config: dict, booking: dict, reason: str) -> str:
    """Mark a not-yet-started booking 'failed' with a message (setup errors before staging)."""
    booking_id = booking.get("id")
    try:
        if not orders_state.begin(booking_id):
            return orders_state.get(booking_id)["status"]
    except Exception:
        log.exception("Order state unavailable for booking %r", booking_id)
    return _fail(config, booking_id, _label(booking), reason)


# ── Planning ─────────────────────────────────────────────────────────────────

def plan_and_stage(config: dict, booking: dict, portal, rules: Rules, picker) -> str:
    """Stage one booking's parts order. Returns pending | ordered | blocked | failed."""
    booking_id, label = booking.get("id"), _label(booking)
    try:
        started = orders_state.begin(booking_id)
    except Exception as e:
        log.exception("Cannot start booking %r", booking_id)
        return _fail(config, booking_id, label, _why(e))  # put fails the same way; logged
    if not started:
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
    booking_id, label = booking["id"], _label(booking)
    order_lines, fluids = split_lines(booking)
    if not order_lines:
        return _block(config, booking, "inga delar att beställa", fluids, text="\n".join(
            [f"Inga delar att beställa för {label}."] + prepare_section(fluids)))
    reg_nr = str(booking.get("reg_nr") or "").strip()
    if not reg_nr:
        return _block(config, booking, "registreringsnummer saknas", fluids)
    wanted, reason = _wanted(order_lines)
    if reason:
        return _block(config, booking, reason, fluids)

    portal.login()
    portal.load_vehicle(reg_nr)
    if (portal.read_cart() or {}).get("lines"):
        return _block(config, booking, "varukorgen är inte tom", fluids, text=(
            "Varukorgen på pro.meca.se är inte tom (annan väntande beställning?). "
            f"Hantera {label} för hand."))

    matched = []
    for line, name, qty in wanted:  # match everything before adding anything
        candidate = choose(line, portal.search(name), rules, picker)
        if candidate is None:
            return _block(config, booking, f"ingen säker träff för '{name}'", fluids)
        matched.append((name, candidate, qty))
    for _, candidate, qty in matched:
        portal.add_to_cart(candidate, qty)

    # What the cart must contain comes from our choices, never from the cart itself.
    expected = [{"title": c.get("title"), "qty": qty, "url_or_id": c.get("url_or_id")}
                for _, c, qty in matched]
    cart = portal.read_cart()
    problem = total_problem(cart.get("total_sek"), rules.ceiling_sek) \
        or cart_problem(cart.get("lines") or [], expected, rules)
    if problem:
        return _block(config, booking, problem, fluids, cart_touched=True)

    parts = [{"name": name, "title": c.get("title"), "brand": c.get("brand"),
              "price_sek": c.get("price_sek"), "qty": qty, "url_or_id": c.get("url_or_id")}
             for name, c, qty in matched]
    plan = {
        "booking_id": booking_id,
        "booking_nr": booking.get("booking_nr"),
        "reg_nr": reg_nr,
        "parts": parts,
        "expected_lines": expected,
        "total_sek": cart.get("total_sek"),
        "fluids": fluids,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    orders_state.put(booking_id, "pending", plan=plan)

    if rules.mode == "auto":
        return execute_order(config, portal, booking_id, plan, rules)
    if rules.mode != "approve":
        raise ValueError(f"unknown rules mode {rules.mode!r}")

    message_id = notifier.send_with_buttons(
        config, build_summary(booking, cart, fluids, parts) + "\n\nGodkänn för att beställa.",
        [("Godkänn", f"approve:{booking_id}"), ("Avbryt", f"cancel:{booking_id}")])
    if message_id is None:
        return _fail(config, booking_id, label,
                     "godkännandemeddelandet kunde inte skickas (varukorgen är fylld)")
    orders_state.put(booking_id, "pending", message_id=message_id)
    return "pending"


# ── Execution (auto mode here, approve mode from the poller) ─────────────────

def execute_order(config: dict, portal, booking_id: str, plan: dict, rules: Rules) -> str:
    """Re-check the live cart against the plan, then check out. Caller guarantees 'pending'.

    Returns "ordered" or "failed". An "ordered" whose message was not delivered is
    flagged with plan["notify_failed"] (see order_notify_failed); it is never downgraded.
    """
    label = str(plan.get("booking_nr") or booking_id)
    checkout_started, ref = False, None
    try:
        orders_state.put(booking_id, "attempted", plan=plan)
        portal.login()
        portal.load_vehicle(plan["reg_nr"])
        cart = portal.read_cart()
        total = cart.get("total_sek")
        problem = total_problem(total, rules.ceiling_sek)
        if problem is None and abs(total - plan["total_sek"]) > 0.005:
            problem = f"totalen har ändrats ({plan['total_sek']:.2f} → {total:.2f} SEK)"
        problem = problem or cart_problem(cart.get("lines") or [], plan["expected_lines"], rules)
        if problem:
            orders_state.put(booking_id, "failed")
            if not notifier.send_message(config, f"Beställning stoppad för {label}: {problem}. "
                                                 "Inget beställdes – beställ för hand."):
                log.error("Stopped-order message for %s was not delivered.", booking_id)
            return "failed"

        checkout_started = True
        ref = portal.checkout()
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("ingen orderbekräftelse")
        ordered_plan = {**plan, "confirmation": ref}
        orders_state.put(booking_id, "ordered", plan=ordered_plan)
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
        if not notifier.send_message(config, text):
            log.error("Failure message for %s was not delivered.", booking_id)
        return "failed"

    text = "\n".join([f"Beställd: {label} ({plan.get('reg_nr')})", f"Ordernummer: {ref}", ""]
                     + order_body(plan.get("parts") or [], plan.get("total_sek"),
                                  plan.get("fluids") or []))
    try:
        delivered = notifier.send_message(config, text)
    except Exception:
        log.exception("Sending the ordered message for %s raised.", booking_id)
        delivered = False
    if not delivered:
        log.error("Ordered message for %s was not delivered (order %s exists).", booking_id, ref)
        try:
            orders_state.put(booking_id, "ordered", plan={**ordered_plan, "notify_failed": True})
        except Exception:
            log.exception("Could not flag notify_failed for booking %r", booking_id)
    return "ordered"
