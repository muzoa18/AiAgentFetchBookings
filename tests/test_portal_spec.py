"""Booking Specifikation parsing (services / Delar / Vätskor) against a FAKE page: no browser.

The fake page wraps a stdlib-parsed copy of the real booking-page structure
(tests/fixtures/booking_delar.html) and answers the few Playwright calls
_extract_specification makes: query_selector / query_selector_all / inner_text.
"""
from pathlib import Path

import pytest

from parts_portal_parse import parse_html
from portal_scraper import _extract_specification

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

    def query_selector(self, selector):
        if selector == "mk-specification-detail":
            found = self.node.find("mk-specification-detail") if self.mode == "exact" else None
            return FakeEl(found) if found else None
        if "Specifikation" in selector:
            return FakeEl(self.node) if self.mode == "broad" else None
        return super().query_selector(selector)


MODES = ["exact", "broad", None]


def spec(html=FIXTURE, mode="exact"):
    return _extract_specification(FakePage(html, mode))


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
    services, parts, fluids, total = spec(mode=mode)
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


@pytest.mark.parametrize("name,qty", [
    ("2026-10-18 13:26:30", "1"),        # timestamp as name
    ("Ny bokning", "1"), ("Notifiering", "1"),
    ("Händelse: Ny bokning", "1"), ("Händelse: Notifiering", "1"),
    ("Luftfilter", "Sentinel Staff Name"),  # non-numeric quantity
    ("Luftfilter", ""),
])
@pytest.mark.parametrize("section", ["Delar", "Vätskor / Oljor"])
def test_item_builder_drops_timestamp_event_and_non_numeric_rows(section, name, qty):
    html = f"""<table><thead><tr><th>{section}</th><th>OEM No</th><th>Artikel-ID</th><th>Antal</th>
    <th>Pris</th><th>Totalt</th></tr></thead><tbody>
    <tr><td>{name}</td><td></td><td></td><td>{qty}</td><td>1,00</td><td>1,00 SEK</td></tr>
    <tr><td>Tändstift</td><td>A</td><td>B</td><td>4</td><td>1,00</td><td>4,00 SEK</td></tr>
    </tbody></table>"""
    _, parts, fluids, _ = spec(html, mode=None)
    assert [i["name"] for i in parts + fluids] == ["Tändstift"]


@pytest.mark.parametrize("qty", ["1", "4", "3.3", "7,5", "0.5"])
def test_numeric_quantities_are_kept(qty):
    html = f"""<table><thead><tr><th>Delar</th><th>OEM No</th><th>Artikel-ID</th><th>Antal</th>
    <th>Totalt</th></tr></thead><tbody>
    <tr><td>Luftfilter</td><td>A</td><td>B</td><td>{qty}</td><td>1,00 SEK</td></tr></tbody></table>"""
    _, parts, _, _ = spec(html, mode=None)
    assert [p["qty"] for p in parts] == [qty]
