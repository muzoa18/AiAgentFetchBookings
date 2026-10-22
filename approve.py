"""
approve.py
----------
Telegram approval poller (approve.yml, once a day at 06:00 UTC). Reads the owner's
Approve/Cancel taps on staged parts orders, checks out or cancels, and expires
approvals older than 48 h. It also handles Klar taps: every resolution message
(ordered, blocked, failed, cancelled, expired) carries a Klar button, and a tap
marks the booking Hanterad in the MECA portal (portal_scraper.mark_booking_handled).
A booking with parts is never marked Hanterad without that tap.

Fails closed. An order is placed only when ALL of these hold at tap time:
  * it is tapped by the owner in their private chat (config telegram.chat_id
    must be that private chat id; groups are never accepted),
  * it is on the booking's own approval message (stored message_id),
  * the booking is still "pending" (re-read from orders_state for every tap),
  * the approval is not older than 48 h.
parts_order.execute_order then re-checks the live cart against the plan and
the ceiling before checkout, and records "attempted" first, so a crash is
never re-ordered (agent.py's start-of-run sweep reports it).

A "Byt <part>" tap (swap:<id>:<line>) gets the same owner, message id, "pending" and
age checks, at most two swaps per booking and a line with a stored alternative
(parts_order.swap_problem). It empties the vehicle's cart, withdraws the old approval
message and restages the same booking with that alternative pinned and the other lines
as staged (parts_order.restage_with_pins): a new approval message, or a truthful
"Bytet misslyckades" with the record no longer pending. A swap never orders.

After the taps, the order checker (_verify_orders) looks for every placed order that is
not verified yet in pro.meca.se Orderöversikt -> "Skickade Beställningar" (read-only;
the row appears there some time after the order) and matches it by car model, date and
registration (order_match). Exactly one row: its order numbers are stored and the owner
is told. Several rows: the owner is asked once to check which one. Nothing found for
3 days: one warning. Portal errors are retried silently on the next run.

A Klar tap is accepted with the same checks (owner, private chat, the booking's
stored klar_message_id) and only once the order is resolved: never while it is
"pending" or "attempted". A failed Hanterad keeps the button so the owner can tap
again; a missing Hanterad button counts as already handled by hand. A Klar button
older than 14 days is withdrawn once with a notice (orders_state.expired_klar).

The Telegram offset (last_update_id) is only advanced after an update was fully
handled. An update that raised while its booking is still "pending" is left
unconfirmed and retried on the next poll; nothing after it is processed.

  python approve.py --check   prints has_pending=true|false (stdlib only); true when an
                              order awaits approval, a Klar button is
                              outstanding or due to be withdrawn, or a placed
                              order still awaits the order checker
  python approve.py           handles taps; exit 1 if anything failed
"""

import argparse
import logging
import os
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone

import orders_state  # stdlib only: everything heavy is imported lazily below

log = logging.getLogger(__name__)

EXPIRY = timedelta(hours=48)
VERIFY_WARN_AFTER = timedelta(days=3)
# The approve step has 8 minutes: an immediate verification after an approved checkout
# (parts_order.verify_now, up to ~90 s each) may only START within this many seconds of the
# run start (_run_start, a parts_order._clock() value set by main), so the order checker
# and the state push still fit. Without a run start it is counted from the tap.
VERIFY_START_S = 270
_run_start: float | None = None
AMBIGUOUS_LIST_MAX = 5
# The verified-order text is order_match.VERIFIED_TEXT (shared with parts_order.verify_now).
AMBIGUOUS_TEXT = ("Flera ordrar för {model} i Skickade Beställningar – kontrollera vilken som "
                  "är {label}: {rows}.")
NOT_VERIFIED_TEXT = ("Kunde inte verifiera beställningen för {label} i Skickade Beställningar "
                     "efter 3 dagar – kontrollera i portalen.")
