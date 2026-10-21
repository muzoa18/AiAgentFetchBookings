"""PartsPortal sequencing against a FAKE page: no browser, no network, nothing is ever ordered."""
from pathlib import Path

import pytest

import parts_portal
from parts_portal import PartsPortal, PartsPortalError

FIXTURES = Path(__file__).parent / "fixtures"
CART = (FIXTURES / "meca_cart.html").read_text(encoding="utf-8")
EMPTY_CART = (FIXTURES / "meca_cart_empty.html").read_text(encoding="utf-8")
CHECKOUT = (FIXTURES / "meca_checkout.html").read_text(encoding="utf-8")
CHECKOUT_URL = "https://pro.meca.se/INTERSHOP/web/WFS/x/ViewData-Start/1?JumpTarget=ViewCheckout-Start"
SUBMIT_URL = "https://pro.meca.se/INTERSHOP/web/WFS/x/ViewCheckout-SubmitOrder"
CONFIRM_URL = "https://pro.meca.se/INTERSHOP/web/WFS/x/ViewOrder-Confirmation"
CONFIRM = "<html><body><h1>Tack för din beställning!</h1><p>Ordernummer: 4500012345</p></body></html>"
LOGGED_IN = ("<html><head><title>Startsida Testverkstad AB</title></head><body>"
             "<a href='https://pro.meca.se/x/ViewUserAccount-LogoutUser'>Logga ut</a></body></html>")
LOGIN_FORM = ("<html><head><title>MECA</title></head><body><form><input id='ShopLoginForm_Login'>"
              "<input id='ShopLoginForm_Password'><button name='login'>Logga in</button></form></body></html>")
OVERVIEW_BEFORE = "<html><body><table><tr><td>4400000001</td></tr></table></body></html>"
OVERVIEW_AFTER = "<html><body><table><tr><td>4400000002</td></tr><tr><td>4400000001</td></tr></table></body></html>"


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def count(self):
        return self.page.count(self.selector)

    def click(self, **_):
        self.page.click(self.selector)


class FakePage:
    """Routes goto() to canned HTML; clicks on the known selectors move between pages."""

    def __init__(self, cart=CART, checkout=CHECKOUT, checkout_url=CHECKOUT_URL,
                 after_order=(CONFIRM_URL, CONFIRM), shipping_live=True,
                 overview=(OVERVIEW_BEFORE, OVERVIEW_AFTER), home=LOGGED_IN):
        self.cart, self.checkout, self.checkout_url = cart, checkout, checkout_url
        self.after_order, self.shipping_live = after_order, shipping_live
        self.overview, self.home = list(overview), home
        self.url, self.html = "about:blank", ""
        self.clicks, self.gotos, self.fills = [], [], []
        self.ordered = False

    def goto(self, url, **_):
        self.gotos.append(url)
        self.url = url
        if url == parts_portal.CART_URL:
            self.html = self.cart
        elif url == parts_portal.ORDER_OVERVIEW_URL:
            idx = 1 if self.ordered else 0
            if self.overview[idx] is None:
                raise TimeoutError("overview unavailable")
            self.html = self.overview[idx]
        elif url == parts_portal.START_URL:
            self.html = self.home
        else:
            raise AssertionError(f"unexpected goto {url}")

    def content(self):
        return self.html

    def wait_for_load_state(self, *_, **__):
        pass

    def wait_for_timeout(self, *_):
        pass

    def fill(self, selector, value):
        self.fills.append(selector)

    def locator(self, selector):
        return FakeLocator(self, selector)

    def count(self, selector):
        if selector == parts_portal.SEL_SHIPPING_CHECKED:
            return 1 if self.shipping_live else 0
        if selector == parts_portal.SEL_PLACE_ORDER:
            return int("checkout-page-place-order__button" in self.html)
        if selector == parts_portal.SEL_GO_ON:
            return int("Gå vidare" in self.html)
        if selector == parts_portal.SEL_LOGIN_USER:
            return int("ShopLoginForm_Login" in self.html)
        if selector == parts_portal.SEL_LOGOUT:
            return int("LogoutUser" in self.html)
        return 0

    def click(self, selector):
        self.clicks.append(selector)
        assert "Avbryt" not in selector and "cancel" not in selector.lower()
        if selector == parts_portal.SEL_GO_ON:
            self.url, self.html = self.checkout_url, self.checkout
        elif selector == parts_portal.SEL_PLACE_ORDER:
            self.ordered = True
            self.url, self.html = self.after_order
        elif selector == parts_portal.SEL_LOGIN_BUTTON:
            self.url, self.html = parts_portal.START_URL, self.home
        else:
            raise AssertionError(f"unexpected click {selector}")


def portal_with(page, reg="ABC123"):
    portal = PartsPortal({"parts_portal": {"username": "u", "password": "p"}, "portal": {}})
    portal._page = page
    portal._logged_in = True
    portal._reg = reg
    return portal


