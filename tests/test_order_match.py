"""order_match: pure matching of a placed order to a row of "Skickade Beställningar"."""
from datetime import date

import pytest

from order_match import (claim_tokens, clean_vehicle_model, match_sent_order, model_token,
                         normalise_model, numbers_text, resolve_orders)

D = date(2026, 10, 21)


def row(supplier_no, model, day="2026-10-21", numbers=None, reg=None):
    """reg=None: the table has no registration column ('' = the column is there, empty)."""
    return {"supplier_no": supplier_no, "status": "", "date": day, "model": model,
            "order_numbers": numbers if numbers is not None else f"0{supplier_no}/0{supplier_no}1",
            "order_status": "Bekräftad", "reg_nr": reg}


# ── normalise_model / model_token ────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("KIA, Rio Kombi-sedan (III), 12-16", "kiariokombisedaniii1216"),
    ("‎KIA,​ Rio", "kiario"),
    ("  Citroën C4 ", "citroenc4"),
    ("Cee'd", "ceed"),
    ("", ""),
    (None, ""),
])
def test_normalise_model(text, expected):
    assert normalise_model(text) == expected


@pytest.mark.parametrize("vehicle, token", [
    ("KIARIO (III)(2014)", "kiario"),
    ("VOLKSWAGEN PASSAT", "volkswagenpassat"),
    ("‎KIARIO‎ (III)", "kiario"),
    ("(III)", ""),
    ("", ""),
    (None, ""),
    ("  -  ", ""),
])
def test_model_token_is_the_text_before_the_first_parenthesis(vehicle, token):
    assert model_token(vehicle) == token


def test_clean_vehicle_model_strips_control_chars_and_caps_the_length():
    assert clean_vehicle_model("KIARIO\x00 (III)\n(2014)‎") == "KIARIO (III) (2014)"
    assert len(clean_vehicle_model("A" * 200)) == 80
    assert clean_vehicle_model(None) == ""
    assert clean_vehicle_model(42) == ""


# ── match_sent_order ─────────────────────────────────────────────────────────

def test_kia_rio_booking_matches_the_shop_row():
    rows = [row("90000002", "KIA, Rio Kombi-sedan (III), 12-16", numbers="01000011/01000012"),
            row("90000006", "KIA, Niro Crossover (I), 16-22")]
    m = match_sent_order(rows, "KIARIO (III)(2014)", D, set())
    assert m.status == "verified"
    assert [(r["supplier_no"], r["order_numbers"]) for r in m.rows] == [
        ("90000002", "01000011/01000012")]
    assert m.row["model"] == "KIA, Rio Kombi-sedan (III), 12-16"


def test_volkswagen_passat_matches():
    rows = [row("90000003", "VOLKSWAGEN, Passat Variant (B7) (365), 10-15")]
    assert match_sent_order(rows, "VOLKSWAGEN PASSAT", D, set()).status == "verified"


def test_zero_width_noise_on_both_sides_is_ignored():
    rows = [row("90000002", "‎KIA,​ Rio‎ Kombi-sedan (III), 12-16")]
    assert match_sent_order(rows, "‎KIARIO (III)", D, set()).status == "verified"


def test_same_model_twice_is_ambiguous_and_returns_all():
    rows = [row("90000004", "OPEL, Astra Sports Tourer (J), 10-16"),
            row("90000005", "OPEL, Astra Sports Tourer (J), 10-16")]
    m = match_sent_order(rows, "OPELASTRA (J)(2012)", D, set())
    assert m.status == "ambiguous"
    assert [r["supplier_no"] for r in m.rows] == ["90000004", "90000005"]
    assert m.row is None


def test_claimed_numbers_are_excluded():
    rows = [row("90000004", "OPEL, Astra Sports Tourer (J), 10-16", numbers="01000031/01000032"),
            row("90000005", "OPEL, Astra Sports Tourer (J), 10-16", numbers="01000041/01000042")]
    m = match_sent_order(rows, "OPELASTRA (J)", D, {"90000004", "01000031", "01000032"})
    assert m.status == "verified" and m.row["supplier_no"] == "90000005"
    # claimed by a portal order number alone is enough
    m = match_sent_order(rows, "OPELASTRA (J)", D, {"01000042"})
    assert m.status == "verified" and m.row["supplier_no"] == "90000004"


def test_rows_older_than_the_order_date_are_ignored():
    rows = [row("90000006", "KIA, Rio (III), 12-16", day="2026-10-20"),
            row("90000007", "KIA, Rio (III), 12-16", day="2026-10-22")]
    m = match_sent_order(rows, "KIARIO (III)", D, set())
    assert m.status == "verified" and m.row["supplier_no"] == "90000007"
    assert match_sent_order(rows[:1], "KIARIO (III)", D, set()).status == "not_found"


