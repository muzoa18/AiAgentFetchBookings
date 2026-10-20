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

If a Telegram message fails, the booking stays unseen (retried next run),
Hanterad is skipped and run() returns exit code 1.

Run manually:   python agent.py
Schedule:       GitHub Actions cron (daily, Mon–Sat, ~17:00 Swedish time)
Config:         env vars / GitHub Secrets (see config_loader.py + README)
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from config_loader import load_config
from notifier import send_message
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

    seen_ids = load_seen_ids()
    log.info("Previously handled bookings: %d", len(seen_ids))

    # ── Fetch all new bookings from the portal Nya tab ────────────────────────
    log.info("Fetching new bookings from MECA portal ...")
    bookings = fetch_bookings(config)

    if not bookings:
        log.info("No new bookings found on the Nya tab. Exiting.")
        log_run("booking_agent", {"bookings_found": 0, "notified": 0, "notify_failed": 0})
        return 0

    log.info("Found %d new booking(s) to process.", len(bookings))
    new_count     = 0
    notify_failed = 0

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

        # (Parts ordering will hook in here, after a successful notify.)

        # ── Mark Hanterad (Bokning only — Förfrågan/Offert have no button) ───
        if booking_type != "Bokning":
            continue

        if not detail_url:
            log.warning("No detail URL for %s.", booking_id)
            continue

        log.info("Marking %s as Hanterad ...", booking_id)
        handled_ok = mark_booking_handled(config, detail_url)

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
    log.info("Done. Sent %d new notification(s), %d failed.", new_count, notify_failed)
    log_run("booking_agent", {
        "bookings_found": len(bookings),
        "notified":       new_count,
        "notify_failed":  notify_failed,
    })
    return 1 if notify_failed else 0


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
