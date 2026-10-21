"""
parts_portal.py
---------------
Playwright adapter for the pro.meca.se parts shop (Intershop B2B).

    with PartsPortal(config) as portal:
        portal.login()
        portal.load_vehicle("ABC123")      # the cart is PER VEHICLE: load it first
        candidates = portal.search("oljefilter")
        portal.add_to_cart(candidates[0], 1)
        cart = portal.read_cart()
        ref = portal.checkout(max_total_sek=10000)   # THE ONLY METHOD THAT SPENDS MONEY

Selectors and behaviour were verified live (progress ledger, "Task 6 live recon").
Page parsing lives in parts_portal_parse.py (pure, unit-tested against fixtures).

checkout() runs every pre-check before the one final click ("Skicka beställning"); any failed
check raises PartsPortalError with nothing ordered. The confirmation page after that click
has NOT been seen yet; it is handled defensively (see checkout()).
"""

import logging
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import unquote

from parts_portal_parse import (PartsPortalError, PartsPortalPartialAdd, current_plate, extract_order_reference,
                                is_login_page, linked_tab_id, order_tokens, page_markers, parse_cart,
                                parse_checkout, parse_qty_text, parse_search_results, parse_sek,
                                same_lines)

__all__ = ["PartsPortal", "PartsPortalError", "PartsPortalPartialAdd", "parse_sek", "parse_search_results", "parse_cart",
           "parse_checkout", "extract_order_reference"]

log = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - the CI image has Playwright
    PLAYWRIGHT_AVAILABLE = False

START_URL = "https://pro.meca.se"
SHOP = "https://pro.meca.se/INTERSHOP/web/WFS/Mekonomen-MecaB2BSE-Site/sv_SE/-/SEK/"
CART_URL = SHOP + "ViewCart-View"
ORDER_OVERVIEW_URL = SHOP + "ViewOrderOverview-Start"

SEL_LOGIN_USER = "#ShopLoginForm_Login"
SEL_LOGIN_PASS = "#ShopLoginForm_Password"
SEL_LOGIN_BUTTON = "button[name='login']"
SEL_LOGOUT = "a[href*='LogoutUser']"
SEL_REG_INPUT = "input[name='search-value']"
SEL_SEARCH = "input[name='SearchTerm']"
SEL_REMOVE_LINE = "span.js-remove-product-item"
SEL_GO_ON = "form#bookingForm button.button--primary:has-text('Gå vidare')"
SEL_SHIPPING_CHECKED = "input[name='CheckoutForm_ShippingMethod']:checked"
SEL_PLACE_ORDER = "button.checkout-page-place-order__button:has-text('Skicka beställning')"
# Never clicked: "Avbryt beställning" (button.checkout-page-cancel-productOrder__button).

NAV_TIMEOUT = 45_000
IDLE_TIMEOUT = 30_000
QTY_TIMEOUT_S = 15
MAX_CLEAR_ATTEMPTS = 10


def _is_qty_update(response) -> bool:
    return "ViewCart-ChangeProductQuantity" in response.url and response.request.method == "POST"


def normalise_reg(reg_nr: str) -> str:
    return re.sub(r"[\s\-]", "", str(reg_nr or "")).upper()


