# Package 1: protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the MECA agent fail closed and loud: state guard, crash/failure alerts on Telegram and the dashboard, a dead-man switch, and a duplicate-order guard at staging.

**Architecture:** One small alert store (`alerts.py`, `config/alerts.json`) that every guard writes to; Telegram delivery is part of it. Guards are thin: `preflight.py` (state + crash), `alerts_cli.py` (workflow-level failures, stdlib), `watchdog.py` (scheduled, stdlib), and one check inside `parts_order.plan_and_stage`. The dashboard reads `alerts.json` like the other state files.

**Tech Stack:** Python 3.11 (stdlib for anything run before `pip install`), pytest, GitHub Actions, the existing no-framework `web/` (Node `--test`).

**Spec:** `docs/superpowers/specs/2026-10-21-package1-protection-design.md`

## Global Constraints

- Python tests offline: `pytest -q` (1356 pass now); web: `cd web && node --test` (106 pass now). Never lower either count.
- Owner messages in Swedish, fixed texts only: no raw exception text, page text, or customer data (booking number, plate, order number are the only identifiers allowed). Tests use fake data.
- `alerts_cli.py` and `watchdog.py` import only the standard library (they run before `pip install`).
- Never place a real order, never push, never touch live portals from tests.
- `agent.py` keeps CRLF line endings (verify with `file agent.py` before and after editing; editing tools may write LF, so convert back).
- Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- State files are committed back by the workflows' persist steps; `config/alerts.json` must be added to those loops.

## Review Focus

- `alerts.json` missing, empty, or corrupt (a crash mid-write): never raises, never loses the Telegram message; corrupt file is renamed aside.
- Telegram down while raising an alert: dashboard state still written, return value says delivery failed, no exception.
- Alert storm: the same key raised on every run sends at most one Telegram message per 24 h.
- The guard must not alert on a legitimate fresh install: `orders.json` absent AND no `seen_ids` is still a problem for ordering but the message must say "saknas", not crash.
- Watchdog on a Sunday and across the UTC date line: no false alarm on Sunday for `bookings.yml` (Mon-Sat), none at 14:00 UTC on a weekday before the slot has passed.
- Duplicate guard: a sent order for the same plate older than 7 days must NOT block; a row claimed by another record must NOT block; a portal error must not be read as "no duplicate found" (fail closed).

---

### Task 1: Alert store

**Files:**
- Create: `alerts.py`, `tests/test_alerts.py`

**Interfaces:**
- Produces:
  - `ALERTS_FILE = Path("config/alerts.json")`, `RESEND_AFTER = timedelta(hours=24)`
  - `raise_alert(config: dict, key: str, text: str, severity: str = "error", now: datetime | None = None) -> bool` returns True when the Telegram message was sent or intentionally suppressed (already sent < 24 h ago); False when delivery was attempted and failed. Never raises.
  - `resolve(key: str, now: datetime | None = None) -> bool` marks `resolved_at` if the alert exists and is unresolved; returns whether anything changed. Never raises.
  - `active() -> list[dict]` unresolved alerts as `{"key", "text", "severity", "first_seen", "last_seen", "count"}`, newest `last_seen` first. Never raises (corrupt file -> `[]`).
- Consumes: `notifier.send_message(config, text) -> bool`.

- [ ] **Step 1: Write failing tests** in `tests/test_alerts.py` (use `tmp_path` + `monkeypatch.setattr(alerts, "ALERTS_FILE", ...)` and a fake `notifier.send_message`):
  - `test_raise_creates_alert_and_sends_once`: first call writes the alert with `count == 1`, sends one message; assertion on file content and send count.
  - `test_same_key_within_24h_is_not_resent`: second call 1 h later: `count == 2`, `last_seen` updated, send count still 1, returns True.
  - `test_resent_after_24h_while_unresolved`: call 25 h later: send count 2.
  - `test_resolve_then_raise_again_sends_again`: after `resolve(key)`, `active()` is empty; raising again sends and creates a fresh `first_seen`.
  - `test_telegram_failure_still_records_alert`: fake send returns False: `raise_alert` returns False, alert present in `active()`, no exception; a later call retries sending (because `last_sent` was not set).
  - `test_corrupt_file_is_renamed_and_replaced`: write `{not json`; `raise_alert` works, `alerts.json.corrupt` exists, `active()` has the new alert.
  - `test_missing_file_active_is_empty_and_resolve_is_noop`.
  - `test_text_is_truncated_and_control_chars_removed`: 2000-char text with `\x00` is stored at most 500 chars without control characters; the Telegram message is the full clean text up to 500 chars.
