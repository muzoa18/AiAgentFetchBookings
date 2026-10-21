"""MECA portal login verification against FAKE pages: no browser, no network."""
from pathlib import Path

import pytest

import portal_scraper
from portal_scraper import PortalLoginError, _logged_in, _login_error_text

LOGIN_URL = portal_scraper.PORTAL_LOGIN_URL
PORTAL = portal_scraper.PORTAL_BASE
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


class FakeResponse:
    def __init__(self, url, body):
        self.url, self.body = url, body

    def json(self):
        if self.body is None:
            raise ValueError("not json")
        return self.body


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
        # JSON body the portal's Authenticate call answers with after a login click
        self.auth_body = None
        self.handlers = []
        self._pending = []

    def on(self, event, handler):
        assert event == "response"
        self.handlers.append(handler)

    def _dispatch(self):
        # Playwright delivers events while the sync API waits, not inside click().
        pending, self._pending = self._pending, []
        for resp in pending:
            for handler in self.handlers:
                handler(resp)

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
        self._dispatch()

    def wait_for_url(self, *_, **__):
        self.actions.append(("wait_for_url",))
        self._dispatch()

    def wait_for_timeout(self, *_):
        self._dispatch()

    def wait_for_selector(self, sel, **_):
        self.actions.append(("wait_for_selector", sel))

    def fill(self, sel, value):
        self.actions.append(("fill", sel))

    def click(self, sel):
        self.actions.append(("click", sel))
        if sel == "#btnLogin":
            self._pending.append(FakeResponse(PORTAL + "/api/V1/Users/Authenticate", self.auth_body)
                                 if self.auth_body is not None
                                 else FakeResponse(PORTAL + "/Content/site.css", None))
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
                  "#password": [FakeElement()], "#btnLogin": [FakeElement("LOGIN")]}
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


def test_login_url_without_password_field_is_logged_in():
    # The real landing page may stay on the bare site root with no known markers.
    assert _logged_in(FakePage(LOGIN_URL, {}, "Start")) is True
    assert _logged_in(FakePage(LOGIN_URL.rstrip("/") + "?ReturnUrl=%2F", {})) is True


def test_login_url_with_password_field_is_not_logged_in():
    assert _logged_in(FakePage(*LOGIN_PAGE)) is False


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
    # Stage 1 passes provisionally (root, no password field, no markers),
    # but the booking list redirects to the login form: the definitive failure.
    page = _login_page((LOGIN_URL, {}, "Start"))
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


# ── submit button (regression: language buttons are type=submit and come first) ──

def test_submit_selectors_never_use_the_bare_submit_button():
    # The real login page has DA/EN/NO/SV buttons that are type=submit BEFORE the
    # LOGIN button; a bare button[type='submit'] clicks the Danish language switch.
    assert "button[type='submit']" not in portal_scraper.LOGIN_SELECTORS["submit"]
    assert portal_scraper.LOGIN_SELECTORS["submit"][0] == "#btnLogin"


def test_fill_login_clicks_the_login_button_not_a_language_button():
    page = FakePage(elements={
        "#userName": [FakeElement()], "#password": [FakeElement()],
        "button[type='submit']": [FakeElement("DA"), FakeElement("EN"), FakeElement("LOGIN")],
        "#btnLogin": [FakeElement("LOGIN")],
    })
    assert portal_scraper._fill_login(page, "u", "p") is True
    clicks = [a[1] for a in page.actions if a[0] == "click"]
    assert clicks == ["#btnLogin"]


def test_fill_login_without_a_known_button_presses_enter_instead_of_a_language_button():
    page = FakePage(elements={
        "#userName": [FakeElement()], "#password": [FakeElement()],
        "button[type='submit']": [FakeElement("DA"), FakeElement("EN")],
    })
    assert portal_scraper._fill_login(page, "u", "p") is True
    assert [a for a in page.actions if a[0] == "click"] == []
    assert ("press", "Enter") in page.actions


# ── The portal's own Authenticate response ───────────────────────────────────

from portal_scraper import _auth_reason  # noqa: E402

