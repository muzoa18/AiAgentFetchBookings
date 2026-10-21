"""
MECA Booking Agent
==================
Workflow (runs once a day on GitHub Actions; see .github/workflows/):

  1. Log in to MECA ProMeister portal
  2. Open /Booking/Index → click Nya tab
  3. For each listed booking, open detail page and extract info
  4. Send a Telegram message with booking details to the owner's chat — ONCE per
     booking: seen_ids only means "the owner was notified".
  5. Förfrågan/Offert (no Hanterad button): local storage decides — notified once,
     then only a PENDING warning in the log while still listed.
  6. Bokning — the PORTAL is the truth: listed on the Nya tab = not handled, so it
     is worked on every run, quietly (no new message unless the outcome changes):
     - no parts (fluids only counts as no parts): Hanterad → Bekräfta every run
       until it succeeds; no order record.
     - with parts: never marked Hanterad here (the Klar flow does that). No order
       record -> queue + stage a pro.meca.se order for Telegram approval
       (parts_order.py); a 'blocked' record whose cart was never touched is
       re-staged (orders_state.restage, at most MAX_TRIES times); any other record
       is left alone. Staging runs after the loop so the parts portal's browser
       never overlaps the MECA portal's Hanterad browser.
     - specification not read completely (spec_fallback / spec_dropped): no parts
       may be missing parts, so Hanterad is NOT pressed; reported instead.
     - type only guessed by the scraper and no Hanterad button on two runs in a
       row: one notice, then handled like Offert/Förfrågan
       (config/no_button_seen_once.txt -> config/no_button_ids.txt).

If a Telegram message fails, the booking stays unseen (retried next run), nothing
else is done for it and run() returns exit code 1. A Hanterad that does not succeed
is reported to the owner and makes run() return 1. A parts order ending "failed"
(or an invalid rules file) also makes run() return 1.

Crash recovery: a booking is queued in config/orders.json right after its
notify succeeds (before seen_ids is saved). At the start of the next run any
record still "attempted" (queued, or interrupted mid-order) is reported to the
owner and marked failed, so a killed job is never silent.

Run manually:   python agent.py
Schedule:       GitHub Actions cron (daily, Mon–Sat, ~17:00 Swedish time)
Config:         env vars / GitHub Secrets (see config_loader.py + README)
"""

import logging
import sys
from collections import Counter
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import orders_state
from config_loader import load_config
from notifier import send_message
from parts_matcher import make_claude_picker
from parts_order import (give_up_if_capped, needs_portal, order_notify_failed, plan_and_stage,
                         record_failure, split_lines)
from rules import load_rules
from portal_scraper import PortalLoginError, fetch_bookings, mark_booking_handled
from run_logger import log_run
from state import (load_no_button_ids, load_no_button_seen_once, load_seen_ids,
                   save_no_button_ids, save_no_button_seen_once, save_seen_ids)

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/agent.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

LOGIN_FAILED_TEXT = ("MECA-inloggningen misslyckades – inga bokningar hämtades. "
                     "Kontrollera portal.username/portal.password i config/config.yaml (lokalt) "
                     "eller GitHub-secrets PORTAL_USERNAME/PORTAL_PASSWORD. Orsak: ")


# The only login reasons forwarded to the owner: the fixed Swedish texts of
# portal_scraper._auth_reason. Anything else (page error text) is logged only.
_FIXED_LOGIN_REASONS = ("Lösenordet har gått ut – byt lösenord i portalen",
                        "Kontot är tillfälligt avstängt", "Portalen avvisade inloggningen")


def _owner_login_reason(reason: str) -> str:
    import re

    import portal_scraper
    if reason in portal_scraper.AUTH_REASONS.values() or reason in _FIXED_LOGIN_REASONS \
            or re.fullmatch(r"Portalen avvisade inloggningen \([A-Za-z0-9]{1,40}\)", reason):
        return reason
    return "okänd orsak"


SPEC_INCOMPLETE_TEXT = ("Bokning {nr}: specifikationen kunde inte läsas fullständigt – jag har "
                        "inte markerat den som Hanterad. Kontrollera delarna i portalen.")
