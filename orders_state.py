"""
orders_state.py
---------------
Per-booking order status store. Guarantees a booking is never ordered twice
and drives the Telegram approval flow.

Stored as config/orders.json (committed back to the repo by the workflows):

    {"last_update_id": int | null,
     "bookings": {"<booking_id>": {"status", "plan", "message_id", "updated_at"}}}

Single-writer assumption: bookings.yml and approve.yml share one concurrency
group, so there is never a concurrent writer. No file locking is used.

A corrupt file raises ValueError rather than being treated as empty, because
an empty state would allow a booking to be ordered again.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args

ORDERS_FILE = Path("config/orders.json")

Status = Literal["attempted", "pending", "ordered", "blocked", "failed", "cancelled", "expired"]
STATUSES = get_args(Status)


def _load() -> dict:
    if not ORDERS_FILE.exists():
        return {"last_update_id": None, "bookings": {}}
    try:
        data = json.loads(ORDERS_FILE.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"Corrupt order state file {ORDERS_FILE}: {e}") from e
    _validate(data)
    return data


def _validate(data) -> None:
    def bad(why: str):
        return ValueError(f"Corrupt order state file {ORDERS_FILE}: {why}")

    if not isinstance(data, dict):
        raise bad("top level is not an object")
    if not isinstance(data.get("bookings"), dict):
        raise bad("'bookings' missing or not an object")
    if "last_update_id" not in data:
        raise bad("'last_update_id' missing")
    luid = data["last_update_id"]
    if luid is not None and (isinstance(luid, bool) or not isinstance(luid, int)):
        raise bad("'last_update_id' is not an int or null")
    for bid, rec in data["bookings"].items():
        if not isinstance(rec, dict):
            raise bad(f"record for booking {bid!r} is not an object")


def _check_id(booking_id) -> None:
    if not isinstance(booking_id, str):
        raise TypeError(f"booking_id must be str, got {type(booking_id).__name__}")
    if not booking_id.strip():
        raise ValueError("booking_id must not be empty")


def _save(data: dict) -> None:
    ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ORDERS_FILE.parent, prefix=ORDERS_FILE.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        os.replace(tmp, ORDERS_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(booking_id: str) -> dict | None:
    _check_id(booking_id)
    return _load()["bookings"].get(booking_id)


def put(booking_id: str, status: Status, plan: dict | None = None,
        message_id: int | None = None) -> None:
    """Set status; plan/message_id are kept from the existing record when None."""
    _check_id(booking_id)
    if status not in STATUSES:
        raise ValueError(f"Unknown order status: {status!r}")
    data = _load()
    old = data["bookings"].get(booking_id) or {}
    data["bookings"][booking_id] = {
        "status": status,
        "plan": plan if plan is not None else old.get("plan"),
        "message_id": message_id if message_id is not None else old.get("message_id"),
        "updated_at": _now(),
    }
    _save(data)


def claim(booking_id: str) -> bool:
    """Mark 'attempted' only if no record exists. Returns False otherwise."""
    _check_id(booking_id)
    data = _load()
    if booking_id in data["bookings"]:
        return False
    data["bookings"][booking_id] = {
        "status": "attempted", "plan": None, "message_id": None, "updated_at": _now(),
    }
    _save(data)
    return True


def pending() -> list[tuple[str, dict]]:
    bookings = _load()["bookings"]
    return [(bid, bookings[bid]) for bid in sorted(bookings)
            if bookings[bid].get("status") == "pending"]


def last_update_id() -> int | None:
    return _load()["last_update_id"]


def set_last_update_id(n: int) -> None:
    data = _load()
    data["last_update_id"] = n
    _save(data)
