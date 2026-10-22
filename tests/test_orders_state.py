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


# ── Task 16a: optional record fields, update_fields, restage ─────────────────

def _write(orders_file, records):
    orders_file.parent.mkdir(parents=True, exist_ok=True)
    orders_file.write_text(json.dumps({"bookings": records, "last_update_id": None}),
                           encoding="utf-8")


OLD_RECORD = {"status": "blocked", "plan": {"reason": "ingen säker träff för 'Pollenfilter'"},
              "message_id": None, "updated_at": "2026-10-21T09:25:58+00:00"}


def test_old_records_without_new_fields_still_load(orders_file):
    _write(orders_file, {"B1": OLD_RECORD})
    assert orders_state.get("B1") == OLD_RECORD


@pytest.mark.parametrize("field,value", [
    ("cart_touched", True), ("tries", 3), ("detail_url", "http://x/1"), ("handled", False),
    ("last_reason", "ingen träff"), ("gave_up", True)])
def test_valid_optional_fields_load(orders_file, field, value):
    _write(orders_file, {"B1": {**OLD_RECORD, field: value}})
    assert orders_state.get("B1")[field] == value


@pytest.mark.parametrize("field,value", [
    ("cart_touched", 1), ("cart_touched", "true"), ("tries", True), ("tries", "3"),
    ("tries", -1), ("tries", 1.0), ("detail_url", 5), ("handled", None), ("last_reason", ["x"]),
    ("gave_up", 0), ("surprise", 1)])
def test_bad_optional_field_or_unknown_key_raises(orders_file, field, value):
    _write(orders_file, {"B1": {**OLD_RECORD, field: value}})
    with pytest.raises(ValueError, match="B1"):
        orders_state.get("B1")


def test_update_fields_sets_whitelisted_fields_and_keeps_record():
    orders_state.put("B1", "pending", plan={"a": 1}, message_id=9)
    orders_state.update_fields("B1", cart_touched=True, detail_url="http://x/B1")
    rec = orders_state.get("B1")
    assert (rec["status"], rec["plan"], rec["message_id"]) == ("pending", {"a": 1}, 9)
    assert rec["cart_touched"] is True and rec["detail_url"] == "http://x/B1"


@pytest.mark.parametrize("fields", [{"status": "ordered"}, {"plan": {}}, {"bogus": 1},
                                    {"tries": True}, {"cart_touched": "yes"}, {"tries": -2}])
def test_update_fields_rejects_unknown_or_badly_typed(fields, orders_file):
    orders_state.put("B1", "blocked")
    before = orders_file.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        orders_state.update_fields("B1", **fields)
    assert orders_file.read_bytes() == before


def test_update_fields_without_record_raises(orders_file):
    with pytest.raises(KeyError):
        orders_state.update_fields("B1", cart_touched=True)
    assert not orders_file.exists()


def test_put_and_begin_keep_optional_fields():
    orders_state.queue("B1")
    orders_state.update_fields("B1", detail_url="http://x/B1", tries=2, last_reason="r")
    assert orders_state.begin("B1") is True
    orders_state.put("B1", "blocked", plan={"reason": "r2"})
    rec = orders_state.get("B1")
    assert (rec["detail_url"], rec["tries"], rec["last_reason"]) == ("http://x/B1", 2, "r")


def test_restage_blocked_untouched_record():
    orders_state.put("B1", "blocked", plan={"reason": "ingen säker träff för 'Pollenfilter'"})
    orders_state.update_fields("B1", detail_url="http://x/B1")
    assert orders_state.restage("B1") is True
    rec = orders_state.get("B1")
    assert orders_state.is_queued(rec)
    assert rec["tries"] == 1
    assert rec["last_reason"] == "ingen säker träff för 'Pollenfilter'"
    assert rec["detail_url"] == "http://x/B1"
    assert orders_state.restage("B1") is False  # queued, not blocked
    assert orders_state.begin("B1") is True       # normal begin() path still works
    assert orders_state.get("B1")["tries"] == 1


def test_restage_counts_tries_up_to_the_cap():
    orders_state.put("B1", "blocked", plan={"reason": "x"})
    orders_state.update_fields("B1", cart_touched=False)
    for n in range(1, orders_state.MAX_TRIES + 1):
        assert orders_state.restage("B1") is True
        assert orders_state.get("B1")["tries"] == n
        orders_state.put("B1", "blocked", plan={"reason": "x"})
    assert orders_state.MAX_TRIES == 10
    assert orders_state.restage("B1") is False
    assert orders_state.get("B1")["status"] == "blocked"


