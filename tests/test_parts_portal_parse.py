from pathlib import Path

import pytest

from parts_portal import (PartsPortalError, extract_order_reference, parse_cart, parse_checkout,
                          parse_search_results, parse_sek)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── parse_sek ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("248:75", 248.75), ("199:-", 199.0), ("1 234:50", 1234.5), ("1\xa0234:50", 1234.5),
    ("1 234:50", 1234.5), ("171:64", 171.64), ("248,75", 248.75), ("248.75", 248.75),
    (" 0:- ", 0.0), ("377:50 kr", 377.5), ("1 208:75", 1208.75), ("42", 42.0),
])
def test_parse_sek_swedish_formats(text, expected):
    assert parse_sek(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["-", "", "   ", None, "abc", "12:5", "1.234,50", "12:345",
                                  "Totalt", "--", ":50", "1 2 3:4x"])
def test_parse_sek_garbage_is_none(text):
    assert parse_sek(text) is None


# ── parse_search_results ─────────────────────────────────────────────────────

def test_search_returns_only_linked_usable_parts():
    result = parse_search_results(fixture("meca_search.html"))
    assert [c["url_or_id"] for c in result] == ["10000001", "10000002"]


def test_search_candidate_shape():
    first, second = parse_search_results(fixture("meca_search.html"))
    assert first == {"title": "Purflux Oljefilter, Filterinsats", "brand": "", "price_sek": 377.5,
                     "url_or_id": "10000001", "article_nr": "1025-L1075", "in_stock": True,
                     "placement": ""}
    assert second["title"] == "Bosch Oljefilter, Filterinsats"
    assert second["price_sek"] == pytest.approx(1208.75)
    assert second["article_nr"] == "1025-F026407026"
    assert second["in_stock"] is False
    assert second["placement"] == "fram"


def test_search_excludes_discontinued_and_replaced():
    ids = {c["url_or_id"] for c in parse_search_results(fixture("meca_search.html"))}
    assert not ids & {"10000003", "10000004", "10000005"}


def test_search_never_returns_other_tabs_or_related_items():
    ids = {c["url_or_id"] for c in parse_search_results(fixture("meca_search.html"))}
    assert not ids & {"20000001", "30000001", "10000099"}


def test_search_no_linked_parts_is_empty():
    assert parse_search_results(fixture("meca_search_no_linked.html")) == []


def test_search_without_tab_layout_is_empty():
    html = ("<html><body><h1>Sökresultat</h1><ul><li class='category-list-item' data-sku='1'>"
            "<h2>Pollenfilter</h2><p class='category-list-item__price'>99:-</p>"
            "<form class='product-form-quick' data-sku='1'></form></li></ul></body></html>")
    assert parse_search_results(html) == []


def test_search_skips_card_without_price_or_add_form():
    html = fixture("meca_search.html")
    html = html.replace('<p class="category-list-item__price">377:50</p>',
                        '<p class="category-list-item__price">-</p>')
    html = html.replace('data-sku="10000002" data-productname', 'data-sku="other" data-productname')
    assert parse_search_results(html) == []


def test_search_unknown_stock_is_none():
    html = fixture("meca_search.html").replace('class="workshop"', 'class="elsewhere"')
    assert [c["in_stock"] for c in parse_search_results(html)] == [None, None]


def test_search_caps_at_fifteen_candidates():
    html = fixture("meca_search.html")
    start = html.index("<!-- 1: normal")
    end = html.index("<!-- 2: linked")
    card = html[start:end]
    many = "".join(card.replace("10000001", f"9{i:07d}") for i in range(20))
    html = html[:start] + many + html[end:]
    assert len(parse_search_results(html)) == 15


# ── parse_cart ───────────────────────────────────────────────────────────────

def test_parse_cart_total_and_lines():
    cart = parse_cart(fixture("meca_cart.html"))
    assert cart["lines"] == [{"title": "Bosch Oljefilter, Filterinsats", "qty": 1,
                              "price_sek": 248.75, "id": "10000002", "url_or_id": "10000002",
                              "article_nr": "1025-F026407026"}]
    assert cart["total_sek"] == pytest.approx(248.75)
    assert cart["reg_nr"] == "ABC123"


