"""
notifier.py
-----------
Telegram Bot API client (stdlib only). Replaces SMS for owner notifications.

Config (config["telegram"]): bot_token, chat_id (kept as a string).
The bot token is never logged; only the Telegram method name is.
"""

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

CHUNK_LIMIT = 4000  # Telegram max is 4096; leave headroom


def _call(config: dict, method: str, payload: dict):
    """POST to a Bot API method. Returns the 'result' value, or None on failure."""
    token = config.get("telegram", {}).get("bot_token", "")
    if not token:
        log.error("Telegram bot_token missing (%s).", method)
        return None

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.error("Telegram %s HTTP error %s", method, e.code)
        return None
    except Exception as e:
        # Log only the exception type: its message may contain the URL (token).
        log.error("Telegram %s failed: %s", method, type(e).__name__)
        return None

    if not body.get("ok"):
        log.error("Telegram %s returned ok=false", method)
        return None
    result = body.get("result")
    return True if result is None else result


def _chat_id(config: dict) -> str:
    return str(config.get("telegram", {}).get("chat_id", ""))


def _chunk(text: str, limit: int = CHUNK_LIMIT) -> list:
    """Split on line boundaries into pieces <= limit; hard-split over-long lines."""
    chunks, current = [], None
    for line in text.split("\n"):
        while len(line) > limit:
            if current is not None:
                chunks.append(current)
                current = None
            chunks.append(line[:limit])
            line = line[limit:]
        if current is None:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current += "\n" + line
        else:
            chunks.append(current)
            current = line
    if current is not None:
        chunks.append(current)
    return chunks or [""]


def send_message(config: dict, text: str) -> bool:
    """Send text (chunked if long). True only if every chunk was sent."""
    chat_id = _chat_id(config)
    ok = True
    for chunk in _chunk(text):
        if _call(config, "sendMessage", {"chat_id": chat_id, "text": chunk}) is None:
            ok = False
    return ok


def send_with_buttons(config: dict, text: str, buttons: list):
    """Send text with one row of inline buttons [(label, callback_data)]. Returns message_id or None."""
    row = [{"text": label, "callback_data": data} for label, data in buttons]
    result = _call(config, "sendMessage", {
        "chat_id": _chat_id(config),
        "text": text,
        "reply_markup": {"inline_keyboard": [row]},
    })
    if isinstance(result, dict):
        return result.get("message_id")
    return None


def get_updates(config: dict, offset=None) -> list:
    """Fetch pending updates (no long polling); return only callback queries, normalised."""
    payload = {"timeout": 0, "allowed_updates": ["callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    result = _call(config, "getUpdates", payload)
    if not isinstance(result, list):
        return []

    updates = []
    for upd in result:
        cb = upd.get("callback_query")
        if not cb:
            continue
        msg = cb.get("message") or {}
        user_id = (cb.get("from") or {}).get("id")
        updates.append({
            "update_id": upd.get("update_id"),
            "chat_id": str(msg.get("chat", {}).get("id", "")),
            "user_id": "" if user_id is None else str(user_id),
            "callback_id": cb.get("id"),
            "data": cb.get("data"),
            "message_id": msg.get("message_id"),
        })
    return updates


def answer_callback(config: dict, callback_id: str, text: str) -> None:
    _call(config, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def clear_buttons(config: dict, message_id: int) -> None:
    _call(config, "editMessageReplyMarkup", {
        "chat_id": _chat_id(config),
        "message_id": message_id,
        "reply_markup": {"inline_keyboard": []},
    })