def test_restage_refuses_a_duplicate_order_block():
    # Blocked before any cart action (cart_touched False) but a real order already exists:
    # never retried automatically.
    orders_state.put("B1", "blocked",
                     plan={"reason": "order finns redan i pro.meca.se (S-1 · 2)"})
    orders_state.update_fields("B1", cart_touched=False)
    assert orders_state.restage("B1") is False
    assert orders_state.get("B1")["status"] == "blocked"
    assert not orders_state.NO_RESTAGE_REASONS[0].startswith(orders_state.PRE_CART_REASONS)


def test_restage_refuses_cart_touched():
    orders_state.put("B1", "blocked", plan={"reason": "x"})
    orders_state.update_fields("B1", cart_touched=True)
    assert orders_state.restage("B1") is False
    assert orders_state.get("B1")["status"] == "blocked"


@pytest.mark.parametrize("status", ["attempted", "pending", "ordered", "failed", "cancelled",
                                    "expired"])
def test_restage_refuses_other_statuses(status):
    orders_state.put("B1", status, plan={"reason": "x"})
    assert orders_state.restage("B1") is False
    assert orders_state.get("B1")["status"] == status


def test_restage_without_record_is_false(orders_file):
    assert orders_state.restage("B1") is False
    assert not orders_file.exists()


def test_restage_last_reason_is_short():
    orders_state.put("B1", "blocked", plan={"reason": "x" * 1000})
    orders_state.update_fields("B1", cart_touched=False)
    assert orders_state.restage("B1") is True
    assert len(orders_state.get("B1")["last_reason"]) <= 200


# ── Review M1: legacy blocked records (no cart_touched key) ──────────────────

@pytest.mark.parametrize("reason", ["ingen säker träff för 'Pollenfilter'",
                                    "inga delar att beställa"])
def test_legacy_blocked_record_with_pre_cart_reason_is_restaged(reason):
    orders_state.put("B1", "blocked", plan={"reason": reason})
    assert orders_state.restage("B1") is True


@pytest.mark.parametrize("reason", ["varukorgen stämmer inte med de valda delarna",
                                    "varukorgen är inte tom", "totalen 12000.00 SEK överstiger"])
def test_legacy_blocked_record_with_other_reason_is_left_alone(reason):
    orders_state.put("B1", "blocked", plan={"reason": reason})
    assert orders_state.restage("B1") is False
    assert orders_state.get("B1")["status"] == "blocked"


def test_legacy_rule_uses_last_reason_on_a_repeat_retry():
    orders_state.put("B1", "blocked", plan={"reason": "ingen säker träff för 'X'"})
    assert orders_state.restage("B1") is True
    orders_state.put("B1", "blocked", plan={"reason": "ingen säker träff för 'X'"})
    assert orders_state.restage("B1") is True


def test_new_record_marked_untouched_is_restaged_for_any_reason():
    orders_state.put("B1", "blocked", plan={"reason": "varukorgen är inte tom"})
    orders_state.update_fields("B1", cart_touched=False)
    assert orders_state.restage("B1") is True


# ── Klar (Task 16b) ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [1, 555, 2**40])
def test_klar_message_id_loads(orders_file, value):
    _write(orders_file, {"B1": {**OLD_RECORD, "klar_message_id": value}})
    assert orders_state.get("B1")["klar_message_id"] == value


@pytest.mark.parametrize("value", [True, False, "555", 5.0, None, 0, -3, [5]])
def test_bad_klar_message_id_raises(orders_file, value):
    _write(orders_file, {"B1": {**OLD_RECORD, "klar_message_id": value}})
    with pytest.raises(ValueError, match="B1"):
        orders_state.get("B1")


@pytest.mark.parametrize("value", [True, "5", 0, -1, 1.0])
def test_update_fields_rejects_bad_klar_message_id(orders_file, value):
    orders_state.put("B1", "blocked")
    before = orders_file.read_bytes()
    with pytest.raises(ValueError):
        orders_state.update_fields("B1", klar_message_id=value)
    assert orders_file.read_bytes() == before


