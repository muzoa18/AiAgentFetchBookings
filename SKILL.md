# MECA Booking Agent — Skill File

Context file read by `improve.py` on every weekly review. Keep it in sync with the code;
anything described here that does not exist in the repo will mislead the improve agent.

## What this agent does
Notifies the owner of MD Bilreparationer (MECA workshop, Eskilstuna) of new bookings from the
MECA ProMeister portal. MECA's own notification email says only "a booking was made", with no
details. The agent logs into the portal, scrapes the real booking, sends an SMS with everything,
and marks confirmed bookings as handled.

Runs entirely on **GitHub Actions** (no local machine, no Task Scheduler).

### Booking run (`.github/workflows/bookings.yml`, every 30 min, Mon–Sat, 05–16 UTC)
1. **Trigger gate** (`email_trigger.py`). In `email` mode, check Gmail for a not-yet-seen MECA
   booking email and exit cheaply if there is none. In `always` mode, skip the gate.
   Any Gmail error **fails open** (scrape anyway) so a real booking is never missed.
2. Log in to https://meca.promeisterportal.com (Playwright, headless Chromium).
3. Open `/Booking/Index` → click the **Nya** tab.
4. For each booking not in `config/seen_ids.txt`, open the BookingInfo detail page and extract:
   customer, phone, email, vehicle reg + model, workshop date, message, services, parts,
   fluids, total price.
5. Send an SMS via 46elks (Twilio supported) to every number in `recipient_number`.
6. `Bokning` → click **Hanterad** → **Bekräfta**. `Förfrågan` / `Offert` → SMS only; there is
   no Hanterad button, so they stay pending in the portal and are logged as `PENDING`.
7. Commit `config/seen_ids.txt`, `config/seen_email_ids.txt` and `logs/run_history.jsonl` back
   to `main` (state must survive between ephemeral CI runs).

### Weekly self-improve (`.github/workflows/weekly-improve.yml`, Mondays 06:00 UTC)
`python improve.py --apply-all` sends source + SKILL.md + recent logs to Claude, applies the
proposals on a branch, runs `py_compile` on each changed `.py`, reverts any file that does not
compile, writes `improve_reports/latest.md`, and opens a **Pull Request**. It never pushes to
`main`; a human reviews and merges.

## File structure
```
agent.py            Orchestrator: gate → scrape → SMS → Hanterad → state/log
email_trigger.py    Gmail gate (should_run); tracks config/seen_email_ids.txt
portal_scraper.py   Playwright login, Nya tab, detail extraction, Hanterad click
sms_sender.py       46elks / Twilio, comma-separated recipients
state.py            seen_ids.txt load/save (dedupe so one booking = one SMS)
run_logger.py       Appends structured runs to logs/run_history.jsonl
config_loader.py    Env-first config (GitHub Secrets) with config/config.yaml fallback
improve.py          Weekly self-improve → PR
extractor.py        LEGACY AI text extraction, superseded by direct table parsing
inspect_portal.py   Dev helper for inspecting portal HTML when selectors break
config/config.example.yaml   Template; env var name for each field is in its comments
```
Not present any more: `parts_agent.py`, `email_agent.py`, Windows Task Scheduler jobs. The old
Förfrågan quote-email flow and the Gmail draft-reply agent were removed.

## Booking types
- **Bokning**: confirmed booking. SMS, then Hanterad → Bekräfta.
- **Förfrågan**: price request. SMS only; needs manual action in the portal.
- **Offert**: quote already sent. SMS only.

## SMS format
```
Ny Bokning - OLRD-3630
Kund: Mamadou Sowe
Tel: +46739133124
Fordon: NXP168 VOLKSWAGEN PASSAT
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

## Scraper selectors and gotchas
- Cookie popup must be dismissed before any click works.
- Nya tab: JS evaluation matching element text that starts with "Nya".
- Booking links: `a[href*='BookingInfo']`, deduplicated by BookingID param.
- Hanterad: JS exact text match `=== 'Hanterad'`, then wait for the Bekräfta dialog; Bekräfta is
  also an exact text match.
- Specification table scraping stops at **Händelselogg** so event-log rows are not parsed.
- SMS failure still adds the ID to `seen_ids` and skips Hanterad (avoids SMS spam on retry, but
  the booking then needs manual follow-up; failures show as `sms_failed` in run history).

## Config (env var overrides config.yaml)
Required: `PORTAL_USERNAME`, `PORTAL_PASSWORD`, `SMS_RECIPIENT_NUMBER`, `SMS_API_USER`,
`SMS_API_PASSWORD`, `ANTHROPIC_API_KEY` (improve only).
Optional: `SMS_PROVIDER`, `SMS_SENDER_NAME`, `GMAIL_TOKEN_JSON`, `GMAIL_CREDENTIALS_JSON`.
Repo variables: `BOOKING_TRIGGER_MODE` (`email`|`always`), `BOOKING_EMAIL_QUERY`.
Secrets live only in GitHub Secrets or the gitignored `config/config.yaml`.

## Improvement priorities (for improve.py)
1. Reliability: scraping failures, timeouts, selector drift after portal changes.
2. Never missing a booking: gate must fail open; no silent SMS failures.
3. SMS content quality: wrong or missing fields.
4. Hanterad confirmation: cases where it fails silently.
5. Performance: reduce browser open time and CI minutes.

## Rules for proposed changes
- Do not add dependencies or touch workflows/secrets handling without a clear reason.
- Do not reference or recreate files listed under "Not present any more".
- Keep changes small and syntax-checkable; every changed `.py` must compile.

## Logs monitored by improve.py
- `logs/run_history.jsonl`: structured per-run summary (the main signal, tracked in git).
- `logs/agent.log`: only exists inside a CI run (gitignored), so it is usually absent.

## Parts vocabulary and lookup rules (owner-confirmed 2026-10-21)
- **Pollenfilter = Kupéfilter.** Searching "pollenfilter" on pro.meca.se returns NOTHING; "kupéfilter" returns the linked parts. Treat both words (and "kupefilter") as the same part and search "kupéfilter".
- **Artikel-ID.** On the booking page the "Delar" table has the columns name, OEM No, Artikel-ID, Antal, Beställda, Pris, Rabatt, Totalt (e.g. `Pollenfilter | 1025-PC40293 | 1025-PC40293 | 1`). The Artikel-ID is the pro.meca.se "Art. Nr" (searching it finds exactly that article, in the extra "Artikelnummer" tab and in "Kopplade reservdelar" when it fits the car). Fluids/oils ("Vätskor / Oljor") have no Artikel-ID.
- The Artikel-ID is **usually a ProMeister part**: ProMeister is never ordered, so use it as the REFERENCE (its title and "Produktinformation" such as filter type and dimensions) and find a similar linked part from another brand (Bosch preferred). It is not always ProMeister (the oil filter on the same booking was a Bosch `1025-F026407142`).
- Searches are only trusted from the "Kopplade reservdelar" tab (parts linked to the vehicle).
