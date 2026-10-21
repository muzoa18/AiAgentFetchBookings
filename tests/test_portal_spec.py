"""Booking Specifikation parsing (services / Delar / Vätskor) against a FAKE page: no browser.

The fake page wraps a stdlib-parsed copy of the real booking-page structure
(tests/fixtures/booking_delar.html) and answers the few Playwright calls
_extract_specification makes: query_selector / query_selector_all / inner_text.
"""
from pathlib import Path

import pytest

import agent
from parts_order import _wanted
from parts_order_helpers import parse_qty
from parts_portal_parse import parse_html
from portal_scraper import _extract_detail_page, _extract_specification

FIXTURE = (Path(__file__).parent / "fixtures" / "booking_delar.html").read_text(encoding="utf-8")


class FakeEl:
    def __init__(self, node):
        self.node = node

    def inner_text(self):
        return self.node.text()

    def query_selector_all(self, selector):
        if selector == "table tr":
            return [FakeEl(tr) for table in self.node.find_all("table") for tr in table.find_all("tr")]
        if selector in ("td", "th"):
            return [FakeEl(n) for n in self.node.find_all(selector)]
        raise AssertionError(f"unexpected selector {selector!r}")

    def query_selector(self, selector):
        found = self.query_selector_all(selector)
        return found[0] if found else None


class FakePage(FakeEl):
    """mode: "exact" = the real <mk-specification-detail> container is found;
    "broad" = only the over-broad 'div:has(h3:has-text(Specifikation))' style selectors
    match, giving an ancestor that ALSO holds the event log; None = no container at all."""

    def __init__(self, html, mode="exact"):
        super().__init__(parse_html(html))
        self.mode = mode

    def inner_text(self, selector=None):
        return self.node.text()

    def query_selector_all(self, selector):
        if selector in ("table tr", "td", "th"):
            return super().query_selector_all(selector)
        return []  # dt / labels / badges: not part of these pages

    def query_selector(self, selector):
        if selector == "mk-specification-detail":
            found = self.node.find("mk-specification-detail") if self.mode == "exact" else None
            return FakeEl(found) if found else None
        if "Specifikation" in selector:
            return FakeEl(self.node) if self.mode == "broad" else None
        return super().query_selector(selector)


MODES = ["exact", "broad", None]


def spec(html=FIXTURE, mode="exact", stats=None):
    return _extract_specification(FakePage(html, mode), stats)


@pytest.mark.parametrize("mode", MODES)
def test_delar_rows_become_parts_with_article_id_and_oem(mode):
    _, parts, _, _ = spec(mode=mode)
    assert [(p["name"], p["article_id"], p["oem_no"], p["qty"]) for p in parts] == [
        ("Pollenfilter", "1025-PC40293", "1025-PC40293", "1"),
        ("Oljefilter", "1025-F026407142", "1025-F026407142", "1"),
        ("Oljepluggspackning", "1500-49470236", "1500-49470236", "1"),
        ("Luftfilter", "1025-ADG022137", "1025-ADG022137", "1"),
        ("Tändstift", "1015-0242135554", "1015-0242135554", "4"),
    ]


def test_part_items_keep_existing_keys():
    _, parts, _, _ = spec()
    assert parts[0] == {"name": "Pollenfilter", "qty": "1", "price": "286,20 SEK",
                        "article_id": "1025-PC40293", "oem_no": "1025-PC40293"}
    assert parts[4]["price"] == "575,60 SEK"


def test_fluids_have_empty_ids_and_are_not_parts():
    _, parts, fluids, _ = spec()
    assert [(f["name"], f["qty"], f["article_id"], f["oem_no"]) for f in fluids] == [
        ("Bromsvätska", "1", "", ""), ("Motorolja", "3.3", "", ""), ("Frostskyddsvätska", "7.5", "", ""),
    ]
    assert not {"Bromsvätska", "Motorolja", "Frostskyddsvätska"} & {p["name"] for p in parts}


def test_service_rows_are_not_parts_and_their_codes_never_become_part_ids():
    services, parts, _, total = spec()
    assert [s["name"] for s in services] == [
        "Byt frostskyddsvätska", "Byt ut Tändstift", "Miljöavgift", "Förbrukningsmaterial"]
    ids = {p["article_id"] for p in parts} | {p["oem_no"] for p in parts}
    assert not ids & {"F00093", "3546"}
    assert services[0]["qty"] == "0.5"  # Antal column, unchanged behaviour
    assert total == "7 857 SEK"