def test_parse_cart_empty():
    cart = parse_cart(fixture("meca_cart_empty.html"))
    assert cart["lines"] == [] and cart["total_sek"] == 0.0
    assert cart["reg_nr"] == "ABC123"


def test_parse_cart_missing_total_is_none():
    html = fixture("meca_cart.html")
    html = html.replace("Totalt:<span>248:75</span>", "Totalt:<span>-</span>")
    html = html.replace('<span class="js-total-tot-sum">248:75</span>',
                        '<span class="js-total-tot-sum"></span>')
    assert parse_cart(html)["total_sek"] is None


def test_parse_cart_uses_higher_total():
    html = fixture("meca_cart.html").replace('<span class="js-total-tot-sum">248:75</span>',
                                             '<span class="js-total-tot-sum">1 310:00</span>')
    assert parse_cart(html)["total_sek"] == pytest.approx(1310.0)


def test_parse_cart_multi_qty():
    html = fixture("meca_cart.html").replace('value="1 st" id="qty"', 'value="3 st" id="qty"')
    assert parse_cart(html)["lines"][0]["qty"] == 3


@pytest.mark.parametrize("old, new", [
    ('value="1 st" id="qty"', 'value="ett" id="qty"'),                       # unreadable qty
    ('value="1 st" id="qty"', 'value="0 st" id="qty"'),                      # zero qty
    ('class="js-product-quantity js-product-quantity-cart"', 'class="x"'),   # no qty input
    ('<span>Bosch Oljefilter, Filterinsats</span></div></div>', '<span> </span></div></div>'),
])
def test_parse_cart_unparseable_row_raises(old, new):
    html = fixture("meca_cart.html")
    assert old in html
    with pytest.raises(PartsPortalError):
        parse_cart(html.replace(old, new))


def test_parse_cart_unknown_row_in_other_table_raises():
    html = fixture("meca_cart.html").replace(
        '<tbody class="js-sortable-table ui-sortable"></tbody>',
        '<tbody class="js-sortable-table ui-sortable"><tr><td>Däckhotell</td></tr></tbody>')
    with pytest.raises(PartsPortalError):
        parse_cart(html)


def test_parse_cart_header_count_without_rows_raises():
    html = fixture("meca_cart_empty.html").replace("Varukorg<span>(0)</span>",
                                                   "Varukorg<span>(2)</span>")
    with pytest.raises(PartsPortalError):
        parse_cart(html)


def test_parse_cart_not_a_cart_page_raises():
    with pytest.raises(PartsPortalError):
        parse_cart(fixture("meca_checkout.html"))


# ── parse_checkout ───────────────────────────────────────────────────────────

def test_parse_checkout():
    co = parse_checkout(fixture("meca_checkout.html"))
    assert co["title"] == "Beställning ABC123"
    assert co["reg_nr"] == "ABC123"
    assert co["total_sek"] == pytest.approx(171.64)
    assert co["shipping_selected"] is True
    assert co["has_place_order"] is True
    assert co["lines"] == [{"id": "10000002", "qty": 1, "title": "Bosch Oljefilter, Filterinsats"}]


def test_parse_checkout_problems():
    html = fixture("meca_checkout.html")
    html = html.replace(' checked="checked"', "")
    html = html.replace('<span class="js-total-tot-sum">171:64</span>',
                        '<span class="js-total-tot-sum">-</span>')
    html = html.replace("checkout-page-place-order__button checkout", "other checkout")
    co = parse_checkout(html)
    assert co["shipping_selected"] is False
    assert co["total_sek"] is None
    assert co["has_place_order"] is False


def test_parse_checkout_unreadable_line_is_none():
    html = fixture("meca_checkout.html").replace('value="1 st" id="qty"', 'value="?" id="qty"')
    assert parse_checkout(html)["lines"] is None


# ── Unreadable totals fail closed ───────────────────────────────────────────