CART_CLEARED = "Varukorgen är tömd."
CART_MANUAL = "Varukorgen kunde inte tömmas – töm varukorgen på pro.meca.se för hand."
CART_LEFT = "Varukorgen på pro.meca.se innehåller fortfarande delarna – töm varukorgen för hand."
SWAP_REFUSED = "Bytet är inte längre möjligt."
SWAP_INDEX_DIGITS = 3  # "swap:<id>:<n>": n is 0-999, plain ASCII digits, no sign or leading 0


# ── Small helpers ────────────────────────────────────────────────────────────

def _label(booking_id: str, record: dict) -> str:
    plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
    return str(plan.get("booking_nr") or booking_id)


def _is_int(value) -> bool:
    return type(value) is int


def _parse(data):
    """'approve:<id>' / 'cancel:<id>' / 'klar:<id>' -> (action, id); 'swap:<id>:<n>' ->
    ("swap", id, n) (rpartition: a booking id may contain ':'); anything else -> None."""
    if not isinstance(data, str):
        return None
    action, sep, booking_id = data.partition(":")
    if not sep or action not in ("approve", "cancel", "klar", "swap"):
        return None
    index = None
    if action == "swap":
        booking_id, sep, digits = booking_id.rpartition(":")
        if (not sep or not digits.isascii() or not digits.isdigit()
                or len(digits) > SWAP_INDEX_DIGITS or (digits != "0" and digits[0] == "0")):
            return None
        index = int(digits)
    if not booking_id or booking_id != booking_id.strip() or any(c.isspace() for c in booking_id):
        return None
    return (action, booking_id) if index is None else (action, booking_id, index)


def _from_owner(update: dict, owner: str) -> bool:
    """Only the owner, tapping in their own private chat with the bot, may act.

    TELEGRAM_CHAT_ID must be the owner's PRIVATE chat id (a positive number); the
    bot must not be used in groups. A group id (negative) is never accepted, even
    if configured, because every group member could otherwise approve orders.
    """
    return (owner.isascii() and owner.isdigit() and int(owner) > 0
            and update.get("chat_id") == owner and update.get("user_id") == owner)


def _too_old(record: dict, now: datetime) -> bool:
    """True if pending for more than 48 h. An unreadable timestamp counts as too old."""
    try:
        since = datetime.fromisoformat(record["updated_at"])
        if since.tzinfo is None:
            return True
        return now - since > EXPIRY
    except Exception:
        return True


class _Portal:
    """Opens the parts portal at most once per run, on first use."""

    def __init__(self, factory, stack: ExitStack):
        self.factory, self.stack = factory, stack
        self.portal, self.error = None, None

    def get(self):
        if self.portal is None and self.error is None:
            try:
                self.portal = self.stack.enter_context(self.factory())
            except Exception as e:
                log.exception("Could not open the parts portal.")
                self.error = f"{type(e).__name__}: {e}"[:200]
        if self.error:
            raise RuntimeError(f"kunde inte öppna pro.meca.se ({self.error})")
        return self.portal


def _clear_cart(session: _Portal, reg_nr) -> bool:
    """Best effort: empty the pro.meca.se cart of vehicle `reg_nr` (carts are per vehicle).

    Never raises."""
    try:
        portal = session.get()
        portal.login()
        portal.clear_cart(reg_nr=reg_nr)
        return True
    except Exception:
        log.exception("Could not clear the pro.meca.se cart.")
        return False


# ── Expiry ───────────────────────────────────────────────────────────────────

def _expire(config: dict, now: datetime) -> tuple[int, bool]:
    """(number expired, any problem). The cart is left for the owner (spec: Expiry)."""
    import notifier
    import parts_order
    expired, problem = 0, False
    for booking_id, record in orders_state.pending():
        if not _too_old(record, now):
            continue
        label = _label(booking_id, record)
        try:
            orders_state.put(booking_id, "expired")
        except Exception:
            log.exception("Could not mark %s expired.", booking_id)
            problem = True
            continue
        expired += 1
        log.info("Approval for %s expired.", booking_id)
        if _is_int(record.get("message_id")):
            notifier.clear_buttons(config, record["message_id"])
        if not parts_order.send_with_klar(
                config, booking_id, f"Godkännandet för {label} har gått ut (48 h). "
                                    f"Beställ för hand om delarna behövs. {CART_LEFT}"):
            log.error("Expiry message for %s was not delivered.", booking_id)
            problem = True
    return expired, problem