def assert_nothing_ordered(page):
    assert parts_portal.SEL_PLACE_ORDER not in page.clicks
    assert not page.ordered


# ── Happy path ───────────────────────────────────────────────────────────────

def test_checkout_clicks_final_button_after_all_checks():
    page = FakePage()
    ref = portal_with(page).checkout(max_total_sek=300)
    assert ref == "4500012345"
    assert page.clicks == [parts_portal.SEL_GO_ON, parts_portal.SEL_PLACE_ORDER]


def test_ceiling_is_required():
    page = FakePage()
    with pytest.raises(TypeError):
        portal_with(page).checkout()
    with pytest.raises(TypeError):
        portal_with(page).checkout(300)  # keyword only
    assert page.clicks == []


# ── Pre-checks: any failure means no final click ─────────────────────────────

def test_ceiling_uses_higher_of_cart_and_checkout_totals():
    page = FakePage()  # cart 248.75, checkout 171.64
    with pytest.raises(PartsPortalError, match="ceiling|tak"):
        portal_with(page).checkout(max_total_sek=200)
    assert_nothing_ordered(page)


def test_ceiling_exceeded_by_checkout_total():
    page = FakePage(checkout=CHECKOUT.replace(">171:64</span></strong>", ">999:00</span></strong>"))
    with pytest.raises(PartsPortalError):
        portal_with(page).checkout(max_total_sek=500)
    assert_nothing_ordered(page)


def test_not_on_checkout_page():
    page = FakePage(checkout_url="https://pro.meca.se/x/ViewCart-View")
    with pytest.raises(PartsPortalError):
        portal_with(page).checkout(max_total_sek=1000)
    assert_nothing_ordered(page)


def test_checkout_title_for_other_vehicle():
    page = FakePage(checkout=CHECKOUT.replace("<title>Beställning ABC123</title>",
                                              "<title>Beställning XYZ999</title>")
                    .replace("Beställning <span>ABC123</span>", "Beställning <span>XYZ999</span>"))
    with pytest.raises(PartsPortalError):
        portal_with(page).checkout(max_total_sek=1000)
    assert_nothing_ordered(page)


def test_no_shipping_method_selected_in_markup():
    page = FakePage(checkout=CHECKOUT.replace(' checked="checked"', ""))
    with pytest.raises(PartsPortalError):
        portal_with(page).checkout(max_total_sek=1000)
    assert_nothing_ordered(page)


def test_no_shipping_method_selected_live():
    page = FakePage(shipping_live=False)
    with pytest.raises(PartsPortalError):
        portal_with(page).checkout(max_total_sek=1000)
    assert_nothing_ordered(page)


def test_checkout_total_unreadable():
    page = FakePage(checkout=CHECKOUT.replace(">171:64</span></strong>", ">-</span></strong>"))
    with pytest.raises(PartsPortalError):
        portal_with(page).checkout(max_total_sek=1000)
    assert_nothing_ordered(page)


def test_cart_total_unreadable():
    cart = CART.replace("Totalt:<span>248:75</span>", "Totalt:<span>-</span>").replace(
        '<span class="js-total-tot-sum">248:75</span>', '<span class="js-total-tot-sum"></span>')
    page = FakePage(cart=cart)
    with pytest.raises(PartsPortalError):
        portal_with(page).checkout(max_total_sek=1000)
    assert page.clicks == []


def test_empty_cart_never_proceeds():
    page = FakePage(cart=EMPTY_CART)
    with pytest.raises(PartsPortalError):
        portal_with(page).checkout(max_total_sek=1000)
    assert page.clicks == []


def test_cart_for_other_vehicle_never_proceeds():
    page = FakePage(cart=CART.replace("kundvagn<span>ABC123</span>", "kundvagn<span>XYZ999</span>"))
    with pytest.raises(PartsPortalError):
        portal_with(page).checkout(max_total_sek=1000)
    assert page.clicks == []


def test_checkout_lines_differ_from_cart():
    page = FakePage(checkout=CHECKOUT.replace('value="1 st" id="qty"', 'value="2 st" id="qty"'))
    with pytest.raises(PartsPortalError):
        portal_with(page).checkout(max_total_sek=1000)
    assert_nothing_ordered(page)


def test_no_place_order_button():
    page = FakePage(checkout=CHECKOUT.replace("checkout-page-place-order__button checkout",
                                              "other checkout"))
    with pytest.raises(PartsPortalError):
        portal_with(page).checkout(max_total_sek=1000)
    assert_nothing_ordered(page)