WRONG = {"Success": False, "ErrorCode": "WrongUsernameOrPassword", "PasswordExpired": False,
         "TemporarilyDeactivated": False, "SomeToken": "SECRET-TOKEN"}


def _logins(page):
    return [a for a in page.actions if a == ("click", "#btnLogin")]


def test_auth_reason_mapping():
    assert _auth_reason(None) == ""
    assert _auth_reason({"Success": True, "ErrorCode": ""}) == ""
    assert _auth_reason({"Success": False, "ErrorCode": "WrongUsernameOrPassword"}) == \
        "Fel användarnamn eller lösenord (portalen avvisade inloggningen)"
    assert _auth_reason({"Success": False, "ErrorCode": "FastLoginAttempts"}) == \
        "För många inloggningsförsök i rad – vänta en stund och försök igen"
    assert _auth_reason({"Success": False, "ErrorCode": "", "PasswordExpired": True}) == \
        "Lösenordet har gått ut – byt lösenord i portalen"
    assert _auth_reason({"Success": False, "ErrorCode": "", "TemporarilyDeactivated": True}) == \
        "Kontot är tillfälligt avstängt"


def test_auth_reason_unknown_code_is_sanitised_and_truncated():
    reason = _auth_reason({"Success": False, "ErrorCode": "Some<b>Odd</b> Code!" + "X" * 60})
    code = reason[len("Portalen avvisade inloggningen ("):-1]
    assert reason.startswith("Portalen avvisade inloggningen (") and reason.endswith(")")
    assert code.isalnum() and len(code) == 40
    assert code.startswith("SomebOddbCode")


def test_wrong_password_response_raises_immediately_with_mapped_reason(fake_pw, caplog):
    caplog.set_level("INFO")
    page = _login_page((LOGIN_URL, PASSWORD_FIELD, LOGIN_BODY))
    page.auth_body = WRONG
    fake_pw(page)

    with pytest.raises(PortalLoginError) as exc:
        portal_scraper.fetch_bookings(CONFIG)

    assert "Fel användarnamn eller lösenord" in str(exc.value)
    assert "SECRET-TOKEN" not in str(exc.value) and "SECRET-TOKEN" not in caplog.text
    assert "S3cretPw!" not in caplog.text
    assert len(_logins(page)) == 1                       # never retried
    assert ("wait_for_url",) not in page.actions          # no 15 s URL wait
    assert ("goto", BOOKING_URL) not in page.actions
    assert ("evaluate",) not in page.actions
    assert page.screenshots == ["logs/login_failed.png"]


def test_fast_login_attempts_reason_reaches_the_error(fake_pw):
    page = _login_page((LOGIN_URL, PASSWORD_FIELD, LOGIN_BODY))
    page.auth_body = {"Success": False, "ErrorCode": "FastLoginAttempts"}
    fake_pw(page)

    with pytest.raises(PortalLoginError, match="För många inloggningsförsök"):
        portal_scraper.fetch_bookings(CONFIG)
    assert len(_logins(page)) == 1


def test_success_response_proceeds_to_booking_list(fake_pw):
    # Even if the form lingers after a good Authenticate, stage 1 trusts the portal;
    # the booking list (no form) is the definitive check.
    page = _login_page((LOGIN_URL, PASSWORD_FIELD, LOGIN_BODY))
    page.auth_body = {"Success": True, "ErrorCode": None}
    page.routes[BOOKING_URL] = (BOOKING_URL, {"table": [FakeElement()]}, "Nya 0")
    fake_pw(page)

    assert portal_scraper.fetch_bookings(CONFIG) == []
    assert ("goto", BOOKING_URL) in page.actions
    assert len(_logins(page)) == 1


def test_success_response_but_booking_list_shows_login_raises(fake_pw):
    page = _login_page((LOGIN_URL, {}, "Start"))
    page.auth_body = {"Success": True}
    page.routes[BOOKING_URL] = LOGIN_PAGE
    fake_pw(page)

    with pytest.raises(PortalLoginError, match="okänd orsak"):
        portal_scraper.fetch_bookings(CONFIG)