class PartsPortal:
    """One pro.meca.se browser session. Use as a context manager."""

    def __init__(self, config: dict):
        self.config = config
        self._pw = self._browser = self._page = None
        self._logged_in = False
        self._reg = None            # vehicle currently loaded (the cart is keyed by it)
        self._last_search = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def __enter__(self):
        if not PLAYWRIGHT_AVAILABLE:
            raise PartsPortalError("Playwright is not installed")
        debug = bool((self.config.get("portal") or {}).get("debug", False))
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=not debug,
                                                     slow_mo=200 if debug else 0)
            self._page = self._browser.new_page(viewport={"width": 1400, "height": 900})
            self._page.set_default_timeout(NAV_TIMEOUT)
        except Exception:
            self._close()
            raise
        return self

    def __exit__(self, *exc):
        self._close()
        return False

    def _close(self):
        for closer in (lambda: self._browser and self._browser.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                log.exception("Closing the pro.meca.se browser failed.")
        self._pw = self._browser = self._page = None

    # ── Page helpers ─────────────────────────────────────────────────────────

    @property
    def page(self):
        if self._page is None:
            raise PartsPortalError("portal is not open (use it as a context manager)")
        return self._page

    def _settle(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT)
        except Exception:
            log.warning("pro.meca.se did not reach network idle; continuing with checks.")

    def _goto(self, url: str) -> str:
        self.page.goto(url, timeout=NAV_TIMEOUT)
        self._settle()
        html = self.page.content()
        if is_login_page(html):
            self._logged_in, self._reg = False, None
            raise PartsPortalError("pro.meca.se session expired (login form shown)", "login")
        return html

    def _count(self, selector: str) -> int:
        return self.page.locator(selector).count()

    def _require_vehicle(self) -> str:
        if not self._reg:
            raise PartsPortalError("no vehicle loaded (call load_vehicle first)", "vehicle_lost")
        return self._reg

    # ── Session ──────────────────────────────────────────────────────────────

    def login(self) -> None:
        """Log in. Idempotent: when already logged in, only re-checks the session."""
        creds = self.config.get("parts_portal") or {}
        user = str(creds.get("username") or "").strip()
        password = str(creds.get("password") or "")
        if not user or not password.strip():
            raise PartsPortalError("parts_portal username/password not configured")
        self.page.goto(START_URL, timeout=NAV_TIMEOUT)
        self._settle()
        if self._logged_in and self._count(SEL_LOGIN_USER) == 0 and self._count(SEL_LOGOUT) > 0:
            return
        self._logged_in, self._reg = False, None
        if self._count(SEL_LOGIN_USER) == 0 and self._count(SEL_LOGOUT) > 0:
            self._logged_in = True       # browser session already logged in
            return
        self.page.fill(SEL_LOGIN_USER, user)
        self.page.fill(SEL_LOGIN_PASS, password)
        self.page.locator(SEL_LOGIN_BUTTON).first.click()
        self._settle()
        if self._count(SEL_LOGIN_USER) > 0 or self._count(SEL_LOGOUT) == 0:
            raise PartsPortalError("login to pro.meca.se failed (still on the login form)", "login")
        self._logged_in = True
        log.info("Logged in to pro.meca.se.")

    def load_vehicle(self, reg_nr: str) -> None:
        reg = normalise_reg(reg_nr)
        if not re.fullmatch(r"[A-Z0-9]{2,8}", reg):
            raise PartsPortalError(f"invalid registration number {reg_nr!r}")
        if not self._logged_in:
            raise PartsPortalError("not logged in", "login")
        self._reg, self._last_search = None, None
        self.page.fill(SEL_REG_INPUT, reg)
        # Enter, not the submit button: that button is display:none once a vehicle is loaded.
        self.page.press(SEL_REG_INPUT, "Enter")
        self._settle()
        url = unquote(self.page.url)
        shown = current_plate(self.page.content())
        if "ViewCarOverview-Start" not in url or f"registrationNumber={reg}" not in url \
                or shown != reg:
            raise PartsPortalError(f"vehicle {reg} could not be loaded on pro.meca.se")
        self._reg = reg
        log.info("Vehicle %s loaded.", reg)

    # ── Search and cart ──────────────────────────────────────────────────────

    def _run_search(self, term: str) -> str:
        reg = self._require_vehicle()
        self.page.fill(SEL_SEARCH, term)
        self.page.press(SEL_SEARCH, "Enter")
        self._settle()
        html = self.page.content()
        if current_plate(html) != reg:
            raise PartsPortalError(f"vehicle {reg} is no longer loaded after searching",
                                   "vehicle_lost")
        self._last_search = term
        return html

    def search(self, term: str) -> list[dict]:
        """Linked ("Kopplade reservdelar") candidates only; [] if none.

        Each candidate carries 'search_term' (the term used), so add_to_cart can bring
        its own result page back after other searches."""
        term = str(term or "").strip()
        if not term:
            raise ValueError("empty search term")
        result = parse_search_results(self._run_search(term))
        for candidate in result:
            candidate["search_term"] = term
        log.info("Search %r: %d linked candidate(s).", term, len(result))
        return result

    def _quick_form(self, sku: str):
        tab = linked_tab_id(self.page.content())
        if not tab:
            return None, None
        tab_sel = f"div.tab-section__item[data-tab-id='{tab}']"
        form_sel = (f"{tab_sel} li.category-list-item[data-sku='{sku}'] "
                    f"fieldset.category-list-item__buy-area form.product-form-quick[data-sku='{sku}']")
        return tab_sel, form_sel

    def add_to_cart(self, candidate: dict, qty: int) -> None:
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise ValueError(f"qty must be an int >= 1, got {qty!r}")
        sku = str((candidate or {}).get("url_or_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", sku):
            raise ValueError(f"candidate has no usable url_or_id: {sku!r}")
        self._require_vehicle()
        tab_sel, form_sel = self._quick_form(sku)
        if not form_sel or self._count(form_sel) == 0:
            # Parts are matched before any is added, so the page usually shows the LAST
            # search: repeat the search that found this candidate (fallback: the last one).
            again = str(candidate.get("search_term") or "").strip() or self._last_search
            if not again:
                raise PartsPortalError(f"part {sku} is not on the current page and no search to repeat",
                                       "not_linked")
            self._run_search(again)
            tab_sel, form_sel = self._quick_form(sku)
            if not form_sel or self._count(form_sel) == 0:
                raise PartsPortalError(f"part {sku} not found among linked parts", "not_linked")
        trigger = self.page.locator(f"{tab_sel} > a.js-search-result-tab-trigger").first
        if "tab-section__btn--open" not in (trigger.get_attribute("class") or ""):
            trigger.click()
            self._settle()
        form = self.page.locator(form_sel).first
        qty_input = form.locator("input.js-change-quantity-quick")
        if parse_qty_text(qty_input.input_value()) != 0:
            raise PartsPortalError(f"part {sku} is already in the cart ({qty_input.input_value()!r})",
                                   "in_cart")
        clicks = 0  # "+" presses attempted: the cart may already hold that many
        try:
            for n in range(1, qty + 1):
                # The site debounces "+" (~0.5 s) and then POSTs ViewCart-ChangeProductQuantity;
                # the input changes client-side at once, so only that response proves the update.
                try:
                    with self.page.expect_response(_is_qty_update,
                                                   timeout=QTY_TIMEOUT_S * 1000) as update:
                        clicks = n  # safe side: a press that raised may still have been sent
                        form.locator("a.qty-wrapper__increase").click()
                    ok = update.value.ok
                except Exception as e:
                    raise PartsPortalPartialAdd(f"no cart update seen for {sku} (step {n}): {e}",
                                                steps_done=clicks) from e
                if not ok:
                    raise PartsPortalPartialAdd(f"cart update for {sku} failed (step {n})",
                                                steps_done=clicks)
                deadline = time.monotonic() + QTY_TIMEOUT_S
                while parse_qty_text(qty_input.input_value()) != n:
                    if time.monotonic() > deadline:
                        raise PartsPortalPartialAdd(f"quantity of {sku} did not reach {n}",
                                                    steps_done=clicks)
                    self.page.wait_for_timeout(250)
                self._settle()
            final = parse_qty_text(qty_input.input_value())
            if final != qty:
                raise PartsPortalPartialAdd(f"quantity of {sku} is {final!r}, expected {qty}",
                                            steps_done=clicks)
        except PartsPortalPartialAdd:
            raise
        except Exception as e:
            # Anything else after the first press (detached element, re-render timeout, ...)
            # may leave 1..clicks units in the cart.
            if clicks >= 1:
                raise PartsPortalPartialAdd(
                    f"part {sku} kan ha lagts till ({clicks} of {qty} step(s)): "
                    f"{type(e).__name__}: {e}", steps_done=clicks) from e
            raise
        log.info("Added %s x%d to the cart for %s.", sku, qty, self._reg)

    def _cart(self) -> dict:
        reg = self._require_vehicle()
        cart = parse_cart(self._goto(CART_URL))
        if cart.get("reg_nr") != reg:
            raise PartsPortalError(f"cart page is for {cart.get('reg_nr')!r}, expected {reg}")
        return cart

    def read_cart(self) -> dict:
        cart = self._cart()
        return {"lines": cart["lines"], "total_sek": cart["total_sek"]}

    def clear_cart(self, reg_nr: str | None = None) -> None:
        """Empty the cart of the loaded vehicle (or load `reg_nr` first)."""
        if reg_nr:
            self.load_vehicle(reg_nr)
        for _ in range(MAX_CLEAR_ATTEMPTS):
            if not self._cart()["lines"]:
                log.info("Cart for %s is empty.", self._reg)
                return
            self.page.locator(SEL_REMOVE_LINE).first.click()
            self.page.wait_for_timeout(1500)
            self._settle()
        if self._cart()["lines"]:
            raise PartsPortalError(f"cart for {self._reg} is not empty after "
                                   f"{MAX_CLEAR_ATTEMPTS} attempts")

    # ── Checkout (spends money) ──────────────────────────────────────────────

    def checkout(self, *, max_total_sek: float) -> str:
        """Place the order for the loaded vehicle's cart. Returns an order reference.

        Every check runs BEFORE the single final click; a failed check raises
        PartsPortalError and nothing is ordered. `max_total_sek` is required: the HIGHER of the
        cart and checkout totals must not exceed it.

        After the click, see _confirmation(): a page that still shows the checkout form or the
        cart raises PartsPortalError; only a page that clearly left checkout yields a
        reference (or "UNVERIFIED-<UTC>" when none can be found).
        """
        if isinstance(max_total_sek, bool) or not isinstance(max_total_sek, (int, float)) \
                or not math.isfinite(max_total_sek) or max_total_sek < 0:
            raise ValueError(f"invalid max_total_sek {max_total_sek!r}")
        reg = self._require_vehicle()
        before = self._overview_tokens()

        cart = self._cart()
        if not cart["lines"]:
            raise PartsPortalError("cart is empty: nothing to order")
        if cart["total_sek"] is None:
            raise PartsPortalError("cart total unreadable: not ordering")
        if self._count(SEL_GO_ON) != 1:
            raise PartsPortalError("'Gå vidare' not found on the cart page")
        self.page.locator(SEL_GO_ON).first.click()
        self._settle()

        checkout_url = self.page.url
        co = parse_checkout(self.page.content())
        problem = None
        if "ViewCheckout-Start" not in checkout_url:
            problem = "not on the checkout page"
        elif co["reg_nr"] != reg or not co["title"].startswith(f"Beställning {reg}"):
            problem = f"checkout page is for {co['reg_nr']!r}, expected {reg}"
        elif not co["has_place_order"] or self._count(SEL_PLACE_ORDER) != 1:
            problem = "order button not found exactly once"
        elif not co["shipping_selected"] or self._count(SEL_SHIPPING_CHECKED) < 1:
            problem = "no shipping method selected"
        elif co["total_sek"] is None:
            problem = "checkout total unreadable"
        elif not same_lines(cart["lines"], co["lines"]):
            problem = "checkout lines differ from the cart"
        else:
            highest = max(cart["total_sek"], co["total_sek"])
            if highest > max_total_sek:
                problem = (f"total {highest:.2f} SEK exceeds the ceiling "
                           f"{max_total_sek:.2f} SEK")
        if problem:
            raise PartsPortalError(f"{problem}: not ordering (nothing was ordered)")

        log.warning("Placing the pro.meca.se order for %s (cart %.2f, checkout %.2f SEK).",
                    reg, cart["total_sek"], co["total_sek"])
        self.page.locator(SEL_PLACE_ORDER).first.click()
        self._settle()
        return self._confirmation(checkout_url, before)

    def _overview_tokens(self) -> set[str] | None:
        try:
            return order_tokens(self._goto(ORDER_OVERVIEW_URL))
        except Exception:
            log.warning("Order overview unavailable before checkout; no fallback reference.")
            return None

    def _confirmation(self, checkout_url: str, before: set[str] | None) -> str:
        """Decide what the final click did, from the URL AND the page content.

        The form posts to ViewCheckout-SubmitOrder, so a re-rendered checkout (e.g. a server
        validation error) can sit at a new URL: content markers decide, not the URL alone.
        """
        url, html = self.page.url, self.page.content()
        marks = page_markers(html, self._reg)
        if marks["place_order"] and marks["checkout_title"]:
            raise PartsPortalError("checkout page re-displayed after submit; nothing confirmed "
                                   "(check pro.meca.se)")
        if any(marks.values()) or "ViewCheckout-Start" in url or url == checkout_url:
            raise PartsPortalError("order may have been placed: could not verify confirmation "
                                   "(the page still looks like checkout or the cart)")
        text = html.casefold()
        if is_login_page(html) or re.search(r"<title>\s*(fel|error)", text) \
                or "ett fel har uppstått" in text:
            raise PartsPortalError("order may have been placed: could not verify confirmation "
                                   "(error page after the order click)")
        ref = extract_order_reference(html, url, self._reg)
        if ref:
            log.info("Order placed for %s: %s", self._reg, ref)
            return ref
        if before is not None:
            try:
                new = order_tokens(self._goto(ORDER_OVERVIEW_URL)) - before
                if len(new) == 1:
                    ref = new.pop()
                    log.info("Order placed for %s: %s (from the order overview)", self._reg, ref)
                    return ref
            except Exception:
                log.exception("Order overview after checkout could not be read.")
        ref = "UNVERIFIED-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log.warning("Order page left checkout but no order number was found: %s", ref)
        return ref
