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


class FakeClock:
    """Injected into PartsPortal: sleep() only advances the fake time (tests stay fast)."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def portal_with(page, reg="ABC123"):
    portal = PartsPortal({"parts_portal": {"username": "u", "password": "p"}, "portal": {}})
    portal._page = page
    portal._logged_in = True
    portal._reg = reg
    portal.clock = FakeClock()
    portal._clock, portal._sleep = portal.clock.monotonic, portal.clock.sleep
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
        return "tab-section__btn" + (" tab-section__btn--open" if self.page.tab_open else "")

    def locator(self, sub):
        return AddEl(self.page, f"{self.selector} {sub}")

    def _sku(self):
        return self.selector.split("data-sku='")[1].split("'")[0]

    def input_value(self):
        self.page.input_reads += 1
        if self.page.input_reads == self.page.input_fails_on_read:
            raise RuntimeError("SECRET-PAGE-TEXT element is not attached to the DOM")
        return f"{self.page.qty.get(self._sku(), 0)} st"

    def click(self, **_):
        if self.selector.endswith("> a.js-search-result-tab-trigger"):
            self.page.tab_clicks += 1
            self.page.tab_open = True
            return
        assert "qty-wrapper__increase" in self.selector
        assert self.page.tab_open, "the linked tab must be open before adding"
        sku = self._sku()
        self.page.clicks_made += 1
        n = self.page.clicks_made
        if n == self.page.click_fails_at:
            self.page.clicks_made -= 1
            raise RuntimeError("SECRET-PAGE-TEXT click intercepted")
        self.page.last_ok = n != self.page.bad_response_at
        if self.page.last_ok and n != self.page.stuck_at:
            self.page.qty[sku] = self.page.qty.get(sku, 0) + 1


class _Response:
    def __init__(self, ok):
        self.ok = ok


class _Expect:
    def __init__(self, page):
        self.page = page

    @property
    def value(self):
        return _Response(self.page.last_ok)

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
        self.tab_open, self.tab_clicks = True, 0
        self.clicks_made, self.last_ok = 0, True
        self.click_fails_at = self.bad_response_at = self.stuck_at = None
        self.close_tab_on_search = False
        self.input_reads, self.input_fails_on_read = 0, None

    url = "https://pro.meca.se/x/ViewParametricSearch-SimpleOfferSearch"

    def fill(self, selector, value):
        assert selector in (parts_portal.SEL_SEARCH, parts_portal.SEL_REG_INPUT)
        self._typed = value

    def press(self, selector, key):
        if selector == parts_portal.SEL_REG_INPUT:   # wave 4: the vehicle is loaded again
            self.vehicle_loads = getattr(self, "vehicle_loads", 0) + 1
            self.url = ("https://pro.meca.se/x/ViewCarOverview-Start?registrationNumber="
                        + self._typed)
            return
        self.url = "https://pro.meca.se/x/ViewParametricSearch-SimpleOfferSearch"
        self.term = self._typed
        self.searches.append(self.term)
        if self.close_tab_on_search:
            self.tab_open = False

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
        return _Expect(self)


@pytest.fixture
def add_page(monkeypatch):
    def make(catalogue, in_cart=None):
        page = AddPage(catalogue, in_cart)
        monkeypatch.setattr(parts_portal, "current_plate", lambda html: "ABC123")
        monkeypatch.setattr(parts_portal, "linked_tab_id", lambda html: "t1")
        # the fake results page echoes the term it shows (like pro.meca.se's search field)
        monkeypatch.setattr(parts_portal, "search_echo", lambda html: page.term, raising=False)
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



def test_re_search_reopens_the_linked_tab(add_page):
    page, portal = add_page({"Kupéfilter": ["k1"], "Tändstift": ["t1"]})
    (kupe,) = portal.search("Kupéfilter")
    portal.search("Tändstift")
    page.close_tab_on_search = True          # after the re-run the linked tab is collapsed

    portal.add_to_cart(kupe, 1)

    assert page.searches[-1] == "Kupéfilter"
    assert page.tab_clicks == 1
    assert page.qty == {"k1": 1}


# ── Error codes; partial adds carry steps_done ───────────────────────────────

from parts_portal_parse import PartsPortalPartialAdd  # noqa: E402


def _err(portal, candidate, qty):
    with pytest.raises(PartsPortalError) as info:
        portal.add_to_cart(candidate, qty)
    return info.value


def test_not_linked_has_a_code(add_page):
    page, portal = add_page({"Oljefilter": ["p1"]})
    portal.search("Oljefilter")
    err = _err(portal, {"url_or_id": "p9", "search_term": "Oljefilter"}, 1)
    assert err.code == "not_linked" and not isinstance(err, PartsPortalPartialAdd)


def test_already_in_cart_has_a_code(add_page):
    page, portal = add_page({"Oljefilter": ["p1"]}, in_cart={"p1": 1})
    (cand,) = portal.search("Oljefilter")
    err = _err(portal, cand, 1)
    assert err.code == "in_cart" and not isinstance(err, PartsPortalPartialAdd)


def test_click_failing_at_step_2_of_2_counts_the_step_on_the_safe_side(add_page):
    page, portal = add_page({"Tändstift": ["t1"]})
    (cand,) = portal.search("Tändstift")
    page.click_fails_at = 2

    err = _err(portal, cand, 2)

    assert isinstance(err, PartsPortalPartialAdd)
    # The press may have been dispatched: the failing step counts (never under-report).
    assert (err.code, err.steps_done) == ("cart_update", 2)
    assert page.qty == {"t1": 1}


def test_bad_response_at_step_1_after_the_click_reports_one_step_done(add_page):
    page, portal = add_page({"Tändstift": ["t1"]})
    (cand,) = portal.search("Tändstift")
    page.bad_response_at = 1

    err = _err(portal, cand, 2)

    assert isinstance(err, PartsPortalPartialAdd)
    assert (err.code, err.steps_done) == ("cart_update", 1)


def test_first_click_failing_counts_one_step_on_the_safe_side(add_page):
    page, portal = add_page({"Tändstift": ["t1"]})
    (cand,) = portal.search("Tändstift")
    page.click_fails_at = 1

    err = _err(portal, cand, 2)

    assert isinstance(err, PartsPortalPartialAdd)
    assert err.code == "cart_update" and err.steps_done == 1


def test_quantity_not_reached_reports_the_steps_clicked(add_page, monkeypatch):
    monkeypatch.setattr(parts_portal, "QTY_TIMEOUT_S", 0)
    page, portal = add_page({"Tändstift": ["t1"]})
    (cand,) = portal.search("Tändstift")
    page.stuck_at = 2

    err = _err(portal, cand, 2)

    assert isinstance(err, PartsPortalPartialAdd)
    assert (err.code, err.steps_done) == ("cart_update", 2)


def test_vehicle_lost_while_re_searching_has_a_code(add_page, monkeypatch):
    page, portal = add_page({"Oljefilter": ["p1"], "Luftfilter": ["p2"]})
    (cand,) = portal.search("Oljefilter")
    portal.search("Luftfilter")
    monkeypatch.setattr(parts_portal, "current_plate", lambda html: "OTHER1")
    err = _err(portal, cand, 1)
    assert err.code == "vehicle_lost"
    assert page.vehicle_loads == 1          # wave 4: one re-load was tried first


def test_no_vehicle_loaded_has_the_vehicle_code():
    portal = portal_with(FakePage(), reg=None)
    assert _err(portal, {"url_or_id": "p1"}, 1).code == "vehicle_lost"


def test_login_failure_has_the_login_code():
    page = FakePage(home=LOGIN_FORM)
    portal = portal_with(page)
    portal._logged_in = False

    def submit(selector):
        page.clicks.append(selector)
        page.url, page.html = parts_portal.START_URL, LOGIN_FORM
    page.click = submit
    with pytest.raises(PartsPortalError) as info:
        portal.login()
    assert info.value.code == "login"


def test_plain_portal_error_has_no_code():
    assert PartsPortalError("x").code is None



# ── Re-review: ANY failure after the first click is a possibly partial add ──

def test_quantity_read_failing_after_click_1_of_2_is_a_partial_add(add_page):
    page, portal = add_page({"Tändstift": ["t1"]})
    (cand,) = portal.search("Tändstift")
    page.input_fails_on_read = 2      # read 1: the already-in-cart check; read 2: after click 1

    err = _err(portal, cand, 2)

    assert isinstance(err, PartsPortalPartialAdd)
    assert (err.code, err.steps_done) == ("cart_update", 1)
    assert "kan ha lagts till" in str(err)
    assert isinstance(err.__cause__, RuntimeError)
    assert page.qty == {"t1": 1}


def test_final_quantity_read_failing_is_a_partial_add_of_the_full_quantity(add_page):
    page, portal = add_page({"Tändstift": ["t1"]})
    (cand,) = portal.search("Tändstift")
    page.input_fails_on_read = 4      # check, after click 1, after click 2, final read

    err = _err(portal, cand, 2)

    assert isinstance(err, PartsPortalPartialAdd)
    assert err.steps_done == 2


def test_failure_before_any_click_stays_a_plain_error(add_page):
    page, portal = add_page({"Tändstift": ["t1"]})
    (cand,) = portal.search("Tändstift")
    page.input_fails_on_read = 1      # the already-in-cart check, before any click

    with pytest.raises(RuntimeError, match="not attached") as info:
        portal.add_to_cart(cand, 2)
    assert not isinstance(info.value, PartsPortalError)
    assert page.clicks_made == 0


def test_keyboard_interrupt_is_never_wrapped(add_page, monkeypatch):
    page, portal = add_page({"Tändstift": ["t1"]})
    (cand,) = portal.search("Tändstift")
    calls = {"n": 0}
    real = AddEl.input_value

    def interrupted(self):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(self)

    monkeypatch.setattr(AddEl, "input_value", interrupted)
    with pytest.raises(KeyboardInterrupt):
        portal.add_to_cart(cand, 2)


class _StagePortal:
    """The real PartsPortal.search/add_to_cart on the fake page, inside parts_order._stage."""

    def __init__(self, portal):
        self.portal, self.cleared = portal, []

    def login(self):
        pass

    def load_vehicle(self, reg_nr):
        pass

    def read_cart(self):
        return {"lines": [], "total_sek": 0.0}

    def search(self, term):
        return self.portal.search(term)

    def add_to_cart(self, candidate, qty):
        return self.portal.add_to_cart(candidate, qty)

    def clear_cart(self, reg_nr=None):
        self.cleared.append(reg_nr)
        raise AssertionError("the cart must never be cleared")


class _AnyTerm(dict):
    def get(self, key, default=None):
        return ["t1"]


def test_stage_lists_a_read_failure_after_a_click_as_maybe_in_the_cart(add_page, monkeypatch,
                                                                     tmp_path):
    import notifier
    import orders_state
    import parts_order
    from rules import Rules

    monkeypatch.setattr(orders_state, "ORDERS_FILE", tmp_path / "orders.json")
    sent = []
    monkeypatch.setattr(notifier, "send_message", lambda config, text: sent.append(text) or True)
    monkeypatch.setattr(notifier, "send_with_buttons",
                        lambda config, text, buttons: sent.append(text) or 901)
    monkeypatch.setattr(notifier, "clear_buttons", lambda config, mid: None)
    page, portal = add_page(_AnyTerm())
    monkeypatch.setattr(parts_portal, "parse_search_results", lambda html: [
        {"title": "Bosch Tändstift FR7", "brand": "Bosch", "price_sek": 50.0, "url_or_id": "t1"}])
    page.input_fails_on_read = 2
    stage_portal = _StagePortal(portal)
    booking = {"id": "B1", "booking_nr": "OLRD-1", "reg_nr": "ABC123",
               "parts": [{"name": "Tändstift", "qty": "2"}], "fluids": []}

    status = parts_order.plan_and_stage(
        {"telegram": {}}, booking, stage_portal,
        Rules(("Bosch",), ("ProMeister",), 10000, "approve"), lambda line, ranked: (0, 0.95))

    assert status == "blocked"
    assert stage_portal.cleared == []
    (text,) = sent
    assert ("Kan ligga delvis i varukorgen – kontrollera antalet: Tändstift "
            "(lade 1 av 2, varukorgen uppdaterades inte).") in text
    assert "Kunde inte läggas till" not in text
    assert "SECRET-PAGE-TEXT" not in text



# ── Wave 4 (+ review): wait for THIS search's results page; reload a lost vehicle once ─

from parts_portal_parse import search_echo  # noqa: E402

NO_LINKED = (FIXTURES / "meca_search_no_linked.html").read_text(encoding="utf-8")
ZERO_HITS = (FIXTURES / "meca_search_zero_hits.html").read_text(encoding="utf-8")
WITH_HITS = (FIXTURES / "meca_search.html").read_text(encoding="utf-8")
MID_LOAD = "<html><head><title>Sök</title></head><body><main>laddar…</main></body></html>"
OVERVIEW = ("<html><body><header><span class='js-current-license-plate-number'>ABC123</span>"
            "</header><main>Fordonsöversikt</main></body></html>")
OVERVIEW_URL = ("https://pro.meca.se/INTERSHOP/web/WFS/x/ViewCarOverview-Start"
                "?registrationNumber=ABC123")
RESULTS_URL = "https://pro.meca.se/INTERSHOP/web/WFS/x/ViewParametricSearch-SimpleOfferSearch"


def results_for(term, base=NO_LINKED):
    """A results page whose search field echoes `term` (as pro.meca.se does)."""
    return base.replace('value="oljefilter"', f'value="{term}"')


class Navigating(Exception):
    """Stands in for playwright's Error while the page is navigating."""


