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


# ── extract_order_reference ─────────────────────────────────────────────────

@pytest.mark.parametrize("html, url, expected", [
    ("<p>Tack för din beställning! Ordernummer: 4501234567</p>", "https://x/ViewOrder", "4501234567"),
    ("<p>Ditt ordernr är SO-778899</p>", "https://x/", "SO-778899"),
    ("<p>Beställningsnummer 12345678</p>", "https://x/", "12345678"),
    ("<p>Tack!</p>", "https://x/ViewOrderConfirmation?OrderNo=99887766&x=1", "99887766"),
])
def test_extract_order_reference(html, url, expected):
    assert extract_order_reference(html, url) == expected


@pytest.mark.parametrize("html", [
    "<p>Tack för din order till oss</p>", "<p>Orderöversikt 0</p>", "<title>Beställning ABC123</title>",
])
def test_extract_order_reference_none(html):
    assert extract_order_reference(html, "https://x/ViewHomepage-Start") is None