@pytest.mark.parametrize("bad_day", ["", "21-10-2026", "2026-13-01", None])
def test_rows_with_an_unreadable_date_are_ignored(bad_day):
    rows = [row("90000002", "KIA, Rio (III), 12-16", day=bad_day)]
    assert match_sent_order(rows, "KIARIO (III)", D, set()).status == "not_found"


def test_rows_without_a_model_are_ignored():
    rows = [row("90000001", ""), row("90000007", "-"),
            row("90000002", "KIA, Rio (III), 12-16")]
    m = match_sent_order(rows, "KIARIO (III)", D, set())
    assert m.status == "verified" and m.row["supplier_no"] == "90000002"


def test_rows_without_a_supplier_number_are_ignored():
    rows = [row("", "KIA, Rio (III), 12-16")]
    assert match_sent_order(rows, "KIARIO (III)", D, set()).status == "not_found"


@pytest.mark.parametrize("vehicle", ["KIA", "KIAROK", "KIARION (I)", "KIAR"])
def test_brand_or_prefix_collisions_do_not_match(vehicle):
    rows = [row("90000002", "KIA, Rio Kombi-sedan (III), 12-16"),
            row("90000006", "KIA, Niro Crossover (I), 16-22")]
    assert match_sent_order(rows, vehicle, D, set()).status == "not_found"


def test_model_words_may_continue_into_the_body_style():
    rows = [row("90000002", "KIA, Rio Kombi-sedan (III), 12-16")]
    assert match_sent_order(rows, "KIARIO KOMBI", D, set()).status == "verified"
    assert match_sent_order(rows, "KIA RIO", D, set()).status == "verified"


@pytest.mark.parametrize("vehicle", ["", None, "(III)(2014)", "  ", "-"])
def test_no_usable_model_is_no_model(vehicle):
    rows = [row("90000002", "KIA, Rio (III), 12-16")]
    assert match_sent_order(rows, vehicle, D, set()).status == "no_model"


def test_no_rows_is_not_found():
    assert match_sent_order([], "KIARIO (III)", D, set()).status == "not_found"


# The live page also carries the registration number in a (hidden) column.

def test_a_different_registration_excludes_a_model_match():
    rows = [row("90000002", "KIA, Rio (III), 12-16", reg="TST009"),
            row("90000003", "KIA, Rio (III), 12-16", reg="TST001")]
    m = match_sent_order(rows, "KIARIO (III)", D, set(), reg_nr="tst 001")
    assert m.status == "verified" and m.row["supplier_no"] == "90000003"


def test_without_a_model_the_registration_alone_can_verify():
    rows = [row("90000002", "KIA, Rio (III), 12-16", reg="TST001"),
            row("90000003", "OPEL, Astra (J), 10-16", reg="TST002"),
            row("90000004", "", reg="")]                 # registration not filled in yet
    m = match_sent_order(rows, "", D, set(), reg_nr="TST001")
    assert m.status == "verified" and m.row["supplier_no"] == "90000002"


def test_without_a_model_rows_without_a_registration_never_match():
    rows = [row("90000002", "KIA, Rio (III), 12-16", reg="")]         # column there, empty
    assert match_sent_order(rows, None, D, set(), reg_nr="TST001").status == "not_found"
    no_column = [row("90000002", "KIA, Rio (III), 12-16")]
    assert match_sent_order(no_column, None, D, set(), reg_nr="TST001").status == "no_model"


def test_the_numbers_limit_is_the_state_limit():
    import order_match
    import orders_state
    assert order_match.NUMBERS_MAX == orders_state.NUMBERS_MAX


def test_without_a_registration_column_a_known_plate_still_matches_by_model():
    rows = [row("90000002", "KIA, Rio (III), 12-16")]                 # column absent
    assert match_sent_order(rows, "KIARIO", D, set(), reg_nr="TST001").status == "verified"


def test_with_a_known_plate_an_empty_registration_row_never_counts():
    rows = [row("90000002", "KIA, Rio (III), 12-16", reg=""),
            row("90000003", "OPEL, Astra (J), 10-16", reg="TST002")]
    assert match_sent_order(rows, "KIARIO", D, set(), reg_nr="TST001").status == "not_found"