Navigating.__module__ = "playwright._impl._errors"
NAVIGATING = Navigating("Unable to retrieve content because the page is navigating "
                        "and changing the content.")


class TargetClosedError(Exception):
    pass


TargetClosedError.__module__ = "playwright._impl._errors"


class PollPage:
    """content() walks through the pages queued by the last Enter press (the last one sticks).

    `searches`: one list per search press; the string "RESULTS" stands for the results page of
    the typed term. An exception instance in a list is raised by content() at that read."""

    def __init__(self, searches, overviews=None, start=MID_LOAD):
        self.search_queue = [list(x) for x in searches]
        self.overview_queue = [list(x) for x in (overviews or [[OVERVIEW]])]
        self.pages = [start]
        self.url = RESULTS_URL
        self.searched, self.loads, self.idle_timeouts, self.reads = [], 0, [], 0

    def fill(self, selector, value):
        self._typed = value

    def press(self, selector, key):
        if selector == parts_portal.SEL_SEARCH:
            self.searched.append(self._typed)
            queued = self.search_queue.pop(0) if self.search_queue else [MID_LOAD]
            self.pages = [results_for(self._typed) if p == "RESULTS" else p for p in queued]
            self.url = RESULTS_URL
        else:
            assert selector == parts_portal.SEL_REG_INPUT
            self.loads += 1
            self.pages = self.overview_queue.pop(0) if self.overview_queue else [OVERVIEW]
            self.url = OVERVIEW_URL

    def content(self):
        self.reads += 1
        page = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        if isinstance(page, BaseException):
            raise page
        return page

    def wait_for_load_state(self, state, timeout=None):
        self.idle_timeouts.append(timeout)


