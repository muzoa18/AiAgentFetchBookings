"""
MECA Booking Agent
==================
Workflow (runs on a schedule via GitHub Actions; see .github/workflows/):

  0. Trigger gate — in "email" mode, only proceed if MECA has emailed us
     that a booking was made (the email has no details, so we still scrape)
  1. Log in to MECA ProMeister portal
  2. Open /Booking/Index → click Nya tab
  3. For each new booking, open detail page and extract info
  4. Send SMS with booking details to configured numbers
  5. Click Hanterad → Bekräfta for confirmed Bokningar
  6. Förfrågan bookings: SMS sent, logged — no Hanterad (button doesn't exist)

Run manually:   python agent.py
Schedule:       GitHub Actions cron (every 30 min, business hours)
Config:         env vars / GitHub Secrets (see config_loader.py + README)
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from config_loader import load_config
from portal_scraper import fetch_bookings, mark_booking_handled
from run_logger import log_run
from sms_sender import send_sms
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


def run():
    log.info("=" * 55)
    log.info("MECA Agent starting — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("=" * 55)

    config   = load_config()

    # ── Trigger gate ─────────────────────────────────────────────────────────
    # In "email" mode we only scrape the portal when MECA has actually emailed
    # us that a booking was made. In "always" mode this is a no-op.
    from email_trigger import should_run
    if not should_run(config):
        log.info("No new booking signal — nothing to do. Exiting.")
        log_run("booking_agent", {"bookings_found": 0, "sms_sent": 0, "sms_failed": 0, "skipped": True})
        return

    seen_ids = load_seen_ids()
    log.info("Previously handled bookings: %d", len(seen_ids))

    # ── Fetch all new bookings from the portal Nya tab ────────────────────────
    log.info("Fetching new bookings from MECA portal ...")
    bookings = fetch_bookings(config)

    if not bookings:
        log.info("No new bookings found on the Nya tab. Exiting.")
        log_run("booking_agent", {"bookings_found": 0, "sms_sent": 0, "sms_failed": 0})
        return

    log.info("Found %d new booking(s) to process.", len(bookings))
    new_count  = 0
    sms_failed = 0

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

        # ── Send SMS ──────────────────────────────────────────────────────────
        message   = build_sms(booking)
        recipient = config["sms"]["recipient_number"]

        log.info("Sending SMS for %s (%s) ...", booking_id, booking_type)
        log.info("─── SMS Preview ───────────────────────────────")
        for line in message.splitlines():
            log.info("  %s", line)
        log.info("───────────────────────────────────────────────")

        sms_ok = send_sms(to=recipient, message=message, config=config)

        if not sms_ok:
            log.error("SMS failed for %s — skipping Hanterad.", booking_id)
            log.error("SMS failure details: %s", booking)
            seen_ids.add(booking_id)
            sms_failed += 1
            continue

        log.info("SMS sent successfully for %s.", booking_id)
        seen_ids.add(booking_id)
        new_count += 1

        # ── Mark Hanterad (Bokning only — Förfrågan has no button) ───────────
        if not detail_url:
            log.warning("No detail URL for %s.", booking_id)
            continue

        log.info("Marking %s as Hanterad ...", booking_id)
        handled_ok = mark_booking_handled(config, detail_url)

        if handled_ok is True:
            log.info("=" * 50)
            log.info("Booking %s CONFIRMED as Hanterad.", booking_id)
            log.info("=" * 50)

        elif handled_ok is None:
            # Expected for Förfrågan — no Hanterad button exists
            log.info(
                "Booking %s is a %s — no Hanterad button. "
                "Handle manually at: %s", booking_id, booking_type, detail_url
            )

        else:
            log.warning(
                "Could not mark %s as Hanterad. "
                "Handle manually at: %s", booking_id, detail_url
            )

    save_seen_ids(seen_ids)
    pending = sum(
        1 for b in bookings
        if b.get("id") in seen_ids
        and b.get("booking_type") in ("Förfrågan", "Offert")
    )
    log.info(
        "Done. Sent %d new SMS notification(s). %d pending Förfrågan/Offert still on Nya tab.",
        new_count, pending,
    )
    log_run("booking_agent", {
        "bookings_found": len(bookings),
        "sms_sent":       new_count,
        "sms_failed":     sms_failed,
        "pending_manual": pending,
    })


def build_sms(booking: dict) -> str:
    """Build SMS text from booking detail page data."""
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
    run()

# NOTE: parts_agent.py is referenced in SKILL.md as being launched for
# Förfrågan bookings, but agent.py currently has no import or call to it.
# Either restore parts_agent.py and wire it into the Förfrågan branch, or
# update SKILL.md to reflect that Förfrågan handling is manual-only.

