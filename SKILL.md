# MECA Booking Agent — Skill File

## What this agent does
Automates the full booking workflow for MD Bilreparationer (MECA workshop, Eskilstuna).

### Daily trigger (17:00 via Windows Task Scheduler)
1. Logs into https://meca.promeisterportal.com
2. Opens /Booking/Index → clicks "Nya" tab
3. For each new booking, opens the detail page (BookingInfo)
4. Extracts: customer name, phone, vehicle reg+model, workshop date, services, parts, fluids, total price
5. Sends SMS via 46elks to configured recipient numbers
6. If booking_type == "Bokning" → clicks Hanterad → Bekräfta (two-step confirmation)
7. If booking_type == "Förfrågan" → launches parts_agent.py

### Förfrågan flow (parts_agent.py)
1. Logs into https://pro.meca.se
2. Loads vehicle by reg number (URL parameter)
3. Searches each required part in the catalog search bar
4. Claude AI picks the best matching part from search results
5. Adds parts to cart
6. Clicks Fritext → fills labor (Arbetstid, hours, rate)
7. Writes AI-generated Offertnotering in Swedish
8. Clicks "Skicka prisförslag (e-post)" → emails quote to test_email or customer

## File structure
```
agent.py           — Main orchestrator. Run this daily.
portal_scraper.py  — Login, Nya tab, detail page extraction, Hanterad click
parts_agent.py     — Förfrågan handler: pro.meca.se login, parts search, quote email
sms_sender.py      — 46elks SMS, supports multiple recipients (comma-separated)
extractor.py       — (legacy) AI text extraction, mostly replaced by direct table parsing
state.py           — seen_ids.txt persistence to avoid duplicate SMS
config_loader.py   — Loads and validates config/config.yaml
improve.py         — Self-improvement: sends code + logs to Claude, proposes changes
SKILL.md           — This file. Describes the agent for improve.py context.
config/
  config.yaml        — Real credentials (never commit)
  config.example.yaml — Template
  seen_ids.txt       — Auto-created: tracks notified bookings
logs/
  agent.log          — Full run history
  proposals/         — Saved improvement proposals from improve.py
```

## Key implementation details

### Portal selectors (confirmed working)
- Login: `#userName`, `#password`, `button[type='submit']`
- Cookie popup: `button:has-text('Accept All Cookies')`
- Nya tab: JS evaluation matching element text starting with "Nya"
- Booking links: `a[href*='BookingInfo']`, deduplicated by BookingID param
- Hanterad: JS exact text match `=== 'Hanterad'`, then wait for Bekräfta dialog
- Bekräfta: JS exact text match `=== 'Bekräfta'`

### SMS format
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

### Booking types
- **Bokning** — confirmed booking. SMS + Hanterad click.
- **Förfrågan** — price request. SMS + parts agent quote email.
- **Offert** — quote already sent. SMS only, no action needed.

### Known issues / watch areas
- Specification table scraping stops at Händelselogg to avoid parsing event log rows
- Cookie popup must be dismissed before any clicks work
- pro.meca.se parts search selectors may need tuning (not yet battle-tested)
- Fritext labor dialog selectors may need adjustment based on actual HTML

## Config structure
```yaml
portal:
  username: "..."
  password: "..."
  bookings_path: '/Booking/Index'
  debug: false          # true = visible browser

anthropic:
  api_key: "sk-ant-..."

sms:
  provider: "46elks"
  recipient_number: "+46709251764,+46705563181"
  sender_name: "MECA"
  api_user: "u..."
  api_password: "..."

parts_portal:
  username: "..."
  password: "..."
  labor_hours: 1
  test_email: "mdbilreparationer@gmail.com"
```

## Improvement priorities (for improve.py)
1. Reliability — any scraping failures or timeouts
2. SMS content quality — wrong/missing fields
3. Parts search accuracy — wrong parts selected
4. Hanterad confirmation — any cases where it fails silently
5. Performance — reduce browser open time

## Email Agent (email_agent.py)
Runs every 30 minutes. Reads unread Gmail, uses Claude to draft replies.