def _budget_polls():
    return int(parts_portal.RESULTS_WAIT_S / parts_portal.POLL_S) + 2


def test_search_echo_reads_the_echoed_term_from_the_fixtures():
    assert search_echo(WITH_HITS) == "oljefilter"
    assert search_echo(NO_LINKED) == "oljefilter"
    assert search_echo(ZERO_HITS) == "pollenfilter"
    assert search_echo(OVERVIEW) is None and search_echo(MID_LOAD) is None


def test_ready_results_page_is_accepted_without_waiting_for_network_idle():
    page = PollPage([["RESULTS"]])
    portal = portal_with(page)

    assert portal.search("Oljepluggspackning") == []

    assert portal.clock.sleeps == []
    assert page.idle_timeouts == []          # readiness is checked before the idle hint
    assert page.loads == 0 and page.searched == ["Oljepluggspackning"]


def test_new_results_on_the_third_poll_are_accepted():
    page = PollPage([[MID_LOAD, MID_LOAD, "RESULTS"]])
    portal = portal_with(page)

    portal.search("Oljepluggspackning")

    assert portal.clock.sleeps == [parts_portal.POLL_S, parts_portal.POLL_S]
    assert all(t <= 10_000 for t in page.idle_timeouts)   # only a short hint
    assert page.loads == 0