def _close_expired_klar(config: dict, now: datetime) -> bool:
    """Withdraw Klar buttons older than orders_state.KLAR_MAX_AGE. True = a problem."""
    import notifier
    problem = False
    for booking_id, record in orders_state.expired_klar(now):
        try:
            notifier.clear_buttons(config, record["klar_message_id"])
        except Exception:
            log.exception("Could not clear the expired Klar button of %s.", booking_id)
        if not notifier.send_message(
                config, f"Klar-knappen för {_label(booking_id, record)} har gått ut – "
                        "markera bokningen som Hanterad i portalen."):
            log.error("Klar expiry notice for %s was not delivered; retried next run.", booking_id)
            problem = True
            continue
        orders_state.update_fields(booking_id, klar_closed=True)
        log.info("Klar button for %s expired.", booking_id)
    return problem


def expire_stale(config: dict, now: datetime) -> int:
    """Expire pending approvals older than 48 h (strictly). Returns the number expired."""
    return _expire(config, now)[0]


# ── Taps ─────────────────────────────────────────────────────────────────────

def _approve(config, session, booking_id, record, rules, cb) -> bool:
    """Execute one approved order. Returns True if the run must exit 1."""
    import notifier
    import parts_order
    notifier.answer_callback(config, cb, "Beställer…")
    notifier.clear_buttons(config, record["message_id"])
    try:
        portal = session.get()
    except Exception:  # already logged by _Portal; its raw text is never shown to the owner
        orders_state.put(booking_id, "failed")
        if not parts_order.send_with_klar(
                config, booking_id,
                f"Beställning misslyckades för {_label(booking_id, record)}: "
                "kunde inte öppna pro.meca.se. Inget beställdes – beställ för hand."):
            log.error("Failure message for %s was not delivered.", booking_id)
        return True
    start = _run_start if _run_start is not None else parts_order._clock()
    result = parts_order.execute_order(config, portal, booking_id, record["plan"], rules,
                                       deadline=start + VERIFY_START_S)
    log.info("Approved order for %s: %s", booking_id, result)
    return result != "ordered" or parts_order.order_notify_failed(booking_id)


def _cancel(config, session, booking_id, record, cb) -> bool:
    """Cancel one staged order. Returns True if the run must exit 1."""
    import notifier
    import parts_order
    orders_state.put(booking_id, "cancelled")
    notifier.clear_buttons(config, record["message_id"])
    notifier.answer_callback(config, cb, "Avbruten.")
    tail = CART_CLEARED if _clear_cart(session, (record.get("plan") or {}).get("reg_nr")) \
        else CART_MANUAL
    log.info("Order for %s cancelled by the owner.", booking_id)
    if not parts_order.send_with_klar(config, booking_id,
                                      f"Avbruten: {_label(booking_id, record)}. {tail}"):
        log.error("Cancel message for %s was not delivered.", booking_id)
        return True
    return False