def test_klar_message_id_is_kept_by_put_and_restage():
    orders_state.put("B1", "blocked", plan={"reason": "r"})
    orders_state.update_fields("B1", klar_message_id=900, cart_touched=False)
    orders_state.put("B1", "blocked", plan={"reason": "r2"})
    assert orders_state.get("B1")["klar_message_id"] == 900
    assert orders_state.restage("B1") is True
    assert orders_state.get("B1")["klar_message_id"] == 900


def test_outstanding_klar_lists_unhandled_records_with_a_klar_button():
    orders_state.put("B3", "ordered")
    orders_state.update_fields("B3", klar_message_id=3)
    orders_state.put("B1", "blocked")
    orders_state.update_fields("B1", klar_message_id=1, handled=False)
    orders_state.put("B2", "cancelled")
    orders_state.update_fields("B2", klar_message_id=2, handled=True)
    orders_state.put("B4", "failed")  # no Klar button: nothing to wait for
    orders_state.put("B5", "pending", message_id=8)

    assert [bid for bid, _ in orders_state.outstanding_klar()] == ["B1", "B3"]
    assert orders_state.outstanding_klar()[0][1]["klar_message_id"] == 1


def test_outstanding_klar_empty_without_file(orders_file):
    assert orders_state.outstanding_klar() == []
    assert not orders_file.exists()


def test_restage_refuses_a_handled_record():
    orders_state.put("B1", "blocked", plan={"reason": "ingen säker träff för 'X'"})
    orders_state.update_fields("B1", cart_touched=False, handled=True)
    assert orders_state.restage("B1") is False
    assert orders_state.get("B1")["status"] == "blocked"


# ── Fix wave: klar_closed, klar_sent_at and Klar expiry ─────────────────────

from datetime import datetime, timedelta, timezone  # noqa: E402

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("field,value", [
    ("klar_closed", True), ("klar_closed", False),
    ("klar_sent_at", "2026-10-21T09:25:58+00:00"), ("klar_sent_at", "2026-10-21T09:25:58.123+02:00")])
def test_new_klar_fields_load(orders_file, field, value):
    _write(orders_file, {"B1": {**OLD_RECORD, field: value}})
    assert orders_state.get("B1")[field] == value


@pytest.mark.parametrize("field,value", [
    ("klar_closed", 1), ("klar_closed", "true"), ("klar_closed", None),
    ("klar_sent_at", 5), ("klar_sent_at", "yesterday"), ("klar_sent_at", ""),
    ("klar_sent_at", "2026-10-21T09:25:58"), ("klar_sent_at", None)])
def test_bad_new_klar_fields_raise(orders_file, field, value):
    _write(orders_file, {"B1": {**OLD_RECORD, field: value}})
    with pytest.raises(ValueError, match="B1"):
        orders_state.get("B1")


def test_update_fields_rejects_a_naive_klar_sent_at(orders_file):
    orders_state.put("B1", "blocked")
    with pytest.raises(ValueError):
        orders_state.update_fields("B1", klar_sent_at="2026-10-21T09:25:58")


def _klar_record(bid, sent_at=None, **fields):
    orders_state.put(bid, "ordered")
    extra = {"klar_message_id": 900, **fields}
    if sent_at is not None:
        extra["klar_sent_at"] = sent_at.isoformat()
    orders_state.update_fields(bid, **extra)


def test_outstanding_klar_excludes_closed_and_expired_records():
    _klar_record("B1", NOW - timedelta(days=13, hours=23))
    _klar_record("B2", NOW - timedelta(days=14, seconds=1))
    _klar_record("B3", NOW - timedelta(days=1), klar_closed=True)
    _klar_record("B4")                                     # pre-fix record: no klar_sent_at
    _klar_record("B5", NOW - timedelta(days=30), handled=True)

    assert [b for b, _ in orders_state.outstanding_klar(NOW)] == ["B1", "B4"]
    assert [b for b, _ in orders_state.expired_klar(NOW)] == ["B2"]


def test_outstanding_klar_defaults_to_now():
    _klar_record("B1", datetime.now(timezone.utc))
    _klar_record("B2", datetime.now(timezone.utc) - timedelta(days=20))
    assert [b for b, _ in orders_state.outstanding_klar()] == ["B1"]
    assert [b for b, _ in orders_state.expired_klar()] == ["B2"]


def test_old_records_have_no_expired_klar(orders_file):
    _write(orders_file, {"B1": OLD_RECORD})
    assert orders_state.outstanding_klar(NOW) == [] and orders_state.expired_klar(NOW) == []


