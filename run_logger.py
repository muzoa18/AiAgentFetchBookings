"""
run_logger.py
-------------
Writes a structured JSON summary after every agent run.
Saved to logs/run_history.jsonl (one line per run).

improve.py reads this to understand patterns over time:
- Which bookings failed?
- Which parts searches failed?
- How many emails processed?
- Any recurring errors?
"""

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

HISTORY_FILE = Path("logs/run_history.jsonl")


def log_run(agent: str, summary: dict):
    """
    Write a run summary to the history file.

    Args:
        agent:   "booking_agent" | "email_agent" | "parts_agent"
        summary: dict with run stats and any errors
    """
    Path("logs").mkdir(exist_ok=True)

    entry = {
        "timestamp": datetime.now().isoformat(),
        "agent":     agent,
        **summary,
    }

    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    log.info("Run logged: %s | %s", agent, summary)


def read_recent_runs(n: int = 20) -> list[dict]:
    """Read the last N run summaries."""
    if not HISTORY_FILE.exists():
        return []
    lines = HISTORY_FILE.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(l) for l in lines[-n:] if l.strip()]