def test_no_auth_response_falls_back_to_page_text(fake_pw):
    page = _login_page((LOGIN_URL, PASSWORD_FIELD, LOGIN_BODY + "\nAccount locked"))
    fake_pw(page)   # auth_body None: only an unrelated response is seen

    with pytest.raises(PortalLoginError, match="Account locked"):
        portal_scraper.fetch_bookings(CONFIG)
    assert len(_logins(page)) == 1


def test_mark_booking_handled_false_on_rejected_auth_without_clicking(fake_pw, caplog):
    caplog.set_level("INFO")
    page = _login_page((LOGIN_URL, PASSWORD_FIELD, LOGIN_BODY))
    page.auth_body = WRONG
    fake_pw(page)

    assert portal_scraper.mark_booking_handled(CONFIG, "https://x/BookingInfo?BookingID=1") is False
    assert [a for a in page.actions if a[0] == "click"] == [("click", "#btnLogin")]
    assert not any(a[0] in ("locator", "goto") and "BookingInfo" in str(a) for a in page.actions)
    assert ("wait_for_url",) not in page.actions
    assert "Fel användarnamn eller lösenord" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


# ── Hanterad button on the booking detail page ───────────────────────────────
# Live markup: <button type="button" class="btn btn-fill btn-info icon-btn common-btn"
#   data-btntype="Status"> <i class="tim-icons "></i>Hanterad </button>
# Its text has whitespace around the word, so r"^Hanterad$" never matched.

import re  # noqa: E402

OLD_HANTERAD = re.compile(r"^Hanterad$", re.IGNORECASE)
DETAIL = "https://meca.promeisterportal.com/Booking/BookingInfo?BookingID=1678702"


class FakeButton:
    def __init__(self, raw, tag="button", attrs=None, appear_after=0, on_click=None):
        self.raw, self.tag, self.attrs = raw, tag, dict(attrs or {})
        self.appear_after, self.on_click = appear_after, on_click


class FakeLocator:
    """Applies has_text regexes to the RAW text, the way the live page exposed it."""

    def __init__(self, page, selector, regex=None):
        self.page, self.selector, self.regex = page, selector, regex

    def filter(self, has_text):
        return FakeLocator(self.page, self.selector, has_text)

    def _all(self):
        tags = {"button"} | ({"a"} if ", a" in self.selector else set())
        found = [b for b in self.page.buttons if b.tag in tags]
        if "[data-btntype='Status']" in self.selector:
            found = [b for b in found if b.attrs.get("data-btntype") == "Status"]
        if ":visible" in self.selector:
            found = [b for b in found if self.page.visible(b)]
        if self.regex is not None:
            found = [b for b in found if self.regex.search(b.raw)]
        return found

    def count(self):
        return len(self._all())

    @property
    def first(self):
        return self

    def all_inner_texts(self):
        return [" ".join(b.raw.split()) for b in self._all()]

    def wait_for(self, state="visible", timeout=30_000):
        self.page.waits.append((self.regex.pattern if self.regex else self.selector, state))
        for _ in range(int(timeout / 250) + 1):
            shown = [b for b in self._all() if self.page.visible(b)]
            if (state == "visible" and shown) or (state == "hidden" and not shown):
                return
            self.page.tick += 1
        raise portal_scraper.PWTimeout(f"wait_for {state} timed out")

    def scroll_into_view_if_needed(self):
        pass

    def click(self):
        shown = [b for b in self._all() if self.page.visible(b)]
        assert shown, "click on a missing element"
        button = shown[0]
        self.page.clicked.append(" ".join(button.raw.split()))
        if button.on_click:
            button.on_click(self.page)


class HanteradPage(FakePage):
    """Logs in (Success true), then the detail route shows self.detail_buttons."""

    def __init__(self, detail_buttons):
        super().__init__(*LOGIN_PAGE)
        self.routes[LOGIN_URL] = LOGIN_PAGE
        self.auth_body = {"Success": True}
        self.after_submit = (BOOKING_URL, {}, "Nya")
        self.detail_buttons = detail_buttons
        self.buttons, self.clicked, self.waits, self.tick = [], [], [], 0

    def goto(self, url, **kw):
        super().goto(url, **kw)
        if url == DETAIL:
            self._set((DETAIL, {}, "Bokning"))
            self.buttons = list(self.detail_buttons)

    def visible(self, button):
        return button in self.buttons and self.tick >= button.appear_after

    def locator(self, sel):
        self.actions.append(("locator", sel))
        return FakeLocator(self, sel)

    def screenshot(self, path):
        self.screenshots.append(path)