def test_part_with_different_oem_and_article_id_keeps_both():
    html = FIXTURE.replace("<td><span>1025-F026407142</span></td><td><span>1025-F026407142</span></td>",
                           "<td><span>0451103336</span></td><td><span>1025-F026407142</span></td>")
    _, parts, _, _ = spec(html)
    oil = next(p for p in parts if p["name"] == "Oljefilter")
    assert (oil["oem_no"], oil["article_id"]) == ("0451103336", "1025-F026407142")


def test_part_with_oem_but_no_article_id_keeps_empty_article_id():
    html = FIXTURE.replace("<td><span>1500-49470236</span></td><td><span>1500-49470236</span></td>",
                           "<td><span>1500-49470236</span></td><td><span>\n</span></td>")
    _, parts, _, _ = spec(html)
    plug = next(p for p in parts if p["name"] == "Oljepluggspackning")
    assert (plug["oem_no"], plug["article_id"], plug["qty"]) == ("1500-49470236", "", "1")


def test_columns_follow_the_header_not_a_fixed_position():
    # A Delar table whose header puts Artikel-ID before OEM No and Antal elsewhere.
    html = """<table><thead><tr><th>Delar</th><th>Artikel-ID</th><th>Antal</th><th>OEM No</th>
    <th>Pris</th><th>Totalt</th></tr></thead><tbody>
    <tr><td>Luftfilter</td><td>1025-ADG022137</td><td>2</td><td>C 27 009</td><td>143,90</td>
    <td>287,80 SEK</td></tr></tbody></table>"""
    _, parts, _, _ = spec(html, mode=None)
    assert parts == [{"name": "Luftfilter", "qty": "2", "price": "287,80 SEK",
                      "article_id": "1025-ADG022137", "oem_no": "C 27 009"}]


def test_rows_without_id_headers_get_empty_ids():
    html = """<table><tr><td>Delar</td></tr>
    <tr><td>Bromsskivor fram</td><td>x</td><td>y</td><td>2</td><td>900,00 SEK</td></tr></table>"""
    _, parts, _, _ = spec(html, mode=None)
    assert parts == [{"name": "Bromsskivor fram", "qty": "2", "price": "900,00 SEK",
                      "article_id": "", "oem_no": ""}]


# ── Privacy: the event log (Händelselogg) never becomes services, parts or fluids ──

SENTINELS = ("Sentinel Staff Name", "Email sent to workshop", "Ny bokning", "Notifiering",
             "2026-10-18", "Händelse", "Användare")


def all_values(*sections):
    return " | ".join(str(v) for section in sections for item in section for v in item.values())


@pytest.mark.parametrize("mode", MODES)
def test_event_log_rows_never_become_items(mode):
    stats = {}
    services, parts, fluids, total = spec(mode=mode, stats=stats)
    assert stats == {"dropped": 0, "fallback": mode != "exact"}
    text = all_values(services, parts, fluids)
    assert not [s for s in SENTINELS if s.casefold() in text.casefold()]
    assert [(f["name"], f["qty"]) for f in fluids] == [
        ("Bromsvätska", "1"), ("Motorolja", "3.3"), ("Frostskyddsvätska", "7.5")]
    assert [p["name"] for p in parts] == [
        "Pollenfilter", "Oljefilter", "Oljepluggspackning", "Luftfilter", "Tändstift"]
    assert [s["name"] for s in services] == [
        "Byt frostskyddsvätska", "Byt ut Tändstift", "Miljöavgift", "Förbrukningsmaterial"]
    assert total == "7 857 SEK"


LOG_TABLE = FIXTURE[FIXTURE.index("<table role=\"grid\">"):FIXTURE.index("</table>", FIXTURE.index("<table role=\"grid\">")) + 8]
FLUIDS_ONLY = """<table><thead><tr><th>Vätskor / Oljor</th><th>OEM No</th><th>Artikel-ID</th><th>Antal</th>
<th>Pris</th><th>Totalt</th></tr></thead><tbody>
<tr><td>Motorolja</td><td></td><td></td><td>3.3</td><td>320,00</td><td>1 056,00 SEK</td></tr>
</tbody></table>"""


