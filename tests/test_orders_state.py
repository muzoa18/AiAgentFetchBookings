import json

import pytest

import orders_state


@pytest.fixture(autouse=True)
def orders_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "orders.json"
    monkeypatch.setattr(orders_state, "ORDERS_FILE", path)
    return path


def test_missing_file_is_empty_state(orders_file):
    assert orders_state.get("B1") is None
    assert orders_state.pending() == []
    assert orders_state.last_update_id() is None
    assert not orders_file.exists()


def test_claim_only_once():
    assert orders_state.claim("B1") is True
    assert orders_state.claim("B1") is False
    assert orders_state.get("B1")["status"] == "attempted"


def test_claim_false_when_any_record_exists():
    orders_state.put("B1", "failed")
    assert orders_state.claim("B1") is False
    assert orders_state.get("B1")["status"] == "failed"


def test_put_overwrites_status_and_keeps_plan():
    plan = {"parts": ["x"]}
    orders_state.put("B1", "pending", plan=plan, message_id=42)
    orders_state.put("B1", "ordered")
    rec = orders_state.get("B1")
    assert rec["status"] == "ordered"
    assert rec["plan"] == plan
    assert rec["message_id"] == 42
    assert rec["updated_at"].endswith("+00:00")


def test_put_overwrites_plan_when_given():
    orders_state.put("B1", "pending", plan={"a": 1}, message_id=1)
    orders_state.put("B1", "pending", plan={"a": 2}, message_id=2)
    rec = orders_state.get("B1")
    assert rec["plan"] == {"a": 2}
    assert rec["message_id"] == 2


def test_put_unknown_status_raises():
    with pytest.raises(ValueError):
        orders_state.put("B1", "bogus")
    assert orders_state.get("B1") is None


def test_pending_lists_only_pending():
    orders_state.put("B1", "pending")
    orders_state.put("B2", "ordered")
    orders_state.put("B3", "attempted")
    result = orders_state.pending()
    assert [bid for bid, _ in result] == ["B1"]
    assert result[0][1]["status"] == "pending"


def test_pending_sorted_by_booking_id():
    for bid in ("B3", "B1", "B2"):
        orders_state.put(bid, "pending")
    assert [bid for bid, _ in orders_state.pending()] == ["B1", "B2", "B3"]


def test_last_update_id_roundtrip():
    assert orders_state.last_update_id() is None
    orders_state.set_last_update_id(123)
    assert orders_state.last_update_id() == 123


def test_last_update_id_and_bookings_coexist():
    orders_state.put("B1", "pending")
    orders_state.set_last_update_id(7)
    orders_state.put("B2", "pending")
    assert orders_state.last_update_id() == 7
    assert orders_state.get("B1") is not None


def test_corrupt_file_raises_and_is_untouched(orders_file):
    orders_file.parent.mkdir(parents=True)
    orders_file.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="orders.json"):
        orders_state.get("B1")
    with pytest.raises(ValueError, match="orders.json"):
        orders_state.claim("B1")
    assert orders_file.read_text(encoding="utf-8") == "{not json"


def test_atomic_write_leaves_no_temp_file(orders_file):
    orders_state.put("B1", "pending")
    orders_state.set_last_update_id(1)
    assert [p.name for p in orders_file.parent.iterdir()] == ["orders.json"]


def test_file_format_is_stable(orders_file):
    orders_state.put("B2", "pending")
    orders_state.put("B1", "pending")
    text = orders_file.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    data = json.loads(text)
    assert list(data["bookings"]) == ["B1", "B2"]
    assert text == json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


BAD_FILES = [
    b"{}",
    b"[]",
    b'{"bookings": [], "last_update_id": null}',
    b'{"bookings": null, "last_update_id": null}',
    b'{"last_update_id": null}',
    b'{"bookings": {"B1": "x"}, "last_update_id": null}',
    b'{"bookings": {}, "last_update_id": "7"}',
    b'{"bookings": {}, "last_update_id": true}',
    b"",
    b'\xef\xbb\xbf{"bookings": {}, "last_update_id": null}',
]


