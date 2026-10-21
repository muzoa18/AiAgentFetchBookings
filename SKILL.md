# MECA Booking Agent — Skill File

Context file read by `improve.py` on every weekly review, and the project's agent context.
Keep it in sync with the code; anything described here that does not exist in the repo will
mislead the improve agent.

## What the system does
MD Bilreparationer (a MECA workshop in Eskilstuna) receives digital bookings through the MECA
ProMeister portal (meca.promeisterportal.com). The system, end to end:

1. **Daily booking run** (`agent.py`): logs into the portal, reads every booking listed on the
   **Nya** tab, and sends the owner **one Telegram message** per booking with all the details.
2. A `Bokning` **without spare parts** is marked **Hanterad → Bekräfta** right away.
3. A `Bokning` **with spare parts** gets a parts order **staged** on pro.meca.se (the MECA parts
   shop): each part is matched in the vehicle's linked parts and put in the cart, and the owner
   gets a Telegram message with the parts, prices, cart total and fluids to prepare, plus
   **Approve** / **Cancel** buttons.
4. **Daily approval run** (`approve.py`): reads the owner's taps. Approve → re-check the cart and
   check out; Cancel → clear the cart. Every resolution message carries a **Klar** button; a Klar
   tap marks the booking Hanterad in the MECA portal.
5. **Weekly self-improve** (`improve.py`): Claude reviews code + logs and opens a Pull Request.

Everything runs on **GitHub Actions** (no local machine, no Task Scheduler). Every booking with
parts ends in a message saying ordered, blocked or failed: nothing fails silently.

## File map
```
agent.py                 Daily booking run: scrape → notify once → Hanterad (no parts) or stage order
approve.py               Daily poller: Approve / Cancel / Klar taps, 48 h expiry, Klar withdrawal
notifier.py              Telegram Bot API client (stdlib): messages, inline buttons, getUpdates
portal_scraper.py        MECA portal (Playwright): login, Nya tab, detail page, Hanterad → Bekräfta
parts_order.py           Plan, stage (cart), approval message, execute_order (checkout)
parts_order_helpers.py   Pure helpers: quantities, cart/total checks, Swedish message text
parts_matcher.py         Pure matching: brand rank, ProMeister/fluid/placement filters, Claude picker
parts_portal.py          pro.meca.se (Playwright): login, load vehicle, search, cart, checkout
parts_portal_parse.py    Pure HTML parsers for pro.meca.se pages (fixture-tested)
orders_state.py          config/orders.json: one status per booking, never order twice
rules.py                 Loads + validates parts_rules.yaml (fails closed)
parts_rules.yaml         Owner-edited rules: brand_prefer, brand_forbid, ceiling_sek, mode
config_loader.py         Env-first config (GitHub Secrets) with config/config.yaml fallback
state.py                 config/seen_ids.txt (notified once) + no-Hanterad-button bookkeeping
run_logger.py            Appends one structured line per run to logs/run_history.jsonl
improve.py               Weekly self-improve → PR (file allowlist + locked symbols enforced in code)
inspect_portal.py        Dev helper: inspect the portal's HTML when selectors break
config/config.example.yaml   Template; env var name for each field is in its comments
tests/                   Offline pytest suite (fixtures for both portals' HTML)
```
Not present any more: `sms_sender.py` (46elks SMS), `email_trigger.py` (Gmail gate),
`extractor.py`, `parts_agent.py`, `email_agent.py`, Windows Task Scheduler jobs. Do not
reference or recreate them.

## Booking loop rules (agent.py)
- **The portal is the truth for a `Bokning`.** Listed on the Nya tab = not handled. Local files
  never decide "handled"; `config/seen_ids.txt` only means "the owner was notified" (log text
  "Already notified").
- **Notify once.** Every run works on every listed `Bokning`, quietly: no new message unless the
  outcome changes. A failed Telegram send leaves the booking unseen (retried next run) and makes
  the run exit 1.
- **No spare parts** (no non-fluid part lines; fluids are only listed): message once, then
  Hanterad → Bekräfta immediately, retried every run until it succeeds. A failed Hanterad is
  reported and the run exits 1.
- **With spare parts:** Hanterad is NOT pressed by agent.py. The order flow runs (staging, then
  approval in approve.py); only the owner's **Klar** tap marks it Hanterad.