def test_log_table_right_after_fluids_without_totals_or_heading_is_ignored():
    # No "Totalt exklusive moms" row and no Händelselogg heading: the HÄNDELSE/DATUM header stops it.
    services, parts, fluids, _ = spec(FLUIDS_ONLY + LOG_TABLE, mode=None)
    assert [(f["name"], f["qty"]) for f in fluids] == [("Motorolja", "3.3")]
    assert services == [] and parts == []


def test_rows_after_totalt_exklusive_moms_are_not_items():
    late = """<table><tbody><tr><td>Luftfilter</td><td>X</td><td>X</td><td>1</td><td>9,00 SEK</td></tr>
    </tbody></table>"""
    html = FIXTURE.replace("</mk-specification-detail>", late + "</mk-specification-detail>")
    _, parts, fluids, _ = spec(html)
    assert [p["name"] for p in parts].count("Luftfilter") == 1
    assert "Luftfilter" not in [f["name"] for f in fluids]


def section(title, rows, ids=True):
    head = "<th>OEM No</th><th>Artikel-ID</th>" if ids else ""
    body = "".join(
        "<tr><td>{}</td>{}<td>{}</td><td>1,00</td><td>1,00 SEK</td></tr>".format(
            name, "<td>{0}</td><td>{0}</td>".format(code) if ids else "", qty)
        for name, code, qty in rows)
    return (f"<thead><tr><th>{title}</th>{head}<th>Antal</th><th>Pris</th><th>Totalt</th></tr></thead>"
            f"<tbody>{body}</tbody>")


def in_container(*sections):
    return "<mk-specification-detail><table>" + "".join(sections) + "</table></mk-specification-detail>"


# ── Review round 1: I1 odd quantities kept (container) / permissive (fallback) ──

def test_container_keeps_part_rows_with_odd_quantities_for_parse_qty():
    html = in_container(section("Delar", [("Luftfilter", "A1", ""), ("Tändstift", "B2", "x4"),
                                          ("Oljefilter", "C3", "2 st")]))
    stats = {}
    _, parts, _, _ = spec(html, mode="exact", stats=stats)
    assert [(p["name"], p["qty"]) for p in parts] == [("Luftfilter", ""), ("Tändstift", "x4"),
                                                      ("Oljefilter", "2 st")]
    assert stats == {"dropped": 0, "fallback": False}
    assert [parse_qty(p["qty"]) for p in parts] == [1, 4, None]
    _, reason = _wanted(parts)
    assert reason == "ogiltigt antal för 'Oljefilter' ('2 st')"  # visible block, never silent


def test_container_keeps_rows_whose_name_has_a_date_or_event_word():
    html = in_container(section("Delar", [("2026-10-18 Bromsbelägg", "A1", "1"),
                                          ("Notifieringslampa", "B2", "1")]))
    _, parts, _, _ = spec(html, mode="exact")
    assert [p["name"] for p in parts] == ["2026-10-18 Bromsbelägg", "Notifieringslampa"]


@pytest.mark.parametrize("mode", ["broad", None])
@pytest.mark.parametrize("qty", ["", "1", "4", "x4", "X 4", "2 st", "1,5", "0.5", "3.3", "7,5"])
def test_fallback_accepts_parse_qty_style_quantities(mode, qty):
    html = "<table>" + section("Delar", [("Luftfilter", "A1", qty)]) + "</table>"
    stats = {}
    _, parts, _, _ = spec(html, mode=mode, stats=stats)
    assert [p["qty"] for p in parts] == [qty]
    assert stats == {"dropped": 0, "fallback": True}


@pytest.mark.parametrize("mode", ["broad", None])
@pytest.mark.parametrize("title", ["Delar", "Vätskor / Oljor"])
def test_fallback_drops_and_counts_suspicious_rows(mode, title):
    rows = [("Luftfilter", "", "Sentinel Staff Name"), ("2026-10-18 13:26:30", "", "1"),
            ("Händelse: Ny bokning", "", "1"), ("Användare: Sentinel Staff Name", "", "1"),
            ("Tändstift", "", "4")]
    html = "<table>" + section(title, rows) + "</table>"
    stats = {}
    _, parts, fluids, _ = spec(html, mode=mode, stats=stats)
    assert [i["name"] for i in parts + fluids] == ["Tändstift"]
    assert stats == {"dropped": 4, "fallback": True}


@pytest.mark.parametrize("mode", MODES)
def test_date_like_part_number_inside_name_is_kept(mode):
    table = section("Delar", [("Bromsbelägg 0986-49-41 fram", "A1", "1")])
    html = in_container(table) if mode == "exact" else "<table>" + table + "</table>"
    _, parts, _, _ = spec(html, mode=mode)
    assert [p["name"] for p in parts] == ["Bromsbelägg 0986-49-41 fram"]