def test_echo_matching_ignores_case_spaces_and_unicode_form():
    import unicodedata
    decomposed = unicodedata.normalize("NFD", "  KUPÉFILTER ")
    page = PollPage([[results_for(decomposed)]])
    portal = portal_with(page)

    portal.search("kupéfilter")

    assert portal.clock.sleeps == []


def test_previous_terms_results_are_never_accepted():
    previous = results_for("Oljefilter", WITH_HITS)      # plate shown, linked hits, old echo
    page = PollPage([[previous]], start=previous)
    portal = portal_with(page)

    with pytest.raises(PartsPortalError) as info:
        portal.search("Luftfilter")

    assert info.value.code == "search_timeout"
    assert page.loads == 0                    # the plate was there: no vehicle reload
    assert page.searched == ["Luftfilter"]
    assert page.reads <= _budget_polls()
    assert "Luftfilter" not in str(portal._last_search)


def test_search_never_returns_another_terms_candidates():
    previous = results_for("Oljefilter", WITH_HITS)
    page = PollPage([[previous, previous, results_for("Luftfilter", NO_LINKED)]], start=previous)
    portal = portal_with(page)

    found = portal.search("Luftfilter")

    assert found == []                        # NO_LINKED has no linked candidates
    assert len(portal.clock.sleeps) == 2