- [ ] **Step 2: Run** `pytest tests/test_alerts.py -q` -> FAIL (module missing).
- [ ] **Step 3: Implement `alerts.py`** per the interfaces. Write atomically (temp file + `os.replace`), UTF-8, `ensure_ascii=False`, LF newlines. Timestamps UTC ISO with `+00:00`. Message sent is `"⚠️ " + text` for `error`, `"ℹ️ " + text` for `warning`.
- [ ] **Step 4: Run** `pytest tests/test_alerts.py -q` -> PASS; then the whole suite.
- [ ] **Step 5: Commit** `feat(alerts): alert store with Telegram delivery and 24 h de-duplication`.

---

### Task 2: State guard and crash guard

**Files:**
- Create: `preflight.py`, `tests/test_preflight.py`
- Modify: `agent.py` (CRLF!), `approve.py`, `tests/test_agent_notify.py` and/or `tests/test_approve.py` only where existing tests break

**Interfaces:**
- Consumes: `alerts.raise_alert`, `alerts.resolve`, `orders_state` (`ORDERS_FILE`, `_validate`/`_load` behaviour: raises on invalid).
- Produces:
  - `state_problems() -> list[str]`: Swedish fixed strings. `config/orders.json` missing -> `"config/orders.json saknas"`; present but `orders_state._load()` raises -> `"config/orders.json kan inte läsas"`.
  - `guarded(name: str, fn: Callable[[], int], config_loader=load_config) -> int`: runs `fn()`; on exception raises alert `crash_<name>` with text `"Körningen av <name> kraschade (<ExceptionClassName>) – se GitHub Actions-loggen."`, returns 1; on `fn()` returning 0 resolves `crash_<name>` and `run_failed_<name>`; on non-zero return raises `run_failed_<name>` with text `"Körningen av <name> slutade med fel – se GitHub Actions-loggen."` and returns the code. If loading the config itself fails, only logs (no alert possible).
  - `check_state_or_alert(config) -> bool`: True when no problems; otherwise raises alert `state_unreadable` with the problems joined, returns False; resolves `state_unreadable` when OK.
- `agent.run()`: after `load_config()`, `if not check_state_or_alert(config): _log_summary(...) ; return 1` before any portal contact. `approve.py`'s main run: same check before handling taps. Both `if __name__ == "__main__"` blocks become `sys.exit(guarded("booking_agent", run))` / `guarded("approve", main)`.
- `approve.py --check` stays stdlib-only and untouched.

- [ ] **Step 1: Write failing tests** `tests/test_preflight.py`:
  - `test_state_problems_missing_file`, `test_state_problems_corrupt_file`, `test_state_ok_for_valid_skeleton` (`{"last_update_id": null, "bookings": {}}`).
  - `test_guarded_exception_raises_crash_alert_and_returns_1`: alert text contains the class name but not the exception message (use an exception whose message is `"SECRET-PAGE-TEXT"`).
  - `test_guarded_success_resolves_alerts`, `test_guarded_nonzero_raises_run_failed_alert_and_keeps_code`.
  - `test_check_state_or_alert_blocks_and_alerts` and `..._resolves_when_fixed`.
  - In the agent tests: `test_agent_run_stops_before_portal_when_orders_json_missing`: `fetch_bookings` is never called (use a sentinel that fails the test), exit code 1, alert `state_unreadable` active.
  - Same for `approve` main: no Telegram `getUpdates` processing happens when the state is unreadable.
