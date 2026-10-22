---
name: meca-agent
description: Work on the MECA booking agent in this repo (AiAgentFetchBookings): a Playwright scraper of the MECA ProMeister portal that sends the owner one Telegram message per new booking, marks bookings without parts Hanterad, stages spare-parts orders on pro.meca.se for Telegram Approve/Cancel (approve.py, Klar button), and opens a weekly self-improve PR, all on GitHub Actions. Use this whenever the user mentions MECA, bookings, Hanterad, Klar, Nya tab, Telegram, parts ordering, pro.meca.se, parts_rules.yaml, the ceiling, seen_ids, orders.json, approve.py, the weekly improve PR, bookings.yml, approve.yml or weekly-improve.yml, or asks to debug, change, deploy or review anything in this project, even if they don't name the skill.
---

# MECA booking agent

Scrapes new bookings from meca.promeisterportal.com, sends the workshop owner one Telegram
message per booking, clicks Hanterad → Bekräfta for a `Bokning` without spare parts, and for a
`Bokning` with spare parts stages an order on pro.meca.se that the owner approves with a
Telegram tap. Hosted on GitHub Actions; a weekly job has Claude review the code and open a PR.
The full description lives in `SKILL.md` at the repo root (also read by `improve.py`) and
`README.md`. Read those before making non-trivial changes.

## Flow
- `agent.py` (daily 15:00 UTC Mon–Sat): `portal_scraper.fetch_bookings` → `build_message` →
  `notifier.send_message` (once per booking, `state.save_seen_ids`) → no parts:
  `portal_scraper.mark_booking_handled`; with parts: `parts_order.plan_and_stage` (cart +
  Godkänn/Avbryt message, plus a `Byt <part>` row per part with an alternative) →
  `run_logger.log_run`.
- `approve.py` (daily 06:00 UTC): `notifier.get_updates` → Approve: `parts_order.execute_order`
  (then `verify_now`: the placed order is looked up at once, by plate only);
  Cancel: clear cart; Byt (`swap:<id>:<index>`): `approve._swap` → `parts_order.restage_with_pins`
  (new approval message, never orders); Klar: `mark_booking_handled`; expire approvals older
  than 48 h.
- Both entry points run inside `preflight.guarded` and first call the state guard
  (`check_state_or_alert`): a missing/unreadable `config/orders.json` stops all ordering before
  portal contact (exit 1, alert `state_unreadable`). Exception: `approve.py --check` runs
  outside `guarded` (no secrets in that step). It sees a missing orders.json as empty
  (`has_pending=false`, no alert from approve) and exits 1 on a corrupt one (generic
  `workflow_failed_` alert only); the guard in approve runs only in the full run. Before any cart action `parts_order._stage`
  runs the duplicate-order guard (below).
- `watchdog.py` (every 6 h, `watchdog.yml`): alerts `stale_bookings` / `stale_approve` when a
  scheduled run has not succeeded; writes only `config/watchdog_alerts.json`.
- `improve.py --apply-all` (Mondays 06:00 UTC): proposals → PR; only `state.py`, `run_logger.py`
  and new `tests/test_*.py` files are ever written; everything else becomes a suggestion.

## Invariants (break these and bookings get missed, duplicated or wrongly ordered)
- **The portal is the truth for a `Bokning`.** Listed on Nya = not handled. `seen_ids` only means
  "notified"; it never decides "handled".
- **Notify once.** No new message unless the outcome changes.
- **A booking with parts is only marked Hanterad by the owner's Klar tap.**
- **Never order twice** (`config/orders.json`, `attempted` before checkout), never above the
  10,000 SEK ceiling, never ProMeister, never fluids/oils, only "Kopplade reservdelar", ambiguity
  means no order. Rules live in `parts_rules.yaml` (`mode: approve`).
- **A swap or an in-stock replacement never orders.** They only change what the owner is asked to
  approve (same ceiling, brand, fluid and cart checks). Swap: owner only, record `pending`, own
  message id, at most 2 per booking, 240 s budget, each swap refreshes the 48 h window. `auto`
  mode never replaces or offers alternatives.