def test_a_plate_match_alone_is_enough_whatever_the_model_naming():
    rows = [row("90000002", "MERCEDES-BENZ, C-Klass (W205), 14-21", reg="TST001"),
            row("90000003", "MERCEDES-BENZ, C-Klass (W205), 14-21", reg="TST002")]
    m = match_sent_order(rows, "MERCEDESC220 (W205)", D, set(), reg_nr="TST001")
    assert m.status == "verified" and m.row["supplier_no"] == "90000002"


def test_a_plate_match_still_needs_number_date_and_no_claim():
    ok = row("90000002", "", reg="TST001")                      # model not filled in yet
    assert match_sent_order([ok], "KIARIO", D, set(), reg_nr="TST001").status == "verified"
    for bad in (row("", "KIA, Rio", reg="TST001"),
                row("90000002", "KIA, Rio", reg="TST001", day="2026-10-20")):
        assert match_sent_order([bad], "KIARIO", D, set(), reg_nr="TST001").status == "not_found"
    assert match_sent_order([ok], "KIARIO", D, {"90000002"}, reg_nr="TST001").status == "not_found"


def test_without_a_plate_the_model_is_used_even_with_a_registration_column():
    rows = [row("90000002", "KIA, Rio (III), 12-16", reg="TST009")]
    assert match_sent_order(rows, "KIARIO", D, set()).status == "verified"


# ── resolve_orders: several records in one run (never swap rows) ─────────────

def test_two_records_for_the_same_plate_and_one_row_verify_nothing():
    rows = [row("90000002", "KIA, Rio (III), 12-16", reg="TST001")]
    out = resolve_orders({"B1": ("KIARIO", D, "TST001"), "B2": ("KIARIO", D, "TST001")},
                         rows, set())
    assert {b: m.status for b, m in out.items()} == {"B1": "waiting", "B2": "waiting"}


def test_two_records_for_the_same_model_and_one_row_verify_nothing():
    rows = [row("90000002", "KIA, Rio (III), 12-16")]
    out = resolve_orders({"B1": ("KIARIO", D, None), "B2": ("KIARIO (III)", D, None)},
                         rows, set())
    assert {m.status for m in out.values()} == {"waiting"}


def test_two_records_and_two_rows_are_ambiguous_for_both():
    rows = [row("90000002", "KIA, Rio (III), 12-16", reg="TST001"),
            row("90000003", "KIA, Rio (III), 12-16", reg="TST001")]
    out = resolve_orders({"B1": ("KIARIO", D, "TST001"), "B2": ("KIARIO", D, "TST001")},
                         rows, set())
    for m in out.values():
        assert m.status == "ambiguous"
        assert [r["supplier_no"] for r in m.rows] == ["90000002", "90000003"]


def test_one_record_one_row_is_verified_and_other_cars_are_unaffected():
    rows = [row("90000002", "KIA, Rio (III), 12-16", reg="TST001"),
            row("90000003", "OPEL, Astra (J), 10-16", reg="TST002"),
            row("90000004", "OPEL, Astra (J), 10-16", reg="TST002")]
    out = resolve_orders({"B1": ("KIARIO", D, "TST001"), "B2": ("OPELASTRA", D, "TST002"),
                          "B3": ("VOLVOV40", D, "TST003"), "B4": ("", D, None)}, rows, set())
    assert out["B1"].status == "verified" and out["B1"].row["supplier_no"] == "90000002"
    assert out["B2"].status == "ambiguous"
    assert out["B3"].status == "not_found" and out["B4"].status == "no_model"


def test_resolve_orders_still_excludes_claimed_rows():
    rows = [row("90000002", "KIA, Rio (III), 12-16", reg="TST001"),
            row("90000003", "KIA, Rio (III), 12-16", reg="TST001")]
    out = resolve_orders({"B2": ("KIARIO", D, "TST001")}, rows, {"90000002"})
    assert out["B2"].status == "verified" and out["B2"].row["supplier_no"] == "90000003"


# ── stored numbers and claims (one representation) ───────────────────────────

def test_numbers_text_is_cut_at_a_number_boundary():
    long = "/".join(f"0100{i:04d}" for i in range(16))              # 143 characters
    text = numbers_text({"supplier_no": "90000002", "order_numbers": long})
    assert len(text) <= 120 and text.startswith("90000002 · 01000000/")
    assert all(len(t) == 8 for t in claim_tokens(text))            # no half number
    assert claim_tokens(numbers_text({"supplier_no": "9", "order_numbers": ""})) == {"9"}


def test_the_input_rows_are_not_modified():
    rows = [row("90000002", "KIA, Rio (III), 12-16")]
    before = [dict(r) for r in rows]
    match_sent_order(rows, "KIARIO", D, {"x"})
    assert rows == before
