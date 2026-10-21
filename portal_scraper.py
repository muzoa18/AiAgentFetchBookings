"""
portal_scraper.py
-----------------
Workflow:
  1. Log in — portal lands directly on Booking/Index with Nya tab active.
     The portal's Authenticate JSON response decides first (Success false ->
     PortalLoginError with its reason); without it, a visible login form does.
     A login form on the booking list also raises; that check is definitive.
  2. Dismiss cookie popup if present
  3. Collect all BookingInfo links visible on the page
  4. Open each detail page, extract all info
  5. Optionally click Hanterad
"""

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    log.warning("Playwright not installed.")

PORTAL_BASE      = "https://meca.promeisterportal.com"
PORTAL_LOGIN_URL = PORTAL_BASE + "/"

LOGIN_SELECTORS = {
    "username": ["#userName", "input[name='UserName']", "input[type='email']"],
    "password": ["#password", "input[name='Password']", "input[type='password']"],
    # The login page has language buttons (DA/EN/NO/SV) that are ALSO type=submit and
    # come first in the DOM: never click a bare "button[type='submit']" here.
    "submit":   ["#btnLogin", "button:has-text('Login')", "button:has-text('Logga in')",
                 "input[type='submit']"],
}


LOGIN_ERROR_SELECTORS = [
    ".validation-summary-errors", ".field-validation-error", ".alert", ".error", "[role=alert]",
]
LOGIN_ERROR_WORDS = re.compile(
    r"\b(invalid|incorrect|wrong|locked|captcha|verify|fel|ogiltig|låst)", re.IGNORECASE
)


class PortalLoginError(Exception):
    """The MECA portal did not accept the login. Never retried: bad attempts can lock the account."""


def _visible(page, selector: str) -> bool:
    try:
        el = page.query_selector(selector)
        return bool(el and el.is_visible())
    except Exception:
        return False


# The LOGIN button is an SPA call that always answers HTTP 200 with JSON such as
# {"Success": false, "ErrorCode": "WrongUsernameOrPassword", ...}. The page shows
# nothing for a wrong password, so this response is the only source of the reason.
AUTH_PATH = "/api/v1/users/authenticate"
AUTH_WAIT_MS = 10_000
AUTH_REASONS = {
    "WrongUsernameOrPassword": "Fel användarnamn eller lösenord (portalen avvisade inloggningen)",
    "FastLoginAttempts": "För många inloggningsförsök i rad – vänta en stund och försök igen",
}


def _error_code(value) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value)[:40] if isinstance(value, str) else ""


def _auth_summary(body) -> dict | None:
    """Only the four non-secret fields; the rest of the body can carry tokens and is dropped."""
    if not isinstance(body, dict) or not isinstance(body.get("Success"), bool):
        return None
    return {
        "Success": body["Success"],
        "ErrorCode": _error_code(body.get("ErrorCode")),
        "PasswordExpired": body.get("PasswordExpired") is True,
        "TemporarilyDeactivated": body.get("TemporarilyDeactivated") is True,
    }


def _watch_auth(page) -> dict:
    """Register (before the login click) a listener keeping the Authenticate summary in ["result"]."""
    holder = {}

    def on_response(response):
        try:
            if AUTH_PATH not in (response.url or "").lower():
                return
            summary = _auth_summary(response.json())
        except Exception as e:
            log.warning("Could not read the portal's login response (%s).", type(e).__name__)
            return
        if summary is not None:
            holder["result"] = summary

    page.on("response", on_response)
    return holder


def _wait_for_auth(page, holder: dict) -> dict | None:
    """Let Playwright deliver the Authenticate response (events arrive while waiting)."""
    waited = 0
    while "result" not in holder and waited < AUTH_WAIT_MS:
        page.wait_for_timeout(250)
        waited += 250
    return holder.get("result")