- **Only the owner's private chat can approve** (`TELEGRAM_CHAT_ID`, never a group).
- **Alerts, never silence.** A crash (`crash_<name>`), a non-zero exit (`run_failed_<name>`), a
  failed workflow (`workflow_failed_<workflow name>`, `alerts_cli.py`), an unreadable order state
  (`state_unreadable`) and a missed schedule (`stale_bookings`, `stale_approve`) each raise a
  Telegram message (once per 24 h per key) and a dashboard banner; success resolves them
  (`guarded` and the "Mark OK" step, `alerts_cli.py workflow-ok`). One fault, one message: no
  `run_failed_` after `state_unreadable`/`crash_` in the same run, and "Alert on failure"
  (`if: failure() || cancelled()`) stays quiet when a specific alert of this workflow is active
  and was seen within 20 min. Main steps have their own `timeout-minutes` (13 / 8) below the
  job's (20 / 15) so a hang fails the step; a job timeout is a cancel, covered by `cancelled()`.
  `alerts.py` never raises. Alert texts never carry exception messages, only the class name.
- **Every file the workflows read must be committed.** The first GitHub run lost order state
  because `config/orders.json` was never committed; a missing file now stops ordering.
- **State is committed back to `main`** by the workflows (`seen_ids.txt`, `orders.json`,
  `no_button_*.txt`, `run_history.jsonl`, `alerts.json`; the watchdog commits
  `watchdog_alerts.json`, its own file). Those files show as modified locally after a pull;
  that is normal, don't "clean" them.
- **No secrets in git.** Config is env-first (`config_loader.py`); `config/config.yaml` is gitignored.
- **Improve agent opens PRs only** and writes only `state.py`, `run_logger.py` and new test
  files; scrapers, parsers and every other file are suggestion-only. Never make it push to `main`
  or widen the allowlist to Python logic files (selector fixes go to a validated data file
  later); every PR needs the owner's review.

## Working on it
- Tests: `python -m pytest` (offline, fixtures for both portals). Run it before every commit.
- Local run: `pip install -r requirements.txt && python -m playwright install chromium`, copy
  `config/config.example.yaml` to `config/config.yaml`, then `python agent.py` or
  `python approve.py`. Set `portal.debug: true` for a visible browser. This sends real Telegram
  messages, clicks the real Hanterad button and can fill a real cart / place a real order, so
  confirm with the user before running against the live portals with real credentials.
- Selector breakage is the usual failure. Use `inspect_portal.py` to look at the live HTML, and prefer
  exact-text matching, which is what the scrapers already do (see "Scraper selectors" in `SKILL.md`).
- Alerts: `alerts.py` (store, 24 h de-dup), `preflight.py` (state guard, `guarded`),
  `alerts_cli.py` (workflow-level: `workflow-failed`, `workflow-ok`), `watchdog.py`. New entry points should run inside
  `preflight.guarded`. The watchdog cannot see GitHub disabling scheduled workflows after 60 days
  of repo inactivity. To get past a duplicate-order block, order by hand in pro.meca.se.
- Cron times in the workflows are UTC; Sweden is UTC+1/+2.
- After changing behaviour, update the root `SKILL.md`. It is the improve agent's only picture of the
  system, and stale text there produces bad weekly PRs.
- Verify a Python change with `python -m py_compile <file>`; the improve job applies the same check.
- `agent.py` and `portal_scraper.py` are committed with CRLF line endings; keep them that way.
- Optional rules keys (defaults): `brand_prefer_by_part` (none), `prefer_in_stock` (true),
  `expensive_ratio` (1.5), `expensive_min_diff_sek` (100), `max_alternatives` (1, allowed 0-3 but
  at most one alternative is ever offered). Invalid values make the whole rules file invalid
  (fail closed). Details in `SKILL.md` ("Alternatives and the swap").

## Duplicate-order guard (parts_order._stage, before any cart action)
- Every staging attempt with a portal (restaged ones too) first reads "Skickade Beställningar"
  once (`PartsPortal.list_sent_orders()`), before login/vehicle/cart steps. A row for the same
  vehicle (plate when the registration column exists, else brand + model words;
  `order_match.candidates`) dated within `DUPLICATE_WINDOW_DAYS = 7` (UTC date) and not claimed
  by another record's `verified_numbers` blocks staging: reason "order finns redan i
  pro.meca.se (<numbers_text(row)>)", `cart_touched` false, Swedish message "Bokning <nr>: en
  skickad order för fordonet finns redan i pro.meca.se (…). Jag har inte förberett någon ny
  order. Beställ för hand om delarna ändå behövs." The reason is in
  `orders_state.NO_RESTAGE_REASONS`, so `restage()` never retries it.