NO_BUTTON_TEXT = ("Bokning {nr} verkar sakna Hanterad-knapp (typen är osäker) – "
                  "kontrollera den i portalen.")


def _log_summary(bookings_found: int, counts: Counter, orders_staged: int, orders_failed: int,
                 login_failed: bool = False) -> None:
    """One log_run entry; the same keys on every path."""
    log_run("booking_agent", {
        "bookings_found": bookings_found,
        "notified":       counts["notified"],
        "notify_failed":  counts["notify_failed"],
        "orders_staged":  orders_staged,
        "orders_failed":  orders_failed + counts["give_up_failed"],
        "handled_failed": counts["handled_failed"],
        "login_failed":   login_failed,
    })


def run() -> int:
    log.info("=" * 55)
    log.info("MECA Agent starting — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("=" * 55)

    config   = load_config()

    # Before this run queues anything: report orders a previous run left unfinished.
    swept_failed = _sweep_stale_orders(config)

    seen_ids = load_seen_ids()
    no_button = load_no_button_ids()
    no_button_once = load_no_button_seen_once()
    log.info("Previously notified bookings: %d", len(seen_ids))
    counts = Counter()

    # ── Fetch all new bookings from the portal Nya tab ────────────────────────
    log.info("Fetching new bookings from MECA portal ...")
    try:
        bookings = fetch_bookings(config)
    except PortalLoginError as e:
        reason = str(e) or "okänd"
        log.error("MECA login failed — no bookings fetched. Reason: %s", reason)
        if not send_message(config, LOGIN_FAILED_TEXT + _owner_login_reason(reason)):
            log.error("Login-failure message was not delivered.")
        _log_summary(0, counts, 0, swept_failed, login_failed=True)
        return 1

    if not bookings:
        log.info("No new bookings found on the Nya tab. Exiting.")
        _log_summary(0, counts, 0, swept_failed)
        return 1 if swept_failed else 0

    log.info("Found %d booking(s) on the Nya tab.", len(bookings))
    to_order = []
    no_button_before, once_before = set(no_button), set(no_button_once)

    for booking in bookings:
        try:
            _process_booking(config, booking, seen_ids, (no_button, no_button_once),
                             to_order, counts)
        except Exception:
            log.exception("Processing %s raised — continuing with the next booking.",
                          booking.get("id", "unknown"))
            counts["errors"] += 1

    # Queued records (orders_state.queue/restage) are written before seen_ids.
    save_seen_ids(seen_ids)
    if no_button != no_button_before:
        save_no_button_ids(no_button)
    if no_button_once != once_before:
        save_no_button_seen_once(no_button_once)

    orders_staged, orders_failed = _order_parts(config, to_order)
    orders_failed += swept_failed

    log.info("Done. Sent %d new notification(s), %d failed. Orders staged %d, failed %d. "
             "Hanterad failed %d. Booking errors %d.",
             counts["notified"], counts["notify_failed"], orders_staged,
             orders_failed + counts["give_up_failed"], counts["handled_failed"], counts["errors"])
    _log_summary(len(bookings), counts, orders_staged, orders_failed)
    failed = (counts["notify_failed"] or orders_failed or counts["give_up_failed"]
              or counts["handled_failed"] or counts["errors"])
    return 1 if failed else 0


def _process_booking(config: dict, booking: dict, seen_ids: set, no_button_lists: tuple,
                     to_order: list, counts: Counter) -> None:
    """Decide what this run does for one booking listed on the Nya tab.

    no_button_lists = (no_button ids, ids with one missing-button result so far).
    """
    no_button, no_button_once = no_button_lists
    booking_id   = booking.get("id", "unknown")
    detail_url   = booking.get("detail_url", "")
    booking_type = booking.get("booking_type", "Bokning")
    seen         = booking_id in seen_ids

    if booking_type != "Bokning" or booking_id in no_button:
        # No Hanterad button: local storage decides (notify once, then only warn).
        if seen:
            log.warning(
                "PENDING %s %s — no Hanterad button, still needs manual action in portal: %s",
                booking_type, booking_id, detail_url
            )
            return
        _notify(config, booking, seen_ids, counts)
        return

    # Bokning: the portal is the truth — listed on the Nya tab means not handled.
    if seen:
        log.info("Already notified: %s — still listed on the Nya tab.", booking_id)
    elif not _notify(config, booking, seen_ids, counts):
        return

    order_lines, _ = split_lines(booking)
    if order_lines:
        _plan_parts_order(config, booking, seen, to_order, counts)
        return

    # "No parts" is only trusted when the whole specification was read.
    if _spec_incomplete(booking):
        log.warning("Specification of %s was not read completely — not marking it Hanterad.",
                    booking_id)
        nr = booking.get("booking_nr") or booking_id
        if not send_message(config, SPEC_INCOMPLETE_TEXT.format(nr=nr)):
            log.error("Incomplete-specification message for %s was not delivered.", booking_id)
        counts["handled_failed"] += 1
        return

    # No parts (fluids are only listed for the owner to prepare): Hanterad now.
    if not detail_url:
        log.warning("No detail URL for %s.", booking_id)
        return
    handled = _mark_handled(config, booking, retry=seen)
    if handled is not None:
        no_button_once.discard(booking_id)   # "twice in a row" starts over
        if handled is False:
            counts["handled_failed"] += 1
        return
    if booking_id not in no_button_once:
        log.info("No Hanterad button on %s (type guessed) — checking again next run.", booking_id)
        no_button_once.add(booking_id)
        return
    nr = booking.get("booking_nr") or booking_id
    if not send_message(config, NO_BUTTON_TEXT.format(nr=nr)):
        log.error("No-button notice for %s was not delivered — trying again next run.",
                  booking_id)
        counts["handled_failed"] += 1
        return
    log.info("No Hanterad button on %s twice in a row — handled like Offert/Förfrågan "
             "from now on.", booking_id)
    no_button_once.discard(booking_id)
    no_button.add(booking_id)


def _spec_incomplete(booking: dict) -> bool:
    """True when the scraper says the specification was not read completely."""
    dropped = booking.get("spec_dropped")
    return bool(booking.get("spec_fallback")) or (
        isinstance(dropped, int) and not isinstance(dropped, bool) and dropped > 0)


def _notify(config: dict, booking: dict, seen_ids: set, counts: Counter) -> bool:
    """Send the one notification for a booking; on success it becomes seen."""
    booking_id   = booking.get("id", "unknown")
    message = build_message(booking)

    log.info("Sending Telegram message for %s (%s) ...", booking_id,
             booking.get("booking_type", "Bokning"))
    # Only the length: the text holds customer data and logs/agent.log is uploaded.
    log.info("Message for %s (%s): %d characters.", booking_id,
             booking.get("booking_nr") or "–", len(message))

    if not send_message(config, message):
        # Leave the id unseen so the next run retries; nothing else is done for it.
        log.error("Telegram notification failed for %s — skipping it this run.", booking_id)
        log.error("Notification failure details: id=%s type=%s", booking_id,
                  booking.get("booking_type", "Bokning"))
        counts["notify_failed"] += 1
        return False

    log.info("Notification sent successfully for %s.", booking_id)
    seen_ids.add(booking_id)
    counts["notified"] += 1
    return True


def _plan_parts_order(config: dict, booking: dict, seen: bool, to_order: list,
                      counts: Counter) -> None:
    """A Bokning with parts: queue it, quietly re-stage a blocked order, or leave it alone."""
    booking_id = booking.get("id")
    try:
        record = orders_state.get(booking_id)
    except Exception:
        log.exception("Order state unreadable for %s.", booking_id)
        if not seen:
            to_order.append(booking)  # plan_and_stage reports the state problem
        return

    if record is None:
        try:
            orders_state.queue(booking_id)
        except Exception:
            log.exception("Could not queue %s for parts ordering.", booking_id)
        to_order.append(booking)
        return

    status = record.get("status")
    if status == "blocked":
        if orders_state.restage(booking_id):
            log.info("Blocked order for %s re-staged quietly (try %d).",
                     booking_id, orders_state.get(booking_id).get("tries", 0))
            to_order.append(booking)
            return
        try:
            if give_up_if_capped(config, booking) is False:
                counts["give_up_failed"] += 1   # not flagged: sent again next run
        except Exception:
            log.exception("Give-up check failed for %s.", booking_id)
            counts["give_up_failed"] += 1
    log.info("Parts order for %s is '%s' — left alone.", booking_id, status)


def _mark_handled(config: dict, booking: dict, retry: bool = False) -> bool | None:
    """Click Hanterad for a Bokning without parts.

    True = marked. None = no Hanterad button on a booking whose type was only guessed
    (logged, not reported; the caller decides). Anything else is reported to the owner
    and returns False.
    """
    booking_id = booking.get("id", "unknown")
    detail_url = booking.get("detail_url", "")
    log.info("Marking %s as Hanterad%s ...", booking_id, " (retry)" if retry else "")
    try:
        handled_ok = mark_booking_handled(config, detail_url)
    except Exception:
        log.exception("Marking %s as Hanterad raised.", booking_id)
        handled_ok = False

    if handled_ok is True:
        log.info("=" * 50)
        log.info("Booking %s CONFIRMED as Hanterad%s.", booking_id, " (retry)" if retry else "")
        log.info("=" * 50)
        return True

    if handled_ok is None and booking.get("booking_type_guess") is True:
        log.info("No Hanterad button on %s and its type was only guessed.", booking_id)
        return None

    log.warning("Could not mark %s as Hanterad (%s). Handle manually at: %s",
                booking_id, handled_ok, detail_url)
    nr = booking.get("booking_nr") or booking_id
    text = (f"Kunde inte markera bokning {nr} som Hanterad i portalen – "
            f"gör det för hand: {detail_url}")
    if not send_message(config, text):
        log.error("Hanterad-failure message for %s was not delivered.", booking_id)
    return False


def _sweep_stale_orders(config: dict) -> int:
    """Report and fail every order record left 'attempted' by an earlier run. Returns count."""
    try:
        stale = orders_state.by_status("attempted")
    except Exception:
        log.exception("Order state file unreadable — cannot check for interrupted orders.")
        send_message(config, "Orderstatusfilen (config/orders.json) kunde inte läsas – "
                             "kontrollera den. Inga beställningar görs.")
        return 1
    for booking_id, record in stale:
        if orders_state.is_queued(record):
            text = (f"Delbeställning för {booking_id} kördes aldrig (jobbet avbröts). "
                    "Beställ för hand.")
        else:
            text = (f"Delbeställning för {booking_id} avbröts mitt i körningen: "
                    "kontrollera pro.meca.se, ordern kan ha lagts.")
        log.error("Stale order record %s (%s).", booking_id, record.get("plan"))
        if not send_message(config, text):
            log.error("Stale-order message for %s was not delivered.", booking_id)
        try:
            orders_state.put(booking_id, "failed")
        except Exception:
            log.exception("Could not mark stale order %s failed.", booking_id)
    return len(stale)


def _missing_ordering_settings(config: dict) -> list[str]:
    needed = [("anthropic", "api_key"), ("parts_portal", "username"), ("parts_portal", "password")]
    missing = []
    for section, key in needed:
        value = (config.get(section) or {}).get(key)
        if not value or (isinstance(value, str) and value.startswith("YOUR_")):
            missing.append(f"{section}.{key}")
    return missing


class _SetupError(RuntimeError):
    """A setup problem with a fixed Swedish text (setting names only): safe for the owner."""


def _open_ordering_tools(config: dict, stack: ExitStack) -> tuple:
    """(portal, picker), opened once per run. Raises with a short reason on any problem."""
    missing = _missing_ordering_settings(config)
    if missing:
        raise _SetupError("inställning saknas: " + ", ".join(missing))
    picker = make_claude_picker(config["anthropic"]["api_key"])
    from parts_portal import PartsPortal  # lazy: Task 6 module, may be missing
    portal = stack.enter_context(PartsPortal(config))
    return portal, picker


def _order_parts(config: dict, bookings: list[dict]) -> tuple[int, int]:
    """Stage parts orders for notified Bokningar. Returns (staged, failed); never raises."""
    if not bookings:
        return 0, 0
    try:
        rules = load_rules()
    except Exception:
        log.exception("parts_rules.yaml is invalid — no parts ordering this run.")
        send_message(config, "Regelfilen är ogiltig – inga beställningar")
        # Close the queued records now, so the next run does not report them as aborted.
        statuses = [record_failure(config, b, "regelfilen parts_rules.yaml är ogiltig")
                    for b in bookings]
        return 0, 1 + statuses.count("failed")

    staged = failed = 0
    tools, setup_error = None, None
    try:
        with ExitStack() as stack:
            for booking in bookings:
                try:
                    if not needs_portal(booking):
                        status = plan_and_stage(config, booking, None, rules, None)
                    else:
                        if tools is None and setup_error is None:
                            try:
                                tools = _open_ordering_tools(config, stack)
                            except _SetupError as e:
                                log.exception("Could not start parts ordering.")
                                setup_error = f"kunde inte starta beställningen ({e})"[:200]
                            except Exception:
                                # The raw text (page text, internals) goes to the log only.
                                log.exception("Could not start parts ordering.")
                                setup_error = "kunde inte starta beställningen"
                        if setup_error:
                            status = record_failure(config, booking, setup_error)
                        else:
                            status = plan_and_stage(config, booking, tools[0], rules, tools[1])
                except Exception:
                    log.exception("Parts ordering raised for %s.", booking.get("id"))
                    status = "failed"
                log.info("Parts order for %s: %s", booking.get("id"), status)
                if status in ("pending", "ordered"):
                    staged += 1
                if status == "failed" or (status == "ordered"
                                          and order_notify_failed(booking.get("id"))):
                    failed += 1
    except Exception:
        log.exception("Closing the parts portal failed.")
    return staged, failed


def build_message(booking: dict) -> str:
    """Build notification text from booking detail page data."""
    btype    = booking.get("booking_type", "Bokning")
    nr       = booking.get("booking_nr", "–")
    name     = booking.get("driver_name", "–")
    phone    = booking.get("driver_phone", "")
    email    = booking.get("driver_email", "")
    reg      = booking.get("reg_nr", "–")
    vehicle  = booking.get("vehicle", "")
    date     = booking.get("workshop_dt", "–")
    msg      = (booking.get("booking_msg") or "").strip()
    total    = booking.get("total_price", "")
    services = booking.get("services", [])
    parts    = booking.get("parts", [])
    fluids   = booking.get("fluids", [])

    vehicle_str = f"{reg} {vehicle}".strip() if vehicle else reg

    lines = [f"Ny {btype} - {nr}", f"Kund: {name}"]
    if phone:
        lines.append(f"Tel: {phone}")
    if email:
        lines.append(f"Email: {email}")
    lines.append(f"Fordon: {vehicle_str}")
    lines.append(f"Datum: {date}")
    if msg:
        lines.append(f"Meddelande: {msg}")

    if services:
        lines.append("---")
        lines.append("Tjänster:")
        for s in services:
            if s.get("name"):
                lines.append(f"- {s['name']}")

    if parts:
        lines.append("Delar:")
        for p in parts:
            n   = p.get("name", "")
            qty = p.get("qty", "")
            if n:
                lines.append(f"- {n}{' x' + qty if qty and qty != '1' else ''}")

    if fluids:
        lines.append("Vätskor/Oljor:")
        for f in fluids:
            n   = f.get("name", "")
            qty = f.get("qty", "")
            if n:
                lines.append(f"- {n}{' x' + qty if qty and qty != '1' else ''}")

    if total:
        lines.append("---")
        lines.append(f"Totalt: {total}")

    # Counts only, never row text: unread rows may come from the event log (staff names).
    dropped = booking.get("spec_dropped")
    if isinstance(dropped, int) and not isinstance(dropped, bool) and dropped > 0:
        lines.append(f"Obs: {dropped} rader i specifikationen kunde inte läsas – "
                     "kontrollera bokningen i portalen.")
    elif booking.get("spec_fallback"):
        lines.append("Obs: specifikationen lästes i reservläge – kontrollera delarna i portalen.")

    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(run())
