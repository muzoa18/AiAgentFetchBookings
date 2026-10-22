"""Wave 4: no customer data in any log record (logs/agent.log is uploaded as a workflow
artifact), and no booking-page screenshots in the uploaded artifact."""
import logging
from pathlib import Path

import pytest
import yaml

import agent
import portal_scraper

REPO = Path(__file__).resolve().parent.parent
NAME, PHONE, EMAIL = "Sentinel Kund AB", "+46700000999", "sentinel@example.com"
MSG = "Sentinel meddelande 42 om bilen"
SENTINELS = (NAME, PHONE, EMAIL, MSG, "Sentinel")


def _assert_no_sentinels(caplog):
    text = "\n".join(r.getMessage() for r in caplog.records).casefold()
    assert caplog.records
    for sentinel in SENTINELS:
        assert sentinel.casefold() not in text, sentinel


# ── portal_scraper._extract_detail_page ──────────────────────────────────────

class El:
    def __init__(self, text="", href=None, nxt=""):
        self.text, self.href, self.nxt = text, href, nxt

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.href

    def is_visible(self):
        return True


class DetailPage:
    BODY = (f"Bokning UNKW-0001\nFörare {NAME}\nTelefon {PHONE}\nE-post {EMAIL}\n"
            f"Bokningsmeddelande {MSG}\nRegnr ABC123")

    def __init__(self):
        self.dts = [El("Bokningsnr", nxt="UNKW-0001"), El("Regnr", nxt="ABC123"),
                    El("Fordon", nxt="VOLVO V70"), El("Förare", nxt=NAME),
                    El("Bokningsmeddelande", nxt=MSG), El("Tid hos verkstad", nxt="2026-11-02 08:00")]

    def inner_text(self, selector):
        return self.BODY

    def query_selector_all(self, selector):
        if selector == "dt":
            return self.dts
        if selector == ".badge":
            return [El(f"Bokning – {NAME}")]
        return []

    def evaluate(self, script, el):
        return el.nxt

    def query_selector(self, selector):
        if selector.startswith("a[href^='tel:"):
            return El(href=f"tel:{PHONE}")
        if selector.startswith("a[href^='mailto:"):
            return El(href=f"mailto:{EMAIL}")
        return None


def test_detail_extraction_logs_no_customer_data(monkeypatch, caplog):
    monkeypatch.setattr(portal_scraper, "_extract_specification",
                        lambda page, stats=None: ([], [], [], "1 500 kr"))
    caplog.set_level(logging.DEBUG)

    booking = portal_scraper._extract_detail_page(DetailPage(), "B1")

    assert booking["driver_name"] == NAME and booking["driver_email"] == EMAIL  # still read
    _assert_no_sentinels(caplog)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for kept in ("UNKW-0001", "ABC123", "Bokning", "2026-11-02 08:00", "1 500 kr"):
        assert kept in text


@pytest.mark.parametrize("element_text", [f"Förfrågan från {NAME}", f"Offert till {NAME}",
                                          f"Bokning – {NAME}"])
def test_booking_type_detection_logs_no_element_text(caplog, element_text):
    class Page:
        def query_selector_all(self, selector):
            return [El(element_text)] if selector == ".badge" else []

    caplog.set_level(logging.DEBUG)
    portal_scraper._detect_booking_type(Page(), element_text)
    _assert_no_sentinels(caplog)


