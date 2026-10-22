import io
import json
import urllib.error

import pytest

import notifier

CONFIG = {"telegram": {"bot_token": "123:ABC", "chat_id": "42"}}


class FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def calls(monkeypatch):
    """Capture requests; respond ok with a message_id by default."""
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append({"url": req.full_url, "body": json.loads(req.data.decode("utf-8"))})
        return FakeResp({"ok": True, "result": {"message_id": 7}})

    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen)
    return sent


def test_send_message_posts_to_bot_token_url(calls):
    assert notifier.send_message(CONFIG, "hello") is True
    assert calls[0]["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert calls[0]["body"]["chat_id"] == "42"
    assert calls[0]["body"]["text"] == "hello"


def test_long_text_split_into_chunks_under_4096(calls):
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    assert len(text) > 8192
    assert notifier.send_message(CONFIG, text) is True
    chunks = [c["body"]["text"] for c in calls]
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    assert "\n".join(chunks) == text


def test_single_overlong_line_is_hard_split(calls):
    text = "y" * 9000
    assert notifier.send_message(CONFIG, text) is True
    chunks = [c["body"]["text"] for c in calls]
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(chunks) == text


def test_send_with_buttons_builds_inline_keyboard(calls):
    mid = notifier.send_with_buttons(CONFIG, "Order?", [("Yes", "y:1"), ("No", "n:1")])
    assert mid == 7
    row = calls[0]["body"]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in row] == ["Yes", "No"]
    assert [b["callback_data"] for b in row] == ["y:1", "n:1"]


def test_get_updates_drops_non_callbacks_and_normalises(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(json.loads(req.data.decode("utf-8")))
        return FakeResp({"ok": True, "result": [
            {"update_id": 1, "message": {"text": "hi"}},
            {"update_id": 2, "callback_query": {
                "id": "cb9", "data": "y:1", "from": {"id": 42},
                "message": {"message_id": 55, "chat": {"id": 42}}}},
        ]})

    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen)
    updates = notifier.get_updates(CONFIG, offset=10)
    assert updates == [{"update_id": 2, "chat_id": "42", "user_id": "42", "callback_id": "cb9",
                        "data": "y:1", "message_id": 55}]
    assert seen[0]["offset"] == 10
    assert seen[0]["timeout"] == 0


@pytest.mark.parametrize("cb_from", [None, {}, {"id": None}, "absent"])
def test_get_updates_missing_from_gives_empty_user_id(monkeypatch, cb_from):
    cb = {"id": "cb9", "data": "y:1", "message": {"message_id": 55, "chat": {"id": 42}}}
    if cb_from != "absent":
        cb["from"] = cb_from
    monkeypatch.setattr(notifier.urllib.request, "urlopen", lambda req, timeout=None: FakeResp(
        {"ok": True, "result": [{"update_id": 2, "callback_query": cb}]}))

    (update,) = notifier.get_updates(CONFIG)
    assert update["user_id"] == ""


def test_send_failure_returns_false(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(notifier.urllib.request, "urlopen", boom)
    assert notifier.send_message(CONFIG, "hello") is False
    assert notifier.send_with_buttons(CONFIG, "x", [("a", "b")]) is None
    assert notifier.get_updates(CONFIG, None) == []


def test_token_never_logged(monkeypatch, caplog):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "err", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(notifier.urllib.request, "urlopen", boom)
    with caplog.at_level("DEBUG"):
        notifier.send_message(CONFIG, "hello")
    assert "123:ABC" not in caplog.text
    assert "sendMessage" in caplog.text


def test_answer_callback_and_clear_buttons(calls):
    notifier.answer_callback(CONFIG, "cb9", "Done")
    notifier.clear_buttons(CONFIG, 55)
    assert calls[0]["url"].endswith("/answerCallbackQuery")
    assert calls[0]["body"] == {"callback_query_id": "cb9", "text": "Done"}
    assert calls[1]["url"].endswith("/editMessageReplyMarkup")
    assert calls[1]["body"]["message_id"] == 55
    assert calls[1]["body"]["reply_markup"] == {"inline_keyboard": []}


def test_send_with_buttons_accepts_rows(calls):
    mid = notifier.send_with_buttons(CONFIG, "Order?", [[("Yes", "y:1"), ("No", "n:1")],
                                                        [("Swap", "s:1:0")]])
    assert mid == 7
    keyboard = calls[0]["body"]["reply_markup"]["inline_keyboard"]
    assert keyboard == [[{"text": "Yes", "callback_data": "y:1"},
                         {"text": "No", "callback_data": "n:1"}],
                        [{"text": "Swap", "callback_data": "s:1:0"}]]


def test_send_with_buttons_single_row_is_unchanged(calls):
    notifier.send_with_buttons(CONFIG, "Order?", [("Yes", "y:1")])
    assert calls[0]["body"]["reply_markup"] == {
        "inline_keyboard": [[{"text": "Yes", "callback_data": "y:1"}]]}


def test_send_with_buttons_rejects_mixed_shapes(calls):
    with pytest.raises(ValueError):
        notifier.send_with_buttons(CONFIG, "x", [("Yes", "y:1"), [("No", "n:1")]])
    assert calls == []
