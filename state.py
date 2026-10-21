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


# Bokningar whose type was only guessed and that showed no Hanterad button on two runs
# in a row: agent.py handles them like Offert/Förfrågan from then on (never Hanterad).
NO_BUTTON_FILE = Path("config/no_button_ids.txt")
# Guessed-type Bokningar that showed no Hanterad button once (the last time they were tried).
NO_BUTTON_SEEN_FILE = Path("config/no_button_seen_once.txt")


def _load_ids(path: Path) -> set:
    if not path.exists():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip()}


def _save_ids(path: Path, ids: set):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(ids)), encoding="utf-8")


def load_no_button_ids() -> set:
    return _load_ids(NO_BUTTON_FILE)


def save_no_button_ids(ids: set):
    _save_ids(NO_BUTTON_FILE, ids)


def load_no_button_seen_once() -> set:
    return _load_ids(NO_BUTTON_SEEN_FILE)


def save_no_button_seen_once(ids: set):
    _save_ids(NO_BUTTON_SEEN_FILE, ids)
