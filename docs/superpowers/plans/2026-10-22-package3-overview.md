# Package 3: overview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One calm daily summary on Telegram, order-status tracking after ordering, a price/margin warning at staging, and history/search in the dashboard.

**Architecture:** A stdlib-only `summary.py` + workflow that only reads state; order-status tracking added to the existing order checker in `approve.py`; a pure `margin_line` helper used by the approval message; client-side filtering in the existing dashboard.

**Tech Stack:** Python 3.11 (stdlib for anything run before `pip install`), pytest, GitHub Actions, the no-framework `web/` (Node `--test`).

**Spec:** `docs/superpowers/specs/2026-10-22-package3-overview-design.md`

## Global Constraints

- Python suite `pytest -q` (1760 pass now) and `cd web && node --test` (122) stay green. Offline tests, fake data only, no live portals/Telegram/GitHub, never place real orders.
- Swedish fixed owner texts; no raw exception/page text; customer data never beyond booking number, plate, model, order numbers.
- No new ordering or approval authority; checkout, ceiling, swaps, approvals unchanged.
- `summary.py` imports only the standard library (plus the repo's stdlib-only `notifier`).
- `agent.py` keeps CRLF. Commit trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not push.

## Review Focus

- A quiet day, a day with 50 open items, orders.json missing/corrupt, alerts files missing: the summary never fails, never exceeds 3000 characters, never leaks anything but booking number/plate/order numbers.
- Status tracking: first sighting stores without a message; an unchanged status never repeats; a status change sends exactly one message and survives a failed send (retried next run, never lost, never doubled); rows not found, portal errors and a vanished order do nothing noisy.
- Tracking must not make approve run the browser forever: `outstanding_tracking()` ends after 21 days / Klar.
- Margin warning with unknown or odd prices (None, 0, text, quantity mismatches) gives no line rather than a wrong one; it never blocks staging.
- Dashboard search: text from state files rendered via `textContent` only; empty result state; filter does not break the stale/alert banners.

---

### Task 1: Daily summary

**Files:** Create `summary.py`, `tests/test_summary.py`, `.github/workflows/daily-summary.yml`; Modify `watchdog.py` (+ `tests/test_watchdog.py`).

**Interfaces:**
- Produces (stdlib only): `build_summary(orders: dict, alerts: list[dict], history: list[dict], now: datetime) -> str`; `load_state(root: Path = Path(".")) -> tuple[dict, list[dict], list[dict]]` (orders from `config/orders.json`, active alerts merged from `config/alerts.json` and `config/watchdog_alerts.json`, last 5 history rows; every missing or corrupt file gives empty data and a note line `OBS: <fil> kunde inte läsas`); `main(env=os.environ, now=None, root=Path(".")) -> int` (always 0).
- Sections (omit empty ones; Swedish): `Väntar på ditt godkännande` (pending), `Väntar på Klar` (ordered/blocked with outstanding Klar), `Beställt, ej verifierat` (ordered without verified_at), `Kräver åtgärd` (blocked/failed/expired within 7 days), `Larm` (active alerts, texts shortened to 120 chars), footer `Senaste lyckade körning: <date UTC>` or `Ingen körning registrerad`. Empty everything: `Inget väntar. Senaste lyckade körning: ...`. Cap 3000 chars with `+N till` collapse per section.
- Workflow `daily-summary.yml`: name `MECA Daily Summary`, cron `45 5 * * *`, `workflow_dispatch`, permissions `contents: read`, own concurrency group `meca-summary` (cancel-in-progress false), `timeout-minutes: 5`, steps: checkout, setup-python (no cache, nothing installed), `python3 summary.py` with only `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`. No persist step, no failure-alert step.
- `watchdog.py`: add a `summary` spec (`file="daily-summary.yml"`, daily 05:45 UTC, key `stale_summary`, label `dagssammanfattningen`, display name `MECA Daily Summary`) following the existing `SPECS` pattern.

- [ ] **Step 1: Failing tests:** pure `build_summary` for: quiet day; one item in each section; 60 pending items collapse to `+N till` and stay under 3000 chars; alert text truncated; no customer fields (assert a fake `driver_name` in a record never appears); `load_state` with missing and corrupt files (note lines, no raise); `main` sends exactly one message via a fake `notifier.send_message`, returns 0 when sending fails; YAML test (cron, permissions, group, timeout, secrets env only the two Telegram ones, no persist step); watchdog spec test (`last_slot` for the 05:45 daily slot, overdue boundary).
- [ ] **Step 2-4:** FAIL, implement, PASS + full suite. **Step 5: Commit** `feat(summary): daily Telegram overview and its watchdog entry`.

---

### Task 2: Margin warning

**Files:** Modify `rules.py`, `parts_rules.yaml` (commented example), `parts_order_helpers.py`, `parts_order.py`, `tests/test_rules.py`, `tests/test_parts_order.py`.

**Interfaces:**
- `Rules.min_margin_pct: int = 0` (new field LAST, default keeps positional constructions valid): int 0..90, not bool; invalid -> `ValueError` naming the key (fail closed).
- `margin_line(booking: dict, cart_total, rules) -> str | None` in `parts_order_helpers.py`: customer parts price = sum over `booking["parts"]` rows of unit `price` times qty (read how `portal_scraper._extract_specification` sets `price`/`qty` and whether a row `total` exists; use what is reliable; unparsable or missing price on ANY row -> None); purchase = `cart_total` (incl. VAT; the booking's prices are as shown by the MECA portal: record in the report whether they include VAT and whether the comparison is therefore apples to apples; if not comparable, say so and do not warn). Returns `OBS: inköp {purchase:.2f} SEK över bokningens delpris {customer:.2f} SEK` when purchase > customer, else `OBS: marginal {pct:.0f} % (under {min} %)` when `min_margin_pct > 0` and margin% < min, else None.
- `build_summary` caller in `_stage` adds the line after the totals; it also appears for swapped orders. It never blocks anything and is skipped in the length-guard fallback only if the text is too long (the guard drops it last).

- [ ] **Step 1: Failing tests:** loss -> line; exactly equal -> None; margin under min -> line; `min_margin_pct` 0 and profit -> None; None/0/text prices, qty strings like 'x2', mixed currencies -> None; invalid rules key -> ValueError; `_stage` message contains the line and staging/cart/ceiling are unaffected; no line in auto mode needed? (auto sends no approval text: assert no crash).
- [ ] **Step 2-4:** FAIL, implement, PASS + full suite. **Step 5: Commit** `feat(parts): warn when the purchase exceeds the booking's parts price`.

---

### Task 3: Order status tracking

**Files:** Modify `orders_state.py`, `order_match.py` (helper for finding a record's row), `approve.py`, `tests/test_orders_state.py`, `tests/test_approve.py`, `tests/test_order_match.py`.

**Interfaces:**
- `orders_state` optional fields `portal_status: str` (<= 80 chars) and `status_changed_at: str` (ISO), validated like the other extras, preserved by `put`/`update_fields`; `tracked_orders(now, max_age=timedelta(days=21)) -> list[tuple[str, dict]]`: `ordered`, has `verified_at` and `verified_numbers`, `updated_at`/`verified_at` within max age, not `klar_closed`; `outstanding_tracking(now=None) -> bool`.
- `order_match.find_row(rows, verified_numbers) -> dict | None`: the unique row whose supplier or order numbers (same tokens `claim_tokens` uses) are contained in the stored numbers; None for none or ambiguous.
- `approve.track_statuses(config, rows, now) -> bool` (True when every needed message was delivered): per tracked record find the row; `order_status` text sanitised to <= 80 chars; first sighting stores `portal_status` silently; a different value sends `Orderstatus {label}: {old} → {new}` (fixed Swedish template, status texts only) and stores it only AFTER the send succeeded (so a failed send is retried next run, never lost or doubled); unchanged -> nothing. Called from the same place and with the same `list_sent_orders()` rows the daily checker already reads (one read per run; if the checker does not read because nothing is unverified, read once when `outstanding_tracking()`); portal errors are logged by class only.
- `approve.py --check`: `has_pending` also true when `outstanding_tracking()`.

- [ ] **Step 1: Failing tests:** first sighting stores silently; change sends once and stores; unchanged silent; failed send keeps the old value (next run sends); row missing/ambiguous -> nothing; record older than 21 days or Klar-closed -> not tracked and `--check` false (subprocess test through `__main__` with no secrets as in `tests/test_approve_check_main.py`); status text with control characters/markup is sanitised; one `list_sent_orders` call per run when both the checker and tracking need it; exit code unaffected by undelivered status messages? (decide: a failed status message exits 1 like the checker's messages; say so in the report).
- [ ] **Step 2-4:** FAIL, implement, PASS + full suite. **Step 5: Commit** `feat(approve): track the portal's order status and tell the owner about changes`.

---

### Task 4: Dashboard search and status

**Files:** Modify `web/lib/summarize.js`, `web/public/render.js`, `web/public/app.js`, `web/public/index.html`, `web/public/style.css`, `web/lib/demo.js`, `web/README.md`, tests under `web/test/`.

**Interfaces:**
- `summarize`: each order gains `portalStatus` (whitelisted string or null) and `verifiedNumbers` (string or null); view model gains `counts: {waiting, klar, ordered, needsAction}`.
- UI: a text input `#order-search` and a select `#order-status-filter` above the orders list; filtering is client-side over the loaded orders, case-insensitive substring over booking id/number, plate, status, portalStatus, verifiedNumbers; shows `Inga träffar` when empty; counts shown at the top. Everything through `textContent`/`setAttribute`; no inline script/style; the alert banner and stale notes unaffected.

- [ ] **Step 1: Failing tests** (`node --test`): summarize fields and counts (missing fields, control characters); render filter function pure and tested (query, status filter, both, none); render uses textContent (grep-style test like the existing alert one); demo data contains portalStatus on one order.
- [ ] **Step 2-4:** FAIL, implement, `cd web && node --test` PASS (>= 122 + new). `npm run preview` still builds (not committed). **Step 5: Commit** `feat(web): search and status filter, portal status and counts`.

---

### Task 5: Docs and package verification

**Files:** Modify root `SKILL.md`, `.claude/skills/meca-agent/SKILL.md`, `README.md`, handoff doc (append "Package 3"), `parts_rules.yaml` comments.

- [ ] **Step 1:** Document: the daily summary (what it shows, schedule, how to run by hand), status tracking (what triggers a message, 21-day window, `has_pending` effect and cost), the margin line and `min_margin_pct`, the dashboard search. Keep root `SKILL.md` accurate (the weekly improve agent reads it).
- [ ] **Step 2:** `pytest -q`, `cd web && node --test`, `py_compile` on changed Python files, YAML parse of all workflows, `git status` clean, `file agent.py` CRLF, `parts_rules.yaml` loads with `mode == 'approve'`. Report outputs. Docs only: list inconsistencies, do not fix code.
- [ ] **Step 3: Commit** `docs: package 3 (daily summary, status tracking, margin warning, dashboard search)`.