def test_same_term_again_is_accepted_immediately():
    page = PollPage([["RESULTS"], ["RESULTS"]])
    portal = portal_with(page)
    portal.search("Oljefilter")
    portal.search("Oljefilter")
    assert portal.clock.sleeps == []


def test_zero_hits_page_is_accepted_fast():
    page = PollPage([[ZERO_HITS]])
    portal = portal_with(page)

    assert portal.search("pollenfilter") == []
    assert portal.clock.sleeps == []


def test_vehicle_overview_is_never_taken_for_results():
    page = PollPage([[OVERVIEW]])
    portal = portal_with(page)
    page.url = OVERVIEW_URL

    with pytest.raises(PartsPortalError) as info:
        portal.search("Oljefilter")
    assert info.value.code == "search_timeout"


def test_results_url_is_required():
    class WrongUrl(PollPage):
        def press(self, selector, key):
            super().press(selector, key)
            self.url = "https://pro.meca.se/x/ViewOther-Start"

    page = WrongUrl([["RESULTS"]])
    with pytest.raises(PartsPortalError):
        portal_with(page).search("Oljefilter")


def test_missing_plate_reloads_the_vehicle_once_and_repeats_the_search():
    page = PollPage([[MID_LOAD], ["RESULTS"]])
    portal = portal_with(page)

    portal.search("Oljepluggspackning")

    assert page.loads == 1
    assert page.searched == ["Oljepluggspackning", "Oljepluggspackning"]
    assert portal._reg == "ABC123" and portal._last_search == "Oljepluggspackning"


def test_missing_plate_after_one_reload_is_vehicle_lost():
    page = PollPage([[MID_LOAD], [MID_LOAD], ["RESULTS"]])
    portal = portal_with(page)

    with pytest.raises(PartsPortalError) as info:
        portal.search("Oljepluggspackning")

    assert info.value.code == "vehicle_lost"
    assert page.loads == 1 and len(page.searched) == 2
    # worst case: two search budgets plus the reload (see the constants in parts_portal)
    assert sum(portal.clock.sleeps) <= 2 * parts_portal.RESULTS_WAIT_S + 4 * parts_portal.POLL_S


def test_login_page_while_polling_is_a_login_error():
    page = PollPage([[MID_LOAD, LOGIN_FORM]])
    portal = portal_with(page)

    with pytest.raises(PartsPortalError) as info:
        portal.search("Oljepluggspackning")

    assert info.value.code == "login"
    assert page.loads == 0


def test_navigation_errors_while_polling_mean_not_ready_yet():
    page = PollPage([[NAVIGATING, NAVIGATING, NAVIGATING, "RESULTS"]])
    portal = portal_with(page)

    portal.search("Oljefilter")

    assert len(portal.clock.sleeps) == 3 and page.loads == 0


def test_navigation_errors_for_the_whole_budget_are_a_search_timeout():
    page = PollPage([[NAVIGATING]], start=OVERVIEW)
    portal = portal_with(page)

    with pytest.raises(PartsPortalError) as info:
        portal.search("Oljefilter")
    # nothing readable at all: the plate cannot be confirmed either -> one reload
    assert info.value.code in ("vehicle_lost", "search_timeout")
    assert page.reads <= 3 * _budget_polls()


