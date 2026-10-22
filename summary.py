"""
summary.py
----------
Daily Telegram overview (stdlib only, plus notifier: runs without pip install).
Reads config/orders.json, config/alerts.json, config/watchdog_alerts.json and
logs/run_history.jsonl from the checkout as plain JSON, sends ONE message; exits 1
when it could not be sent (else 0). It writes no state and never opens a browser; the watchdog checks that the
workflow keeps succeeding.

Only booking numbers and plates are shown (no customer data).

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import notifier

log = logging.getLogger("summary")

MAX_CHARS = 3000
ALERT_MAX = 120
ACTION_WINDOW = timedelta(days=7)
KLAR_MAX_AGE = timedelta(days=14)   # as orders_state.KLAR_MAX_AGE
HISTORY_ROWS = 5
SECTION_MAX = 10   # items shown per section before "+N till"


def _parse(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _label(booking_id, rec) -> str:
    plan = rec.get("plan") if isinstance(rec.get("plan"), dict) else {}
    plate = plan.get("reg_nr")
    return f"{booking_id} · {plate}" if isinstance(plate, str) and plate else str(booking_id)


def _last_success(history):
    best = None
    for row in history:
        if (not isinstance(row, dict) or row.get("login_failed")
                or row.get("state_unreadable")):
            continue
        ts = _parse(row.get("timestamp"))
        if ts and (best is None or ts > best):
            best = ts
    return best


def klar_outstanding(rec: dict, now: datetime) -> bool:
    """Mirror of orders_state._open_klar's "outstanding" side (kept here so summary.py stays
    stdlib-only; tests/test_summary.py pins both to the same records). A missing or
    unparseable klar_sent_at never expires (orders_state itself would raise on the latter)."""
    if (type(rec.get("klar_message_id")) is not int or rec.get("handled") is True
            or rec.get("klar_closed") is True):
        return False
    sent = rec.get("klar_sent_at")
    if isinstance(sent, str):
        try:
            when = datetime.fromisoformat(sent)
        except ValueError:
            return True
        if when.tzinfo is not None and now - when > KLAR_MAX_AGE:
            return False
    return True


STATUS_WORDS = {"blocked": "blockerad", "cancelled": "avbruten", "failed": "misslyckad",
                "expired": "utgången"}


def _unverified(rec: dict) -> bool:
    """Mirror of approve._to_verify (orders_state.unverified_orders + not verify_warned):
    placed orders the order checker still works on. Pinned to it by a test."""
    return (rec.get("status") == "ordered" and "verified_at" not in rec
            and rec.get("verify_warned") is not True)


def _sections(orders, alerts, now):
    pending, klar, unverified, action = [], [], [], []
    bookings = orders.get("bookings") if isinstance(orders, dict) else None
    for bid, rec in sorted((bookings or {}).items()):
        if not isinstance(rec, dict):
            continue
        status = rec.get("status")
        line = _label(bid, rec)
        if status == "pending":
            pending.append(line)
        elif status != "attempted" and klar_outstanding(rec, now):
            # Each booking once: any record with an open Klar button is listed only here
            # (orders_state._open_klar has no status filter), annotated with its status.
            word = STATUS_WORDS.get(status)
            if status == "ordered" and "verified_at" not in rec:
                word = "ej verifierad"
            klar.append(line + (f" ({word})" if word else ""))
        elif _unverified(rec):
            unverified.append(line)
        elif status in ("blocked", "failed", "expired"):
            when = _parse(rec.get("updated_at"))
            # A missing or unparseable updated_at is shown (fail-open), never dropped.
            if when is None or now - when <= ACTION_WINDOW:
                action.append(f"{line} ({STATUS_WORDS[status]})")
    alert_lines = []
    for a in alerts:
        text = " ".join(str(a.get("text") or "").split())[:ALERT_MAX]
        if text:
            alert_lines.append(text)
    return [("Väntar på ditt godkännande", pending), ("Väntar på Klar", klar),
            ("Beställt, ej verifierat", unverified), ("Kräver åtgärd", action),
            ("Larm", alert_lines)]


def _render(sections, shown, header, footer):
    parts = [header] if header else []
    for (title, items), n in zip(sections, shown):
        if not items:
            continue
        lines = [f"  {i}" for i in items[:n]]
        if n < len(items):
            lines.append(f"  +{len(items) - n} till")
        parts.append(f"{title} ({len(items)}):\n" + "\n".join(lines))
    parts.append(footer)
    return "\n\n".join(parts)


def build_summary(orders: dict, alerts: list, history: list, now: datetime) -> str:
    last = _last_success(history)
    footer = (f"Senaste lyckade körning: {last.astimezone(timezone.utc):%Y-%m-%d} UTC"
              if last else "Ingen körning registrerad")
    notes = orders.get("_notes") if isinstance(orders, dict) else None
    note_text = "\n".join(str(n) for n in notes or ())
    sections = _sections(orders, alerts, now)
    if not any(items for _, items in sections):
        if isinstance(orders, dict) and orders.get("_unreadable"):
            # Never claim "nothing waits" when the order state could not be read.
            return f"{note_text}\nLäget är okänt. {footer}"[:MAX_CHARS]
        return (f"Inget väntar. {footer}" + (f"\n{note_text}" if note_text else ""))[:MAX_CHARS]
    shown = [min(len(items), SECTION_MAX) for _, items in sections]
    while True:
        text = _render(sections, shown, note_text, footer)
        if len(text) <= MAX_CHARS:
            return text
        # Collapse one more item from the section that currently shows the most.
        idx = max(range(len(shown)), key=lambda i: shown[i])
        if shown[idx] == 0:
            return text[:MAX_CHARS]
        shown[idx] -= 1


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_state(root: Path = Path(".")):
    """(orders, active alerts, last history rows). A missing or unreadable file gives empty
    data and an `OBS: <file> kunde inte läsas` line, carried in orders["_notes"] for
    build_summary (a missing alerts file is normal: nothing was ever raised)."""
    root = Path(root)
    notes = []

    orders = {"last_update_id": None, "bookings": {}}
    path = root / "config/orders.json"
    try:
        data = _read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("bookings"), dict):
            raise ValueError("shape")
        orders = data
    except Exception:
        notes.append("OBS: orders.json kunde inte läsas")
        orders["_unreadable"] = True

    active = []
    for name in ("alerts.json", "watchdog_alerts.json"):
        path = root / "config" / name
        if not path.exists() and name == "watchdog_alerts.json":
            continue  # written by the watchdog's first alert only
        try:
            data = _read_json(path)
            entries = data["alerts"]
            if not isinstance(entries, dict):
                raise ValueError("shape")
            for key, e in entries.items():
                if isinstance(e, dict) and not e.get("resolved_at"):
                    active.append({"key": key, "text": e.get("text") or "",
                                   "severity": e.get("severity", "error"),
                                   "last_seen": e.get("last_seen") or ""})
        except Exception:
            if name == "alerts.json" and not path.exists():
                continue  # no alert has ever been raised
            notes.append(f"OBS: {name} kunde inte läsas")
    active.sort(key=lambda a: str(a["last_seen"]), reverse=True)

    history = []
    path = root / "logs/run_history.jsonl"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    history.append(row)
    except Exception:
        history = []
        notes.append("OBS: run_history.jsonl kunde inte läsas")
    if notes:
        orders["_notes"] = notes
    return orders, active, history[-HISTORY_ROWS:]


def main(env=None, now=None, root=Path(".")) -> int:
    """Send the summary. 0 when it was sent; 1 when sending failed or building it crashed
    (after trying a short fallback message), so the workflow shows red."""
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    config = {"telegram": {"bot_token": env.get("TELEGRAM_BOT_TOKEN", ""),
                           "chat_id": env.get("TELEGRAM_CHAT_ID", "")}}
    try:
        orders, active, history = load_state(root)
        text = build_summary(orders, active, history, now)
        if notifier.send_message(config, text):
            return 0
        log.error("Daily summary could not be sent.")
        return 1
    except Exception as e:
        log.error("Daily summary failed (%s).", type(e).__name__)
    try:
        notifier.send_message(config, "Dagssammanfattningen kunde inte skapas")
    except Exception as e:
        log.error("Fallback message failed (%s).", type(e).__name__)
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