- A `blocked` order whose cart was never touched (e.g. "ingen säker träff") is re-staged
  automatically each run, at most 10 tries, with a message only when the outcome changes.
  Blocked-after-cart-touched, failed, cancelled, expired, pending, attempted and ordered bookings
  are never retried automatically.
- Specification not read completely (`spec_fallback` / `spec_dropped`): Hanterad is not pressed;
  the owner is told to check the booking.
- **Offert / Förfrågan** (no Hanterad button): local storage handles them — notified once, then
  only a PENDING line in the log while still listed. A booking whose type was only guessed and
  that shows no Hanterad button on two runs in a row gets one notice and is then handled like
  Offert/Förfrågan (`config/no_button_seen_once.txt` → `config/no_button_ids.txt`).
- Crash recovery: a booking is queued in `config/orders.json` right after its notification;
  any record still `attempted` at the start of the next run is reported and marked `failed`.

### Telegram message (booking notification)
```
Ny Bokning - OLRD-0001
Kund: Test Kund
Tel: +46700000001
Fordon: TST001 VOLKSWAGEN PASSAT
Datum: 2026-04-01 (3 Dagar)
---
Tjänster:
- Servicekontroll 150 000 KM
Delar:
- Oljefilter
Vätskor/Oljor:
- Motorolja x4
---
Totalt: 3 709 SEK
```
Optional lines (Tel, Email, Meddelande, Tjänster/Delar/Vätskor, Totalt) are omitted when empty.

## Approval and Klar (approve.py)
- An order is placed only if the tap comes from the owner (`telegram.chat_id`, a **private** chat,
  never a group), on that booking's own approval message (stored message id), the booking is
  still `pending`, and the approval is not older than **48 h** (checked at poll time).
- `execute_order` re-checks the live cart against the stored plan and the ceiling, records
  `attempted` before checkout, then places the order and reports the result.
- Cancel clears the vehicle's cart (best effort). An expired approval leaves the cart for the
  owner to clear.
- **Klar** is accepted with the same owner checks, only once the order is resolved (never while
  `pending` or `attempted`). A failed Hanterad keeps the button; a missing Hanterad button counts
  as already handled by hand. Klar buttons older than 14 days are withdrawn once with a notice.
- `python approve.py --check` (stdlib only) prints `has_pending=true|false`; the workflow skips
  installing Playwright when nothing is pending.

## Parts rules
- Only parts under **"Kopplade reservdelar"** (linked to the loaded vehicle) are candidates.
  Discontinued ("Produkten har utgått") and replaced ("Ersätts av") items are excluded.
- **Bosch preferred**, then another linked brand. **Never ProMeister** (hard-coded in
  `parts_matcher.py`, also in `brand_forbid`).
- **Never fluids or oils** (any kind). Fluid-looking lines are listed under "Förbered", never
  searched. Some real parts are over-blocked (e.g. "Fettnippel", "Oil pump"): order those by hand.
- **Placement:** when the booking line names an axle (fram/bak) or side (vä/hö), the candidate
  must match it; unknown placement is dropped. **Axle-specific parts** (belägg, bromsskiva,
  stötdämpare, …) need an axle in the booking line, otherwise the booking is blocked, not guessed.
- Candidate titles/brands with non-Latin letters are dropped; the Claude picker (`claude-sonnet-5-5`)
  must be confident (≥ 0.8) or nothing is ordered. Only the first 15 linked results are considered.
- **Ceiling 10,000 SEK** per booking order, compared against the higher of the cart total and the
  checkout-page total, at staging, at approval and inside `checkout()` before the final click.
  A missing or unreadable total means no order.
- The pro.meca.se cart is **per vehicle**; staging requires that vehicle's cart to be empty.
- `parts_rules.yaml` `mode: approve` (ask the owner first) is the current setting; `auto` would
  order directly under the same limits and is not to be switched on before the checks listed in
  the handoff doc are done.