def _swap(config, session, booking_id, record, line_index, rules, cb) -> tuple[int, bool]:
    """The owner tapped "Byt <part>" on the current approval message (owner, message id,
    'pending' and age already checked): swap that line to its stored alternative.

    (actions taken, run must exit 1). Refused (nothing changes) when parts_order.swap_problem
    says so. Otherwise: empty the vehicle's cart (a failure stops here and leaves the old
    approval valid: execute_order re-checks the cart anyway), withdraw the old message,
    count the swap, and restage with the alternative pinned and the other lines as staged.
    restage_with_pins sends the new approval message or the SWAP_FAILED text. Never orders.
    """
    import notifier
    import parts_order
    # The swap's own budget, cart clear included (it must fit in the approve step).
    deadline = parts_order._clock() + parts_order.SWAP_BUDGET_S
    problem = parts_order.swap_problem(record, line_index, rules)
    if problem:
        log.info("Swap of %s line %r refused: %s.", booking_id, line_index, problem)
        notifier.answer_callback(config, cb, SWAP_REFUSED)
        return 0, False
    label = _label(booking_id, record)
    plan = record["plan"]
    notifier.answer_callback(config, cb, "Byter…")
    if not _clear_cart(session, plan["reg_nr"]):
        log.error("Swap of %s stopped: the cart could not be emptied.", booking_id)
        if not notifier.send_message(config, f"Bytet för {label} gick inte: {CART_MANUAL}"):
            log.error("Swap failure message for %s was not delivered.", booking_id)
        return 1, True
    notifier.clear_buttons(config, record["message_id"])
    booking = parts_order.swap_booking(booking_id, record)
    try:
        # Not "pending" from here on: the old message is withdrawn and its cart is gone.
        orders_state.put(booking_id, "attempted")
        orders_state.update_fields(booking_id, swaps=record.get("swaps", 0) + 1,
                                   cart_touched=True)
    except Exception:
        log.exception("Could not record the swap of %s.", booking_id)
        notifier.send_message(config, f"Byte för {label}: {parts_order.SWAP_FAILED}.")
        raise
    parts = plan["parts"]
    keep = {i: part for i, part in enumerate(parts) if i != line_index}
    try:
        portal = session.get()
    except Exception:  # already logged by _Portal (it was open a moment ago)
        return 1, parts_order.swap_failed(config, booking, "kunde inte öppna pro.meca.se") \
            != "pending"
    status = parts_order.restage_with_pins(
        config, portal, booking, {line_index: parts[line_index]["alternative"]}, rules,
        _no_picker, keep=keep, deadline=deadline)
    log.info("Swap of %s line %d: %s.", booking_id, line_index, status)
    return 1, status != "pending"


def _no_picker(line, ranked):
    """A swap keeps every other line as staged; nothing is ever chosen anew."""
    return 0, 0.0


def mark_handled(config: dict, detail_url: str):
    """Press Hanterad -> Bekräfta for one booking (True / False / None = no button)."""
    from portal_scraper import mark_booking_handled  # lazy: Playwright
    return mark_booking_handled(config, detail_url)


def _klar(config, booking_id, record, cb, mark) -> tuple[int, bool]:
    """The owner tapped Klar on a resolution message: mark the booking Hanterad.

    (actions taken, run must exit 1). The record was re-read for this tap."""
    import notifier
    label = _label(booking_id, record)
    if record.get("handled") is True:
        notifier.answer_callback(config, cb, "Redan hanterad.")
        notifier.clear_buttons(config, record["klar_message_id"])
        return 0, False
    if record.get("klar_closed") is True:  # withdrawn (no link, or expired)
        notifier.answer_callback(config, cb, "Knappen är inte längre giltig.")
        return 0, False
    if record.get("status") == "pending":
        notifier.answer_callback(config, cb, "Godkänn eller avbryt beställningen först.")
        return 0, False
    if record.get("status") == "attempted":
        notifier.answer_callback(config, cb, "Beställningen pågår – vänta.")
        return 0, False
    url = record.get("detail_url")
    if not isinstance(url, str) or not url.strip():
        notifier.answer_callback(config, cb, "Saknar länk – markera bokningen som Hanterad i portalen.")
        if not notifier.send_message(
                config, f"Saknar länk till bokning {label} – markera bokningen som Hanterad "
                        "i portalen."):
            log.error("Missing-link message for %s was not delivered.", booking_id)
        notifier.clear_buttons(config, record["klar_message_id"])
        orders_state.update_fields(booking_id, klar_closed=True)  # reported once, not every run
        return 0, True
    notifier.answer_callback(config, cb, "Markerar som Hanterad…")
    try:
        result = mark(config, url)
    except Exception:
        log.exception("Marking %s Hanterad raised.", booking_id)
        result = False
    if result is None:
        # Logged in and on the booking, but no Hanterad button: most likely handled by hand.
        log.info("No Hanterad button for %s; treating it as already handled.", booking_id)
        orders_state.update_fields(booking_id, handled=True)
        notifier.clear_buttons(config, record["klar_message_id"])
        if not notifier.send_message(
                config, "Hanterad-knappen finns inte – bokningen verkar redan vara hanterad "
                        f"(kontrollera i portalen: {url})"):
            log.error("Already-handled message for %s was not delivered.", booking_id)
            return 1, True
        return 1, False
    if result is not True:
        log.error("Could not mark %s Hanterad (result %r); the Klar button stays.",
                  booking_id, result)
        if not notifier.send_message(
                config, f"Kunde inte markera bokning {label} som Hanterad – "
                        f"gör det för hand: {url}"):
            log.error("Hanterad failure message for %s was not delivered.", booking_id)
        return 1, True
    orders_state.update_fields(booking_id, handled=True)
    log.info("Booking %s marked Hanterad after the owner's Klar tap.", booking_id)
    notifier.clear_buttons(config, record["klar_message_id"])
    if not notifier.send_message(config, f"Klar: bokning {label} är markerad som Hanterad."):
        log.error("Klar confirmation for %s was not delivered.", booking_id)
        return 1, True
    return 1, False


