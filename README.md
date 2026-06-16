# MECA Booking Agent

Automatically notifies the workshop owner of new MECA ProMeister bookings, and
self-improves once a week.

When a customer books, MECA emails the workshop *"a booking was made"* — but with
no details. This agent fills that gap: it's triggered by that email, logs into
the portal, scrapes the real booking (type, customer, vehicle, services, price),
**sends an SMS** with everything, and marks confirmed `Bokning` bookings as
`Hanterad`.

It runs entirely on **GitHub Actions** (no local machine needed) on a schedule.

---

## How it works

```
GitHub Actions (every 30 min, business hours)
        │
        ▼
  Trigger gate ── mode "email": new MECA booking email in Gmail? ──no──▶ exit (cheap)
        │ yes (or mode "always")
        ▼
  Log in to meca.promeisterportal.com  (Playwright / headless Chromium)
        ▼
  Open Nya tab → for each NEW booking, open detail page, extract info
        ▼
  Send SMS to workshop owner(s) via 46elks
        ▼
  Bokning → click Hanterad → Bekräfta   │   Förfrågan/Offert → SMS only
        ▼
  Commit booking state back to the repo (so it survives between runs)
```

```
GitHub Actions (weekly, Mondays)
        │
        ▼
  improve.py reviews code + run history with Claude (claude-opus-4-8)
        ▼
  Applies proposals to a branch, syntax-checks, reverts anything broken
        ▼
  Opens a Pull Request → you review & merge
```

---

## Architecture choices (why it's built this way)

- **GitHub Actions, not Vercel.** The core is a real browser driving a JS portal
  across many pages — that doesn't fit Vercel's serverless model (Chromium size,
  function timeouts, read-only FS). Actions gives a full Linux VM, native
  Chromium, cron, free secrets, and the weekly agent can commit back via a PR.
- **Email-gated trigger.** Scraping is the fragile part, so we only do it when a
  booking actually happened (per the MECA email). Set `BOOKING_TRIGGER_MODE` to
  `always` to scrape every run regardless (works without Gmail).
- **Improve agent opens PRs.** Self-modifying code that runs unattended against a
  live portal must be reviewed — the weekly agent never pushes to `main` directly.
- **No secrets in git.** Real credentials live only in GitHub Secrets / a local
  `config/config.yaml` (gitignored). See [`config/config.example.yaml`](config/config.example.yaml).

---

## Deployment (one time)

### 1. Add GitHub Secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**.

**Required:**

| Secret | What it is |
|---|---|
| `PORTAL_USERNAME` | MECA ProMeister login |
| `PORTAL_PASSWORD` | MECA ProMeister password |
| `SMS_RECIPIENT_NUMBER` | Number(s) to SMS, e.g. `+46700000000,+46711111111` |
| `SMS_API_USER` | 46elks API username |
| `SMS_API_PASSWORD` | 46elks API password |
| `ANTHROPIC_API_KEY` | Claude API key (used by the weekly improve agent) |

**Optional:**

| Secret | Default |
|---|---|
| `SMS_PROVIDER` | `46elks` (or `twilio`) |
| `SMS_SENDER_NAME` | `MECA` |
| `GMAIL_TOKEN_JSON` | (none) — paste the full contents of `config/gmail_token.json` to enable email-gated mode |
| `GMAIL_CREDENTIALS_JSON` | (none) — full contents of `config/gmail_credentials.json` |

### 2. Add Variables (non-secret)

Same page → **Variables** tab:

| Variable | Value |
|---|---|
| `BOOKING_TRIGGER_MODE` | `email` (gate on the MECA email) or `always` |
| `BOOKING_EMAIL_QUERY` | Gmail search matching the MECA notification, e.g. `newer_than:1d (subject:bokning)` |

### 3. Enable Actions → PR creation

Repo → **Settings → Actions → General → Workflow permissions** →
✅ *Allow GitHub Actions to create and approve pull requests*
(needed for the weekly improve PR).

### 4. Done

- `Actions` tab → **MECA Booking Agent** → *Run workflow* to test immediately.
- Edit the cron in [`.github/workflows/bookings.yml`](.github/workflows/bookings.yml)
  to change frequency/hours (it's in **UTC** — Sweden is UTC+1/+2).

---

## Local development

```bash
pip install -r requirements.txt
python -m playwright install chromium

cp config/config.example.yaml config/config.yaml   # fill in your real values
python agent.py            # run the booking agent once
python improve.py          # interactive self-improve (review each proposal)
```

Any env var overrides the matching `config.yaml` field, so you can test the
exact CI behaviour locally by exporting the secrets instead.

---

## Setting up the Gmail trigger (optional but recommended)

Email-gated mode needs read access to the Gmail account that receives MECA
booking notifications. Follow [`GMAIL_SETUP.md`](GMAIL_SETUP.md) once on your
machine to produce `config/gmail_token.json` and `config/gmail_credentials.json`,
then paste their contents into the `GMAIL_TOKEN_JSON` / `GMAIL_CREDENTIALS_JSON`
secrets and set `BOOKING_TRIGGER_MODE=email`.

If Gmail isn't configured, the trigger **fails open** — the agent simply scrapes
on every scheduled run.

---

## Project structure

```
agent.py            # Main entry point — trigger gate, scrape, SMS, Hanterad
email_trigger.py    # Gmail check: was a MECA booking email received?
portal_scraper.py   # Playwright login, Nya tab, detail extraction, Hanterad
sms_sender.py       # 46elks / Twilio SMS (multiple recipients)
state.py            # seen_ids.txt — avoids duplicate SMS
run_logger.py       # logs/run_history.jsonl — structured run history
config_loader.py    # env-first config (GitHub Secrets) + config.yaml fallback
improve.py          # Weekly self-improve — opens a PR with proposed fixes
.github/workflows/
  bookings.yml         # Frequent, email-gated booking run
  weekly-improve.yml   # Weekly improve → Pull Request
config/
  config.example.yaml  # Template (copy to config.yaml locally)
SETUP_LOG.md        # Full log of the changes made to host this on GitHub
```

---

## Security notes

- `config/config.yaml`, Gmail tokens, and the browser context are **gitignored** —
  never commit them.
- The credentials previously stored in a local `config.yaml` should be **rotated**
  (MECA password, 46elks keys, Anthropic key) and re-added only as GitHub Secrets.
