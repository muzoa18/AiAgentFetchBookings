"""
email_trigger.py
----------------
Decides whether the booking workflow should run, based on whether MECA has
sent a new "a booking was made" notification email to Gmail.

The MECA notification email itself contains no booking details — it only tells
us that *something* was booked. So this module is purely a GATE: if a new
notification is found, agent.py logs into the portal and scrapes the actual
details. This keeps the (fragile, heavier) portal automation from running on
every scheduled tick.

Behaviour:
  - trigger.mode == "always"  → always returns True (no Gmail needed).
  - trigger.mode == "email"   → returns True only if a not-yet-seen MECA
                                booking email matches the configured query.
  - On ANY Gmail error (missing creds, auth failure, library not installed)
    it FAILS OPEN (returns True) and logs a warning — we would rather scrape
    unnecessarily than silently miss a real booking.

Seen message IDs are tracked in config/seen_email_ids.txt so the same
notification doesn't trigger repeated runs.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

GMAIL_TOKEN_PATH = Path("config/gmail_token.json")
GMAIL_CREDS_PATH = Path("config/gmail_credentials.json")
SEEN_EMAIL_IDS   = Path("config/seen_email_ids.txt")
GMAIL_SCOPES     = ["https://www.googleapis.com/auth/gmail.modify"]


def should_run(config: dict) -> bool:
    """Return True if the booking workflow should proceed."""
    mode = str(config.get("trigger", {}).get("mode", "always")).lower()

    if mode != "email":
        log.info("Trigger mode '%s' — running unconditionally.", mode)
        return True

    query = config.get("trigger", {}).get(
        "email_query", "newer_than:1d (subject:bokning OR subject:booking)"
    )

    try:
        return _has_new_booking_email(query)
    except Exception as exc:  # fail OPEN — never miss a booking due to Gmail issues
        log.warning(
            "Email trigger check failed (%s). Failing OPEN and running the "
            "booking scrape anyway.", exc
        )
        return True


def _has_new_booking_email(query: str) -> bool:
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Gmail libraries not installed (google-api-python-client, "
            "google-auth). Add them to requirements.txt."
        ) from exc

    if not GMAIL_TOKEN_PATH.exists():
        raise RuntimeError(f"{GMAIL_TOKEN_PATH} not found — Gmail not authorised.")

    creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH), GMAIL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        log.info("Refreshing expired Gmail token ...")
        creds.refresh(Request())
        GMAIL_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    if not creds or not creds.valid:
        raise RuntimeError("Gmail credentials are not valid.")

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    log.info("Checking Gmail for new MECA booking notifications (query: %s)", query)

    resp = service.users().messages().list(userId="me", q=query, maxResults=25).execute()
    messages = resp.get("messages", [])
    if not messages:
        log.info("No matching booking emails found — skipping portal scrape.")
        return False

    seen = _load_seen()
    new_ids = [m["id"] for m in messages if m["id"] not in seen]

    if not new_ids:
        log.info("Found %d booking email(s), all already processed — skipping.", len(messages))
        return False

    log.info("Found %d NEW booking notification email(s) — triggering scrape.", len(new_ids))
    _save_seen(seen | set(new_ids))
    return True


def _load_seen() -> set:
    if not SEEN_EMAIL_IDS.exists():
        return set()
    return {
        line.strip()
        for line in SEEN_EMAIL_IDS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _save_seen(ids: set) -> None:
    SEEN_EMAIL_IDS.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file bounded — last 500 ids is plenty for dedupe.
    trimmed = sorted(ids)[-500:]
    SEEN_EMAIL_IDS.write_text("\n".join(trimmed), encoding="utf-8")