- [ ] **Step 2: Run** the new tests -> FAIL.
- [ ] **Step 3: Implement** `preflight.py` and the two integrations. Preserve CRLF in `agent.py`.
- [ ] **Step 4: Run** the full suite -> PASS (existing tests that run `agent.run()` need a valid `orders.json` skeleton fixture in their temp dir: add it in the shared fixture, not in each test).
- [ ] **Step 5: Commit** `feat(preflight): fail closed on unreadable order state; alert on crashes and failed runs`.

---

### Task 3: Workflow-level failure alerts

**Files:**
- Create: `alerts_cli.py`, `tests/test_alerts_cli.py`
- Modify: `.github/workflows/bookings.yml`, `.github/workflows/approve.yml`

**Interfaces:**
- Produces: `python alerts_cli.py workflow-failed <workflow-name>`. stdlib only (imports `alerts` and `notifier`, which are stdlib-only; verify neither imports a third-party package at module level). Reads `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY`, `GITHUB_RUN_ID` from the environment; raises alert key `workflow_failed_<name>` with text `"GitHub-körningen <name> misslyckades: <run url>"`; exit code 0 always (an alert helper must not mask or add failures); a missing token is logged, exit 0.
- Workflows: new final step `Alert on failure` with `if: ${{ failure() }}` after "Upload logs"/persist, and the persist step must run after it so `config/alerts.json` is committed: order is `... -> Alert on failure (if failure()) -> Persist state (if always())`. Add `config/alerts.json` to the persist loops (`bookings.yml` loop list; `approve.yml` `git add` line). The step passes only the two Telegram secrets (no portal secrets).

- [ ] **Step 1: Failing tests** `tests/test_alerts_cli.py`: sends one message containing the run URL and the workflow name; writes an active alert; exits 0 when the token is missing; never prints the token (capture stdout/stderr and assert absence); `test_workflows_list_alerts_json_in_persist` reads both YAML files as text and asserts `config/alerts.json` appears in each persist step and that `Alert on failure` precedes `Persist state back to repo` in both.
- [ ] **Step 2: Run** -> FAIL. **Step 3: Implement** `alerts_cli.py` and edit both workflows (keep existing comments; add a comment explaining why the step exists). **Step 4: Run** full suite. 
- [ ] **Step 5: Commit** `feat(workflows): alert on workflow-level failures (installs, timeouts, crashes)`.

---

### Task 4: Dead-man switch

**Files:**
- Create: `watchdog.py`, `tests/test_watchdog.py`, `.github/workflows/watchdog.yml`

**Interfaces:**
- Produces (stdlib only):
  - `SPECS = {"bookings": Spec(file="bookings.yml", days=(0,1,2,3,4,5), hour=15, key="stale_bookings", label="bokningskontrollen"), "approve": Spec(file="approve.yml", days=(0,..,6), hour=6, key="stale_approve", label="godkännandekontrollen")}` (`days` are `datetime.weekday()` values).
  - `last_slot(now: datetime, spec: Spec) -> datetime`: latest slot `<= now` (UTC, timezone-aware).
  - `is_overdue(now, last_success: datetime | None, spec, grace=timedelta(hours=3)) -> bool`: True when `now > last_slot + grace` and (`last_success is None` or `last_success < last_slot - timedelta(minutes=30)`). Note GitHub may start a cron run late: a run that started up to 30 min before the slot is not a miss.
  - `fetch_last_success(repo: str, token: str, workflow_file: str, urlopen=urllib.request.urlopen) -> datetime | None` reading `created_at` of the newest success; raises `WatchdogError` on HTTP or parse problems.
  - `main(env, now=None, urlopen=...) -> int`: for each spec: API error -> log, no alert, continue; overdue -> `alerts.raise_alert(config, spec.key, "Ingen lyckad körning av <label> sedan <date UTC>. Kontrollera GitHub Actions.")`; fresh -> `alerts.resolve(spec.key)`. Always returns 0. Config built from `TELEGRAM_*` env only.