## Parts vocabulary and lookup rules (owner-confirmed 2026-10-21)
- **Pollenfilter = Kupéfilter.** Searching "pollenfilter" on pro.meca.se returns NOTHING; "kupéfilter" returns the linked parts. Treat both words (and "kupefilter") as the same part and search "kupéfilter".
- **Artikel-ID.** On the booking page the "Delar" table has the columns name, OEM No, Artikel-ID, Antal, Beställda, Pris, Rabatt, Totalt (e.g. `Pollenfilter | 1025-PC40293 | 1025-PC40293 | 1`). The Artikel-ID is the pro.meca.se "Art. Nr" (searching it finds exactly that article, in the extra "Artikelnummer" tab and in "Kopplade reservdelar" when it fits the car). Fluids/oils ("Vätskor / Oljor") have no Artikel-ID.
- The Artikel-ID is **usually a ProMeister part**: ProMeister is never ordered, so use it as the REFERENCE (its title and "Produktinformation" such as filter type and dimensions) and find a similar linked part from another brand (Bosch preferred). It is not always ProMeister (the oil filter on the same booking was a Bosch `1025-F026407142`).
- Searches are only trusted from the "Kopplade reservdelar" tab (parts linked to the vehicle).

## Workflows and schedules (cron is UTC; Sweden is UTC+1 winter / UTC+2 summer)
| Workflow | Schedule | Runs |
|---|---|---|
| `.github/workflows/bookings.yml` | daily 15:00 UTC, Mon–Sat (`0 15 * * 1-6`) | `python agent.py`, commits `config/seen_ids.txt`, `config/orders.json`, `logs/run_history.jsonl`, `config/no_button_*.txt` |
| `.github/workflows/approve.yml` | daily 06:00 UTC, every day (`0 6 * * *`) | `python approve.py --check`, then `python approve.py` if anything is pending; commits `config/orders.json` |
| `.github/workflows/weekly-improve.yml` | Mondays 06:00 UTC | `python improve.py --apply-all`, opens a PR |

bookings.yml and approve.yml share the concurrency group `meca-bookings`, so they never overlap
(single writer of `orders.json`, one cart per vehicle). Run approve.yml by hand from the Actions
tab to process a tap immediately. Telegram keeps unread taps for 24 h only, so the poller runs
every day; tap within ~36 h of the staging message.

### Weekly self-improve
`python improve.py --apply-all` sends source + SKILL.md + recent logs to Claude
(`claude-opus-5-5`), applies the proposals in the working tree, runs `py_compile` on each changed
`.py`, reverts any file that does not compile, writes `improve_reports/latest.md`, and the
workflow opens a **Pull Request**. It never pushes to `main`; a human reviews and merges.

