"""
parts_order.py
--------------
Plans a pro.meca.se parts order for one booking, stages it for Telegram approval
(or orders directly in auto mode), and executes an approved order.

Fails closed: every path ends in a non-"attempted" status in orders_state plus a
Telegram message (ordered, blocked or failed). A message that cannot be delivered
turns blocked into "failed" so the run goes red. No exception leaves this module.

Every resolution message (ordered, blocked, failed, give-up; cancelled and expired in
approve.py) carries a Klar button (send_with_klar). The owner's tap makes approve.py
mark the booking Hanterad; a booking with parts is never marked Hanterad otherwise.

pro.meca.se keeps one cart PER VEHICLE, so staging requires the loaded vehicle's cart
to be empty: a second booking for a vehicle whose cart is in use is blocked rather than
mixed into the same cart.

Customer data (name, phone, email, booking message) is never persisted: the plan
only holds what approval needs (orders.json is committed to git).

A 'blocked' order whose cart was never touched is re-staged by agent.py through
orders_state.restage(). Such a quiet retry sends no message when it blocks again for
the same reason; reaching orders_state.MAX_TRIES sends one give-up message.
"""

import logging
from datetime import datetime, timezone

import notifier
import orders_state
from parts_matcher import choose, is_fluid, search_terms
from parts_order_helpers import (build_summary, cart_problem, clean_fluids, order_body,
                                 parse_qty, prepare_section, total_problem)
from rules import Rules

log = logging.getLogger(__name__)

GIVE_UP_TEXT = "Försöker inte mer automatiskt för {label} – beställ för hand eller åtgärda orsaken."

KLAR_FALLBACK = "\n(Klar-knappen kunde inte skickas – markera bokningen som Hanterad i portalen.)"

EMPTY_CART_TEXT = ("Varukorgen på pro.meca.se är tom – ordern kan redan vara lagd. "
                   "Kontrollera Orderöversikt på pro.meca.se innan du beställer för hand.")


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


# ── Klar button ──────────────────────────────────────────────────────────────

def send_with_klar(config: dict, booking_id, text: str) -> bool:
    """Send a resolution message with a Klar button; True if the owner got the text.

    The new message id is stored as the record's klar_message_id and the buttons of
    an older Klar message are cleared (best effort). Without a record, or if the
    button message cannot be sent, a plain message asks the owner to mark the
    booking Hanterad by hand.
    """
    try:
        record = orders_state.get(booking_id) if isinstance(booking_id, str) else None
    except Exception:
        log.exception("Order state unreadable for %r — sending without Klar.", booking_id)
        record = None
    mid = None
    if record is not None:
        mid = notifier.send_with_buttons(config, text, [("Klar", f"klar:{booking_id}")])
    if type(mid) is not int or mid < 1:
        if record is not None:
            log.error("Klar message for %s could not be sent — sending it plain.", booking_id)
        return bool(notifier.send_message(config, text + KLAR_FALLBACK))
    old = record.get("klar_message_id")
    if type(old) is int and old != mid:
        try:
            notifier.clear_buttons(config, old)
        except Exception:
            log.exception("Could not clear the old Klar button of %s.", booking_id)
    try:
        orders_state.update_fields(booking_id, klar_message_id=mid)
    except Exception:
        log.exception("Could not store the Klar message id of %s.", booking_id)
    return True


# ── Status endings (each sends a message) ────────────────────────────────────

def _fail(config: dict, booking_id, label: str, reason: str) -> str:
    try:
        orders_state.put(booking_id, "failed")
    except Exception:
        log.exception("Could not record 'failed' for booking %r", booking_id)
    if not send_with_klar(
            config, booking_id, f"Delbeställning misslyckades för {label}: {reason}. Beställ för hand."):
        log.error("Failure message for %r was not delivered.", booking_id)
    return "failed"


def _block(config: dict, booking: dict, reason: str, fluids: list[dict],
           cart_touched: bool = False, text: str | None = None,
           quiet_reason: str | None = None) -> str:
    """Record 'blocked' and message the owner, unless this is a quiet retry
    (quiet_reason = the previous block reason) that blocked for the same reason."""
    booking_id = booking["id"]
    orders_state.put(booking_id, "blocked", plan={
        "reason": reason, "booking_id": booking_id, "booking_nr": booking.get("booking_nr"),
        "reg_nr": booking.get("reg_nr"), "fluids": fluids,
    })
    if quiet_reason is not None and reason[:orders_state.REASON_MAX] == quiet_reason:
        log.info("Retry of %s blocked again for the same reason — no new message.", booking_id)
        return "blocked"
    if text is None:
        lines = [f"Delbeställning stoppad för {_label(booking)}: {reason}. "
                 "Inget beställdes – beställ för hand."]
        if cart_touched:
            lines.append("Varukorgen på pro.meca.se innehåller delarna – "
                         "töm den eller beställ därifrån.")
        text = "\n".join(lines + prepare_section(fluids))
    if send_with_klar(config, booking_id, text):
        return "blocked"
    log.error("Blocked-order message for %s was not delivered — marking failed.", booking_id)
    orders_state.put(booking_id, "failed")
    return "failed"


