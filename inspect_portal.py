"""
inspect_portal.py
-----------------
Run this to discover the MECA portal structure after login.

It opens a VISIBLE browser, logs in using your config credentials,
then prints all navigation links from the dashboard so you can
identify the bookings page path.

Usage:
    python inspect_portal.py
"""

import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("ERROR: Playwright not installed. Run:  pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run:  pip install pyyaml")
    sys.exit(1)

CONFIG_PATH = Path("config/config.yaml")
if not CONFIG_PATH.exists():
    print(f"ERROR: {CONFIG_PATH} not found. Copy config.example.yaml and fill in your credentials.")
    sys.exit(1)

with open(CONFIG_PATH, encoding="utf-8") as f:
    config = yaml.safe_load(f)

username = config["portal"]["username"]
password = config["portal"]["password"]

print("\n" + "=" * 60)
print("  MECA Portal Inspector")
print("=" * 60)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=300)
    page = browser.new_page()

    # Step 1: Open login page
    print("\n-> Opening https://meca.promeisterportal.com/ ...")
    page.goto("https://meca.promeisterportal.com/", timeout=30_000)
    page.wait_for_load_state("networkidle")

    # Step 2: Log in with confirmed selectors
    print("\n-> Logging in ...")
    try:
        page.fill("#userName", username)
        print("  OK: Filled #userName")
    except Exception:
        print("  FAIL: Could not fill #userName")

    try:
        page.fill("#password", password)
        print("  OK: Filled #password")
    except Exception:
        print("  FAIL: Could not fill #password")

    try:
        page.click("button[type='submit']")
        print("  OK: Clicked submit")
    except Exception:
        page.keyboard.press("Enter")
        print("  OK: Pressed Enter (no button found)")

    # Step 3: Wait for dashboard
    print("\n-> Waiting for dashboard ...")
    try:
        page.wait_for_url(lambda url: url != "https://meca.promeisterportal.com/", timeout=15_000)
        page.wait_for_load_state("networkidle", timeout=15_000)
        print(f"  Logged in! URL: {page.url}")
    except PWTimeout:
        print(f"  Still on: {page.url}")
        print("  Login failed — check username/password in config.yaml")
        print("  Log in manually in the browser, then press Enter here.")
        input("  Press Enter after logging in -> ")
        page.wait_for_load_state("networkidle")

    # Step 4: Print all navigation links
    print("\n-- Navigation links on dashboard --")
    links = page.query_selector_all("a[href]")
    seen = set()
    nav_links = []
    for link in links:
        href = link.get_attribute("href") or ""
        text = (link.inner_text() or "").strip().replace("\n", " ")[:50]
        if href and href not in seen and not href.startswith("http"):
            seen.add(href)
            nav_links.append((text, href))
            print(f"  [{text}]  ->  {href}")

    if not nav_links:
        print("  (no internal links found)")

    # Step 5: Find booking candidates
    booking_candidates = [
        (t, h) for t, h in nav_links
        if any(kw in h.lower() for kw in ["booking", "bokning", "appointment", "order", "work", "service"])
    ]

    if booking_candidates:
        print("\n-- Likely booking pages --")
        for text, href in booking_candidates:
            print(f"  [{text}]  ->  {href}")

        first = booking_candidates[0][1]
        print(f"\n-> Opening: {first} ...")
        page.goto("https://meca.promeisterportal.com" + first)
        page.wait_for_load_state("networkidle")

        print(f"  URL: {page.url}")
        body_text = page.inner_text("body")
        print("\n-- Page text (first 2000 chars) --")
        print(body_text[:2000])

        rows = page.query_selector_all("table tr")
        print(f"\n-- Table rows: {len(rows)} found --")
        for i, row in enumerate(rows[:5]):
            print(f"  Row {i}: {row.inner_text().strip()[:120]}")

    print("\n" + "=" * 60)
    print("  NEXT STEPS:")
    if booking_candidates:
        path = booking_candidates[0][1]
        print(f"\n  Set this in config/config.yaml:")
        print(f"    portal:")
        print(f"      bookings_path: '{path}'")
    else:
        print("\n  No booking link found automatically.")
        print("  Navigate manually in the browser, copy the URL path,")
        print("  and set portal.bookings_path in config/config.yaml.")
    print("\n  Then run: python agent.py")
    print("=" * 60 + "\n")
    print("Browser stays open 60s. Ctrl+C to close early.")

    try:
        time.sleep(60)
    except KeyboardInterrupt:
        pass

    browser.close()