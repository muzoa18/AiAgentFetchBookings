"""MECA portal login verification against FAKE pages: no browser, no network."""
from pathlib import Path

import pytest

import portal_scraper
from portal_scraper import PortalLoginError, _logged_in, _login_error_text

LOGIN_URL = portal_scraper.PORTAL_LOGIN_URL
BOOKING_URL = portal_scraper.PORTAL_BASE + "/Booking/Index"
CONFIG = {"portal": {"username": "verkstad.user", "password": "S3cretPw!"}}


class FakeElement:
    def __init__(self, text="", visible=True):
        self.text, self.visible = text, visible

    def is_visible(self):
        return self.visible

    def inner_text(self):
        return self.text


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.actions.append(("press", key))


class FakePage:
    """A page is a URL plus {selector: [FakeElement, ...]} and a body text."""

    def __init__(self, url=LOGIN_URL, elements=None, body=""):
        self.url = url
        self.elements = dict(elements or {})
        self.body = body
        self.actions = []
        self.screenshots = []
        self.keyboard = FakeKeyboard(self)
        # what the page turns into after a login submit / a goto
        self.after_submit = None
        self.routes = {}

    def _set(self, state):
        self.url, self.elements, self.body = state

    def query_selector(self, sel):
        found = self.elements.get(sel) or []
        return found[0] if found else None

    def query_selector_all(self, sel):
        return list(self.elements.get(sel) or [])

    def inner_text(self, sel="body"):
        assert sel == "body"
        return self.body

    def goto(self, url, **_):
        self.actions.append(("goto", url))
        if url in self.routes:
            self._set(self.routes[url])

    def wait_for_load_state(self, *_, **__):
        pass

    def wait_for_url(self, *_, **__):
        pass

    def wait_for_timeout(self, *_):
        pass

    def wait_for_selector(self, sel, **_):
        self.actions.append(("wait_for_selector", sel))

    def fill(self, sel, value):
        self.actions.append(("fill", sel))

    def click(self, sel):
        self.actions.append(("click", sel))
        if self.after_submit is not None:
            self._set(self.after_submit)

    def screenshot(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"png")
        self.screenshots.append(path)

    def evaluate(self, *_):
        self.actions.append(("evaluate",))
        return "not found"

    def locator(self, sel):
        self.actions.append(("locator", sel))
        raise AssertionError("must not look for buttons on a login page")


PASSWORD_FIELD = {"input[type='password']": [FakeElement()], "#userName": [FakeElement()],
                  "#password": [FakeElement()], "button[type='submit']": [FakeElement("LOGIN")]}
LOGIN_BODY = "DA EN NO SV | Login | User name | Password | LOGIN | RESET PASSWORD | CONTACT SUPPORT"
LOGIN_PAGE = (LOGIN_URL, PASSWORD_FIELD, LOGIN_BODY)


# ── _logged_in ───────────────────────────────────────────────────────────────

def test_visible_password_field_means_not_logged_in():
    page = FakePage(BOOKING_URL, {"input[type='password']": [FakeElement()]})
    assert _logged_in(page) is False


def test_any_login_password_selector_counts():
    page = FakePage(BOOKING_URL, {"#password": [FakeElement()]})
    assert _logged_in(page) is False


def test_hidden_password_field_is_ignored():
    page = FakePage(BOOKING_URL, {"input[type='password']": [FakeElement(visible=False)]})
    assert _logged_in(page) is True


def test_no_password_field_off_login_url_is_logged_in():
    assert _logged_in(FakePage(BOOKING_URL, {})) is True


def test_login_url_without_password_field_but_booking_marker_is_logged_in():
    page = FakePage(LOGIN_URL, {"a[href*='Booking']": [FakeElement("Bokningar")]})
    assert _logged_in(page) is True


def test_login_url_without_password_field_or_marker_is_not_logged_in():
    # e.g. a captcha / verification step on the site root
    assert _logged_in(FakePage(LOGIN_URL, {})) is False
    assert _logged_in(FakePage(LOGIN_URL.rstrip("/") + "?ReturnUrl=%2F", {})) is False


# ── _login_error_text ────────────────────────────────────────────────────────

def test_error_text_from_validation_summary():
    page = FakePage(elements={".validation-summary-errors":
                              [FakeElement("  Invalid   user name\n or password.  ")]})
    assert _login_error_text(page) == "Invalid user name or password."


def test_error_text_from_keyword_in_body():
    page = FakePage(body=LOGIN_BODY + "\nFelaktigt användarnamn eller lösenord\nCONTACT")
    assert _login_error_text(page) == "Felaktigt användarnamn eller lösenord"


def test_error_text_ignores_invisible_containers():
    page = FakePage(elements={".alert": [FakeElement("Account locked", visible=False)]})
    assert _login_error_text(page) == ""


