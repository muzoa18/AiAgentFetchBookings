# MECA Booking Agent

Notifies the workshop owner of new MECA ProMeister bookings on **Telegram**, marks bookings
without spare parts as `Hanterad`, prepares the spare-parts order on **pro.meca.se** for every
booking that needs parts and places it after the owner taps **Approve**, and self-improves once
a week through a reviewed Pull Request.

MECA's own notification email only says *"a booking was made"*. This agent logs into the portal,
reads the real booking (type, customer, vehicle, services, parts, fluids, price) and acts on it.

It runs entirely on **GitHub Actions** (no local machine needed). The full description of the
rules is in [`SKILL.md`](SKILL.md).

---

## How it works

```
bookings.yml — daily 15:00 UTC, Mon–Sat                       (python agent.py)
        │
        ▼
  Log in to meca.promeisterportal.com (Playwright / headless Chromium)
        ▼
  Nya tab → every listed booking: open detail page, extract info
        ▼
  Telegram message to the owner — ONCE per booking (config/seen_ids.txt)
        ▼
  ┌──────────────────────────────┬──────────────────────────────────┬────────────────────┐
  │ Bokning, no spare parts      │ Bokning with spare parts         │ Offert / Förfrågan │
  │ Hanterad → Bekräfta now      │ pro.meca.se: load vehicle,       │ message only       │
  │ (retried every run)          │ match parts, fill cart, ceiling  │ (no Hanterad)      │
  │                              │ check → Telegram Approve/Cancel  │                    │
  └──────────────────────────────┴──────────────────────────────────┴────────────────────┘
        ▼
  Commit state back to the repo (seen_ids, orders.json, run history)

approve.yml — daily 06:00 UTC, every day                      (python approve.py)
        │
        ▼
  approve.py --check: anything pending? ──no──▶ exit (cheap, no browser)
        │ yes
        ▼
  Read the owner's Telegram taps (owner's private chat, booking's own message only)
        ├─ Approve → re-check cart vs plan and ceiling → place order → result message + Klar
        ├─ Cancel  → clear the vehicle's cart → message + Klar
        ├─ Klar    → Hanterad → Bekräfta in the MECA portal
        └─ approvals older than 48 h expire
        ▼
  Order checker: placed orders not yet verified → pro.meca.se "Skickade Beställningar"
        ▼
  Commit config/orders.json back

weekly-improve.yml — Mondays 06:00 UTC                        (python improve.py --apply-all)
        │
        ▼
  Claude (claude-opus-5-5) reviews code + SKILL.md + logs → proposals + suggestions
        ▼
  Apply only state.py / run_logger.py / new tests, syntax-check, revert anything broken;
  everything else becomes a written suggestion for a developer
        ▼
  Pull Request → you review & merge
```

**Order checker.** pro.meca.se often shows no order number right after "Skicka beställning"; the
Beställd message then says "Ordernummer: bekräftas senare". The placed order appears later under
Orderöversikt → "Skickade Beställningar". Every approve run (daily, or by hand) opens that tab
read-only and looks for a row dated on or after the order day for the booking's car: the same
registration number (the table's hidden registration column), or, when no plate is known or
the table has no such column, the same car model ("Bilmodell"). Orders that compete for the
same rows are resolved together and never swapped. Exactly one row → the numbers are stored in
`config/orders.json` (`verified_numbers`) and the owner gets "Beställning verifierad: <bokning>
– <modell>, ordernummer …" (if that message cannot be delivered it is not resent; the stored
state is correct and the run shows as failed). Several rows → one message asking which one it
is (never picked automatically). Nothing found within **3 days** → one warning to check by
hand; a pro.meca.se error is retried silently on the next run, but the 3-day warning still
comes. Only the first page of "Skickade Beställningar" (about 50 rows, newest first) is read,
so the checker is meant for recent orders.

Parts rules (owner-editable in [`parts_rules.yaml`](parts_rules.yaml), no code change needed):
Bosch preferred, then another brand; never ProMeister; never fluids or oils; only parts linked to
the vehicle ("Kopplade reservdelar"); ceiling **10,000 SEK** per booking; `mode: approve` (ask the
owner first).

---

## Alerts: what they mean and what to do

Nothing should fail silently. Every alert is a Telegram message (at most once per 24 h per
problem, repeated as a reminder while it lasts) and a banner on the dashboard; it clears itself
when the problem is gone.

