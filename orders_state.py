"""
orders_state.py
---------------
Per-booking order status store. Guarantees a booking is never ordered twice
and drives the Telegram approval flow.

Stored as config/orders.json (committed back to the repo by the workflows):

    {"last_update_id": int | null,
     "bookings": {"<booking_id>": {"status", "plan", "message_id", "updated_at",
                                   optional: "cart_touched", "tries", "detail_url",
                                   "handled", "last_reason", "gave_up",
                                   "klar_message_id", "klar_sent_at", "klar_closed"}}}

Optional fields (absent in older records) are kept by put()/begin() and set with
update_fields(). Unknown record keys are rejected. Never store customer data here.

Single-writer assumption: bookings.yml and approve.yml share one concurrency
group, so there is never a concurrent writer. No file locking is used.

A corrupt file raises ValueError rather than being treated as empty, because
an empty state would allow a booking to be ordered again.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, get_args

ORDERS_FILE = Path("config/orders.json")

Status = Literal["attempted", "pending", "ordered", "blocked", "failed", "cancelled", "expired"]
STATUSES = get_args(Status)

BASE_FIELDS = ("status", "plan", "message_id", "updated_at")
# Optional per-record fields and their exact types (bool is not int).
OPTIONAL_FIELDS = {
    "cart_touched": bool,   # parts_order added something to the pro.meca.se cart
    "tries": int,           # automatic re-stagings of a blocked order (restage())
    "detail_url": str,      # the booking's portal detail URL (holds only the booking id)
    "handled": bool,        # marked Hanterad in the portal (Klar flow, Task 16b)
    "last_reason": str,     # block reason before the latest re-stage (short, no customer data)
    "gave_up": bool,        # the "no more automatic tries" message was sent
    "klar_message_id": int,  # Telegram message carrying the booking's current Klar button
    "klar_sent_at": str,    # when that Klar message was sent (ISO 8601 with time zone)
    "klar_closed": bool,    # the Klar button was withdrawn (no link, or it expired)
}
KLAR_MAX_AGE = timedelta(days=14)
MAX_TRIES = 10
REASON_MAX = 200
# Block reasons that happen before anything is added to the cart. A legacy record (no
# 'cart_touched' key, written before the field existed) is only re-staged for these.
PRE_CART_REASONS = ("ingen säker träff", "inga delar")


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
        for key, value in rec.items():
            if key in BASE_FIELDS:
                continue
            problem = _field_problem(key, value)
            if problem:
                raise bad(f"record for booking {bid!r}: {problem}")


def _field_problem(key: str, value) -> str | None:
    """Why an optional field is unacceptable, or None."""
    if key not in OPTIONAL_FIELDS:
        return f"unknown key {key!r}"
    want = OPTIONAL_FIELDS[key]
    if type(value) is not want:  # exact: bool is not int
        return f"{key!r} is not {want.__name__}"
    if key == "tries" and value < 0:
        return "'tries' is negative"
    if key == "klar_message_id" and value < 1:
        return "'klar_message_id' is not positive"
    if key == "klar_sent_at" and _parse_time(value) is None:
        return "'klar_sent_at' is not an ISO time with a time zone"
    return None


def _parse_time(value: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _extras(record: dict | None) -> dict:
    """The optional fields of a record, to carry over when it is rewritten."""
    return {k: v for k, v in (record or {}).items() if k in OPTIONAL_FIELDS}


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
        **_extras(old),
        "status": status,
        "plan": plan if plan is not None else old.get("plan"),
        "message_id": message_id if message_id is not None else old.get("message_id"),
        "updated_at": _now(),
    }
    _save(data)


def update_fields(booking_id: str, **fields) -> None:
    """Set optional fields (OPTIONAL_FIELDS only, exact types) on an existing record.

    Status, plan, message_id and updated_at are untouched. KeyError if no record exists.
    """
    _check_id(booking_id)
    for key, value in fields.items():
        problem = _field_problem(key, value)
        if problem:
            raise ValueError(f"update_fields({booking_id!r}): {problem}")
    data = _load()
    record = data["bookings"].get(booking_id)
    if record is None:
        raise KeyError(booking_id)
    record.update(fields)
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


# Plan marker for a booking queued for ordering but not yet started (crash recovery).
QUEUED_PLAN = {"queued": True}


def is_queued(record: dict | None) -> bool:
    return (bool(record) and record.get("status") == "attempted"
            and record.get("plan") == QUEUED_PLAN)


def queue(booking_id: str) -> bool:
    """Record 'attempted' with the queued marker only if no record exists."""
    _check_id(booking_id)
    data = _load()
    if booking_id in data["bookings"]:
        return False
    data["bookings"][booking_id] = {
        "status": "attempted", "plan": dict(QUEUED_PLAN), "message_id": None, "updated_at": _now(),
    }
    _save(data)
    return True


def begin(booking_id: str) -> bool:
    """Like claim(), but also converts a queued record (exactly once) into a fresh 'attempted'."""
    _check_id(booking_id)
    data = _load()
    record = data["bookings"].get(booking_id)
    if record is not None and not is_queued(record):
        return False
    data["bookings"][booking_id] = {
        **_extras(record),
        "status": "attempted", "plan": None, "message_id": None, "updated_at": _now(),
    }
    _save(data)
    return True


def restage(booking_id: str) -> bool:
    """Re-queue a 'blocked' order for one more automatic staging attempt.

    Only when the cart was never touched, fewer than MAX_TRIES retries were made and
    the booking was not marked Hanterad (Klar).
    A legacy record without a 'cart_touched' key qualifies only for PRE_CART_REASONS.
    The record becomes the queued marker (begin() then starts it as usual) with
    tries + 1 and the previous block reason kept as 'last_reason'.
    """
    _check_id(booking_id)
    data = _load()
    record = data["bookings"].get(booking_id)
    if (record is None or record.get("status") != "blocked"
            or record.get("cart_touched") is True or record.get("handled") is True
            or record.get("tries", 0) >= MAX_TRIES):
        return False
    plan = record.get("plan")
    reason = plan.get("reason") if isinstance(plan, dict) else None
    if "cart_touched" not in record:
        legacy_reason = reason if isinstance(reason, str) else record.get("last_reason") or ""
        if not legacy_reason.startswith(PRE_CART_REASONS):
            return False
    new = {
        **_extras(record),
        "status": "attempted", "plan": dict(QUEUED_PLAN), "message_id": None,
        "updated_at": _now(), "tries": record.get("tries", 0) + 1,
    }
    if isinstance(reason, str):
        new["last_reason"] = reason[:REASON_MAX]
    data["bookings"][booking_id] = new
    _save(data)
    return True


def by_status(status: Status) -> list[tuple[str, dict]]:
    if status not in STATUSES:
        raise ValueError(f"Unknown order status: {status!r}")
    bookings = _load()["bookings"]
    return [(bid, bookings[bid]) for bid in sorted(bookings)
            if bookings[bid].get("status") == status]


def pending() -> list[tuple[str, dict]]:
    bookings = _load()["bookings"]
    return [(bid, bookings[bid]) for bid in sorted(bookings)
            if bookings[bid].get("status") == "pending"]


def _open_klar(now: datetime | None) -> tuple[list, list]:
    """(outstanding, expired) records with a live Klar button: not handled, not closed."""
    now = now or datetime.now(timezone.utc)
    bookings = _load()["bookings"]
    outstanding, expired = [], []
    for bid in sorted(bookings):
        rec = bookings[bid]
        if (type(rec.get("klar_message_id")) is not int or rec.get("handled") is True
                or rec.get("klar_closed") is True):
            continue
        sent = rec.get("klar_sent_at")
        # A record from before klar_sent_at existed never expires.
        too_old = isinstance(sent, str) and now - _parse_time(sent) > KLAR_MAX_AGE
        (expired if too_old else outstanding).append((bid, rec))
    return outstanding, expired


def outstanding_klar(now: datetime | None = None) -> list[tuple[str, dict]]:
    """Records whose Klar button is out and not yet acted on (not handled, closed or expired)."""
    return _open_klar(now)[0]


def expired_klar(now: datetime | None = None) -> list[tuple[str, dict]]:
    """Records whose Klar button is older than KLAR_MAX_AGE and not yet closed."""
    return _open_klar(now)[1]


def last_update_id() -> int | None:
    return _load()["last_update_id"]


def set_last_update_id(n: int) -> None:
    data = _load()
    data["last_update_id"] = n
    _save(data)
