"""
MECA Booking Agent
==================
Workflow (runs once a day on GitHub Actions; see .github/workflows/):

  1. Log in to MECA ProMeister portal
  2. Open /Booking/Index → click Nya tab
  3. For each new booking, open detail page and extract info
  4. Send a Telegram message with booking details to the owner's chat
  5. Click Hanterad → Bekräfta for confirmed Bokningar
  6. Förfrågan/Offert bookings: notified and logged — no Hanterad (button doesn't exist)
  7. Notified Bokningar with parts/fluids: stage a pro.meca.se parts order for
     Telegram approval (parts_order.py). Runs after the loop so the parts
     portal's browser never overlaps the MECA portal's Hanterad browser.

If a Telegram message fails, the booking stays unseen (retried next run),
Hanterad is skipped and run() returns exit code 1. A parts order ending
"failed" (or an invalid rules file) also makes run() return 1.

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
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import orders_state
from config_loader import load_config
from notifier import send_message
from parts_matcher import make_claude_picker
from parts_order import needs_portal, order_notify_failed, plan_and_stage, record_failure
from rules import load_rules
from portal_scraper import fetch_bookings, mark_booking_handled
from run_logger import log_run
from state import load_seen_ids, save_seen_ids

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


def run() -> int:
    log.info("=" * 55)
    log.info("MECA Agent starting — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("=" * 55)

    config   = load_config()

    # Before this run queues anything: report orders a previous run left unfinished.
    swept_failed = _sweep_stale_orders(config)

    seen_ids = load_seen_ids()
    log.info("Previously handled bookings: %d", len(seen_ids))

    # ── Fetch all new bookings from the portal Nya tab ────────────────────────
    log.info("Fetching new bookings from MECA portal ...")
    bookings = fetch_bookings(config)

    if not bookings:
        log.info("No new bookings found on the Nya tab. Exiting.")
        log_run("booking_agent", {"bookings_found": 0, "notified": 0, "notify_failed": 0,
                                  "orders_staged": 0, "orders_failed": swept_failed})
        return 1 if swept_failed else 0

    log.info("Found %d new booking(s) to process.", len(bookings))
    new_count     = 0
    notify_failed = 0
    to_order      = []

    for booking in bookings:
        booking_id   = booking.get("id", "unknown")
        detail_url   = booking.get("detail_url", "")
        booking_type = booking.get("booking_type", "Bokning")

        if booking_id in seen_ids:
            if booking_type in ("Förfrågan", "Offert"):
                log.warning(
                    "PENDING %s %s — no Hanterad button, still needs manual action in portal: %s",
                    booking_type, booking_id, detail_url
                )
            else:
                log.info("Already handled: %s — skipping.", booking_id)
            continue

        # ── Send Telegram message ─────────────────────────────────────────────
        message = build_message(booking)

        log.info("Sending Telegram message for %s (%s) ...", booking_id, booking_type)
        log.info("─── Message Preview ───────────────────────────")
        for line in message.splitlines():
            log.info("  %s", line)
        log.info("───────────────────────────────────────────────")

        if not send_message(config, message):
            # Leave the id unseen so the next run retries; skip Hanterad.
            log.error("Telegram notification failed for %s — skipping Hanterad.", booking_id)
            log.error("Notification failure details: %s", booking)
            notify_failed += 1
            continue

        log.info("Notification sent successfully for %s.", booking_id)
        seen_ids.add(booking_id)
        new_count += 1

        # ── Mark Hanterad (Bokning only — Förfrågan/Offert have no button) ───
        if booking_type != "Bokning":
            continue

        if booking.get("parts") or booking.get("fluids"):
            try:
                orders_state.queue(booking.get("id"))
            except Exception:
                log.exception("Could not queue %s for parts ordering.", booking_id)
            to_order.append(booking)

        if not detail_url:
            log.warning("No detail URL for %s.", booking_id)
            continue

        log.info("Marking %s as Hanterad ...", booking_id)
        try:
            handled_ok = mark_booking_handled(config, detail_url)
        except Exception:
            log.exception("Marking %s as Hanterad raised.", booking_id)
            handled_ok = False

        if handled_ok is True:
            log.info("=" * 50)
            log.info("Booking %s CONFIRMED as Hanterad.", booking_id)
            log.info("=" * 50)
        else:
            log.warning(
                "Could not mark %s as Hanterad. "
                "Handle manually at: %s", booking_id, detail_url
            )

    save_seen_ids(seen_ids)

    orders_staged, orders_failed = _order_parts(config, to_order)
    orders_failed += swept_failed

    log.info("Done. Sent %d new notification(s), %d failed. Orders staged %d, failed %d.",
             new_count, notify_failed, orders_staged, orders_failed)
    log_run("booking_agent", {
        "bookings_found": len(bookings),
        "notified":       new_count,
        "notify_failed":  notify_failed,
        "orders_staged":  orders_staged,
        "orders_failed":  orders_failed,
    })
    return 1 if notify_failed or orders_failed else 0


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


def _open_ordering_tools(config: dict, stack: ExitStack) -> tuple:
    """(portal, picker), opened once per run. Raises with a short reason on any problem."""
    missing = _missing_ordering_settings(config)
    if missing:
        raise RuntimeError("inställning saknas: " + ", ".join(missing))
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
                            except Exception as e:
                                log.exception("Could not start parts ordering.")
                                setup_error = ("kunde inte starta beställningen "
                                               f"({type(e).__name__}: {e})")[:200]
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

    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(run())