def _handle_one(config, session, rules, update, owner: str, mark=None) -> tuple[int, bool]:
    """(actions taken, run must exit 1) for one normalised callback update."""
    import notifier
    if not _from_owner(update, owner):
        log.warning("Ignoring callback not sent by the owner in their private chat.")
        return 0, False
    cb = update.get("callback_id")
    parsed = _parse(update.get("data"))
    if parsed is None:
        notifier.answer_callback(config, cb, "Okänd åtgärd.")
        return 0, False
    action, booking_id = parsed[0], parsed[1]
    record = orders_state.get(booking_id)
    if record is None:
        notifier.answer_callback(config, cb, "Okänd bokning.")
        return 0, False
    # Approve/Cancel/Swap live on the approval message, Klar on the latest resolution message.
    stored = record.get("klar_message_id" if action == "klar" else "message_id")
    if not _is_int(stored) or not _is_int(update.get("message_id")) \
            or update.get("message_id") != stored:
        notifier.answer_callback(config, cb, "Knappen är inte längre giltig.")
        return 0, False
    if action == "klar":
        return _klar(config, booking_id, record, cb, mark if mark is not None else mark_handled)
    if record.get("status") != "pending":
        notifier.answer_callback(config, cb, "Redan hanterad.")
        return 0, False
    if action == "cancel":
        return 1, _cancel(config, session, booking_id, record, cb)
    if _too_old(record, datetime.now(timezone.utc)):
        notifier.answer_callback(config, cb, "Godkännandet har gått ut.")
        return 0, False
    if action == "swap":
        return _swap(config, session, booking_id, record, parsed[2], rules, cb)
    if action != "approve":
        notifier.answer_callback(config, cb, "Okänd åtgärd.")
        return 0, False
    return 1, _approve(config, session, booking_id, record, rules, cb)


def _still_pending(update) -> bool:
    """After an exception: True unless the tapped booking is known to have left 'pending'."""
    parsed = _parse(update.get("data"))
    if parsed is None:
        return True
    try:
        record = orders_state.get(parsed[1])
    except Exception:
        return True
    return record is None or record.get("status") == "pending"


def _handle(config: dict, portal_factory, rules, mark=None, session=None) -> tuple[int, bool]:
    """(actions, problem). Uses `session` (a _Portal) when given, else opens its own."""
    import notifier
    last = orders_state.last_update_id()
    owner = str(config.get("telegram", {}).get("chat_id") or "")
    actions, problem = 0, False
    try:
        with ExitStack() as stack:
            session = session or _Portal(portal_factory, stack)
            for update in notifier.get_updates(config, None if last is None else last + 1):
                try:
                    n, bad = _handle_one(config, session, rules, update, owner, mark)
                    actions += n
                    problem = problem or bad
                except Exception:
                    log.exception("Handling update %r failed.", update.get("update_id"))
                    problem = True
                    if _still_pending(update):
                        break  # leave the offset: this update and later ones are retried
                uid = update.get("update_id")
                if _is_int(uid) and (last is None or uid > last):
                    orders_state.set_last_update_id(uid)
                    last = uid
    except Exception:
        log.exception("Approval polling failed.")
        problem = True
    return actions, problem