# ── Order checker fields (Task 14) ───────────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("vehicle_model", "KIARIO (III)(2014)"), ("vehicle_model", ""), ("vehicle_model", "A" * 80),
    ("verified_at", "2026-10-22T06:00:01+00:00"),
    ("verified_numbers", "28894701 · 01683001/01683002"), ("verified_numbers", "9" * 120),
    ("verify_warned", True), ("verify_warned", False)])
def test_order_checker_fields_load(orders_file, field, value):
    _write(orders_file, {"B1": {**OLD_RECORD, field: value}})
    assert orders_state.get("B1")[field] == value


@pytest.mark.parametrize("field,value", [
    ("vehicle_model", 5), ("vehicle_model", None), ("vehicle_model", "A" * 81),
    ("vehicle_model", "KIA\nRIO"), ("vehicle_model", "KIA\x00"),
    ("verified_at", "2026-10-22T06:00:01"), ("verified_at", "igår"), ("verified_at", 1),
    ("verified_numbers", "9" * 121), ("verified_numbers", "1\t2"), ("verified_numbers", 28894701),
    ("verify_warned", 1), ("verify_warned", "yes"), ("verify_warned", None)])
def test_bad_order_checker_fields_raise(orders_file, field, value):
    _write(orders_file, {"B1": {**OLD_RECORD, field: value}})
    with pytest.raises(ValueError, match="B1"):
        orders_state.get("B1")


def test_update_fields_validates_order_checker_fields(orders_file):
    orders_state.put("B1", "ordered")
    orders_state.update_fields("B1", vehicle_model="KIARIO (III)", verify_warned=True,
                               verified_at="2026-10-22T06:00:01+00:00",
                               verified_numbers="28894701 · 01683001/01683002")
    for bad in ({"verify_warned": 1}, {"verified_at": "2026-10-22"}, {"vehicle_model": "x\r"}):
        with pytest.raises(ValueError):
            orders_state.update_fields("B1", **bad)
    assert orders_state.get("B1")["verify_warned"] is True


def test_unverified_orders_are_ordered_records_without_verified_at():
    orders_state.put("B1", "ordered")
    orders_state.put("B2", "ordered")
    orders_state.update_fields("B2", verified_at="2026-10-22T06:00:01+00:00")
    orders_state.put("B3", "failed")
    orders_state.put("B4", "pending")
    orders_state.put("B5", "ordered")
    orders_state.update_fields("B5", verify_warned=True)
    assert [b for b, _ in orders_state.unverified_orders()] == ["B1", "B5"]


def test_unverified_orders_empty_without_file(orders_file):
    assert orders_state.unverified_orders() == []


# ── Swap counter (package 2 task 4) ──────────────────────────────────────────

@pytest.mark.parametrize("value", [0, 1, 2])
def test_swaps_loads(orders_file, value):
    _write(orders_file, {"B1": {**OLD_RECORD, "swaps": value}})
    assert orders_state.get("B1")["swaps"] == value


@pytest.mark.parametrize("value", [-1, 3, True, False, "1", 1.0, None])
def test_bad_swaps_raise(orders_file, value):
    _write(orders_file, {"B1": {**OLD_RECORD, "swaps": value}})
    with pytest.raises(ValueError, match="B1"):
        orders_state.get("B1")


@pytest.mark.parametrize("value", [-1, 3, True, "1", 1.0])
def test_update_fields_rejects_bad_swaps(orders_file, value):
    orders_state.put("B1", "pending")
    before = orders_file.read_bytes()
    with pytest.raises(ValueError):
        orders_state.update_fields("B1", swaps=value)
    assert orders_file.read_bytes() == before


def test_swaps_is_kept_by_put_update_fields_and_begin():
    orders_state.put("B1", "pending", plan={"a": 1}, message_id=9)
    orders_state.update_fields("B1", swaps=1)
    orders_state.put("B1", "attempted")
    orders_state.update_fields("B1", cart_touched=True)
    orders_state.put("B1", "pending", plan={"a": 2}, message_id=10)
    rec = orders_state.get("B1")
    assert rec["swaps"] == 1 and rec["plan"] == {"a": 2} and rec["message_id"] == 10
    orders_state.queue("B2")
    orders_state.update_fields("B2", swaps=2)
    assert orders_state.begin("B2") is True
    assert orders_state.get("B2")["swaps"] == 2