- Fail closed: a transient error reading the list (timeout, login, …) is the usual transient
  block (quiet retry); a parse error fails the booking. Never read as "no duplicate".
  A NON-empty list that cannot be matched (no registration column and no vehicle model, or no
  row date readable) blocks with "kan inte kontrollera tidigare order" (cart untouched, owner
  asked to check Skickade Beställningar by hand; re-staged like a transient problem). Only an
  empty list lets staging proceed without a match.

## Immediate verification (parts_order.verify_now)
- After checkout the Beställd message goes first and unchanged; then the order is looked up in
  "Skickade Beställningar" (3 reads, 10 s apart, 90 s budget; not after approve run start + 270 s,
  nor past the staging deadline in auto mode). Only by plate with the registration column; a real
  reference must appear in the row. A match stores `verified_at`/`verified_numbers` and sends a
  separate `Beställning verifierad: …` message. It never fails the order. Residual risk: a
  same-plate earlier row when the reference is UNVERIFIED. The daily checker below is the fallback.

## Order checker (approve.py, after the taps)
- A placed order often gets only an `UNVERIFIED-<UTC>` reference (no number readable after the
  final click); the Beställd message then says "Ordernummer: bekräftas senare (kolla Skickade
  Beställningar i pro.meca.se)". The real order shows up later in Orderöversikt → tab
  "Skickade Beställningar" (columns: Ordernummer, Status, Datum, Registreringsnummer (hidden),
  Bilmodell, Ordernummer (portal, "01681392/01681393"), …, Beställningsstatus).
- Each approve run (daily 06:00 UTC, or by hand) checks every `ordered` record without
  `verified_at` that has not been warned about: `PartsPortal.list_sent_orders()` (read-only; the
  only click is the tab `li.order-tabs__item[data-filter=sent]`; only the first ~50 rows are
  read, so it is meant for recent orders) → `order_match.resolve_orders`.
- A candidate row is dated on or after the order's Swedish date, has a supplier number and is not
  already claimed by another record's `verified_numbers`. With a plate (`plan.reg_nr`) and a
  registration column, ONLY a row with the same plate counts (the model is ignored: naming
  differs, e.g. MERCEDES-BENZ vs MERCEDESC220; an empty-registration row never counts).
  Without a plate or without the column, the Bilmodell must start with the booking's brand +
  whole model words (`KIARIO (III)(2014)` ↔ `KIA, Rio Kombi-sedan (III), 12-16`).
- All unverified records are matched together, nothing is claimed while matching. Records that
  share candidate rows form a group: one record + one row → verified; one record + several
  rows → ambiguous; several records with fewer rows than records → wait (silent); several
  records with as many rows or more → ambiguous for each (never paired by guess).
- Verified → `verified_at` + `verified_numbers` (cut at a number boundary to 120 chars) stored,
  message "Beställning verifierad: …" (not resent if undelivered; run exits 1). Ambiguous → ONE
  message listing the rows (max 5). Not found / waiting / portal error for 3 days (by
  `updated_at`) → ONE warning. `verify_warned` stops repeats. Portal errors are logged without
  page text and retried next run.
- `plan_and_stage` stores the booking's car model as `vehicle_model` (sanitised, ≤ 80 chars).

## Parts vocabulary and lookup rules (owner-confirmed 2026-10-21)
- **Pollenfilter = Kupéfilter.** Searching "pollenfilter" on pro.meca.se returns NOTHING; "kupéfilter" returns the linked parts. Treat both words (and "kupefilter") as the same part and search "kupéfilter".
- **Artikel-ID.** On the booking page the "Delar" table has the columns name, OEM No, Artikel-ID, Antal, Beställda, Pris, Rabatt, Totalt (e.g. `Pollenfilter | 1025-PC40293 | 1025-PC40293 | 1`). The Artikel-ID is the pro.meca.se "Art. Nr" (searching it finds exactly that article, in the extra "Artikelnummer" tab and in "Kopplade reservdelar" when it fits the car). Fluids/oils ("Vätskor / Oljor") have no Artikel-ID.
- The Artikel-ID is **usually a ProMeister part**: ProMeister is never ordered, so use it as the REFERENCE (its title and "Produktinformation" such as filter type and dimensions) and find a similar linked part from another brand (Bosch preferred). It is not always ProMeister (the oil filter on the same booking was a Bosch `1025-F026407142`).
- Searches are only trusted from the "Kopplade reservdelar" tab (parts linked to the vehicle).
