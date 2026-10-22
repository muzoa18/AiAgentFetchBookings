# Package 1: protection (design)

Date: 2026-10-21. Owner-approved scope (chat, 2026-10-21): "start with package 1, then 2 and 3".

## Why

The first real GitHub run re-staged an order that was already placed, because
`config/orders.json` had never been committed and the run started with empty order
state. Nothing warned anyone, and nothing would have warned the owner if the job had
silently stopped. Package 1 makes the system fail closed, loudly, in two places:
Telegram (the owner works from the phone) and the dashboard.

## Goals

1. **State guard.** A missing or unreadable `config/orders.json` stops all parts
   ordering for the run and raises an alert; it is never treated as "no orders".
2. **Crash and failure alerts.** Any failed run, crash (even before Python starts, e.g.
   a dependency install) or timeout produces one Telegram message and one dashboard alert.
3. **Dead-man switch.** If the booking run or the approval poll has not succeeded when
   it should have, an alert is raised (separate scheduled workflow, stdlib only).
4. **Duplicate-order guard at staging.** Before a cart is filled for a booking that has
   no order record, `Skickade Beställningar` is checked; a recent sent order for the same
   vehicle blocks staging and tells the owner.
5. **Dashboard shows active alerts** and how old the last successful run is.

## Non-goals

No new ordering features (package 2), no summaries or delivery tracking (package 3), no
multi-user roles (later), no change to approval rules, ceiling or Hanterad logic.
Alerts go to the one owner chat (`TELEGRAM_CHAT_ID`).

## Design

### Alerts (`alerts.py`, state in `config/alerts.json`)
`{"alerts": {"<key>": {"text", "severity": "error"|"warning", "first_seen", "last_seen",
"count", "last_sent", "resolved_at"}}}`. `raise_alert(config, key, text, severity)` upserts,
sends Telegram at most once per 24 h per key (an unresolved alert is re-sent after 24 h as
a reminder), and returns whether the state was written. `resolve(key)` sets `resolved_at`.
`active()` returns unresolved alerts. Texts are fixed Swedish strings: never raw exception
text, page text or customer data (booking number, plate, order number only). A corrupt
`alerts.json` is renamed aside (`alerts.json.corrupt`) and replaced, never raised from.
Telegram failure is logged and does not lose the dashboard alert.

### State guard and crash guard (`preflight.py`)
`state_problems() -> list[str]`: `config/orders.json` absent, unparsable or invalid
(uses `orders_state` validation) -> problem. `agent.py` and `approve.py` call it first:
on a problem, alert key `state_unreadable`, no ordering, exit code 1. `guarded(name, fn)`
wraps `run()` of both scripts: an uncaught exception raises alert `crash_<name>` (exception
class name only) and exits 1. A clean run resolves `crash_<name>`, `run_failed_<name>`.
A non-zero exit from the counters in `agent.run()` raises `run_failed_booking_agent`.

### Workflow failure step (`alerts_cli.py`, stdlib only)
Last step of `bookings.yml` and `approve.yml`, `if: failure()`: `python alerts_cli.py
workflow-failed <workflow name>`; sends the Telegram message (with the run URL from
`GITHUB_SERVER_URL/GITHUB_REPOSITORY/actions/runs/GITHUB_RUN_ID`) and writes the alert,
then the persist step commits `config/alerts.json` (added to both persist loops). Covers
failures that happen before or outside Python.

### Dead-man switch (`watchdog.py`, `.github/workflows/watchdog.yml`)
Cron every 6 hours (`17 */6 * * *`, off the hour), stdlib only, permissions `contents:
write` + `actions: read`, concurrency group `meca-watchdog` (see below). Pure functions:
`last_slot(now, spec)` returns the most recent expected run time (bookings: Mon-Sat 15:00
UTC; approve: daily 06:00 UTC); `is_overdue(now, last_success, spec, grace=3h)`. The last
success comes from the Actions API
(`/repos/{repo}/actions/workflows/{file}/runs?status=success&per_page=1`, token =
`GITHUB_TOKEN`). Overdue -> alert `stale_bookings` / `stale_approve`; fresh -> resolve.
An API error is logged and raises nothing (it is not evidence of a stale run). Note: the
watchdog cannot detect GitHub disabling scheduled workflows after 60 days of repository
inactivity; the dashboard's last-run age covers that.

*Revised (Task 5 review):* the watchdog runs in its own concurrency group `meca-watchdog`
(in `meca-bookings` a queued watchdog run would replace a pending bookings/approve run), so
it can overlap them. It therefore never writes `config/alerts.json`: its alerts go to its own
`config/watchdog_alerts.json`, committed by its own persist step. With two writers of one
file, a rebase conflict would make the losing run drop its whole state commit (orders.json,
seen_ids, last_update_id). `workflow_failed_<workflow name>` is resolved by
`preflight.guarded` when a run succeeds (inside the single writer group), not by the
watchdog. The dashboard (Task 6) reads and merges both `config/alerts.json` and
`config/watchdog_alerts.json`.

### Duplicate-order guard (in `parts_order.plan_and_stage`)
When a portal is available and the booking has just been queued (no order evidence), call
`portal.list_sent_orders()` once and look for a row for the same vehicle dated within the
last 7 days and not claimed by another record's `verified_numbers`
(`order_match.candidates`). Found -> `block` with reason "order finns redan i pro.meca.se
(…)", cart untouched, reason NOT in `PRE_CART_REASONS` (so it is not quietly re-staged),
Swedish message to the owner naming the booking and supplier/order numbers from the row.
A portal error while checking -> treated like any transient pro.meca.se error (blocked,
retried), never "go ahead".
*My call:* 7 days. A genuine second parts booking for the same plate within a week is
rare at 1-2 bookings a week; the message says how to proceed (order by hand).

### Dashboard (`web/`)
`config/alerts.json` joins the files the status endpoint reads. The view model gets
`alerts` (active only, whitelisted fields, max 20) and `lastSuccessAgeHours`; the page
shows a red banner above the orders list when an alert is active, and a stale-run warning
beyond `STALE_HOURS` (existing constant). Demo data gets one alert.

## Constraints (all tasks)

Python tests offline (`pytest -q`, currently 1356 pass), web tests `node --test` (106 pass).
Swedish owner messages; no raw exception text; no customer data in logs, alerts or commits
(fake data in tests). Never place real orders. Do not push. Commit trailer
`Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. `agent.py`/`portal_scraper.py`
keep CRLF line endings. Update root `SKILL.md` and `.claude/skills/meca-agent/SKILL.md`.