def test_parse_cart_present_but_unreadable_total_is_none():
    html = fixture("meca_cart.html").replace("Totalt:<span>248:75</span>", "Totalt:<span>2x8</span>")
    assert parse_cart(html)["total_sek"] is None


def test_parse_checkout_present_but_unreadable_total_is_none():
    html = fixture("meca_checkout.html").replace(
        "</strong></p>", "</strong></p><p>Totalt <span class=\"js-total-tot-sum\">??</span></p>", 1)
    assert parse_checkout(html)["total_sek"] is None


# ── extract_order_reference ─────────────────────────────────────────────────

REG = "ABC123"


@pytest.mark.parametrize("html, url, expected", [
    ("<p>Tack för din beställning! Ordernummer: 4501234567</p>", "https://x/ViewOrder", "4501234567"),
    ("<p>Ordernummer 123456789</p>", "https://x/", "123456789"),
    ("<p>Ditt ordernr är SO-778899</p>", "https://x/", "SO-778899"),
    ("<p>Beställningsnummer 12345678</p>", "https://x/", "12345678"),
    ("<p>Tack!</p>", "https://x/ViewOrderConfirmation?OrderNo=99887766&x=1", "99887766"),
])
def test_extract_order_reference(html, url, expected):
    assert extract_order_reference(html, url, REG) == expected


@pytest.mark.parametrize("html, url", [
    ("<p>Tack för din order till oss</p>", "https://x/ViewHomepage-Start"),
    ("<p>Orderöversikt 0</p>", "https://x/"),
    ("<title>Beställning ABC123</title>", "https://x/"),
    ("<label>Ert ordernummer</label> <p>ABC123</p>", "https://x/"),            # customer's own number
    ("<p>Ert ordernummer 98765432</p>", "https://x/"),
    ("<p>Kundordernummer ABC123</p>", "https://x/"),
    ("<p>Kundordernummer 98765432</p>", "https://x/"),
    ("<p>Kund ordernummer 98765432</p>", "https://x/"),
    ("<p>Ordernummer: ABC123</p>", "https://x/"),                               # equals the reg
    ("<p>Ordernummer: abc-123</p>", "https://x/"),
    ("<p>Tack!</p>", "https://x/Confirm?OrderNo=ABC123"),
    ("<p>Ordernummer: 12</p>", "https://x/"),                                   # implausible shape
    ("<p>Xordernummer 12345678</p>", "https://x/"),                             # no word boundary
])
def test_extract_order_reference_none(html, url):
    assert extract_order_reference(html, url, REG) is None


def test_extract_order_reference_never_from_cart_or_checkout_page():
    cart = fixture("meca_cart.html").replace("</main>", "<p>Ordernummer 123456789</p></main>")
    checkout = fixture("meca_checkout.html").replace("</main>", "<p>Ordernummer 123456789</p></main>")
    assert extract_order_reference(cart, "https://x/ViewOrder-Done", REG) is None
    assert extract_order_reference(checkout, "https://x/ViewOrder-Done", REG) is None


# ── Skickade Beställningar (order checker) ───────────────────────────────────

from parts_portal_parse import parse_sent_orders, sent_orders_ready  # noqa: E402

SENT = fixture("meca_sent_orders.html")


def test_parse_sent_orders_reads_every_order_row_by_header_position():
    rows = parse_sent_orders(SENT)
    assert [r["supplier_no"] for r in rows] == [f"9000000{i}" for i in range(1, 8)]
    assert rows[1] == {"supplier_no": "90000002", "status": "", "date": "2026-10-21",
                       "reg_nr": "TST001", "model": "KIA, Rio Kombi-sedan (III), 12-16",
                       "order_numbers": "01000011/01000012", "order_status": "Bekräftad"}
    assert rows[2]["status"] == "Följesedel utskriven"
    assert rows[2]["model"] == "VOLKSWAGEN, Passat Variant (B7) (365), 10-15"


def test_parse_sent_orders_model_dash_or_empty_is_empty():
    rows = parse_sent_orders(SENT)
    assert rows[0]["model"] == "" and rows[0]["order_numbers"] == ""
    assert rows[0]["order_status"] == "-"
    assert rows[6]["model"] == ""