| Alert | Meaning | What to do |
|---|---|---|
| `state_unreadable` ("config/orders.json saknas" / "kan inte läsas") | The order state is missing or broken. All ordering is stopped before the portal is touched, so nothing can be ordered twice. | Restore `config/orders.json` from git history (it must be committed) and run the workflow again. |
| `crash_<name>` / `run_failed_<name>` | `agent.py` (`booking_agent`) or `approve.py` (`approve`) crashed or ended with an error. | Open the GitHub Actions log. Fix the cause (often a portal selector) or re-run. |
| `workflow_failed_<workflow>` | The whole GitHub workflow failed or was cancelled: a failed install, the main step's own timeout (13 min bookings / 8 min approve), the job timeout (15 / 10 min, a cancel), a manual cancel, or a crash before our code started. Not sent when the same run already sent a more specific alert (`state_unreadable`, `crash_<name>`, `run_failed_<name>`), so one fault gives one message. A later successful run clears it (step "Mark OK"). | Open the run link in the message. |
| `stale_bookings` / `stale_approve` | The watchdog (every 6 h) found no successful booking run (Mon–Sat 15:00 UTC) or approval poll (daily 06:00 UTC) within 3 h of its slot. | Check the Actions tab; run the workflow by hand. A GitHub API error never raises this alert. |

**Limit:** the watchdog is itself a scheduled workflow, so it cannot notice GitHub disabling
scheduled workflows after 60 days without repository activity. If the dashboard shows no
recent run, check the Actions tab.

