"""
approve.py
----------
Telegram approval poller (approve.yml, every 20 min in working hours). Reads the owner's
Approve/Cancel taps on staged parts orders, checks out or cancels, and expires
approvals older than 48 h.

Fails closed. An order is placed only when ALL of these hold at tap time:
  * it is tapped by the owner in their private chat (config telegram.chat_id
    must be that private chat id; groups are never accepted),
  * it is on the booking's own approval message (stored message_id),
  * the booking is still "pending" (re-read from orders_state for every tap),
  * the approval is not older than 48 h.
parts_order.execute_order then re-checks the live cart against the plan and
the ceiling before checkout, and records "attempted" first, so a crash is
never re-ordered (agent.py's start-of-run sweep reports it).

The Telegram offset (last_update_id) is only advanced after an update was fully
handled. An update that raised while its booking is still "pending" is left
unconfirmed and retried on the next poll; nothing after it is processed.

  python approve.py --check   prints has_pending=true|false (stdlib only)
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
CART_CLEARED = "Varukorgen är tömd."
CART_MANUAL = "Varukorgen kunde inte tömmas – töm varukorgen på pro.meca.se för hand."
CART_LEFT = "Varukorgen på pro.meca.se innehåller fortfarande delarna – töm varukorgen för hand."


# ── Small helpers ────────────────────────────────────────────────────────────

def _label(booking_id: str, record: dict) -> str:
    plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
    return str(plan.get("booking_nr") or booking_id)


def _is_int(value) -> bool:
    return type(value) is int


def _parse(data):
    """'approve:<id>' / 'cancel:<id>' -> (action, id); anything else -> None."""
    if not isinstance(data, str):
        return None
    action, sep, booking_id = data.partition(":")
    if not sep or action not in ("approve", "cancel"):
        return None
    if not booking_id or booking_id != booking_id.strip() or any(c.isspace() for c in booking_id):
        return None
    return action, booking_id


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
        if not notifier.send_message(
                config, f"Godkännandet för {label} har gått ut (48 h). "
                        f"Beställ för hand om delarna behövs. {CART_LEFT}"):
            log.error("Expiry message for %s was not delivered.", booking_id)
            problem = True
    return expired, problem


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
    except Exception as e:
        orders_state.put(booking_id, "failed")
        if not notifier.send_message(
                config, f"Beställning misslyckades för {_label(booking_id, record)}: {e}. "
                        "Inget beställdes – beställ för hand."):
            log.error("Failure message for %s was not delivered.", booking_id)
        return True
    result = parts_order.execute_order(config, portal, booking_id, record["plan"], rules)
    log.info("Approved order for %s: %s", booking_id, result)
    return result != "ordered" or parts_order.order_notify_failed(booking_id)


def _cancel(config, session, booking_id, record, cb) -> bool:
    """Cancel one staged order. Returns True if the run must exit 1."""
    import notifier
    orders_state.put(booking_id, "cancelled")
    notifier.clear_buttons(config, record["message_id"])
    notifier.answer_callback(config, cb, "Avbruten.")
    tail = CART_CLEARED if _clear_cart(session, (record.get("plan") or {}).get("reg_nr")) \
        else CART_MANUAL
    log.info("Order for %s cancelled by the owner.", booking_id)
    if not notifier.send_message(config, f"Avbruten: {_label(booking_id, record)}. {tail}"):
        log.error("Cancel message for %s was not delivered.", booking_id)
        return True
    return False


def _handle_one(config, session, rules, update, owner: str) -> tuple[int, bool]:
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
    action, booking_id = parsed
    record = orders_state.get(booking_id)
    if record is None:
        notifier.answer_callback(config, cb, "Okänd bokning.")
        return 0, False
    stored = record.get("message_id")
    if not _is_int(stored) or update.get("message_id") != stored:
        notifier.answer_callback(config, cb, "Knappen är inte längre giltig.")
        return 0, False
    if record.get("status") != "pending":
        notifier.answer_callback(config, cb, "Redan hanterad.")
        return 0, False
    if action == "cancel":
        return 1, _cancel(config, session, booking_id, record, cb)
    if _too_old(record, datetime.now(timezone.utc)):
        notifier.answer_callback(config, cb, "Godkännandet har gått ut.")
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


def _handle(config: dict, portal_factory, rules) -> tuple[int, bool]:
    import notifier
    last = orders_state.last_update_id()
    owner = str(config.get("telegram", {}).get("chat_id") or "")
    actions, problem = 0, False
    try:
        with ExitStack() as stack:
            session = _Portal(portal_factory, stack)
            for update in notifier.get_updates(config, None if last is None else last + 1):
                try:
                    n, bad = _handle_one(config, session, rules, update, owner)
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


def handle_updates(config: dict, portal_factory, rules) -> int:
    """Handle the owner's Approve/Cancel taps. Returns the number of actions taken."""
    return _handle(config, portal_factory, rules)[0]


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
        has_pending = bool(orders_state.pending())
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

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    try:
        if not orders_state.pending():
            log.info("No pending approvals.")
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
    actions, problem = _handle(config, lambda: make_portal(config), rules)
    log.info("Approval poll done: %d action(s).", actions)
    return 1 if expire_problem or problem else 0


if __name__ == "__main__":
    sys.exit(main())