@pytest.mark.parametrize("content", BAD_FILES)
def test_wrong_shape_file_raises_and_is_untouched(orders_file, content):
    orders_file.parent.mkdir(parents=True)
    orders_file.write_bytes(content)
    calls = [
        lambda: orders_state.claim("B1"),
        lambda: orders_state.put("B1", "pending"),
        lambda: orders_state.get("B1"),
        orders_state.pending,
        orders_state.last_update_id,
        lambda: orders_state.set_last_update_id(1),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="orders.json"):
            call()
    assert orders_file.read_bytes() == content


def test_bad_record_error_names_booking_id(orders_file):
    orders_file.parent.mkdir(parents=True)
    orders_file.write_text('{"bookings": {"B9": "x"}, "last_update_id": null}')
    with pytest.raises(ValueError, match="B9"):
        orders_state.get("B1")


def test_fresh_file_always_has_both_keys(orders_file):
    orders_state.claim("B1")
    assert set(json.loads(orders_file.read_text())) == {"bookings", "last_update_id"}
    orders_state.set_last_update_id(3)
    assert orders_state.claim("B2") is True


def test_non_str_booking_id_raises_and_file_unchanged(orders_file):
    orders_state.claim("B1")
    before = orders_file.read_bytes()
    for call in (lambda: orders_state.claim(5), lambda: orders_state.put(5, "pending"),
                 lambda: orders_state.get(5)):
        with pytest.raises(TypeError):
            call()
    assert orders_file.read_bytes() == before


def test_non_str_booking_id_raises_without_creating_file(orders_file):
    with pytest.raises(TypeError):
        orders_state.claim(5)
    assert not orders_file.exists()


@pytest.mark.parametrize("bad", ["", "  ", "\t\n"])
def test_blank_booking_id_raises(orders_file, bad):
    for call in (lambda: orders_state.claim(bad), lambda: orders_state.put(bad, "pending"),
                 lambda: orders_state.get(bad)):
        with pytest.raises(ValueError):
            call()
    assert not orders_file.exists()


def test_booking_ids_are_case_sensitive():
    assert orders_state.claim("B1") is True
    assert orders_state.claim("b1") is True
    assert orders_state.claim("B1") is False


# ── queue / begin / by_status (crash recovery for the ordering step) ─────────

def test_queue_only_when_no_record():
    assert orders_state.queue("B1") is True
    rec = orders_state.get("B1")
    assert rec["status"] == "attempted"
    assert rec["plan"] == {"queued": True}
    assert orders_state.queue("B1") is False
    orders_state.put("B2", "pending", plan={"x": 1})
    assert orders_state.queue("B2") is False
    assert orders_state.get("B2")["status"] == "pending"


def test_begin_converts_queued_record_exactly_once():
    orders_state.queue("B1")
    assert orders_state.begin("B1") is True
    rec = orders_state.get("B1")
    assert rec["status"] == "attempted"
    assert rec["plan"] is None
    assert orders_state.begin("B1") is False


def test_begin_without_record_claims():
    assert orders_state.begin("B1") is True
    assert orders_state.get("B1")["status"] == "attempted"
    assert orders_state.begin("B1") is False


@pytest.mark.parametrize("status", ["pending", "ordered", "blocked", "failed"])
def test_begin_refuses_other_records(status):
    orders_state.put("B1", status, plan={"queued": True})
    assert orders_state.begin("B1") is False
    assert orders_state.get("B1")["status"] == status


def test_begin_and_queue_check_ids():
    with pytest.raises(TypeError):
        orders_state.queue(1)
    with pytest.raises(ValueError):
        orders_state.begin(" ")


def test_by_status_sorted_and_validated():
    orders_state.put("B2", "attempted")
    orders_state.put("B1", "attempted")
    orders_state.put("B3", "pending")
    assert [bid for bid, _ in orders_state.by_status("attempted")] == ["B1", "B2"]
    assert orders_state.by_status("ordered") == []
    with pytest.raises(ValueError):
        orders_state.by_status("bogus")