def _open_dialog(*labels, then_remove_hanterad=True):
    def on_hanterad(page):
        def on_confirm(p):
            if then_remove_hanterad:
                p.buttons = [b for b in p.buttons if not re.search(r"^\s*Hanterad\s*$", b.raw)]
            p.buttons = [b for b in p.buttons if b not in dialog]
        dialog = [FakeButton(f" {label} ", on_click=on_confirm if label == "Bekräfta" else None)
                  for label in labels]
        page.buttons = page.buttons + dialog
    return on_hanterad


def _detail_buttons(on_hanterad=None, appear_after=0):
    return [
        FakeButton(" Skicka bekräftelse "),
        FakeButton(" Anteckning "),
        FakeButton(" Avboka "),
        FakeButton("Hanterad 37", tag="a"),
        FakeButton(" \n Hanterad ", attrs={"data-btntype": "Status"},
                   appear_after=appear_after, on_click=on_hanterad or _open_dialog("Bekräfta", "Avbryt")),
    ]


def test_old_regex_misses_live_markup_new_one_matches():
    page = HanteradPage([])
    page.buttons = [FakeButton(" \n Hanterad ", attrs={"data-btntype": "Status"}),
                    FakeButton("Hanterad 37", tag="a")]
    assert page.locator("button, a").filter(has_text=OLD_HANTERAD).count() == 0
    assert page.locator("button, a").filter(has_text=portal_scraper.HANTERAD_TEXT).count() == 1
    assert page.locator("button").filter(has_text=portal_scraper.HANTERAD_TEXT).count() == 1


def test_only_real_hanterad_and_bekrafta_are_clicked(fake_pw):
    page = HanteradPage(_detail_buttons())
    fake_pw(page)

    assert portal_scraper.mark_booking_handled(CONFIG, DETAIL) is True
    assert page.clicked == ["Hanterad", "Bekräfta"]


def test_button_appearing_after_a_delay_is_waited_for(fake_pw):
    page = HanteradPage(_detail_buttons(appear_after=1))
    fake_pw(page)

    assert portal_scraper.mark_booking_handled(CONFIG, DETAIL) is True
    assert page.clicked == ["Hanterad", "Bekräfta"]


def test_button_never_appearing_returns_none_and_logs_labels(fake_pw, caplog):
    caplog.set_level("INFO")
    page = HanteradPage([b for b in _detail_buttons() if b.tag == "a" or "Hanterad" not in b.raw])
    fake_pw(page)

    assert portal_scraper.mark_booking_handled(CONFIG, DETAIL) is None
    assert page.clicked == []
    assert "Skicka bekräftelse" in caplog.text and "Avboka" in caplog.text


def test_two_bekrafta_buttons_return_false_without_clicking(fake_pw):
    page = HanteradPage(_detail_buttons(_open_dialog("Bekräfta", "Bekräfta", "Avbryt")))
    fake_pw(page)

    assert portal_scraper.mark_booking_handled(CONFIG, DETAIL) is False
    assert page.clicked == ["Hanterad"]


def test_dialog_without_bekrafta_returns_false(fake_pw):
    page = HanteradPage(_detail_buttons(_open_dialog("Avbryt")))
    fake_pw(page)

    assert portal_scraper.mark_booking_handled(CONFIG, DETAIL) is False
    assert page.clicked == ["Hanterad"]


def test_hanterad_still_present_after_confirm_returns_false(fake_pw, caplog):
    page = HanteradPage(_detail_buttons(_open_dialog("Bekräfta", "Avbryt",
                                                     then_remove_hanterad=False)))
    fake_pw(page)

    assert portal_scraper.mark_booking_handled(CONFIG, DETAIL) is False
    assert page.clicked == ["Hanterad", "Bekräfta"]
    assert "still visible" in caplog.text