def test_error_text_redacts_username_and_password():
    page = FakePage(elements={"[role=alert]":
                              [FakeElement("Wrong password S3cretPw! for VERKSTAD.USER")]})
    text = _login_error_text(page, "verkstad.user", "S3cretPw!")
    assert "S3cretPw!" not in text
    assert "verkstad.user" not in text.lower()
    assert text == "Wrong password *** for ***"


def test_error_text_is_capped():
    page = FakePage(elements={".error": [FakeElement("invalid " * 100)]})
    assert len(_login_error_text(page)) <= 200


def test_error_text_empty_when_nothing_found():
    assert _login_error_text(FakePage(body=LOGIN_BODY)) == ""


# ── fetch_bookings / mark_booking_handled with a fake Playwright ─────────────

class FakeBrowser:
    def __init__(self, page):
        self.page, self.closed = page, False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self, page):
        self.browser = FakeBrowser(page)
        self.chromium = self

    def launch(self, **_):
        return self.browser

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_pw(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    holder = {}

    def install(page):
        holder["pw"] = FakePlaywright(page)
        monkeypatch.setattr(portal_scraper, "sync_playwright", lambda: holder["pw"])
        monkeypatch.setattr(portal_scraper, "PLAYWRIGHT_AVAILABLE", True)
        return holder["pw"]

    return install


def _login_page(after_submit):
    page = FakePage(*LOGIN_PAGE)
    page.routes[LOGIN_URL] = LOGIN_PAGE
    page.after_submit = after_submit
    return page


def test_fetch_bookings_raises_when_login_form_still_showing(fake_pw, tmp_path, caplog):
    caplog.set_level("INFO")
    rejected = (LOGIN_URL, PASSWORD_FIELD,
                LOGIN_BODY + "\nInvalid user name or password for verkstad.user")
    page = _login_page(rejected)
    pw = fake_pw(page)

    with pytest.raises(PortalLoginError) as exc:
        portal_scraper.fetch_bookings(CONFIG)

    assert "Invalid user name or password" in str(exc.value)
    assert "verkstad.user" not in str(exc.value)
    assert ("goto", BOOKING_URL) not in page.actions          # never reached the list
    assert ("evaluate",) not in page.actions                   # never scraped
    assert page.screenshots == ["logs/login_failed.png"]
    assert (tmp_path / "logs" / "login_failed.png").exists()
    assert pw.browser.closed
    assert "Logged in" not in caplog.text
    assert "S3cretPw!" not in caplog.text and "verkstad.user" not in caplog.text


def test_fetch_bookings_raises_when_booking_list_shows_login_again(fake_pw):
    # Login seems to work (password field gone), but the list redirects to the form.
    page = _login_page((LOGIN_URL, {"a[href*='Booking']": [FakeElement("Bokningar")]}, "Start"))
    page.routes[BOOKING_URL] = LOGIN_PAGE
    fake_pw(page)

    with pytest.raises(PortalLoginError):
        portal_scraper.fetch_bookings(CONFIG)

    assert ("goto", BOOKING_URL) in page.actions
    assert ("evaluate",) not in page.actions


def test_fetch_bookings_raises_when_login_form_missing(fake_pw):
    page = FakePage(LOGIN_URL, {}, "Maintenance")
    page.routes[LOGIN_URL] = (LOGIN_URL, {}, "Maintenance")
    fake_pw(page)

    with pytest.raises(PortalLoginError, match="login form not found"):
        portal_scraper.fetch_bookings(CONFIG)
    assert page.screenshots == ["logs/login_failed.png"]


def test_fetch_bookings_logged_in_with_empty_list_returns_empty(fake_pw, caplog):
    caplog.set_level("INFO")
    page = _login_page((BOOKING_URL, {"table": [FakeElement()]}, "Nya 0"))
    fake_pw(page)

    assert portal_scraper.fetch_bookings(CONFIG) == []
    assert "Logged in" in caplog.text


def test_mark_booking_handled_returns_false_on_failed_login(fake_pw, caplog):
    caplog.set_level("INFO")
    page = _login_page((LOGIN_URL, PASSWORD_FIELD, LOGIN_BODY))
    fake_pw(page)

    assert portal_scraper.mark_booking_handled(CONFIG, "https://x/BookingInfo?BookingID=1") is False
    assert ("goto", "https://x/BookingInfo?BookingID=1") not in page.actions
    assert not any(a[0] == "locator" for a in page.actions)
    assert "S3cretPw!" not in caplog.text


def test_mark_booking_handled_returns_false_when_detail_page_shows_login(fake_pw):
    detail = "https://x/BookingInfo?BookingID=1"
    page = _login_page((BOOKING_URL, {"table": [FakeElement()]}, "Nya"))
    page.routes[detail] = (LOGIN_URL, PASSWORD_FIELD, LOGIN_BODY)
    fake_pw(page)

    assert portal_scraper.mark_booking_handled(CONFIG, detail) is False
    assert ("goto", detail) in page.actions
    assert not any(a[0] == "locator" for a in page.actions)   # never looked for Hanterad