- Workflow `watchdog.yml`: cron `17 */6 * * *` + `workflow_dispatch`; permissions `contents: write`, `actions: read`; concurrency group `meca-bookings`, `cancel-in-progress: false`; steps: checkout, setup-python (no pip cache, nothing installed), run `python watchdog.py` with `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, Telegram secrets, then the standard persist step for `config/alerts.json` (message `chore: update alert state [skip ci]`, same 3-attempt pull-rebase-push loop). `timeout-minutes: 5`.

- [ ] **Step 1: Failing tests** `tests/test_watchdog.py` with fixed datetimes (2026-10-21 is a Wednesday): `last_slot` Wednesday 14:00 -> Tuesday 15:00 (bookings); Wednesday 15:30 -> Wednesday 15:00; Sunday 20:00 bookings -> Saturday 15:00; Monday 10:00 bookings -> Saturday 15:00; approve Sunday 07:00 -> Sunday 06:00. `is_overdue`: Wednesday 14:00 with Tuesday-15:02 success -> False; Wednesday 18:30 with Tuesday-15:02 success -> True; Wednesday 15:20 (inside grace) -> False; Sunday 20:00 with Saturday-15:05 success -> False (no Sunday slot for bookings); success 20 min before the slot counts as fresh; `last_success None` after grace -> True. `main`: stale -> one Telegram message + alert; fresh -> resolves; API error (`urlopen` raising `URLError`/returning bad JSON) -> no alert, return 0; token never appears in logs/stdout.
- [ ] **Step 2-4:** run FAIL, implement, run PASS + full suite. A YAML sanity test asserts the cron string, the permissions and the concurrency group.
- [ ] **Step 5: Commit** `feat(watchdog): dead-man switch for the booking run and the approval poll`.

---

### Task 5: Duplicate-order guard at staging

**Files:**
- Modify: `parts_order.py`, `parts_portal.py` only if needed (no behaviour change to `list_sent_orders`), `order_match.py` (only to expose a helper if needed), `tests/test_parts_order.py`
- Reuse: `PartsPortal.list_sent_orders() -> list[dict]`, `order_match.candidates(rows, vehicle_model, order_date, claimed, reg_nr=...)` (read its real signature before using it), `orders_state.by_status`, `claim_tokens`.

**Interfaces:**
- Produces in `parts_order.py`: `_existing_sent_order(portal, booking, now) -> dict | None` (returns the matching row or None; raises `PartsPortalError` through) and its use at the start of `_stage`, after the "any lines to order?" decision and before the first cart action: when portal is not None and the record is a fresh queue (`tries == 0`) or any non-restaged start, call it. Found -> `block("order finns redan i pro.meca.se (<supplier/order numbers from the row, as numbers_text(row)>)")` with `cart_touched` false; the reason string must NOT start with any `PRE_CART_REASONS` entry. A `PartsPortalError` -> the existing transient path (`TRANSIENT_REASON` handling), i.e. blocked/retry, never proceeding.
- Window: `DUPLICATE_WINDOW_DAYS = 7`, row date `>= now.date() - 7 days`, rows claimed by other records (`claim_tokens` of every record's `verified_numbers`) excluded, and rows are matched by plate when the registration column exists, else by model words (use the existing `candidates` logic with `order_date = today - 7 days`).
- Owner message (Swedish): `"Bokning <nr>: en skickad order för fordonet finns redan i pro.meca.se (<numbers>). Jag har inte förberett någon ny order. Beställ för hand om delarna ändå behövs."`

- [ ] **Step 1: Failing tests** in `tests/test_parts_order.py` using the existing fake portal helpers: matching recent row blocks staging, cart methods never called, record `blocked` with `cart_touched` false, `restage()` returns False for it, message contains the numbers; row older than 7 days -> staging proceeds; row for another plate -> proceeds; row claimed by another record's `verified_numbers` -> proceeds; `list_sent_orders` raising `PartsPortalError("search_timeout")` -> blocked via the transient path, no cart action, retried by `restage`; portal `None` (no portal needed) -> guard skipped.
- [ ] **Step 2-4:** FAIL, implement, PASS + full suite. **Step 5: Commit** `feat(parts): never stage an order when a recent sent order for the vehicle exists`.

---

### Task 6: Dashboard alerts

**Files:**
- Modify: `web/lib/summarize.js`, `web/lib/handlers.js`, `web/lib/github.js` (add the file to the fetch list), `web/lib/demo.js`, `web/public/render.js`, `web/public/index.html`, `web/public/style.css`, `web/README.md` ("Data freshness" and "Files" sections), and the matching tests under `web/test/`.

**Interfaces:**
- `summarize` input gains the raw text of `config/alerts.json` (null when missing); output gains `alerts: [{key, text, severity, firstSeen, lastSeen, count}]` (unresolved only, sorted by `lastSeen` desc, at most 20, `text` whitelisted/length-limited like other strings, severity coerced to `error|warning`) and `lastSuccessAgeHours: number | null` derived from the newest history entry without `login_failed`/with exit-neutral counters (use the newest entry whose `login_failed` is falsy; null when none). Bad JSON adds a `problems` entry and `alerts: []`, never throws.
- UI: when `alerts.length`, a banner section `#alerts` above the orders list (red for `error`, amber for `warning`), each item showing text and "sedan <relative time>"; plus the existing stale warning is driven by `lastSuccessAgeHours > STALE_HOURS`. All text via `textContent` (CSP is strict; no inline script or style).
- Demo (`--demo`) data includes one `warning` alert so the screenshot shows the banner.