def _auth_reason(result) -> str:
    """Short Swedish reason from the Authenticate summary; "" on success or when none was seen."""
    if not result or result.get("Success") is not False:
        return ""
    code = _error_code(result.get("ErrorCode"))
    if code in AUTH_REASONS:
        return AUTH_REASONS[code]
    if result.get("PasswordExpired") is True:
        return "Lösenordet har gått ut – byt lösenord i portalen"
    if result.get("TemporarilyDeactivated") is True:
        return "Kontot är tillfälligt avstängt"
    return f"Portalen avvisade inloggningen ({code})" if code else "Portalen avvisade inloggningen"


def _failure_reason(page, auth_result, username: str, password: str) -> str:
    return (_auth_reason(auth_result) or _login_error_text(page, username, password)
            or "okänd orsak")



def _logged_in(page) -> bool:
    """
    False while the login form is showing (a visible password input), else True.

    The URL is deliberately not used: the login URL is the site root, which is
    both where a rejected login lands and where a good login may stay. Right
    after login this is only a provisional check; the definitive one is made
    on the booking list, where a session the portal did not accept shows the
    login form again.
    """
    return not any(_visible(page, sel) for sel in LOGIN_SELECTORS["password"])


def _login_error_text(page, username: str = "", password: str = "") -> str:
    """Best-effort short error text shown on the login page; credentials redacted; "" if none."""
    found = ""
    for sel in LOGIN_ERROR_SELECTORS:
        try:
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    text = " ".join((el.inner_text() or "").split())
                    if text:
                        found = text
                        break
        except Exception:
            continue
        if found:
            break
    if not found:
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""
        for line in body.splitlines():
            line = " ".join(line.split())
            if line and LOGIN_ERROR_WORDS.search(line):
                found = line
                break
    for secret in (password, username):
        if secret:
            found = re.sub(re.escape(secret), "***", found, flags=re.IGNORECASE)
    return found[:200]


def _login_failed(page, auth_result, username: str, password: str, stage: str):
    """Screenshot, ERROR log (no credentials, no response body) and raise PortalLoginError."""
    _login_failed_screenshot(page)
    reason = _failure_reason(page, auth_result, username, password)
    log.error("MECA login not accepted (%s). URL: %s. Reason: %s", stage, page.url, reason)
    raise PortalLoginError(reason)


def _require_login(page, auth_result, username: str, password: str, stage: str) -> None:
    """Raise PortalLoginError unless the login form is gone."""
    if not _logged_in(page):
        _login_failed(page, auth_result, username, password, stage)


def _login_failed_screenshot(page) -> None:
    try:
        Path("logs").mkdir(exist_ok=True)
        page.screenshot(path="logs/login_failed.png")
    except Exception as e:
        log.warning("Could not save logs/login_failed.png: %s", e)


