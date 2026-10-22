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

The cart is never emptied here: when some parts cannot be added, the others stay in the
cart untouched so the owner can complete the order by hand on pro.meca.se.

Owner-facing text never carries raw exception text (owner_reason); that goes to the log.

pro.meca.se keeps one cart PER VEHICLE, so staging requires the loaded vehicle's cart
to be empty: a second booking for a vehicle whose cart is in use is blocked rather than
mixed into the same cart.

Customer data (name, phone, email, booking message) is never persisted: the plan
only holds what approval needs (orders.json is committed to git).

Before the first portal step, pro.meca.se "Skickade Beställningar" is read once per
staging attempt: a sent order for the same vehicle dated within DUPLICATE_WINDOW_DAYS and
not claimed by another record blocks staging (never re-staged; the owner orders by hand).
A portal error while checking is never read as "no duplicate".

A 'blocked' order whose cart was never touched is re-staged by agent.py through
orders_state.restage(). Such a quiet retry sends no message when it blocks again for
the same reason; reaching orders_state.MAX_TRIES sends one give-up message.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import notifier
import orders_state
from logsafe import describe
from order_match import candidates, claim_tokens, clean_vehicle_model, numbers_text
from parts_matcher import choose, is_fluid, search_terms
from parts_portal_parse import PartsPortalError
from parts_order_helpers import (build_summary, cart_problem, clean_fluids, error_code,
                                 is_transient, order_body, owner_reason, parse_qty,
                                 prepare_section, reference_line, total_problem)
from rules import Rules

log = logging.getLogger(__name__)

GIVE_UP_TEXT = "Försöker inte mer automatiskt för {label} – beställ för hand eller åtgärda orsaken."

KLAR_FALLBACK = "\n(Klar-knappen kunde inte skickas – markera bokningen som Hanterad i portalen.)"

PARTIAL_REASON = "kunde inte lägga alla delar i varukorgen"
# A pro.meca.se hiccup before anything was added: blocked (not failed), so
# orders_state.restage retries it quietly on the next run (stable text = quiet repeats).
TRANSIENT_REASON = "pro.meca.se svarade inte som väntat"
TRANSIENT_TEXT = ("Delbeställning för {label}: pro.meca.se svarade inte som väntat – inget "
                  "lades i varukorgen. Jag försöker igen vid nästa körning.")
PARTIAL_PROMISE = ("Delarna som lades till ligger kvar orörda i varukorgen på pro.meca.se – "
                   "komplettera för hand.")
PARTIAL_PROMISE_CHECK = ("Det som redan ligger i varukorgen är orört – kontrollera antalet "
                         "innan du lägger till mer.")
CART_UNTOUCHED = "Det som redan ligger i varukorgen är orört."
# Time budget for staging one booking (searches + adds), checked before each search and add;
# a slow pro.meca.se must not hold the whole run. Injectable clock for tests.
STAGE_BUDGET_S = 480
_clock = time.monotonic
# After these the session cannot add anything more: the remaining parts are not tried.
STOP_CODES = ("vehicle_lost", "login")

# Duplicate-order guard: a sent order for the vehicle this recent blocks staging. The
# reason starts with orders_state.NO_RESTAGE_REASONS, so it is never retried quietly.
DUPLICATE_WINDOW_DAYS = 7
DUPLICATE_REASON = "order finns redan i pro.meca.se ({numbers})"
DUPLICATE_TEXT = ("Bokning {label}: en skickad order för fordonet finns redan i pro.meca.se "
                  "({numbers}). Jag har inte förberett någon ny order. "
                  "Beställ för hand om delarna ändå behövs.")

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

    The new message id is stored as the record's klar_message_id (with klar_sent_at),
    then the buttons of an older Klar message are cleared (best effort). A failed store
    returns False. Without a record, or if the
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
    # Store first: if that fails the old button stays the valid one (and this counts as a
    # delivery problem for the caller), so the owner is never left without a working Klar.
    try:
        orders_state.update_fields(booking_id, klar_message_id=mid, klar_closed=False,
                                   klar_sent_at=datetime.now(timezone.utc).isoformat())
    except Exception:
        log.exception("Could not store the Klar message id of %s.", booking_id)
        return False
    old = record.get("klar_message_id")
    if type(old) is int and old != mid:
        try:
            notifier.clear_buttons(config, old)
        except Exception:
            log.exception("Could not clear the old Klar button of %s.", booking_id)
    return True


# ── Status endings (each sends a message) ────────────────────────────────────