### Flow
1. Fetch unread emails (excludes MECA portal notifications, noreply senders)
2. Claude reads each email and decides:
   - `ask_info` — missing reg number, mileage or problem description → asks customer
   - `send_quote` — enough info → price estimate + booking suggestion
   - `general_reply` — general question → helpful answer
3. Creates Gmail DRAFT (never sends automatically)
4. Marks original email as read
5. Sends SMS to owner: "Nytt kundmail från X — utkast klart i Gmail"

### State file
config/seen_email_ids.txt — separate from booking seen_ids.txt

### Setup required
See GMAIL_SETUP.md — one-time Google OAuth setup needed.

## Service Package Knowledge (email_agent.py)
The AI knows these packages by name — mätarställning is NEVER required:

| Customer says | Package | Price range |
|---|---|---|
| oljebyte, oljebyte + filter | Liten service | 800–1 200 kr |
| stor service, full service | Stor service | 2 000–3 500 kr |
| bromsar fram | Bromsbyte fram | 2 500–4 000 kr |
| bromsar bak | Bromsbyte bak | 2 000–3 500 kr |
| alla bromsar | Bromsbyte alla | 4 500–7 000 kr |
| däckbyte, vinterdäck | Däckbyte | 600–900 kr |
| felkod, diagnos | Diagnosläsning | från 500 kr |

## Email Agent Rules
- Read FULL thread — never ask for info already given
- NEVER ask for mätarställning, årsmodell, or anything except regnr + work type
- Only need: regnr + work type → send_quote immediately
- Code override: if reg_nr + work_description extracted and only mätarställning missing → force send_quote
- **SMS sent ONLY when action=send_quote AND PDF was successfully created**
- No SMS for ask_info, general_reply, or failed quotes
- Non-customer emails (suppliers, newsletters) → marked back as UNREAD in inbox (not added to seen_ids)
- One action per sender per run (thread-based deduplication)
- Supplier domains: riddermarkbil.se, dssparts.se, mekonomen.se, meca.se etc.

## SMS logic summary
| Situation | SMS? | Mark as read? |
|---|---|---|
| Quote created (PDF ready) | YES | YES |
| Quote triggered but PDF failed | NO (warning logged) | YES |
| ask_info draft created | NO | YES |
| general_reply draft created | NO | YES |
| Supplier/non-customer email | NO | MARKED BACK AS UNREAD |
| action=ignore | NO | MARKED BACK AS UNREAD |

## Log files monitored by improve.py
- logs/agent.log — booking agent runs
- logs/email_agent.log — email agent runs
- logs/improve_auto.log — auto-improvement history

## Work-to-parts mapping (_work_to_parts in email_agent.py)
When an email quote is triggered, work description is mapped to actual parts.

### Brand rules (enforced in both _work_to_parts and AI picker):
- **Always Bosch** for filters, brake pads, discs, spark plugs
- **Always Mobil 1** for motor oil
- **Never ProMeister** branded parts — AI picker explicitly excludes them
- Oil quantity is vehicle-dependent — looked up on pro.meca.se, not hardcoded

### Service packages:
| Customer says | Parts searched |
|---|---|
| oljebyte / liten service | Bosch oljefilter + Bosch pollenfilter kolfilter + Mobil 1 motorolja |
| stor service / full service | Same + Bosch luftfilter + Bosch tändstift |
| bromsbyte / broms fram | Bosch bromsbelägg fram + Bosch bromsskiva fram x2 |
| bromsbyte bak | Bosch bromsbelägg bak + Bosch bromsskiva bak x2 |
| bromsbyte alla | Both front and rear |
| bromsvätska | Bosch bromsvätska DOT4 |
| däckbyte | däck x4 (no brand — size varies) |
| tändstift | Bosch tändstift |
| (unknown) | Work description used as search term directly |

### Note on pollenfilter:
Liten service ALWAYS includes pollenfilter/kupéfilter (kolfilter).
Search term "Bosch pollenfilter kolfilter" finds the activated carbon cabin filter.