def fetch_bookings(config: dict) -> list[dict]:
    if not PLAYWRIGHT_AVAILABLE:
        log.error("Playwright not available.")
        return []

    username      = config["portal"]["username"]
    password      = config["portal"]["password"]
    debug         = config["portal"].get("debug", False)
    bookings_path = config["portal"].get("bookings_path", "/Booking/Index")
    booking_list_url = PORTAL_BASE.rstrip("/") + "/" + bookings_path.lstrip("/")

    bookings = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not debug, slow_mo=200 if debug else 0)
        page    = browser.new_page()

        try:
            # ── Step 1: Login ─────────────────────────────────────────────────
            log.info("Logging in ...")
            page.goto(PORTAL_LOGIN_URL, timeout=30_000)
            page.wait_for_load_state("networkidle")

            auth = _watch_auth(page)   # before the click, so the response is not missed
            if not _fill_login(page, username, password):
                log.error("Login form not found.")
                _login_failed_screenshot(page)
                raise PortalLoginError("login form not found")

            # Never retried: repeated bad attempts can lock the account.
            auth_result = _wait_for_auth(page, auth)
            if auth_result is not None and not auth_result["Success"]:
                _login_failed(page, auth_result, username, password, "authenticate")

            try:
                page.wait_for_url(
                    lambda url: "promeisterportal.com" in url and url != PORTAL_LOGIN_URL,
                    timeout=15_000
                )
            except PWTimeout:
                pass
            page.wait_for_load_state("networkidle", timeout=15_000)
            if auth_result is None:
                # No Authenticate response seen: provisional check on the login form.
                # The definitive check is on the booking list below.
                _require_login(page, None, username, password, "after login")
                log.info("Login form gone. URL: %s", page.url)
            else:
                log.info("Portal accepted the login. URL: %s", page.url)

            # ── Step 2: Go to booking list if not already there ───────────────
            if bookings_path.lstrip("/") not in page.url:
                log.info("Navigating to: %s", booking_list_url)
                page.goto(booking_list_url, timeout=20_000)
                page.wait_for_load_state("networkidle")
            else:
                log.info("Already on booking list.")
            # Definitive: the login form on the booking list means the session was not accepted.
            _require_login(page, auth_result, username, password, "booking list")
            log.info("Logged in (verified on booking list).")

            # ── Step 3: Dismiss cookie popup ──────────────────────────────────
            _dismiss_cookies(page)

            # ── Step 4: Wait for table ────────────────────────────────────────
            try:
                page.wait_for_selector("table", timeout=10_000)
                log.info("Booking table loaded.")
            except PWTimeout:
                log.warning("Table not found after 10s.")

            # ── Step 5: Ensure Nya tab is active ──────────────────────────────
            # The portal remembers the last tab, but we click Nya to be safe.
            # We use JavaScript to find and click it — bypasses any overlay issues.
            nya_result = page.evaluate("""
                () => {
                    const candidates = document.querySelectorAll('a, button, span, li, div');
                    for (const el of candidates) {
                        const text = (el.innerText || '').trim();
                        if (/^Nya\\b/.test(text) && text.length < 15) {
                            el.click();
                            return 'clicked: ' + el.tagName + ' "' + text + '"';
                        }
                    }
                    return 'not found';
                }
            """)
            log.info("Nya tab JS result: %s", nya_result)
            page.wait_for_timeout(2000)

            # ── Step 6: Collect all booking links ─────────────────────────────
            booking_links = _collect_booking_links(page)
            log.info("Booking links found: %d", len(booking_links))

            if not booking_links:
                log.warning("No booking links found on page.")
                page.screenshot(path="logs/no_bookings.png")
                log.info("Page text: %s", page.inner_text("body")[:500])
                return []

            # ── Step 7: Process each booking detail page ──────────────────────
            for link in booking_links:
                booking_id = link["id"]
                detail_url = link["url"]

                log.info("Opening booking %s ...", booking_id)
                try:
                    page.goto(detail_url, timeout=20_000)
                    page.wait_for_load_state("networkidle", timeout=15_000)
                    _dismiss_cookies(page)
                except PWTimeout:
                    log.error("Timeout opening %s — skipping.", detail_url)
                    continue

                booking = _extract_detail_page(page, booking_id)
                booking["detail_url"] = detail_url



                bookings.append(booking)

                # Return to list for next booking
                page.goto(booking_list_url, timeout=20_000)
                page.wait_for_load_state("networkidle")
                _dismiss_cookies(page)
                try:
                    page.wait_for_selector("table", timeout=8_000)
                except PWTimeout:
                    pass
                page.evaluate("""
                    () => {
                        const candidates = document.querySelectorAll('a, button, span, li, div');
                        for (const el of candidates) {
                            const text = (el.innerText || '').trim();
                            if (/^Nya\\b/.test(text) && text.length < 15) {
                                el.click(); return;
                            }
                        }
                    }
                """)
                page.wait_for_timeout(1500)

        except PortalLoginError:
            raise
        except PWTimeout:
            log.error("Timeout.")
        except Exception as exc:
            log.exception("Scraping error: %s", exc)
            try:
                Path("logs").mkdir(exist_ok=True)
                page.screenshot(path="logs/error_screenshot.png")
            except Exception as _e:
                log.warning("Suppressed exception: %s", _e)
        finally:
            browser.close()

    return bookings


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dismiss_cookies(page) -> None:
    for sel in [
        "button:has-text('Accept All Cookies')",
        "button:has-text('Acceptera alla')",
        "button:has-text('Acceptera')",
        "button:has-text('Accept')",
        "button:has-text('Godkänn')",
        "[class*='cookie'] button",
        "#cookie-accept",
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(800)
                log.info("Dismissed cookie popup: %s", sel)
                return
        except Exception:
            continue


def _fill_login(page, username: str, password: str) -> bool:
    filled_user = filled_pass = False
    for sel in LOGIN_SELECTORS["username"]:
        if page.query_selector(sel):
            page.fill(sel, username)
            filled_user = True
            break
    for sel in LOGIN_SELECTORS["password"]:
        if page.query_selector(sel):
            page.fill(sel, password)
            filled_pass = True
            break
    if not filled_user or not filled_pass:
        return False
    for sel in LOGIN_SELECTORS["submit"]:
        if page.query_selector(sel):
            page.click(sel)
            return True
    page.keyboard.press("Enter")
    return True


def _collect_booking_links(page) -> list[dict]:
    """Collect all BookingInfo links — deduplicated by BookingID."""
    links = []
    seen_ids = set()

    anchors = page.query_selector_all("a[href*='BookingInfo']")
    log.info("Raw BookingInfo anchors: %d", len(anchors))

    for a in anchors:
        href = (a.get_attribute("href") or "").strip()
        text = a.inner_text().strip().split("\n")[0].strip()

        # Extract the numeric BookingID to deduplicate
        # e.g. /Booking/BookingInfo?BookingID=1678702  and  ...#!2 are the same booking
        m = re.search(r'BookingID=(\d+)', href)
        if not m:
            continue
        numeric_id = m.group(1)
        if numeric_id in seen_ids:
            continue
        seen_ids.add(numeric_id)

        full_url = href if href.startswith("http") else PORTAL_BASE + href.split("#")[0]

        # Prefer the booking number text (e.g. OLRD-3630) over the numeric ID
        booking_id = text if text and not text[0].isdigit() else numeric_id

        log.info("  Booking: %s -> %s", booking_id, full_url)
        links.append({"id": booking_id, "url": full_url})

    return links


def _extract_detail_page(page, booking_id: str) -> dict:
    full_text = page.inner_text("body")

    def after_label(labels: list) -> str:
        for label in labels:
            for dt in page.query_selector_all("dt"):
                if label.lower() in (dt.inner_text() or "").lower():
                    val = page.evaluate(
                        "(el) => el.nextElementSibling ? el.nextElementSibling.innerText.trim() : ''", dt
                    )
                    if val:
                        return val.strip()
            for el in page.query_selector_all("strong, label, th, .col-form-label, [class*='label']"):
                if (el.inner_text() or "").strip().lower() == label.lower():
                    val = page.evaluate(
                        "(el) => el.nextElementSibling ? el.nextElementSibling.innerText.trim() : "
                        "el.parentElement ? el.parentElement.innerText.replace(el.innerText,'').trim() : ''",
                        el
                    )
                    if val:
                        return val.split("\n")[0].strip()
        return ""

    booking_nr  = after_label(["Bokningsnr", "Bokningsnr."]) or booking_id
    reg_nr      = after_label(["Regnr", "Regnr.", "Reg.nr"])
    vehicle     = after_label(["Fordon"])
    driver_name = after_label(["Förare", "Kund"])
    booked_at   = after_label(["Bokade", "Bokad"])
    workshop_dt = after_label(["Tid hos verkstad", "Verkstadstid"])
    mileage     = after_label(["Mätarställning", "Mätarst"])
    booking_msg = after_label(["Bokningsmeddelande", "Meddelande"])

    driver_phone = ""
    phone_el = page.query_selector("a[href^='tel:']")
    if phone_el:
        driver_phone = (phone_el.get_attribute("href") or "").replace("tel:", "").strip()
    if not driver_phone:
        matches = re.findall(r'\+46\d{7,11}|\b07\d{8}\b', full_text)
        driver_phone = matches[0] if matches else ""

    driver_email = ""
    email_el = page.query_selector("a[href^='mailto:']")
    if email_el:
        driver_email = (email_el.get_attribute("href") or "").replace("mailto:", "").strip()

    services, parts, fluids, total_price = _extract_specification(page)
    booking_type = _detect_booking_type(page, full_text)

    result = {
        "id": booking_id, "booking_nr": booking_nr, "booking_type": booking_type,
        "reg_nr": reg_nr, "vehicle": vehicle, "driver_name": driver_name,
        "driver_phone": driver_phone, "driver_email": driver_email,
        "booked_at": booked_at, "workshop_dt": workshop_dt, "mileage": mileage,
        "booking_msg": booking_msg, "services": services, "parts": parts,
        "fluids": fluids, "total_price": total_price, "raw_text": full_text[:4000],
    }

    log.info("  Type    : %s", booking_type)
    log.info("  Nr      : %s", booking_nr)
    log.info("  Customer: %s  %s  %s", driver_name, driver_phone, driver_email)
    log.info("  Vehicle : %s  %s", reg_nr, vehicle)
    log.info("  Date    : %s", workshop_dt)
    log.info("  Services: %d  Parts: %d  Total: %s", len(services), len(parts), total_price)
    return result


# Specifikation column headers (the portal repeats a header row per section: Tjänster,
# Delar, Vätskor / Oljor). Rows are read by column, never by position in the text: a fluid
# row has EMPTY OEM No / Artikel-ID cells, and service rows carry labour codes (F00093,
# 3546) in those columns that must never become part ids.
SPEC_COLUMNS = {"oem no": "oem_no", "artikel-id": "article_id", "antal": "qty"}
DEFAULT_SPEC_COLUMNS = {"qty": 3}  # no recognised header row: the historical Antal position


def _spec_columns(header_texts) -> dict:
    """{key: cell index} from one header row's texts; defaults when nothing is recognised."""
    columns = dict(DEFAULT_SPEC_COLUMNS)
    seen = set()
    for i, text in enumerate(header_texts):
        key = SPEC_COLUMNS.get(" ".join(str(text or "").split()).casefold())
        if key and key not in seen:
            columns[key] = i
            seen.add(key)
    return columns


def _extract_specification(page) -> tuple:
    """
    Extract only from the Specifikation section — stops before Handelselogg.

    The page has two tables:
      1. Specifikation  — services, parts, fluids  ← we want this
      2. Handelselogg   — event log with dates/names ← must ignore this

    Strategy: find the Specifikation heading, then only read rows
    inside that container. Stop at any heading containing 'logg' or 'händelse'.

    Each section's header row (th cells) names the columns; parts and fluids get
    `oem_no` and `article_id` from the "OEM No" / "Artikel-ID" columns ("" when absent).
    """
    services = []
    parts    = []
    fluids   = []
    total    = ""

    # Junk patterns to skip — row numbers, log entries, customer names in log
    SKIP_PATTERNS = [
        r"^\d+$",               # pure numbers (row indices 14, 15, 16...)
        r"^notifiering$",        # event log entries
        r"^ny bokning$",         # event log entries
        r"^händelse$",           # table headers from event log
        r"^datum$",
        r"^anteckningar$",
        r"^användare$",
        r"^email sent",
    ]

    try:
        # Find the Specifikation section container
        spec_container = page.query_selector(
            "section:has(h2:has-text('Specifikation')), "
            "div:has(h2:has-text('Specifikation')), "
            "div:has(h3:has-text('Specifikation')), "
            "*:has(> h2:has-text('Specifikation'))"
        )

        if spec_container:
            log.info("Found Specifikation container — reading only that section.")
            rows = spec_container.query_selector_all("table tr")
        else:
            # Fallback: read all tables but stop at Handelselogg
            log.info("Specifikation container not found — using all tables with logg filter.")
            rows = page.query_selector_all("table tr")

        category = "service"
        in_handelselogg = False
        columns = dict(DEFAULT_SPEC_COLUMNS)

        for row in rows:
            row_text  = row.inner_text().strip()
            row_lower = row_text.lower()

            # Stop parsing when we hit the event log section
            if "händelselogg" in row_lower or "handelselogg" in row_lower:
                log.info("Reached Händelselogg — stopping spec parsing.")
                break
            if in_handelselogg:
                continue

            cells = row.query_selector_all("td")

            # Category header rows (few or no cells, bold text)
            if not cells or len(cells) <= 2:
                if "delar" in row_lower:
                    category = "parts"
                elif "vätskor" in row_lower or "oljor" in row_lower:
                    category = "fluids"
                elif row_lower.startswith("service") and "kontroll" not in row_lower:
                    category = "service"
                headers = row.query_selector_all("th")
                if headers:
                    columns = _spec_columns([h.inner_text() for h in headers])
                continue

            # Skip header rows (th elements)
            if row.query_selector("th"):
                continue

            # Total row - check multiple patterns
            if any(pattern in row_lower for pattern in ["att betala", "totalt", "total", "summa"]):
                for cell in reversed(cells):
                    t = cell.inner_text().strip()
                    if re.search(r"\d", t) and "%" not in t and "moms" not in t.lower():
                        total = t
                        break
                continue

            # Skip moms/tax rows
            if "moms" in row_lower:
                continue

            name = cells[0].inner_text().strip()
            if not name or len(name) < 2:
                continue

            # Skip junk rows matching known bad patterns
            skip = False
            for pattern in SKIP_PATTERNS:
                if re.match(pattern, name.lower()):
                    skip = True
                    break
            if skip:
                continue

            # Skip if name looks like a date (from event log)
            if re.match(r"\d{4}-\d{2}-\d{2}", name):
                continue

            price = ""
            for cell in reversed(cells):
                t = cell.inner_text().strip()
                if "SEK" in t:
                    price = t
                    break

            def column(key: str) -> str:
                i = columns.get(key)
                return cells[i].inner_text().strip() if i is not None and i < len(cells) else ""

            item = {"name": name, "qty": column("qty"), "price": price}

            if category in ("parts", "fluids"):
                item["article_id"] = column("article_id")
                item["oem_no"] = column("oem_no")
            if category == "parts":
                parts.append(item)
            elif category == "fluids":
                fluids.append(item)
            else:
                services.append(item)

    except Exception as e:
        log.warning("Spec parse error: %s", e)

    return services, parts, fluids, total


def _detect_booking_type(page, full_text: str) -> str:
    text_lower = full_text.lower()
    for sel in [".badge", "[class*='status']", "h1", "h2"]:
        try:
            for el in page.query_selector_all(sel):
                t = (el.inner_text() or "").lower()
                if "förfrågan" in t: 
                    log.info("Detected type 'Förfrågan' from element text: %s", t[:50])
                    return "Förfrågan"
                if "offert" in t: 
                    log.info("Detected type 'Offert' from element text: %s", t[:50])
                    return "Offert"
                if "bokning" in t: 
                    log.info("Detected type 'Bokning' from element text: %s", t[:50])
                    return "Bokning"
        except Exception:
            log.warning("Silenced exception in %s", __name__)
    if "förfrågan" in text_lower: 
        log.info("Detected type 'Förfrågan' from full text")
        return "Förfrågan"
    if "offert" in text_lower: 
        log.info("Detected type 'Offert' from full text")
        return "Offert"
    log.info("Defaulting to type 'Bokning'")
    return "Bokning"





def _click_hanterad(page) -> bool:
    """
    Two-step Hanterad flow:
      1. Click the "Hanterad" button (top right, in Händelselogg section)
      2. Confirm with "Bekräfta" in the dialog that appears
    """
    Path("logs").mkdir(exist_ok=True)

    # ── Diagnostic: log ALL buttons and links on the page ────────────────────
    log.info("Scanning page for Hanterad button ...")
    all_buttons = page.query_selector_all("button, a, input[type='button'], input[type='submit']")
    log.info("Total clickable elements: %d", len(all_buttons))
    for el in all_buttons:
        try:
            text    = (el.inner_text() or el.get_attribute("value") or "").strip()
            visible = el.is_visible()
            tag     = page.evaluate("(el) => el.tagName", el)
            if text:
                log.info("  [%s] visible=%s text='%s'", tag, visible, text[:50])
        except Exception:
            log.warning("Silenced exception in %s", __name__)

    page.screenshot(path="logs/before_hanterad.png")
    log.info("Screenshot: logs/before_hanterad.png")

    # ── Step 1: Click Hanterad using JS to avoid selector issues ─────────────
    clicked = page.evaluate("""
        () => {
            const els = document.querySelectorAll('button, a, input');
            for (const el of els) {
                const text = (el.innerText || el.value || '').trim();
                // Match 'Hanterad' but NOT 'Hanterad 37' (the tab) — tab has a number
                if (text === 'Hanterad' || text === 'hanterad') {
                    el.click();
                    return 'clicked: ' + el.tagName + ' "' + text + '"';
                }
            }
            return 'not found';
        }
    """)
    log.info("Hanterad JS click result: %s", clicked)

    if "not found" in clicked:
        log.warning("Hanterad button not found — check logs/before_hanterad.png")
        return False

    # ── Step 2: Wait for and click Bekräfta ──────────────────────────────────
    page.wait_for_timeout(1500)
    page.screenshot(path="logs/after_hanterad_click.png")
    log.info("Screenshot: logs/after_hanterad_click.png")

    try:
        page.wait_for_selector("button:has-text('Bekräfta')", timeout=6_000)
    except PWTimeout:
        log.warning("Bekräfta dialog did not appear — check logs/after_hanterad_click.png")
        return False

    confirmed = page.evaluate("""
        () => {
            const els = document.querySelectorAll('button');
            for (const el of els) {
                const text = (el.innerText || '').trim();
                if (text === 'Bekräfta') {
                    el.click();
                    return 'clicked Bekräfta';
                }
            }
            return 'not found';
        }
    """)
    log.info("Bekräfta result: %s", confirmed)

    if "not found" in confirmed:
        log.warning("Bekräfta button not found.")
        return False

    page.wait_for_load_state("networkidle", timeout=8_000)
    log.info("Booking successfully marked as Hanterad.")
    return True


# Whitespace-tolerant exact words: the live button text is " Hanterad " (icon + spaces).
HANTERAD_TEXT = re.compile(r"^\s*Hanterad\s*$", re.IGNORECASE)
BEKRAFTA_TEXT = re.compile(r"^\s*Bekräfta\s*$", re.IGNORECASE)


def _visible_button_labels(page) -> list[str]:
    """Short, deduplicated labels of the visible buttons (diagnostics only)."""
    try:
        texts = page.locator("button:visible").all_inner_texts()
    except Exception:
        return []
    labels = []
    for text in texts:
        label = " ".join((text or "").split())[:30]
        if label and label not in labels:
            labels.append(label)
    return labels[:20]


def mark_booking_handled(config: dict, detail_url: str) -> bool | None:
    """
    Open a fresh browser session, navigate directly to the booking
    detail page, and click Hanterad → Bekräfta.

    Called from agent.py only after SMS has been confirmed sent.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return False

    username = config["portal"]["username"]
    password = config["portal"]["password"]
    debug    = config["portal"].get("debug", False)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not debug, slow_mo=200 if debug else 0)
        page    = browser.new_page()

        try:
            # Login
            page.goto(PORTAL_LOGIN_URL, timeout=30_000)
            page.wait_for_load_state("networkidle")
            auth = _watch_auth(page)   # before the click
            if not _fill_login(page, username, password):
                log.error("Hanterad step: login form not found — not marking.")
                return False
            auth_result = _wait_for_auth(page, auth)   # never retried
            if auth_result is not None and not auth_result["Success"]:
                log.error("Hanterad step: MECA login not accepted — not marking. Reason: %s",
                          _auth_reason(auth_result))
                return False
            try:
                page.wait_for_url(
                    lambda url: url != PORTAL_LOGIN_URL,
                    timeout=15_000
                )
            except PWTimeout:
                pass
            page.wait_for_load_state("networkidle", timeout=15_000)
            if auth_result is None and not _logged_in(page):
                log.error("Hanterad step: MECA login not accepted — not marking. Reason: %s",
                          _failure_reason(page, None, username, password))
                return False
            _dismiss_cookies(page)
            log.info("Logged in for Hanterad step (verified).")

            # Navigate directly to the booking detail page
            log.info("Opening booking detail: %s", detail_url)
            page.goto(detail_url, timeout=20_000)
            page.wait_for_load_state("networkidle", timeout=15_000)
            if not _logged_in(page):
                log.error("Hanterad step: detail page shows the login form — not marking.")
                return False
            _dismiss_cookies(page)

            # Wait for page JS to fully initialise before interacting
            page.wait_for_timeout(1000)

            # The action button is <button data-btntype="Status"> Hanterad </button>,
            # built asynchronously. Buttons only: the "Hanterad 37" tab is a link.
            any_btn = page.locator("button").filter(has_text=HANTERAD_TEXT)
            try:
                any_btn.first.wait_for(state="visible", timeout=10_000)
            except PWTimeout:
                # Normal for Förfrågan/Offert — no action button exists
                log.info("Hanterad button not present — booking is likely a Förfrågan or Offert. "
                         "Visible buttons: %s", _visible_button_labels(page))
                return None
            preferred = page.locator("button[data-btntype='Status']").filter(has_text=HANTERAD_TEXT)
            hanterad_btn = preferred if preferred.count() else any_btn

            # Use Playwright native click so pointer/mouse events fire correctly
            hanterad_btn.first.scroll_into_view_if_needed()
            hanterad_btn.first.click()
            log.info("Hanterad button clicked (native).")

            # Dialog "Vill du ändra status till Hanterad": click exactly "Bekräfta" —
            # never "Skicka bekräftelse" (mails the customer), "Avboka" or "Avbryt".
            confirm = page.locator("button:visible").filter(has_text=BEKRAFTA_TEXT)
            try:
                confirm.first.wait_for(state="visible", timeout=6_000)
            except PWTimeout:
                Path("logs").mkdir(exist_ok=True)
                page.screenshot(path="logs/bekrafta_not_found.png")
                log.warning("Bekräfta dialog did not appear. Visible buttons: %s. "
                            "Screenshot: logs/bekrafta_not_found.png", _visible_button_labels(page))
                return False
            n_confirm = confirm.count()
            if n_confirm != 1:
                log.warning("Expected exactly one Bekräfta button, found %d — not clicking. "
                            "Visible buttons: %s", n_confirm, _visible_button_labels(page))
                return False

            # Native click on Bekräfta
            confirm.first.click()
            log.info("Bekräfta clicked (native).")
            page.wait_for_load_state("networkidle", timeout=8_000)
            page.wait_for_timeout(1000)

            # Verification: success only when the Hanterad action button is gone.
            Path("logs").mkdir(exist_ok=True)
            page.screenshot(path="logs/hanterad_confirmed.png")
            remaining = page.locator("button:visible").filter(has_text=HANTERAD_TEXT)
            try:
                remaining.first.wait_for(state="hidden", timeout=8_000)
            except PWTimeout:
                log.warning("Hanterad button still visible after Bekräfta — status not changed. "
                            "Check logs/hanterad_confirmed.png")
                return False
            log.info("Hanterad confirmed — action button gone. Screenshot: logs/hanterad_confirmed.png")
            return True

        except Exception as e:
            log.exception("Error marking Hanterad: %s", e)
            return False
        finally:
            browser.close()