def test_no_vehicle_loaded():
    page = FakePage()
    with pytest.raises(PartsPortalError):
        portal_with(page, reg=None).checkout(max_total_sek=1000)
    assert page.clicks == []


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -1, "500", True])
def test_bad_ceiling_rejected_before_anything(bad):
    page = FakePage()
    with pytest.raises((PartsPortalError, ValueError)):
        portal_with(page).checkout(max_total_sek=bad)
    assert page.clicks == []


# ── After the final click ────────────────────────────────────────────────────

def test_still_on_checkout_after_click_raises():
    page = FakePage(after_order=(CHECKOUT_URL, CHECKOUT))
    with pytest.raises(PartsPortalError, match="pro.meca.se|may have been placed"):
        portal_with(page).checkout(max_total_sek=1000)


def test_checkout_form_redisplayed_at_new_url_is_not_an_order():
    # A server-side validation failure re-renders the form at ViewCheckout-SubmitOrder.
    page = FakePage(after_order=(SUBMIT_URL, CHECKOUT))
    with pytest.raises(PartsPortalError, match="re-displayed after submit; nothing confirmed"):
        portal_with(page).checkout(max_total_sek=1000)


@pytest.mark.parametrize("html", [
    CART,                                                                    # cart article table
    "<html><head><title>Beställning ABC123</title></head><body>Tack</body></html>",
    "<html><body><h1>Beställning <span>ABC123</span></h1><p>Ordernummer 123456789</p></body></html>",
    "<html><body><button class='checkout-page-place-order__button'>Skicka beställning</button>"
    "<p>Ordernummer 123456789</p></body></html>",
], ids=["cart-table", "checkout-title", "checkout-h1", "place-order-button"])
def test_checkout_markers_after_click_never_count_as_confirmation(html):
    page = FakePage(after_order=(SUBMIT_URL, html))
    with pytest.raises(PartsPortalError, match="order may have been placed|nothing confirmed"):
        portal_with(page).checkout(max_total_sek=1000)


def test_reference_never_equals_the_vehicle_reg():
    page = FakePage(after_order=(CONFIRM_URL, "<html><body><h1>Tack!</h1>"
                                              "<p>Ert ordernummer ABC123</p></body></html>"),
                    overview=(OVERVIEW_BEFORE, OVERVIEW_BEFORE))
    ref = portal_with(page).checkout(max_total_sek=1000)
    assert ref.startswith("UNVERIFIED-") and "ABC123" not in ref


def test_error_page_after_click_may_have_ordered():
    page = FakePage(after_order=("https://pro.meca.se/x/ViewError", "<html><title>Fel</title>"
                                 "<body><h1>Ett fel har uppstått</h1></body></html>"))
    with pytest.raises(PartsPortalError, match="order may have been placed"):
        portal_with(page).checkout(max_total_sek=1000)


def test_unknown_confirmation_falls_back_to_order_overview():
    page = FakePage(after_order=(CONFIRM_URL, "<html><body><h1>Tack!</h1></body></html>"))
    assert portal_with(page).checkout(max_total_sek=1000) == "4400000002"


def test_unknown_confirmation_without_new_order_is_unverified():
    page = FakePage(after_order=(CONFIRM_URL, "<html><body><h1>Tack!</h1></body></html>"),
                    overview=(OVERVIEW_BEFORE, OVERVIEW_BEFORE))
    assert portal_with(page).checkout(max_total_sek=1000).startswith("UNVERIFIED-")


def test_overview_unavailable_is_unverified_not_crash():
    page = FakePage(after_order=(CONFIRM_URL, "<html><body><h1>Tack!</h1></body></html>"),
                    overview=(None, None))
    assert portal_with(page).checkout(max_total_sek=1000).startswith("UNVERIFIED-")


# ── Other guards ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("qty", [0, -1, True, 1.5, "2", None])
def test_add_to_cart_rejects_bad_qty(qty):
    portal = portal_with(FakePage())
    with pytest.raises(ValueError):
        portal.add_to_cart({"url_or_id": "10000002", "title": "x"}, qty)


def test_login_without_credentials_raises():
    portal = PartsPortal({"parts_portal": {"username": "", "password": ""}, "portal": {}})
    portal._page = FakePage()
    with pytest.raises(PartsPortalError):
        portal.login()


def test_login_is_idempotent():
    page = FakePage(home=LOGIN_FORM)
    portal = portal_with(page)
    portal._logged_in = False
    page.home = LOGIN_FORM

    def submit(selector):  # the login button leads to the logged-in start page
        page.clicks.append(selector)
        page.url, page.html = parts_portal.START_URL, LOGGED_IN
        page.home = LOGGED_IN
    page.click = submit
    portal.login()
    assert len(page.fills) == 2
    portal.login()
    assert len(page.fills) == 2  # already logged in: no second credential entry


def test_login_still_on_login_form_raises():
    page = FakePage(home=LOGIN_FORM)
    portal = portal_with(page)
    portal._logged_in = False

    def submit(selector):
        page.clicks.append(selector)
        page.url, page.html = parts_portal.START_URL, LOGIN_FORM
    page.click = submit
    with pytest.raises(PartsPortalError):
        portal.login()


