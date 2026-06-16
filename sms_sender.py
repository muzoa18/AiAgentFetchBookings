"""
sms_sender.py
-------------
Sends SMS messages via either 46elks (Swedish, recommended)
or Twilio (international).

Configure your preferred provider in config.yaml under [sms].
"""

import logging
import urllib.parse
import urllib.request
import urllib.error
import base64
import json

log = logging.getLogger(__name__)


def send_sms(to: str, message: str, config: dict) -> bool:
    """
    Send an SMS to one or more recipients.

    'to' can be a single number or comma-separated list:
        "+46701234567"
        "+46701234567,+46739999999,+46701111111"

    recipient_number in config.yaml works the same way.
    Returns True if ALL messages sent successfully.
    """
    # Support comma-separated numbers in both the 'to' argument
    # and the config recipient_number field
    recipients = [r.strip() for r in to.split(",") if r.strip()]

    if not recipients:
        log.error("No recipient numbers provided.")
        return False
    
    log.info("Sending SMS to %d recipient(s): %s", len(recipients), ", ".join(recipients))

    provider = config.get("sms", {}).get("provider", "46elks").lower()
    all_ok = True

    for number in recipients:
        log.info("Sending to %s ...", number)
        if provider == "46elks":
            ok = _send_46elks(number, message, config)
        elif provider == "twilio":
            ok = _send_twilio(number, message, config)
        else:
            log.error("Unknown SMS provider '%s'.", provider)
            ok = False

        if not ok:
            log.error("Failed to send to %s.", number)
            all_ok = False

    return all_ok


# ── 46elks ────────────────────────────────────────────────────────────────────

def _send_46elks(to: str, message: str, config: dict) -> bool:
    """Send via 46elks API (https://46elks.se)."""
    sms_cfg = config.get("sms", {})
    api_user     = sms_cfg.get("api_user", "")
    api_password = sms_cfg.get("api_password", "")
    sender       = sms_cfg.get("sender_name", "MECA")  # Max 11 chars

    if not api_user or not api_password:
        log.error("46elks credentials missing. Set sms.api_user and sms.api_password in config.yaml.")
        return False

    url = "https://api.46elks.com/a1/sms"
    payload = urllib.parse.urlencode({
        "from": sender[:11],
        "to":   to,
        "message": message,
    }).encode("utf-8")

    credentials = base64.b64encode(f"{api_user}:{api_password}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body   = resp.read().decode("utf-8")
            result = json.loads(body)

            # 46elks returns a dict for one recipient, a list for multiple
            if isinstance(result, list):
                # All items in the list are individual send results
                failed = [r for r in result if r.get("status") not in ("created", "delivered", "sent")]
                if failed:
                    log.error("46elks some sends failed: %s", failed)
                    return False
                log.info("46elks SMS sent to %d recipient(s).", len(result))
                return True
            else:
                status = result.get("status", "")
                if status in ("created", "delivered", "sent"):
                    log.info("46elks SMS sent. ID: %s", result.get("id"))
                    return True
                else:
                    log.error("46elks unexpected response: %s", result)
                    return False
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        log.error("46elks HTTP error %s: %s", e.code, error_body)
        log.error("Request was to %s with payload length %d", to, len(message))
        return False
    except Exception as e:
        log.exception("46elks send failed: %s", e)
        return False


# ── Twilio ────────────────────────────────────────────────────────────────────

def _send_twilio(to: str, message: str, config: dict) -> bool:
    """Send via Twilio REST API."""
    sms_cfg      = config.get("sms", {})
    account_sid  = sms_cfg.get("twilio_account_sid", "")
    auth_token   = sms_cfg.get("twilio_auth_token", "")
    from_number  = sms_cfg.get("twilio_from_number", "")

    if not all([account_sid, auth_token, from_number]):
        log.error("Twilio credentials missing. Set twilio_account_sid, twilio_auth_token, twilio_from_number.")
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = urllib.parse.urlencode({
        "From": from_number,
        "To":   to,
        "Body": message,
    }).encode("utf-8")

    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
            if result.get("sid"):
                log.info("Twilio SMS sent. SID: %s", result["sid"])
                return True
            else:
                log.error("Twilio unexpected response: %s", result)
                return False
    except urllib.error.HTTPError as e:
        log.error("Twilio HTTP error %s: %s", e.code, e.read().decode())
        return False
    except Exception as e:
        log.exception("Twilio send failed: %s", e)
        return False