def test_detail_page_exposes_drop_and_fallback_counts():
    exact = _extract_detail_page(FakePage(FIXTURE, "exact"), "B-1")
    assert (exact["spec_dropped"], exact["spec_fallback"]) == (0, False)
    html = "<table>" + section("Delar", [("Luftfilter", "", "Sentinel Staff Name"),
                                         ("Tändstift", "", "4")]) + "</table>"
    fallback = _extract_detail_page(FakePage(html, None), "B-2")
    assert (fallback["spec_dropped"], fallback["spec_fallback"]) == (1, True)
    assert [p["name"] for p in fallback["parts"]] == ["Tändstift"]


BASE = {"booking_type": "Bokning", "booking_nr": "X-1", "parts": [{"name": "Luftfilter", "qty": "1"}]}


@pytest.mark.parametrize("extra,expected", [
    ({}, None),
    ({"spec_dropped": 0, "spec_fallback": False}, None),
    ({"spec_dropped": 2, "spec_fallback": False},
     "Obs: 2 rader i specifikationen kunde inte läsas – kontrollera bokningen i portalen."),
    ({"spec_dropped": 1, "spec_fallback": True},
     "Obs: 1 rader i specifikationen kunde inte läsas – kontrollera bokningen i portalen."),
    ({"spec_dropped": 0, "spec_fallback": True},
     "Obs: specifikationen lästes i reservläge – kontrollera delarna i portalen."),
])
def test_build_message_warns_about_unread_spec_rows(extra, expected):
    lines = agent.build_message({**BASE, **extra}).split("\n")
    obs = [line for line in lines if line.startswith("Obs:")]
    assert obs == ([expected] if expected else [])


# ── Review round 1: I2 Tjänster after Delar switches back to services ─────────

def test_tjanster_after_delar_is_service_again():
    html = in_container(
        section("Tjänster", [("Byt olja", "F00093", "0.5")]),
        section("Delar", [("Oljefilter", "1025-F026407142", "1")]),
        section("Tjänster", [("Byt bromsbelägg", "F00093", "1.2")]),
        section("Vätskor / Oljor", [("Motorolja", "", "3.3")]),
        section("Delar", [("Bromsbelägg", "1025-BP1", "1")]),
        section("Förbrukningsmaterial", [("Förbrukningsmaterial", "", "1")]),
    )
    services, parts, fluids, _ = spec(html)
    assert [s["name"] for s in services] == ["Byt olja", "Byt bromsbelägg", "Förbrukningsmaterial"]
    assert [(p["name"], p["article_id"]) for p in parts] == [
        ("Oljefilter", "1025-F026407142"), ("Bromsbelägg", "1025-BP1")]
    assert [f["name"] for f in fluids] == ["Motorolja"]
    assert "F00093" not in {p["article_id"] for p in parts} | {p["oem_no"] for p in parts}


# ── Review round 1: minors ────────────────────────────────────────────────────

def test_total_only_from_an_amount_cell_and_att_betala_is_final():
    html = in_container(section("Delar", [("Luftfilter", "A1", "1")])) + """<table><tbody>
    <tr><td>Summa</td><td>1 234,50 SEK</td><td>se ovan 3</td></tr>
    <tr><td>Att betala</td><td>7&nbsp;857 SEK</td><td></td></tr>
    <tr><td>Totalt</td><td>999 SEK</td></tr></tbody></table>"""
    html = html.replace("</mk-specification-detail>", "") + "</mk-specification-detail>"
    _, _, _, total = spec(html)
    assert total == "7 857 SEK"


def test_total_skips_non_amount_cells():
    html = in_container(section("Delar", [("Luftfilter", "A1", "1")])).replace(
        "</table>", "<tbody><tr><td>Summa</td><td>1 234,50 SEK</td><td>se ovan 3</td></tr></tbody></table>")
    _, _, _, total = spec(html)
    assert total == "1 234,50 SEK"


def test_empty_specification_logs_a_warning_without_row_text(caplog):
    with caplog.at_level("WARNING", logger="portal_scraper"):
        assert spec("<mk-specification-detail><table></table></mk-specification-detail>")[:3] == ([], [], [])
    assert any("no services, parts or fluids" in r.getMessage() for r in caplog.records)
