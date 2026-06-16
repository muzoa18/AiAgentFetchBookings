"""
config_loader.py
----------------
Loads configuration and validates required fields.

Resolution order (later wins):
  1. config/config.yaml         — local development (gitignored)
  2. Environment variables      — GitHub Actions / any host (GitHub Secrets)

In CI there is no config.yaml, so the whole config is built from env vars.
Locally you can keep using config.yaml exactly as before, and any env var
that is set will override the matching yaml field.
"""

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

CONFIG_PATH = Path("config/config.yaml")

# Fields that MUST be present (after env overlay) or we refuse to run.
REQUIRED_FIELDS = [
    ("portal", "username"),
    ("portal", "password"),
    ("sms", "recipient_number"),
]

# Maps (section, key) -> environment variable name.
ENV_MAP = {
    ("portal", "username"):            "PORTAL_USERNAME",
    ("portal", "password"):            "PORTAL_PASSWORD",
    ("portal", "bookings_path"):       "PORTAL_BOOKINGS_PATH",
    ("portal", "debug"):               "PORTAL_DEBUG",
    ("anthropic", "api_key"):          "ANTHROPIC_API_KEY",
    ("sms", "provider"):               "SMS_PROVIDER",
    ("sms", "recipient_number"):       "SMS_RECIPIENT_NUMBER",
    ("sms", "sender_name"):            "SMS_SENDER_NAME",
    ("sms", "api_user"):               "SMS_API_USER",
    ("sms", "api_password"):           "SMS_API_PASSWORD",
    ("sms", "twilio_account_sid"):     "TWILIO_ACCOUNT_SID",
    ("sms", "twilio_auth_token"):      "TWILIO_AUTH_TOKEN",
    ("sms", "twilio_from_number"):     "TWILIO_FROM_NUMBER",
    ("trigger", "mode"):               "BOOKING_TRIGGER_MODE",
    ("trigger", "email_query"):        "BOOKING_EMAIL_QUERY",
    ("parts_portal", "username"):      "PARTS_PORTAL_USERNAME",
    ("parts_portal", "password"):      "PARTS_PORTAL_PASSWORD",
    ("parts_portal", "test_email"):    "PARTS_PORTAL_TEST_EMAIL",
    ("workshop", "phone"):             "WORKSHOP_PHONE",
    ("workshop", "email"):             "WORKSHOP_EMAIL",
    ("workshop", "name"):              "WORKSHOP_NAME",
    ("workshop", "city"):              "WORKSHOP_CITY",
}

# Sensible defaults so the system runs with the minimum set of secrets.
DEFAULTS = {
    ("portal", "bookings_path"): "/Booking/Index",
    ("portal", "debug"):         False,
    ("sms", "provider"):         "46elks",
    ("sms", "sender_name"):      "MECA",
    ("trigger", "mode"):         "always",
    ("trigger", "email_query"):  "newer_than:1d (subject:bokning OR subject:booking)",
}

_BOOL_FIELDS = {("portal", "debug")}


def _coerce(section: str, key: str, value):
    """Coerce known boolean fields from strings like 'true'/'1'."""
    if (section, key) in _BOOL_FIELDS and isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value


def load_config() -> dict:
    """Load config from yaml (if present) then overlay env vars. Exits on critical errors."""
    config: dict = {}

    # ── 1. Base from yaml (local dev) ────────────────────────────────────────
    if CONFIG_PATH.exists():
        if not YAML_AVAILABLE:
            log.error("PyYAML not installed but config.yaml present. Run: pip install pyyaml")
            sys.exit(1)
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        log.info("Loaded base config from %s", CONFIG_PATH)
    else:
        log.info("No config.yaml found — building config from environment variables.")

    # ── 2. Apply defaults (only where nothing is set yet) ────────────────────
    for (section, key), default in DEFAULTS.items():
        config.setdefault(section, {})
        config[section].setdefault(key, default)

    # ── 3. Overlay environment variables (these win) ─────────────────────────
    for (section, key), env_name in ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is not None and raw != "":
            config.setdefault(section, {})
            config[section][key] = _coerce(section, key, raw)

    # ── 4. Validate required fields ──────────────────────────────────────────
    errors = []
    for section, key in REQUIRED_FIELDS:
        val = config.get(section, {}).get(key, "")
        if not val or (isinstance(val, str) and val.startswith("YOUR_")):
            env_name = ENV_MAP.get((section, key), "?")
            errors.append(f"  {section}.{key} is not set (env: {env_name})")

    if errors:
        log.error(
            "Config is incomplete. Set these via config/config.yaml (local) "
            "or GitHub Secrets (CI):\n%s",
            "\n".join(errors),
        )
        sys.exit(1)

    return config
