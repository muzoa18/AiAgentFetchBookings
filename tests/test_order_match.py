"""order_match: pure matching of a placed order to a row of "Skickade Beställningar"."""
from datetime import date

import pytest

from order_match import clean_vehicle_model, match_sent_order, model_token, normalise_model

D = date(2026, 10, 21)


def row(supplier_no, model, day="2026-10-21", numbers=None, reg=""):
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
            row("90000004", "", reg="TST001")]           # no model yet: not filled in
    m = match_sent_order(rows, "", D, set(), reg_nr="TST001")
    assert m.status == "verified" and m.row["supplier_no"] == "90000002"


def test_without_a_model_rows_without_a_registration_never_match():
    rows = [row("90000002", "KIA, Rio (III), 12-16")]
    assert match_sent_order(rows, None, D, set(), reg_nr="TST001").status == "not_found"


def test_rows_without_a_registration_still_match_by_model():
    rows = [row("90000002", "KIA, Rio (III), 12-16")]
    assert match_sent_order(rows, "KIARIO", D, set(), reg_nr="TST001").status == "verified"


def test_the_input_rows_are_not_modified():
    rows = [row("90000002", "KIA, Rio (III), 12-16")]
    before = [dict(r) for r in rows]
    match_sent_order(rows, "KIARIO", D, {"x"})
    assert rows == before