- [ ] **Step 1: Failing tests** in `web/test/`: summarize returns active alerts only, drops resolved, caps at 20, survives corrupt JSON and missing file, strips control characters; `lastSuccessAgeHours` computed from a fixed `now`; handler `status` includes the alerts file in its GitHub fetch (use the existing fake transport) and the demo transport returns an alert; a render test that an `error` alert produces a banner element with the alert text and uses `textContent` (assert no `innerHTML` assignment with alert text, grep-style test is acceptable).
- [ ] **Step 2-4:** FAIL, implement, `cd web && node --test` PASS (>= 106 + new). Rebuild the preview if the repo's `npm run preview` is quick (`cd web && npm run preview`), but do not commit the preview file (gitignored).
- [ ] **Step 5: Commit** `feat(web): show active alerts and last successful run age on the dashboard`.

---

### Task 7: Docs, skills, and whole-package verification

**Files:**
- Modify: root `SKILL.md`, `.claude/skills/meca-agent/SKILL.md`, `README.md`, `docs/superpowers/plans/2026-10-21-parts-ordering-handoff.md` (append a short "Package 1" section)

- [ ] **Step 1:** Document: alerts store and keys (`state_unreadable`, `crash_<name>`, `run_failed_<name>`, `workflow_failed_<name>`, `stale_bookings`, `stale_approve`), the preflight rule (missing `orders.json` stops ordering), the watchdog schedule and its limit (cannot see GitHub disabling schedules), the duplicate guard (7 days, how to proceed by hand), `config/alerts.json` in the persist loops. Keep root `SKILL.md` accurate: it is the weekly improve agent's only picture of the system.
- [ ] **Step 2:** Run `pytest -q` and `cd web && node --test`; report counts. Run `python -m py_compile` on every changed Python file.
- [ ] **Step 3:** `git status`: nothing unexpected (no `config/alerts.json` with real data, no scratch files). Confirm `file agent.py` still says CRLF.
- [ ] **Step 4: Commit** `docs: package 1 (alerts, state guard, watchdog, duplicate guard)`.