def test_parse_sent_orders_strips_zero_width_marks_from_cells():
    html = SENT.replace("<td>KIA, Rio", "<td>&#8206;KIA, Rio").replace(
        "<td>90000002</td>", "<td>​90000002‎</td>")
    row = parse_sent_orders(html)[1]
    assert row["supplier_no"] == "90000002" and row["model"].startswith("KIA, Rio")


def test_parse_sent_orders_follows_the_header_order():
    # Swap the Datum and Status columns (headers and cells): positions come from the headers.
    html = SENT.replace(">Status</a>", ">TMP</a>").replace(">Datum</a>", ">Status</a>") \
        .replace(">TMP</a>", ">Datum</a>")
    html = html.replace("<td></td>\n<td>2026-10-21</td><td>TST001</td>",
                        "<td>2026-10-21</td>\n<td></td><td>TST001</td>")
    row = parse_sent_orders(html)[1]
    assert row["date"] == "2026-10-21" and row["status"] == ""


def test_parse_sent_orders_without_a_registration_column():
    html = SENT.replace("&#8206;Registreringsnummer", "&#8206;Servicetyp")
    row = parse_sent_orders(html)[1]
    assert row["reg_nr"] is None and row["model"].startswith("KIA, Rio")   # column absent
    assert parse_sent_orders(SENT)[0]["reg_nr"] == ""                    # column there, empty


def test_parse_sent_orders_empty_table_is_empty_list():
    start, end = SENT.index("<tbody>"), SENT.index("</tbody>")
    assert parse_sent_orders(SENT[:start] + "<tbody>" + SENT[end:]) == []


@pytest.mark.parametrize("html", [
    "<html><body><p>Inga ordrar</p></body></html>",
    SENT.replace("&#8206;Bilmodell", "&#8206;Fordon"),
    SENT.replace("&#8206;Beställningsstatus", "&#8206;Läge"),
    SENT.replace("&#8206;Ordernummer", "&#8206;Nummer"),     # only one Ordernummer left
    SENT.replace(">Datum</a>", ">Dag</a>"),
    "",
])
def test_parse_sent_orders_without_the_table_or_headers_is_a_parse_error(html):
    with pytest.raises(PartsPortalError) as e:
        parse_sent_orders(html)
    assert e.value.code == "parse"


def test_parse_sent_orders_fixture_has_no_customer_data():
    import re
    assert "@" not in SENT and "+46" not in SENT and "customerid" not in SENT.casefold()
    assert not re.search(r"\b07\d{8}\b", SENT)                       # no phone numbers
    plates = set(re.findall(r"\b[A-Z]{3}\d{2}[A-Z0-9]\b", SENT))
    assert plates <= {"TST001", "TST002", "TST003", "TST004"}       # invented plates only


def test_sent_orders_ready_only_for_the_active_sent_tab_with_sent_rows():
    assert sent_orders_ready(SENT)
    open_tab = SENT.replace("sent active js-orderhistory-view", "sent js-orderhistory-view") \
        .replace("open  js-orderhistory-view", "open active js-orderhistory-view")
    assert not sent_orders_ready(open_tab)
    assert not sent_orders_ready(SENT.replace("90000001&amp;view=sent", "90000001&amp;view=open"))
    assert not sent_orders_ready(SENT.replace("&#8206;Bilmodell", "&#8206;Fordon"))
    assert not sent_orders_ready("<html><body>laddar…</body></html>")


def _empty(html):
    start, end = html.index("<tbody>"), html.index("</tbody>")
    return html[:start] + "<tbody>" + html[end:]


def test_an_empty_sent_table_is_only_ready_when_empty_is_allowed():
    assert not sent_orders_ready(_empty(SENT))                  # may still be loading
    assert sent_orders_ready(_empty(SENT), allow_empty=True)
    rows_without_links = SENT.replace("data-url=", "data-x=")
    assert not sent_orders_ready(rows_without_links)            # no row proves the sent view
