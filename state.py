"""
state.py
--------
Persists the set of booking IDs we have already sent SMS for,
so we don't send duplicate messages on each run.

Stored as a plain text file: config/seen_ids.txt
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

STATE_FILE = Path("config/seen_ids.txt")


def load_seen_ids() -> set:
    """Load the set of booking IDs already notified."""
    if not STATE_FILE.exists():
        return set()
    lines = STATE_FILE.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip()}


def save_seen_ids(seen_ids: set):
    """Save the set of notified booking IDs."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text("\n".join(sorted(seen_ids)), encoding="utf-8")
    log.debug("Saved %d seen booking ID(s).", len(seen_ids))