def give_up_if_capped(config: dict, booking: dict) -> bool:
    """Send the one give-up message for a blocked, untouched order out of retries.

    True = sent now (flagged 'gave_up'); None = nothing to send; False = not delivered
    (a failure for the caller; not flagged, so it is sent again next run).
    """
    booking_id = booking.get("id")
    record = orders_state.get(booking_id)
    if (not record or record.get("status") != "blocked" or record.get("cart_touched") is True
            or record.get("tries", 0) < orders_state.MAX_TRIES or record.get("gave_up")
            or record.get("handled") is True):
        return None
    if not send_with_klar(config, booking_id, GIVE_UP_TEXT.format(label=_label(booking))):
        log.error("Give-up message for %s was not delivered.", booking_id)
        return False
    orders_state.update_fields(booking_id, gave_up=True)
    return True


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
        status = _stage(config, booking, portal, rules, picker)
    except Exception as e:
        log.exception("Parts ordering failed for %s", booking_id)
        return _fail(config, booking_id, label, _why(e))
    if status == "blocked":
        try:
            if give_up_if_capped(config, booking) is False:
                return "failed"  # record stays blocked; the message is retried next run
        except Exception:
            log.exception("Give-up check failed for %s", booking_id)
            return "failed"
    return status


def _stage(config: dict, booking: dict, portal, rules: Rules, picker) -> str:
    booking_id, label = booking["id"], _label(booking)
    record = orders_state.get(booking_id) or {}
    # A quiet retry (orders_state.restage) remembers the reason the last attempt blocked.
    quiet_reason = record.get("last_reason") if record.get("tries") else None
    fields = {} if record.get("cart_touched") is True else {"cart_touched": False}
    detail_url = booking.get("detail_url")
    if isinstance(detail_url, str) and detail_url.strip():
        fields["detail_url"] = detail_url.strip()
    if fields:  # cart_touched=False marks the record as not legacy (orders_state.restage)
        orders_state.update_fields(booking_id, **fields)
    order_lines, fluids = split_lines(booking)

    def block(reason: str, **kw) -> str:
        return _block(config, booking, reason, fluids, quiet_reason=quiet_reason, **kw)

    if not order_lines:
        return block("inga delar att beställa", text="\n".join(
            [f"Inga delar att beställa för {label}."] + prepare_section(fluids)))
    reg_nr = str(booking.get("reg_nr") or "").strip()
    if not reg_nr:
        return block("registreringsnummer saknas")
    wanted, reason = _wanted(order_lines)
    if reason:
        return block(reason)

    portal.login()
    portal.load_vehicle(reg_nr)
    if (portal.read_cart() or {}).get("lines"):
        return block("varukorgen är inte tom", text=(
            "Varukorgen på pro.meca.se är inte tom (annan väntande beställning?). "
            f"Hantera {label} för hand."))

    matched = []
    for line, name, qty in wanted:  # match everything before adding anything
        candidate = None
        for term in search_terms(name):  # synonyms first, then the normalised term, the original last
            candidate = choose(line, portal.search(term), rules, picker)
            if candidate is not None:
                break
        if candidate is None:
            return block(f"ingen säker träff för '{name}'")
        matched.append((name, candidate, qty))
    if matched:
        # Persisted before the first add: a later block or failure must not be retried.
        orders_state.update_fields(booking_id, cart_touched=True)
    for _, candidate, qty in matched:
        portal.add_to_cart(candidate, qty)

    # What the cart must contain comes from our choices, never from the cart itself.
    expected = [{"title": c.get("title"), "qty": qty, "url_or_id": c.get("url_or_id")}
                for _, c, qty in matched]
    cart = portal.read_cart()
    problem = total_problem(cart.get("total_sek"), rules.ceiling_sek) \
        or cart_problem(cart.get("lines") or [], expected, rules)
    if problem:
        return block(problem, cart_touched=True)

    parts = [{"name": name, "title": c.get("title"), "brand": c.get("brand"),
              "price_sek": c.get("price_sek"), "qty": qty, "url_or_id": c.get("url_or_id"),
              "in_stock": c.get("in_stock")}
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
        if not (cart.get("lines") or []):
            # An earlier run may have ordered it already (state push failed) or the owner
            # ordered the cart by hand: never suggest ordering again without checking.
            orders_state.put(booking_id, "failed")
            if not send_with_klar(config, booking_id, EMPTY_CART_TEXT):
                log.error("Empty-cart message for %s was not delivered.", booking_id)
            return "failed"
        total = cart.get("total_sek")
        problem = total_problem(total, rules.ceiling_sek)
        if problem is None and abs(total - plan["total_sek"]) > 0.005:
            problem = f"totalen har ändrats ({plan['total_sek']:.2f} → {total:.2f} SEK)"
        problem = problem or cart_problem(cart.get("lines") or [], plan["expected_lines"], rules)
        if problem:
            orders_state.put(booking_id, "failed")
            if not send_with_klar(config, booking_id, f"Beställning stoppad för {label}: {problem}. "
                                                      "Inget beställdes – beställ för hand."):
                log.error("Stopped-order message for %s was not delivered.", booking_id)
            return "failed"

        checkout_started = True
        ref = portal.checkout(max_total_sek=rules.ceiling_sek)
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
        if not send_with_klar(config, booking_id, text):
            log.error("Failure message for %s was not delivered.", booking_id)
        return "failed"

    text = "\n".join([f"Beställd: {label} ({plan.get('reg_nr')})", f"Ordernummer: {ref}", ""]
                     + order_body(plan.get("parts") or [], plan.get("total_sek"),
                                  plan.get("fluids") or []))
    try:
        delivered = send_with_klar(config, booking_id, text)
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