**Order already placed.** Before staging, the agent reads "Skickade Beställningar" and blocks the
booking if an order for the same vehicle was sent in the last 7 days ("order finns redan i
pro.meca.se"), or if the list cannot be matched ("kan inte kontrollera tidigare order"). If
you still need the parts, order them by hand in pro.meca.se. Only the first ~50 sent rows are read.

**Dashboard** (`web/`): shows active alerts from both alert files, warns when the newest commit
is older than 36 h, and warns when the last successful run (no `login_failed`, no
`state_unreadable`) is older than 52 h (Saturday to Monday is 48 h without a Sunday run).

---

## Architecture choices (why it's built this way)

- **GitHub Actions, not Vercel.** The core is a real browser driving two JS portals — that
  doesn't fit Vercel's serverless model (Chromium size, function timeouts, read-only FS).
- **The portal is the truth.** A `Bokning` still on the Nya tab is not handled; local files only
  remember who was notified and the order status, so nothing is notified or ordered twice.
- **Telegram instead of SMS.** Free, and inline buttons give one-tap approval. The approval
  poller runs once a day; Telegram keeps unread taps for 24 h, so it runs every day.
- **Fail closed.** Real money is spent unattended: any doubt (no confident match, unreadable
  total, cart mismatch, wrong chat) means no order and a message saying why.
- **Improve agent opens PRs** and may only edit `state.py` and `run_logger.py` and add new
  test files — enforced in code. Every other file (scrapers, parsers, notifier, approval and
  ordering modules, workflows, config, rules) is suggestion-only: the PR body lists them under
  "Suggestions (not applied — need a developer)". Selector and search-word fixes will move to a
  validated data file later. Every PR still needs your review; read the new test files before
  running pytest locally, since they are LLM-written code.
- **No secrets in git.** Credentials live only in GitHub Secrets / a local
  `config/config.yaml` (gitignored). See [`config/config.example.yaml`](config/config.example.yaml).

---

## Deployment (one time)

### 1. Add GitHub Secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**.

| Secret | What it is | Used by |
|---|---|---|
| `PORTAL_USERNAME` | MECA ProMeister login | bookings, approve (Klar) |
| `PORTAL_PASSWORD` | MECA ProMeister password | bookings, approve (Klar) |
| `PARTS_PORTAL_USERNAME` | pro.meca.se login | bookings, approve |
| `PARTS_PORTAL_PASSWORD` | pro.meca.se password | bookings, approve |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | bookings, approve |
| `TELEGRAM_CHAT_ID` | The owner's **private** chat id with the bot (never a group) | bookings, approve |
| `ANTHROPIC_API_KEY` | Claude API key (part matching + weekly improve) | all three |

No repository variables are needed.

### 2. Enable Actions → PR creation

Repo → **Settings → Actions → General → Workflow permissions** →
✅ *Allow GitHub Actions to create and approve pull requests*
(needed for the weekly improve PR).

### 3. Workflows

| Workflow | Schedule (UTC) | What |
|---|---|---|
| [`bookings.yml`](.github/workflows/bookings.yml) | `0 15 * * 1-6` (17:00 summer / 16:00 winter, Mon–Sat) | booking run, stages parts orders |
| [`approve.yml`](.github/workflows/approve.yml) | `0 6 * * *` (every day) | Approve / Cancel / Klar taps, expiry |
| [`watchdog.yml`](.github/workflows/watchdog.yml) | `17 */6 * * *` (every 6 h) | dead-man switch: alerts when a run was missed |
| [`weekly-improve.yml`](.github/workflows/weekly-improve.yml) | `0 6 * * 1` (Mondays) | self-improve PR |

`Actions` tab → pick a workflow → *Run workflow* to run it now (e.g. approve.yml to process a tap
immediately). bookings and approve share one concurrency group and never overlap; the watchdog
has its own group.

**Every file the workflows read must be committed in the repo.** The first GitHub run once lost
order state because `config/orders.json` had never been committed (the run saw an empty state).
A missing or unreadable `config/orders.json` now stops all ordering, but the file still has to
be there.

State files the workflows commit back to `main` (they show as modified locally after a pull;
that is normal): `config/seen_ids.txt`, `config/orders.json`, `config/no_button_ids.txt`,
`config/no_button_seen_once.txt`, `logs/run_history.jsonl`, `config/alerts.json` (bookings and
approve) and `config/watchdog_alerts.json` (watchdog only). The two alert files are created by
the workflows the first time an alert is raised, so they may not exist yet.

### 4. First live order

Before the first real order, follow the checklist in
[`docs/superpowers/plans/2026-10-21-parts-ordering-handoff.md`](docs/superpowers/plans/2026-10-21-parts-ordering-handoff.md)
("Before the first supervised live order"): disable the old local scheduler, set the secrets,
push, then run the first order as a supervised test with `mode: approve`.

---

## Local development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m playwright install chromium

python -m pytest           # offline test suite — run before every commit

cp config/config.example.yaml config/config.yaml   # fill in your real values
python agent.py            # one booking run (real portal, real Telegram messages)
python approve.py --check  # prints has_pending=true|false (no browser, no network)
python approve.py          # process Approve / Cancel / Klar taps now (can place a real order)
python improve.py          # interactive self-improve (review each proposal)
python inspect_portal.py   # dev helper: look at the portal's HTML when selectors break
```

Any env var overrides the matching `config.yaml` field, so you can test the exact CI behaviour
locally by exporting the secrets instead. Set `portal.debug: true` for a visible browser.

**Do not run the agent locally and on GitHub Actions at the same time:** each copy keeps its own
`config/seen_ids.txt` / `config/orders.json`, so a local run can click Hanterad or fill a cart
that the GitHub runs never see.

---

## Project structure

```
agent.py               # Daily booking run: scrape, notify once, Hanterad or stage order
alerts.py              # Alert store (config/alerts.json): Telegram + dashboard, 24 h de-dup
preflight.py           # State guard (orders.json must exist) and guarded() run wrapper
alerts_cli.py          # Workflow-level failure alert (used by bookings.yml / approve.yml)
watchdog.py            # Dead-man switch (watchdog.yml): alerts when a scheduled run is missed
approve.py             # Daily poller: Approve / Cancel / Klar taps, expiry, order checker
order_match.py         # Order checker: match a placed order to a "Skickade Beställningar" row
notifier.py            # Telegram Bot API client
portal_scraper.py      # MECA portal: login, Nya tab, detail extraction, Hanterad
parts_order.py         # Plan, stage and execute a pro.meca.se order
parts_order_helpers.py # Quantities, cart/total checks, message text
parts_matcher.py       # Part matching: brand, ProMeister/fluid/placement rules, Claude picker
parts_portal.py        # pro.meca.se: login, vehicle, search, cart, checkout
parts_portal_parse.py  # pro.meca.se HTML parsers
orders_state.py        # config/orders.json — one status per booking, never order twice
rules.py               # Loads parts_rules.yaml
parts_rules.yaml       # Owner-editable parts rules (brands, ceiling, mode)
state.py               # config/seen_ids.txt — notified once
run_logger.py          # logs/run_history.jsonl — structured run history
config_loader.py       # env-first config (GitHub Secrets) + config.yaml fallback
improve.py             # Weekly self-improve — opens a PR with proposed fixes
inspect_portal.py      # Dev helper for selector work
tests/                 # Offline pytest suite
.github/workflows/
  bookings.yml         # Daily booking run
  approve.yml          # Daily approval poller
  watchdog.yml         # Dead-man switch, every 6 h
  weekly-improve.yml   # Weekly improve → Pull Request
config/
  config.example.yaml  # Template (copy to config.yaml locally)
SETUP_LOG.md           # History: the original (SMS/Gmail) move to GitHub Actions
```

---

## Security notes

- `config/config.yaml` and the browser context are **gitignored** — never commit them.
- `config/orders.json` is committed and never holds customer data; `logs/agent.log` (a 14-day
  workflow artifact) can hold full booking details.
- Rotate the MECA password and any key that was ever stored in a local file, and keep them only
  in GitHub Secrets.