def _cart_touched(booking_id) -> bool:
    try:
        return bool((orders_state.get(booking_id) or {}).get("cart_touched") is True)
    except Exception:
        return False


def _fail(config: dict, booking_id, label: str, reason: str) -> str:
    """Record 'failed' and message the owner. `reason` must be owner-safe Swedish text."""
    try:
        orders_state.put(booking_id, "failed")
    except Exception:
        log.exception("Could not record 'failed' for booking %r", booking_id)
    text = f"Delbeställning misslyckades för {label}: {reason}. Beställ för hand."
    if _cart_touched(booking_id):
        text += " " + CART_UNTOUCHED
    if not send_with_klar(config, booking_id, text):
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
                         f"töm den eller beställ därifrån. {CART_UNTOUCHED}")
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
        return _fail(config, booking_id, label, owner_reason(e))  # put fails the same way; logged
    if not started:
        try:
            return orders_state.get(booking_id)["status"]
        except Exception:
            log.exception("Cannot read order state for %r", booking_id)
            return "failed"
    try:
        status = _stage(config, booking, portal, rules, picker)
    except Exception as e:
        log.error("Parts ordering failed for %s: %s", booking_id, describe(e))
        log.debug("Traceback", exc_info=True)
        if not (is_transient(e) and not _cart_touched(booking_id)):
            return _fail(config, booking_id, label, owner_reason(e))
        try:
            status = _block_transient(config, booking)
        except Exception:
            log.exception("Could not record the transient block for %s", booking_id)
            return _fail(config, booking_id, label, owner_reason(e))
    if status == "blocked":
        try:
            if give_up_if_capped(config, booking) is False:
                return "failed"  # record stays blocked; the message is retried next run
        except Exception:
            log.exception("Give-up check failed for %s", booking_id)
            return "failed"
    return status


def _block_transient(config: dict, booking: dict) -> str:
    """Block (for a quiet automatic retry) after a pro.meca.se hiccup; nothing is in the cart."""
    record = orders_state.get(booking["id"]) or {}
    quiet_reason = record.get("last_reason") if record.get("tries") else None
    return _block(config, booking, TRANSIENT_REASON, split_lines(booking)[1],
                  text=TRANSIENT_TEXT.format(label=_label(booking)), quiet_reason=quiet_reason)


def _claimed_by_others(booking_id) -> set[str]:
    """Supplier/portal order numbers stored as verified_numbers on any OTHER record."""
    taken = set()
    for status in orders_state.STATUSES:
        for bid, rec in orders_state.by_status(status):
            if bid != booking_id:
                taken |= claim_tokens(rec.get("verified_numbers"))
    return taken


def _existing_sent_order(portal, booking: dict, now: datetime) -> dict | None:
    """The newest "Skickade Beställningar" row that may already be this booking's order:
    same vehicle (plate when the table has the registration column, else brand + model
    words; order_match.candidates), dated on or after DUPLICATE_WINDOW_DAYS before `now`
    (a naive `now` is UTC; the UTC date is never later than the Swedish one, so the window
    is never shorter), and not claimed by another record. None = no such row.
    PartsPortalError from list_sent_orders() is raised through (never "no duplicate")."""
    rows = portal.list_sent_orders()
    when = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    since = when.astimezone(timezone.utc).date() - timedelta(days=DUPLICATE_WINDOW_DAYS)
    found = candidates(rows, clean_vehicle_model(booking.get("vehicle")), since,
                       _claimed_by_others(booking.get("id")),
                       reg_nr=str(booking.get("reg_nr") or "").strip())
    if found is None:  # nothing to match by (no plate column and no model)
        log.warning("Duplicate check for %s: nothing to match sent orders by.", booking.get("id"))
        return None
    return found[0] if found else None


