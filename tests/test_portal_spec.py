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
    def __init__(self, html, has_container=True):
        super().__init__(parse_html(html))
        self.has_container = has_container

    def query_selector(self, selector):
        if "Specifikation" in selector:
            return FakeEl(self.node) if self.has_container else None
        return super().query_selector(selector)


def spec(html=FIXTURE, has_container=True):
    return _extract_specification(FakePage(html, has_container))


@pytest.mark.parametrize("has_container", [True, False])
def test_delar_rows_become_parts_with_article_id_and_oem(has_container):
    _, parts, _, _ = spec(has_container=has_container)
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
    _, parts, _, _ = spec(html, has_container=False)
    assert parts == [{"name": "Luftfilter", "qty": "2", "price": "287,80 SEK",
                      "article_id": "1025-ADG022137", "oem_no": "C 27 009"}]


def test_rows_without_id_headers_get_empty_ids():
    html = """<table><tr><td>Delar</td></tr>
    <tr><td>Bromsskivor fram</td><td>x</td><td>y</td><td>2</td><td>900,00 SEK</td></tr></table>"""
    _, parts, _, _ = spec(html, has_container=False)
    assert parts == [{"name": "Bromsskivor fram", "qty": "2", "price": "900,00 SEK",
                      "article_id": "", "oem_no": ""}]
