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


def test_no_booking_links_logs_only_the_page_text_length():
    source = (REPO / "portal_scraper.py").read_text(encoding="utf-8")
    assert 'log.info("Page text: %s", page.inner_text("body")[:500])' not in source
    assert "Page text length" in source


# ── agent.run ────────────────────────────────────────────────────────────────

@pytest.fixture
def run_agent(monkeypatch, tmp_path):
    import orders_state
    import state

    monkeypatch.setattr(orders_state, "ORDERS_FILE", tmp_path / "orders.json")
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