def handle_updates(config: dict, portal_factory, rules, mark=None) -> int:
    """Handle the owner's Approve/Cancel/Klar taps. Returns the number of actions taken.

    `mark(config, detail_url)` presses Hanterad (default: mark_handled)."""
    return _handle(config, portal_factory, rules, mark)[0]


# ── Order checker ────────────────────────────────────────────────────────────

def _to_verify() -> list[tuple[str, dict]]:
    """Placed orders the checker still works on: not verified and not yet warned about."""
    return [(bid, rec) for bid, rec in orders_state.unverified_orders()
            if rec.get("verify_warned") is not True]


def _local_date(when: datetime):
    """The Swedish calendar date of `when` (order_match.local_date, shared with
    parts_order.verify_now)."""
    from order_match import local_date
    return local_date(when)


def _numbers_text(row: dict) -> str:
    from order_match import display_numbers
    return display_numbers(row)


def _claimed() -> set[str]:
    """Order numbers already stored on verified records (never given to another order).
    Read back with order_match.claim_tokens from the same cut text that was stored."""
    from order_match import claim_tokens
    taken = set()
    for _, rec in orders_state.by_status("ordered"):
        taken |= claim_tokens(rec.get("verified_numbers"))
    return taken


def _send_once(config, booking_id, text) -> bool:
    """One warning message; verify_warned is only set when it was delivered."""
    import notifier
    if not notifier.send_message(config, text):
        log.error("Order checker message for %s was not delivered; retried next run.",
                  booking_id)
        return False
    orders_state.update_fields(booking_id, verify_warned=True)
    return True


def _verify_orders(config: dict, session: "_Portal", now: datetime) -> bool:
    """Find unverified placed orders in pro.meca.se "Skickade Beställningar" (read-only).

    The portal is opened (once, through `session`) only when a record can be matched (a
    car model or a registration number). All records are matched together
    (order_match.resolve_orders), so two orders for the same car never swap rows.
    Returns True when a message was not delivered. An undelivered "verified" message is
    NOT sent again: the stored state is correct and the run exits 1 so it shows.
    """
    import notifier
    from order_match import match_key, numbers_text, order_facts, resolve_orders, verified_text
    from parts_order_helpers import error_code

    todo = _to_verify()
    if not todo:
        return False
    # booking_id -> (label, model, reg, since or None); the same inputs as verify_now
    facts = {booking_id: (_label(booking_id, record), *order_facts(record))
             for booking_id, record in todo}
    checkable = {bid: key for bid, key in ((bid, match_key(rec)) for bid, rec in todo)
                 if key is not None}
    results = {}
    if checkable:
        try:
            rows = session.get().list_sent_orders()
        except Exception as e:  # retried next run; never raw page text in the log
            code = error_code(e)
            log.warning("Could not read Skickade Beställningar (%s%s); retried on the "
                        "next run.", type(e).__name__, f", {code}" if code else "")
        else:
            results = resolve_orders(checkable, rows, _claimed())

    problem = False
    for booking_id, (label, model, reg, since) in facts.items():
        result = results.get(booking_id)
        status = result.status if result is not None else "unchecked"
        log.info("Order checker: %s -> %s.", booking_id, status)
        too_old = since is None or now - since > VERIFY_WARN_AFTER
        if status == "verified":
            row = result.row
            orders_state.update_fields(booking_id, verified_at=now.isoformat(),
                                       verified_numbers=numbers_text(row))
            text = verified_text(label, row, model)
            if not notifier.send_message(config, text):  # not resent: the state is right
                log.error("Verified message for %s was not delivered.", booking_id)
                problem = True
        elif status == "ambiguous":
            listed = ", ".join(_numbers_text(r) for r in result.rows[:AMBIGUOUS_LIST_MAX])
            shown = next((r["model"] for r in result.rows if r.get("model")), model or "bilen")
            text = AMBIGUOUS_TEXT.format(model=shown, label=label, rows=listed)
            problem = not _send_once(config, booking_id, text) or problem
        elif too_old:  # not found, waiting, not checkable or portal error for 3 days
            problem = not _send_once(config, booking_id,
                                     NOT_VERIFIED_TEXT.format(label=label)) or problem
    return problem