def _stage(config: dict, booking: dict, portal, rules: Rules, picker) -> str:
    booking_id, label = booking["id"], _label(booking)
    record = orders_state.get(booking_id) or {}
    # A quiet retry (orders_state.restage) remembers the reason the last attempt blocked.
    quiet_reason = record.get("last_reason") if record.get("tries") else None
    fields = {} if record.get("cart_touched") is True else {"cart_touched": False}
    detail_url = booking.get("detail_url")
    if isinstance(detail_url, str) and detail_url.strip():
        fields["detail_url"] = detail_url.strip()
    vehicle_model = clean_vehicle_model(booking.get("vehicle"))
    if vehicle_model:  # the car model only, for the order checker (approve.py)
        fields["vehicle_model"] = vehicle_model
    if fields:  # cart_touched=False marks the record as not legacy (orders_state.restage)
        orders_state.update_fields(booking_id, **fields)
    order_lines, fluids = split_lines(booking)
    deadline = _clock() + STAGE_BUDGET_S

    def out_of_time() -> bool:
        return _clock() > deadline

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

    # Duplicate-order guard: once per staging attempt (restaged ones too: the order may
    # have been placed by hand meanwhile), before any portal step that touches a cart.
    if portal is not None:
        row = _existing_sent_order(portal, booking, datetime.now(timezone.utc))
        if row is not None:
            numbers = numbers_text(row)
            log.warning("A recent sent order exists for %s — not staging.", booking_id)
            return block(DUPLICATE_REASON.format(numbers=numbers),
                         text=DUPLICATE_TEXT.format(label=label, numbers=numbers))

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
            if out_of_time():  # nothing is in the cart yet: retried quietly (transient)
                raise PartsPortalError(f"staging {booking_id} took longer than "
                                       f"{STAGE_BUDGET_S} s", "search_timeout")
            candidate = choose(line, portal.search(term), rules, picker)
            if candidate is not None:
                break
        if candidate is None:
            return block(f"ingen säker träff för '{name}'")
        matched.append((name, candidate, qty))
    if matched:
        # Persisted before the first add: a later block or failure must not be retried.
        orders_state.update_fields(booking_id, cart_touched=True)
    added, not_added, partly, untried = [], [], [], []
    for i, (name, candidate, qty) in enumerate(matched):
        if out_of_time():  # stop adding; what is in the cart stays untouched
            log.warning("Staging %s ran out of time — %d part(s) not tried.",
                        booking_id, len(matched) - i)
            untried = [n for n, _, _ in matched[i:]]
            break
        # Parts are independent: one that cannot be added never stops (or undoes) the others,
        # unless the session itself is gone (vehicle or login lost).
        try:
            portal.add_to_cart(candidate, qty)
        except Exception as e:
            log.warning("Could not add %r (%s) for %s to the cart: %s",
                        name, candidate.get("url_or_id"), booking_id, describe(e))
            steps = getattr(e, "steps_done", 0)
            if isinstance(steps, int) and not isinstance(steps, bool) and steps >= 1:
                partly.append((name, steps, qty, owner_reason(e)))  # "+" was already clicked
            else:
                not_added.append((name, owner_reason(e)))
            if error_code(e) in STOP_CODES:
                untried = [n for n, _, _ in matched[i + 1:]]
                break
        else:
            added.append((candidate, qty))
    if not_added or partly or untried:
        return block(PARTIAL_REASON, cart_touched=True,
                     text=_partial_text(label, added, not_added, partly, untried, fluids))

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


def _partial_text(label: str, added: list, not_added: list, partly: list, untried: list,
                  fluids: list[dict]) -> str:
    if added:
        lines = [f"Delbeställning för {label}: i varukorgen: "
                 + ", ".join(f"{c.get('title')} x{qty}" for c, qty in added) + "."]
    elif partly:
        lines = [f"Delbeställning för {label}: ingen del lades helt i varukorgen."]
    else:
        lines = [f"Delbeställning för {label}: Inget lades i varukorgen."]
    if not_added:
        lines.append("Kunde inte läggas till: "
                     + "; ".join(f"{name} ({reason})" for name, reason in not_added) + ".")
    if partly:
        lines.append("Kan ligga delvis i varukorgen – kontrollera antalet: " + "; ".join(
            f"{name} (lade {steps} av {qty}, {reason})" for name, steps, qty, reason in partly)
            + ".")
    if untried:
        lines.append("Inte försökt: " + ", ".join(untried) + ".")
    lines.append(PARTIAL_PROMISE_CHECK if partly else PARTIAL_PROMISE)
    return "\n".join(lines + prepare_section(fluids))


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
            text = (f"Beställning misslyckades för {label}{tail}: {owner_reason(e)}. "
                    "Kontrollera pro.meca.se: ordern kan ha lagts.")
        else:
            text = (f"Beställning misslyckades för {label}: {owner_reason(e)}. "
                    "Inget beställdes – beställ för hand.")
        if not send_with_klar(config, booking_id, text):
            log.error("Failure message for %s was not delivered.", booking_id)
        return "failed"

    text = "\n".join([f"Beställd: {label} ({plan.get('reg_nr')})", reference_line(ref), ""]
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
