"""
alerts.py
---------
Alert store for the booking agent (stdlib only: it runs before `pip install`).

State lives in config/alerts.json:
  {"alerts": {"<key>": {"text", "severity", "first_seen", "last_seen",
                        "count", "last_sent", "resolved_at"}}}

raise_alert upserts and sends a Telegram message at most once per 24 h per key
(an unresolved alert is re-sent after 24 h as a reminder). Every public function
never raises: a broken alert store must not take the agent down.
"""

import json
import logging
import os
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import notifier

log = logging.getLogger(__name__)

ALERTS_FILE = Path("config/alerts.json")
RESEND_AFTER = timedelta(hours=24)
MAX_TEXT = 500
_PREFIX = {"error": "⚠️ ", "warning": "ℹ️ "}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _clean(text) -> str:
    """Control characters (incl. newlines/tabs) become spaces or vanish; cap length."""
    out = []
    for ch in str(text):
        if ch in "\n\r\t":
            out.append(" ")
        elif unicodedata.category(ch) != "Cc":
            out.append(ch)
    return "".join(out)[:MAX_TEXT]


def _load(rename_corrupt: bool = False) -> dict:
    """Return the alerts mapping. Missing -> {}; corrupt -> {} (renamed aside if asked)."""
    path = Path(ALERTS_FILE)
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        alerts = data["alerts"]
        if not isinstance(alerts, dict):
            raise ValueError("alerts is not an object")
        return alerts
    except Exception as e:
        log.error("alerts file unreadable (%s).", type(e).__name__)
        if rename_corrupt:
            try:
                os.replace(path, path.with_name(path.name + ".corrupt"))
            except Exception as e2:
                log.error("could not move corrupt alerts file (%s).", type(e2).__name__)
        return {}


def _save(alerts: dict) -> None:
    path = Path(ALERTS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"alerts": alerts}, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def parse_time(value):
    """An ISO 8601 timestamp as an aware datetime (naive = UTC); None if unusable."""
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


_parse = parse_time


def raise_alert(config: dict, key: str, text: str, severity: str = "error",
                now: datetime | None = None) -> bool:
    """Record the alert and notify. True = sent or suppressed (sent < 24 h ago);
    False = delivery attempted and failed. Never raises."""
    try:
        now = now or datetime.now(timezone.utc)
        severity = severity if severity in _PREFIX else "error"
        text = _clean(text)
        alerts = _load(rename_corrupt=True)
        entry = alerts.get(key)
        if not isinstance(entry, dict) or entry.get("resolved_at"):
            entry = {"first_seen": _iso(now), "count": 0, "last_sent": None}
        entry.update(text=text, severity=severity, last_seen=_iso(now),
                     count=int(entry.get("count", 0)) + 1, resolved_at=None)

        last_sent = _parse(entry.get("last_sent"))
        due = last_sent is None or now - last_sent >= RESEND_AFTER
        ok = True
        if due:
            try:
                ok = bool(notifier.send_message(config, _PREFIX[severity] + text))
            except Exception as e:
                log.error("alert delivery raised %s.", type(e).__name__)
                ok = False
            if ok:
                entry["last_sent"] = _iso(now)
        alerts[key] = entry
        try:
            _save(alerts)
        except Exception as e:
            log.error("could not write alerts file (%s).", type(e).__name__)
        return ok
    except Exception as e:
        log.error("raise_alert failed (%s).", type(e).__name__)
        return False


def resolve(key: str, now: datetime | None = None) -> bool:
    """Mark an unresolved alert resolved. Returns whether anything changed. Never raises."""
    try:
        now = now or datetime.now(timezone.utc)
        alerts = _load()
        entry = alerts.get(key)
        if not isinstance(entry, dict) or entry.get("resolved_at"):
            return False
        entry["resolved_at"] = _iso(now)
        _save(alerts)
        return True
    except Exception as e:
        log.error("resolve failed (%s).", type(e).__name__)
        return False


def active() -> list[dict]:
    """Unresolved alerts, newest last_seen first. Never raises."""
    try:
        out = []
        for key, e in _load().items():
            if not isinstance(e, dict) or e.get("resolved_at"):
                continue
            out.append({"key": key, "text": e.get("text", ""),
                        "severity": e.get("severity", "error"),
                        "first_seen": e.get("first_seen"),
                        "last_seen": e.get("last_seen"),
                        "count": e.get("count", 1)})
        out.sort(key=lambda a: a["last_seen"] or "", reverse=True)
        return out
    except Exception as e:
        log.error("active failed (%s).", type(e).__name__)
        return []