# ── Entry point ──────────────────────────────────────────────────────────────

def make_portal(config: dict):
    """The real pro.meca.se session (context manager). Raises if settings are missing."""
    missing = [k for k in ("username", "password")
               if not str((config.get("parts_portal") or {}).get(k) or "").strip()
               or str(config["parts_portal"][k]).startswith("YOUR_")]
    if missing:
        raise RuntimeError("inställning saknas: " + ", ".join(f"parts_portal.{k}" for k in missing))
    from parts_portal import PartsPortal  # lazy: Playwright
    return PartsPortal(config)


def _check() -> int:
    try:
        has_pending = (bool(orders_state.pending()) or bool(orders_state.outstanding_klar())
                       or bool(orders_state.expired_klar()) or bool(_to_verify()))
    except Exception as e:
        print(f"Order state unreadable: {e}", file=sys.stderr)
        return 1
    line = f"has_pending={'true' if has_pending else 'false'}"
    print(line)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MECA Telegram approval poller")
    parser.add_argument("--check", action="store_true",
                        help="only print has_pending=true|false (no network)")
    args = parser.parse_args(argv)
    if args.check:
        return _check()
    global _run_start
    import parts_order  # lazy: not needed by --check
    _run_start = parts_order._clock()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    import alerts
    import preflight  # lazy: --check above stays stdlib only
    if preflight.state_problems():
        import config_loader
        preflight.check_state_or_alert(config_loader.load_config())
        return 1
    alerts.resolve(preflight.STATE_ALERT_KEY)  # a repaired file clears the dashboard alert

    try:
        if not (orders_state.pending() or orders_state.outstanding_klar()
                or orders_state.expired_klar() or _to_verify()):
            log.info("No pending approvals, no outstanding Klar buttons, no order to verify.")
            return 0
    except Exception:
        log.exception("Order state file unreadable — no approvals handled.")
        return 1

    import config_loader
    import notifier
    import rules as rules_module
    config = config_loader.load_config()
    try:
        rules = rules_module.load_rules()
    except Exception:
        log.exception("parts_rules.yaml is invalid — no approvals handled.")
        notifier.send_message(config, "Regelfilen är ogiltig – inga godkännanden hanteras")
        return 1

    try:
        _, expire_problem = _expire(config, datetime.now(timezone.utc))
    except Exception:
        log.exception("Expiring stale approvals failed.")
        expire_problem = True
    try:
        expire_problem = _close_expired_klar(config, datetime.now(timezone.utc)) or expire_problem
    except Exception:
        log.exception("Withdrawing expired Klar buttons failed.")
        expire_problem = True
    with ExitStack() as stack:  # one pro.meca.se session for the taps and the order checker
        session = _Portal(lambda: make_portal(config), stack)
        actions, problem = _handle(config, None, rules, session=session)
        log.info("Approval poll done: %d action(s).", actions)
        try:
            verify_problem = _verify_orders(config, session, datetime.now(timezone.utc))
        except Exception:
            log.exception("The order checker failed; it runs again next time.")
            verify_problem = False
    return 1 if expire_problem or problem or verify_problem else 0


if __name__ == "__main__":
    # --check runs in the "Anything pending?" step: no secrets, nothing pip-installed.
    # It stays outside guarded(), which may load the config (sys.exit without secrets),
    # and its success is recorded by the workflow's "Mark OK" step instead.
    if "--check" in sys.argv[1:]:
        sys.exit(main())
    import preflight
    sys.exit(preflight.guarded("approve", main))
