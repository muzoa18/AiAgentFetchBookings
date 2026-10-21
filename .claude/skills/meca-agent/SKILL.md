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
  Approve/Cancel message) → `run_logger.log_run`.
- `approve.py` (daily 06:00 UTC): `notifier.get_updates` → Approve: `parts_order.execute_order`;
  Cancel: clear cart; Klar: `mark_booking_handled`; expire approvals older than 48 h.
- `improve.py --apply-all` (Mondays 06:00 UTC): proposals → PR; only `ALLOWED_FILES` (outside
  `PROTECTED_SYMBOLS`) and new `tests/test_*.py` files are ever written.

## Invariants (break these and bookings get missed, duplicated or wrongly ordered)
- **The portal is the truth for a `Bokning`.** Listed on Nya = not handled. `seen_ids` only means
  "notified"; it never decides "handled".
- **Notify once.** No new message unless the outcome changes.
- **A booking with parts is only marked Hanterad by the owner's Klar tap.**
- **Never order twice** (`config/orders.json`, `attempted` before checkout), never above the
  10,000 SEK ceiling, never ProMeister, never fluids/oils, only "Kopplade reservdelar", ambiguity
  means no order. Rules live in `parts_rules.yaml` (`mode: approve`).
- **Only the owner's private chat can approve** (`TELEGRAM_CHAT_ID`, never a group).
- **State is committed back to `main`** by the workflows (`seen_ids.txt`, `orders.json`,
  `no_button_*.txt`, `run_history.jsonl`). Those files show as modified locally after a pull;
  that is normal, don't "clean" them.
- **No secrets in git.** Config is env-first (`config_loader.py`); `config/config.yaml` is gitignored.
- **Improve agent opens PRs only** and writes only its allowlist (scrapers, parsers, state,
  run_logger, new tests), never the locked money-gate / Hanterad symbols. Never make it push to
  `main` or widen the allowlist casually; every PR needs the owner's review.

## Working on it
- Tests: `python -m pytest` (offline, fixtures for both portals). Run it before every commit.
- Local run: `pip install -r requirements.txt && python -m playwright install chromium`, copy
  `config/config.example.yaml` to `config/config.yaml`, then `python agent.py` or
  `python approve.py`. Set `portal.debug: true` for a visible browser. This sends real Telegram
  messages, clicks the real Hanterad button and can fill a real cart / place a real order, so
  confirm with the user before running against the live portals with real credentials.
- Selector breakage is the usual failure. Use `inspect_portal.py` to look at the live HTML, and prefer
  exact-text matching, which is what the scrapers already do (see "Scraper selectors" in `SKILL.md`).
- Cron times in the workflows are UTC; Sweden is UTC+1/+2.
- After changing behaviour, update the root `SKILL.md`. It is the improve agent's only picture of the
  system, and stale text there produces bad weekly PRs.
- Verify a Python change with `python -m py_compile <file>`; the improve job applies the same check.
- `agent.py` and `portal_scraper.py` are committed with CRLF line endings; keep them that way.

## Parts vocabulary and lookup rules (owner-confirmed 2026-10-21)
- **Pollenfilter = Kupéfilter.** Searching "pollenfilter" on pro.meca.se returns NOTHING; "kupéfilter" returns the linked parts. Treat both words (and "kupefilter") as the same part and search "kupéfilter".
- **Artikel-ID.** On the booking page the "Delar" table has the columns name, OEM No, Artikel-ID, Antal, Beställda, Pris, Rabatt, Totalt (e.g. `Pollenfilter | 1025-PC40293 | 1025-PC40293 | 1`). The Artikel-ID is the pro.meca.se "Art. Nr" (searching it finds exactly that article, in the extra "Artikelnummer" tab and in "Kopplade reservdelar" when it fits the car). Fluids/oils ("Vätskor / Oljor") have no Artikel-ID.
- The Artikel-ID is **usually a ProMeister part**: ProMeister is never ordered, so use it as the REFERENCE (its title and "Produktinformation" such as filter type and dimensions) and find a similar linked part from another brand (Bosch preferred). It is not always ProMeister (the oil filter on the same booking was a Bosch `1025-F026407142`).
- Searches are only trusted from the "Kopplade reservdelar" tab (parts linked to the vehicle).