# ── add_to_cart repeats the search that found the part (live bug UNKW-9240) ──

class AddEl:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def count(self):
        return self.page.count(self.selector)

    def get_attribute(self, name):
        return "tab-section__btn tab-section__btn--open"

    def locator(self, sub):
        return AddEl(self.page, f"{self.selector} {sub}")

    def _sku(self):
        return self.selector.split("data-sku='")[1].split("'")[0]

    def input_value(self):
        return f"{self.page.qty.get(self._sku(), 0)} st"

    def click(self, **_):
        assert "qty-wrapper__increase" in self.selector
        sku = self._sku()
        self.page.qty[sku] = self.page.qty.get(sku, 0) + 1


class _Response:
    ok = True


class _Expect:
    value = _Response()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AddPage:
    """A search-results page: `catalogue` maps a search term to the skus listed for it."""

    def __init__(self, catalogue, in_cart=None):
        self.catalogue = catalogue
        self.term = None
        self.searches = []
        self.qty = dict(in_cart or {})

    def fill(self, selector, value):
        assert selector == parts_portal.SEL_SEARCH
        self._typed = value

    def press(self, selector, key):
        self.term = self._typed
        self.searches.append(self.term)

    def wait_for_load_state(self, *_, **__):
        pass

    def wait_for_timeout(self, *_):
        pass

    def content(self):
        return f"<html>{self.term}</html>"

    def locator(self, selector):
        return AddEl(self, selector)

    def count(self, selector):
        return int(any(f"data-sku='{sku}'" in selector
                       for sku in self.catalogue.get(self.term, ())))

    def expect_response(self, *_, **__):
        return _Expect()


@pytest.fixture
def add_page(monkeypatch):
    def make(catalogue, in_cart=None):
        page = AddPage(catalogue, in_cart)
        monkeypatch.setattr(parts_portal, "current_plate", lambda html: "ABC123")
        monkeypatch.setattr(parts_portal, "linked_tab_id", lambda html: "t1")
        monkeypatch.setattr(parts_portal, "parse_search_results", lambda html: [
            {"title": f"Del {sku}", "url_or_id": sku} for sku in catalogue.get(page.term, ())])
        return page, portal_with(page)
    return make


def test_search_tags_every_candidate_with_its_term(add_page):
    page, portal = add_page({"Kupéfilter": ["10087157", "10087158"]})
    found = portal.search("  Kupéfilter ")
    assert [c["search_term"] for c in found] == ["Kupéfilter", "Kupéfilter"]


def test_add_to_cart_repeats_the_candidates_own_search(add_page):
    page, portal = add_page({"Kupéfilter": ["10087157"], "Tändstift": ["20000001"]})
    (kupe,) = portal.search("Kupéfilter")
    (spark,) = portal.search("Tändstift")

    portal.add_to_cart(kupe, 1)        # not on the Tändstift page: search Kupéfilter again
    portal.add_to_cart(spark, 2)       # not on the Kupéfilter page any more

    assert page.searches == ["Kupéfilter", "Tändstift", "Kupéfilter", "Tändstift"]
    assert page.qty == {"10087157": 1, "20000001": 2}


def test_add_to_cart_on_the_current_page_does_not_search(add_page):
    page, portal = add_page({"Oljefilter": ["p1"]})
    (cand,) = portal.search("Oljefilter")
    portal.add_to_cart(cand, 1)
    assert page.searches == ["Oljefilter"]


def test_add_to_cart_without_search_term_falls_back_to_the_last_search(add_page):
    page, portal = add_page({"Oljefilter": ["p1"], "Luftfilter": ["p2"]})
    portal.search("Oljefilter")
    page.term = "elsewhere"            # the page moved on (e.g. the cart was shown)

    portal.add_to_cart({"title": "Del p1", "url_or_id": "p1"}, 1)

    assert page.searches == ["Oljefilter", "Oljefilter"]
    assert page.qty == {"p1": 1}


def test_add_to_cart_unknown_sku_still_raises(add_page):
    page, portal = add_page({"Oljefilter": ["p1"]})
    (cand,) = portal.search("Oljefilter")
    page.term = "elsewhere"
    with pytest.raises(PartsPortalError, match="not found among linked parts"):
        portal.add_to_cart({**cand, "url_or_id": "p999"}, 1)
    assert page.qty == {}


def test_add_to_cart_part_already_in_cart_raises(add_page):
    page, portal = add_page({"Oljefilter": ["p1"]}, in_cart={"p1": 1})
    (cand,) = portal.search("Oljefilter")
    with pytest.raises(PartsPortalError, match="already in the cart"):
        portal.add_to_cart(cand, 1)
