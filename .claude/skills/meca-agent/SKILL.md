---
name: meca-agent
description: Work on the MECA booking agent in this repo (AiAgentFetchBookings), a Playwright scraper of the MECA ProMeister portal that sends SMS for new bookings, runs on GitHub Actions behind a Gmail email gate, and opens a weekly self-improve PR. Use this whenever the user mentions MECA, bookings, Hanterad, Nya tab, the SMS or 46elks sender, seen_ids, the email trigger, the weekly improve PR, bookings.yml or weekly-improve.yml, or asks to debug, change, deploy or review anything in this project, even if they don't name the skill.
---

# MECA booking agent

Scrapes new bookings from meca.promeisterportal.com, SMSes the workshop owner, and clicks
Hanterad → Bekräfta for confirmed `Bokning`s. Hosted on GitHub Actions; a weekly job has Claude
review the code and open a PR. The full description lives in `SKILL.md` at the repo root
(also read by `improve.py`) and `README.md`. Read those before making non-trivial changes.

## Flow
`agent.py`: `email_trigger.should_run` → `portal_scraper.fetch_bookings` → `build_sms` →
`sms_sender.send_sms` → `portal_scraper.mark_booking_handled` → `state.save_seen_ids` + `run_logger.log_run`.

## Invariants (break these and bookings get missed or duplicated)
- **Gate fails open.** Any Gmail error means scrape anyway. A wasted scrape is cheap; a missed booking is not.
- **Dedupe via `config/seen_ids.txt`.** One booking, one SMS. IDs are added even when SMS fails, so a
  failure shows up as `sms_failed` in run history rather than being retried and spamming.
- **Only `Bokning` has a Hanterad button.** `Förfrågan`/`Offert` are SMS-only and logged as PENDING.
- **State is committed back to `main`** by the workflow (`seen_ids`, `seen_email_ids`, `run_history.jsonl`).
  Those three files show as modified locally after a pull; that is normal, don't "clean" them.
- **No secrets in git.** Config is env-first (`config_loader.py`); `config/config.yaml` is gitignored.
- **Improve agent opens PRs only.** Never make it push to `main`.

## Working on it
- Local run: `pip install -r requirements.txt && python -m playwright install chromium`, copy
  `config/config.example.yaml` to `config/config.yaml`, then `python agent.py`. Set `portal.debug: true`
  for a visible browser. This sends real SMS and clicks the real Hanterad button, so confirm with the user
  before running against the live portal with real credentials.
- Selector breakage is the usual failure. Use `inspect_portal.py` to look at the live HTML, and prefer
  exact-text JS matching, which is what the scraper already does (see "Scraper selectors" in `SKILL.md`).
- Cron times in the workflows are UTC; Sweden is UTC+1/+2.
- After changing behaviour, update the root `SKILL.md`. It is the improve agent's only picture of the
  system, and stale text there produces bad weekly PRs.
- Verify a Python change with `python -m py_compile <file>`; the improve job applies the same check.

## Known drift to be aware of
`improve.py` still lists `parts_agent.py`, `email_agent.py` and their logs in `SOURCE_FILES` / `LOG_FILES`.
Those files no longer exist, so the improve prompt gets "FILE NOT FOUND" noise. Its `IMPROVE_MODEL`
should also be checked against current model IDs.

## Parts vocabulary and lookup rules (owner-confirmed 2026-10-21)
- **Pollenfilter = Kupéfilter.** Searching "pollenfilter" on pro.meca.se returns NOTHING; "kupéfilter" returns the linked parts. Treat both words (and "kupefilter") as the same part and search "kupéfilter".
- **Artikel-ID.** On the booking page the "Delar" table has the columns name, OEM No, Artikel-ID, Antal, Beställda, Pris, Rabatt, Totalt (e.g. `Pollenfilter | 1025-PC40293 | 1025-PC40293 | 1`). The Artikel-ID is the pro.meca.se "Art. Nr" (searching it finds exactly that article, in the extra "Artikelnummer" tab and in "Kopplade reservdelar" when it fits the car). Fluids/oils ("Vätskor / Oljor") have no Artikel-ID.
- The Artikel-ID is **usually a ProMeister part**: ProMeister is never ordered, so use it as the REFERENCE (its title and "Produktinformation" such as filter type and dimensions) and find a similar linked part from another brand (Bosch preferred). It is not always ProMeister (the oil filter on the same booking was a Bosch `1025-F026407142`).
- Searches are only trusted from the "Kopplade reservdelar" tab (parts linked to the vehicle).
