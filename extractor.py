"""
extractor.py
------------
Uses Claude to extract structured booking information
from raw scraped text.

Returns a clean dict with customer name, phone, vehicle reg,
service type, date, and any notes.
"""

import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    log.warning("anthropic package not installed. Run:  pip install anthropic")


SYSTEM_PROMPT = """
You are a data extraction assistant for a Swedish car workshop (MECA).
You receive raw text scraped from a booking portal and must extract structured information.

Always respond with ONLY a valid JSON object — no explanation, no markdown code fences.

If a field is not found in the text, use null.

Required JSON structure:
{
  "customer_name":  "Full name of the customer",
  "customer_phone": "Phone number in international format if possible, e.g. +46701234567",
  "vehicle_reg":    "Vehicle registration number, e.g. ABC123",
  "service_type":   "Type of service or repair requested",
  "booking_date":   "Date and time of the booking, e.g. 2025-04-10 09:00",
  "notes":          "Any additional notes or special requests"
}
"""


def extract_booking_info(raw: dict, config: dict) -> Optional[dict]:
    """
    Send the raw booking text to Claude and return extracted fields.

    Args:
        raw:    Dict with 'id' and 'raw_text' from the scraper.
        config: App config (used to get the Anthropic API key).

    Returns:
        Dict with extracted booking fields, or None on failure.
    """
    if not ANTHROPIC_AVAILABLE:
        log.error("anthropic package not available. Cannot extract booking info.")
        return None

    api_key = config.get("anthropic", {}).get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("No Anthropic API key found. Set it in config.yaml or ANTHROPIC_API_KEY env var.")
        return None

    raw_text = raw.get("raw_text", "")
    if not raw_text.strip():
        log.warning("Empty raw text for booking %s.", raw.get("id"))
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Extract booking information from this text:\n\n{raw_text[:4000]}"
                }
            ],
        )

        content = response.content[0].text.strip()

        # Strip accidental markdown fences if present
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        booking = json.loads(content)
        return booking

    except json.JSONDecodeError as e:
        log.error("Claude returned invalid JSON for booking %s: %s", raw.get("id"), e)
        return None
    except Exception as e:
        log.exception("Error calling Claude API for booking %s: %s", raw.get("id"), e)
        return None