def test_target_closed_is_not_swallowed():
    page = PollPage([[TargetClosedError("Target page, context or browser has been closed")]])
    with pytest.raises(TargetClosedError):
        portal_with(page).search("Oljefilter")


def test_load_vehicle_waits_for_the_plate():
    page = PollPage([], overviews=[[MID_LOAD, NAVIGATING, OVERVIEW]])
    portal = portal_with(page, reg=None)

    portal.load_vehicle("ABC 123")

    assert portal._reg == "ABC123"
    assert len(portal.clock.sleeps) == 2


def test_unknown_plate_has_its_own_code():
    page = PollPage([], overviews=[[MID_LOAD]])
    portal = portal_with(page, reg=None)

    with pytest.raises(PartsPortalError, match="could not be loaded") as info:
        portal.load_vehicle("ABC123")
    assert info.value.code == "vehicle_unknown"
    assert portal._reg is None


def test_cart_page_is_polled_through_navigation_errors():
    page = FakePage()
    portal = portal_with(page)
    seq = [MID_LOAD, NAVIGATING, CART]

    def content():
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, BaseException):
            raise item
        return item

    page.content = content
    cart = portal.read_cart()

    assert cart["lines"] and len(portal.clock.sleeps) == 2


def test_add_to_cart_quick_form_read_tolerates_a_navigation_error(add_page):
    page, portal = add_page({"Oljefilter": ["p1"]})
    (cand,) = portal.search("Oljefilter")
    real = page.content
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise NAVIGATING
        return real()

    page.content = flaky
    portal.add_to_cart(cand, 1)
    assert page.qty == {"p1": 1}


# ── list_sent_orders (order checker, read-only) ──────────────────────────────

SENT = (FIXTURES / "meca_sent_orders.html").read_text(encoding="utf-8")
OPEN_CARTS = (SENT.replace("sent active js-orderhistory-view", "sent js-orderhistory-view")
              .replace("open  js-orderhistory-view", "open active js-orderhistory-view")
              .replace("view=sent", "view=open").replace("<td>90000001</td>", "<td>80000001</td>"))


class SentPage:
    """The order overview: goto() shows `start`; a click on the sent tab queues `after`
    (an exception instance is raised by content() at that read; the last page sticks)."""

    def __init__(self, start=OPEN_CARTS, after=(SENT,), tab_count=1, text_tab_count=1):
        self.start, self.pages, self.after = start, [start], list(after)
        self.tab_count, self.text_tab_count = tab_count, text_tab_count
        self.url, self.clicks, self.gotos, self.reads = "about:blank", [], [], 0

    def goto(self, url, **_):
        self.gotos.append(url)
        self.url = url
        if url == parts_portal.START_URL:
            self.pages = [LOGGED_IN]
        elif url == parts_portal.ORDER_OVERVIEW_URL:
            self.pages = [self.start]
        else:
            raise AssertionError(f"unexpected goto {url}")

    def content(self):
        self.reads += 1
        page = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        if isinstance(page, BaseException):
            raise page
        return page

    def wait_for_load_state(self, *_, **__):
        pass

    def wait_for_timeout(self, *_):
        pass

    def locator(self, selector):
        return SentLocator(self, selector)

    def click(self, what):
        self.clicks.append(what)
        self.pages = list(self.after)


class SentLocator:
    def __init__(self, page, selector, text=None):
        self.page, self.selector, self.text = page, selector, text

    @property
    def first(self):
        return self

    def filter(self, has_text=None, **_):
        return SentLocator(self.page, self.selector, has_text)

    def count(self):
        if self.selector == parts_portal.SEL_SENT_TAB:
            return self.page.tab_count
        if self.text is not None and self.text.search("Skickade Beställningar"):
            return self.page.text_tab_count
        if self.selector in (parts_portal.SEL_LOGIN_USER,):
            return 0
        if self.selector == parts_portal.SEL_LOGOUT:
            return 1
        return 0

    def click(self, **_):
        assert self.count() >= 1, f"click on a missing element {self.selector}"
        self.page.click(self.selector if self.text is None else ("text", self.text.pattern))


