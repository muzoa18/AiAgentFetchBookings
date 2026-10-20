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