**What the improve agent may change** (enforced in code in `improve.py`, not only in the prompt;
every PR still needs the owner's review before merging):
- `portal_scraper.py`, `parts_portal.py`, `parts_portal_parse.py`, `state.py`, `run_logger.py`
  (`ALLOWED_FILES`, existing files only): selectors, parsing robustness, logging and error
  handling **outside the locked symbols**;
- NEW test files `tests/test_<lowercase_name>.py` (never an existing test, `conftest.py`,
  `__init__.py`, `sitecustomize.py` or `*.pth`).

**Locked symbols** (`PROTECTED_SYMBOLS`, compared with `ast` before anything is written; any
source change inside them, even a docstring or comment, or a new definition shadowing them, is
refused as "skipped (protected symbol: <name>)"; a change that does not parse is refused too):
`parts_portal.py` — `PartsPortal.checkout`, `add_to_cart`, `clear_cart`, `read_cart`, `_cart`,
`_confirmation`, `_overview_tokens`, `load_vehicle`, `_require_vehicle`, `SEL_PLACE_ORDER`,
`SEL_GO_ON` and any top-level symbol naming "Skicka beställning" / "Avbryt beställning" /
"Gå vidare"; `parts_portal_parse.py` — `parse_sek`, `parse_cart`, `parse_checkout`,
`_checkout_lines`, `parse_search_results`, `_candidate`, `EXCLUDE_TEXTS`, `linked_tab_id`,
`current_plate`, `extract_order_reference`, `page_markers` and their helpers;
`portal_scraper.py` — `mark_booking_handled`, `_click_hanterad`, `HANTERAD_TEXT`,
`BEKRAFTA_TEXT`, `_logged_in`, `_require_login`, `_auth_reason`, `_detect_booking_type`.

**Everything else is locked** and reported as "skipped (not on the allowlist)": `.git/`,
`.github/`, `.claude/`, `config/`, `parts_rules.yaml`, `rules.py`, `approve.py` (approval
authorisation), `notifier.py` (`get_updates` produces the chat/user ids approve.py trusts),
`parts_matcher.py`, `parts_order*.py` (ceiling, ProMeister, fluid, placement safety),
`orders_state.py`, `agent.py`, `config_loader.py`, `improve.py`, docs, existing tests, new
modules. Dot-components, `..`, absolute paths, backslashes and symlinks are always refused; the
path is checked again at write time. Applied changes to the three scraper/parser files are
flagged "safety-adjacent — review carefully" in the report. Read test diffs before running
pytest locally: the tests are LLM-written code. The workflow checks out with
`persist-credentials: false`.

## Secrets (GitHub Secrets; env var overrides config.yaml)
`PORTAL_USERNAME`, `PORTAL_PASSWORD` (MECA portal), `PARTS_PORTAL_USERNAME`,
`PARTS_PORTAL_PASSWORD` (pro.meca.se), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (the owner's
private chat id), `ANTHROPIC_API_KEY` (part picker + weekly improve). Optional env:
`PORTAL_BOOKINGS_PATH`, `PORTAL_DEBUG`. Secrets live only in GitHub Secrets or the gitignored
`config/config.yaml`. Required by `config_loader`: portal username/password and telegram
bot_token/chat_id; ordering additionally needs the parts portal login and the Anthropic key.

## Scraper selectors and gotchas
- Cookie popups on both portals must be dismissed before any click works.
- MECA portal: the Authenticate JSON response decides login success; a failed login is never
  retried (bad attempts can lock the account) and is reported to the owner.
- Booking links: `a[href*='BookingInfo']`, deduplicated by BookingID param.
- Hanterad: exact text match, then wait for the Bekräfta dialog; Bekräfta is also exact text.
- Specification scraping stops at **Händelselogg** so event-log rows are not parsed.
- pro.meca.se login: TWO email fields — use `#ShopLoginForm_Login` / `#ShopLoginForm_Password`.
- pro.meca.se checkout: cart → "Gå vidare" → "Beställning <REG>" → "Skicka beställning" (the only
  click that places an order). The confirmation page has not been seen yet; it is handled
  defensively (an order without a readable reference is reported as `UNVERIFIED-…`).

## Safety invariants (never break these)
- A booking is never ordered twice (`orders_state`; `attempted` is written before checkout and a
  crashed attempt is never re-ordered automatically).
- Nothing is ordered above the ceiling, from ProMeister, for fluids/oils, or outside "Kopplade
  reservdelar"; ambiguity means no order.
- Only the owner's private chat can approve, cancel or Klar, on the booking's own message.
- Every booking with parts ends in a message (ordered, blocked or failed); a message that cannot
  be delivered turns the run red.
- Owner messages are Swedish and never carry raw exception text; customer data is never written
  to `config/orders.json` (it is committed to git).
- No secrets in git. The improve agent only opens PRs, only writes allowlisted files outside the
  locked symbols, and every PR needs the owner's review.

## Known limitations
- The order confirmation page, quantity semantics ("x4" pieces vs sets), the real format of
  `data-placements`, and the 0-stock policy are unverified until the first supervised live order
  (see `docs/superpowers/plans/2026-10-21-parts-ordering-handoff.md`).
- The axle-specific part list is incomplete (bromsbackar, bromsslang, English lines); title-only
  matching is used when the portal gives no ids; brand preference is enforced within one search
  term only.
- An approve run killed mid-checkout is only reported by the next daily booking run's sweep.
- `approve.py` repeats the "invalid rules" warning every poll; a missing secret turns every poll red.
- Cancel clears the whole vehicle cart, including anything the owner added by hand.
- `logs/agent.log` (a 14-day workflow artifact) can contain full booking details.

## Logs monitored by improve.py
- `logs/run_history.jsonl`: structured per-run summary (the main signal, tracked in git).
- `logs/agent.log`: only exists inside a CI run or a local run (gitignored), so usually absent.
- `logs/improve_auto.log`: written by `improve.py --auto` (local only).

## Rules for proposed changes
- Do not add dependencies or touch workflows/secrets handling without a clear reason.
- Do not reference or recreate files listed under "Not present any more".
- Keep changes small and syntax-checkable; every changed `.py` must compile and the offline test
  suite (`python -m pytest`) must stay green.