def test_no_booking_links_logs_only_the_page_text_length(caplog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class ListPage:
        def inner_text(self, selector):
            return f"Nya bokningar\n{NAME} {PHONE} {EMAIL}"

        def screenshot(self, path):
            pass

    caplog.set_level(logging.DEBUG)
    portal_scraper._report_no_booking_links(ListPage())

    _assert_no_sentinels(caplog)
    assert any("Page text length" in r.getMessage() for r in caplog.records)


def test_unused_hanterad_scanner_is_gone():
    assert not hasattr(portal_scraper, "_click_hanterad")


# ── exception text in the booking flow ───────────────────────────────────────

CALL_LOG = ("Timeout 15000ms exceeded.\nCall log:\n  - waiting for locator('button')\n"
            f"  -   locator resolved to <a href=\"tel:{PHONE}\">ring</a> "
            f"<a href=\"mailto:{EMAIL}\">mejla</a>")


def test_describe_masks_digits_and_addresses_and_is_short():
    import logsafe

    text = logsafe.describe(RuntimeError(CALL_LOG + "x" * 500))

    assert text.startswith("RuntimeError: ")
    assert PHONE not in text and EMAIL not in text and "700000999" not in text
    assert "@" not in text and not any(c.isdigit() for c in text)
    assert len(text) <= len("RuntimeError: ") + 200


def test_agent_booking_errors_log_no_phone_or_email(run_agent, monkeypatch, caplog):
    run, sent = run_agent

    def boom(config, url):
        raise RuntimeError(CALL_LOG)

    monkeypatch.setattr(agent, "mark_booking_handled", boom)
    caplog.set_level(logging.INFO)

    run(True)

    text = caplog.text
    assert "RuntimeError" in text
    assert PHONE not in text and EMAIL not in text


def test_scraper_hanterad_error_logs_no_phone_or_email(monkeypatch, caplog):
    class Browser:
        def new_page(self):
            raise RuntimeError(CALL_LOG)

        def close(self):
            pass

    class Chromium:
        def launch(self, **kw):
            return Browser()

    class PW:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(portal_scraper, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(portal_scraper, "sync_playwright", lambda: PW(), raising=False)
    caplog.set_level(logging.INFO)
    config = {"portal": {"username": "u", "password": "p"}}

    with pytest.raises(RuntimeError):
        portal_scraper.mark_booking_handled(config, "http://x/B1")  # new_page is outside the try
    # the try/except path: make new_page work and goto fail
    class Page:
        def goto(self, *a, **k):
            raise RuntimeError(CALL_LOG)

    Browser.new_page = lambda self: Page()
    assert portal_scraper.mark_booking_handled(config, "http://x/B1") is False
    assert "RuntimeError" in caplog.text
    assert PHONE not in caplog.text and EMAIL not in caplog.text


# ── agent.run ────────────────────────────────────────────────────────────────

@pytest.fixture
def run_agent(monkeypatch, tmp_path):
    import orders_state
    import state

    monkeypatch.setattr(orders_state, "ORDERS_FILE", tmp_path / "orders.json")
    from conftest import write_orders_skeleton
    write_orders_skeleton(tmp_path / "orders.json")
    monkeypatch.setattr(state, "NO_BUTTON_FILE", tmp_path / "nb.txt")
    monkeypatch.setattr(state, "NO_BUTTON_SEEN_FILE", tmp_path / "nbs.txt")
    booking = {"id": "B1", "booking_type": "Bokning", "booking_nr": "UNKW-0001",
               "driver_name": NAME, "driver_phone": PHONE, "driver_email": EMAIL,
               "booking_msg": MSG, "raw_text": f"{NAME} {PHONE} {EMAIL} {MSG}",
               "reg_nr": "ABC123", "vehicle": "VOLVO V70", "workshop_dt": "2026-11-02 08:00",
               "services": [], "parts": [], "fluids": [], "total_price": "1 500 kr",
               "detail_url": "http://x/B1"}
    sent = []
    monkeypatch.setattr(agent, "load_config", lambda: {"telegram": {"bot_token": "t",
                                                                    "chat_id": "42"}})
    monkeypatch.setattr(agent, "load_seen_ids", lambda: set())
    monkeypatch.setattr(agent, "save_seen_ids", lambda ids: None)
    monkeypatch.setattr(agent, "fetch_bookings", lambda config: [dict(booking)])
    monkeypatch.setattr(agent, "mark_booking_handled", lambda config, url: True)
    monkeypatch.setattr(agent, "log_run", lambda name, summary: None)

    def run(send_ok):
        monkeypatch.setattr(agent, "send_message",
                            lambda config, text: sent.append(text) or send_ok)
        return agent.run()

    return run, sent


@pytest.mark.parametrize("send_ok", [True, False])
def test_agent_run_logs_no_customer_data(run_agent, caplog, send_ok):
    run, sent = run_agent
    caplog.set_level(logging.DEBUG)

    run(send_ok)

    assert NAME in sent[0]                     # the owner still gets the full notification
    _assert_no_sentinels(caplog)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "UNKW-0001" in text or "B1" in text
    assert f"{len(sent[0])}" in text           # only the message length is logged


# ── bookings.yml artifact ────────────────────────────────────────────────────

def test_uploaded_artifact_holds_only_log_files():
    wf = yaml.safe_load((REPO / ".github" / "workflows" / "bookings.yml").read_text(encoding="utf-8"))
    (job,) = wf["jobs"].values()
    upload = next(s for s in job["steps"] if "upload-artifact" in str(s.get("uses", "")))
    paths = [p.strip() for p in upload["with"]["path"].splitlines() if p.strip()]
    assert paths == ["logs/*.log"]