def sent_portal(page):
    portal = portal_with(page)
    portal._reg = None              # the vehicle need not be loaded
    return portal


def make_sent_page(**kw):
    return SentPage(**kw)


def test_list_sent_orders_clicks_only_the_sent_tab_and_parses_rows():
    page = make_sent_page()
    rows = sent_portal(page).list_sent_orders()
    assert [r["supplier_no"] for r in rows][:2] == ["90000001", "90000002"]
    assert rows[1]["model"] == "KIA, Rio Kombi-sedan (III), 12-16"
    assert page.clicks == [parts_portal.SEL_SENT_TAB]
    assert parts_portal.ORDER_OVERVIEW_URL in page.gotos
    assert all(g in (parts_portal.START_URL, parts_portal.ORDER_OVERVIEW_URL) for g in page.gotos)


def test_list_sent_orders_logs_in_first():
    page = make_sent_page()
    portal = sent_portal(page)
    portal._logged_in = False
    portal.list_sent_orders()
    assert page.gotos[0] == parts_portal.START_URL


def test_list_sent_orders_waits_for_the_sent_table_through_navigation():
    page = make_sent_page(after=(OPEN_CARTS, NAVIGATING, OPEN_CARTS, SENT))
    portal = sent_portal(page)
    rows = portal.list_sent_orders()
    assert len(rows) == 7 and page.clicks == [parts_portal.SEL_SENT_TAB]
    assert portal.clock.sleeps                  # it polled


def test_list_sent_orders_already_on_the_sent_tab_needs_no_click():
    page = make_sent_page(start=SENT)
    assert len(sent_portal(page).list_sent_orders()) == 7
    assert page.clicks == []


def test_list_sent_orders_times_out_when_the_table_never_switches():
    page = make_sent_page(after=(OPEN_CARTS,))
    portal = sent_portal(page)
    with pytest.raises(PartsPortalError) as e:
        portal.list_sent_orders()
    assert e.value.code == "search_timeout"
    assert portal.clock.now <= parts_portal.RESULTS_WAIT_S + 1
    assert page.clicks == [parts_portal.SEL_SENT_TAB]


def test_list_sent_orders_falls_back_to_the_tab_text():
    page = make_sent_page(tab_count=0)
    assert len(sent_portal(page).list_sent_orders()) == 7
    assert len(page.clicks) == 1 and page.clicks[0][0] == "text"


def test_list_sent_orders_without_the_tab_is_a_parse_error_and_clicks_nothing():
    page = make_sent_page(tab_count=0, text_tab_count=0)
    with pytest.raises(PartsPortalError) as e:
        sent_portal(page).list_sent_orders()
    assert e.value.code == "parse"
    assert page.clicks == []


def test_list_sent_orders_login_page_is_a_login_error():
    page = make_sent_page(after=(LOGIN_FORM,))
    with pytest.raises(PartsPortalError) as e:
        sent_portal(page).list_sent_orders()
    assert e.value.code == "login"


def test_list_sent_orders_overview_that_never_loads_is_a_timeout():
    page = make_sent_page(start="<html><body>laddar…</body></html>")
    with pytest.raises(PartsPortalError) as e:
        sent_portal(page).list_sent_orders()
    assert e.value.code == "search_timeout"
    assert page.clicks == []


def _empty_table(html):
    start, end = html.index("<tbody>"), html.index("</tbody>")
    return html[:start] + "<tbody>" + html[end:]


def test_list_sent_orders_does_not_take_a_still_empty_table_for_the_result():
    page = make_sent_page(after=(_empty_table(SENT), _empty_table(SENT), SENT))
    assert len(sent_portal(page).list_sent_orders()) == 7


def test_list_sent_orders_accepts_an_empty_sent_list_only_after_the_full_budget():
    page = make_sent_page(after=(_empty_table(SENT),))
    portal = sent_portal(page)
    assert portal.list_sent_orders() == []
    assert portal.clock.now >= parts_portal.RESULTS_WAIT_S


def test_list_sent_orders_already_on_an_empty_sent_tab_waits_the_budget_too():
    page = make_sent_page(start=_empty_table(SENT))
    portal = sent_portal(page)
    assert portal.list_sent_orders() == []
    assert page.clicks == [] and portal.clock.now >= parts_portal.RESULTS_WAIT_S

