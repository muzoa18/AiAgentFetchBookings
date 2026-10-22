# MECA Booking Agent — Skill File

Context file read by `improve.py` on every weekly review, and the project's agent context.
Keep it in sync with the code; anything described here that does not exist in the repo will
mislead the improve agent.

## What the system does
MD Bilreparationer (a MECA workshop in Eskilstuna) receives digital bookings through the MECA
ProMeister portal (meca.promeisterportal.com). The system, end to end:

1. **Daily booking run** (`agent.py`): logs into the portal, reads every booking listed on the
   **Nya** tab, and sends the owner **one Telegram message** per booking with all the details.
2. A `Bokning` **without spare parts** is marked **Hanterad → Bekräfta** right away.
3. A `Bokning` **with spare parts** gets a parts order **staged** on pro.meca.se (the MECA parts
   shop): each part is matched in the vehicle's linked parts and put in the cart, and the owner
   gets a Telegram message with the parts, prices, cart total and fluids to prepare, plus
   **Godkänn** / **Avbryt** buttons (Approve / Cancel). A part that is out of stock or clearly
   too expensive can come with an alternative and a **Byt <part>** button (see "Alternatives
   and the swap").
4. **Daily approval run** (`approve.py`): reads the owner's taps. Approve → re-check the cart and
   check out, then look the order up right away in "Skickade Beställningar"; Cancel → clear the
   cart; Byt → swap one line to its alternative and send a new approval message. Every resolution message carries a **Klar** button; a Klar
   tap marks the booking Hanterad in the MECA portal.
5. **Weekly self-improve** (`improve.py`): Claude reviews code + logs and opens a Pull Request.
6. **Daily summary** (`summary.py`): one Telegram overview a day of what waits for the owner.
   After an order is placed, `approve.py` also follows the portal's order status and tells the
   owner when it changes (see "Daily summary" and "Order status tracking").

Everything runs on **GitHub Actions** (no local machine, no Task Scheduler). Every booking with
parts ends in a message saying ordered, blocked or failed: nothing fails silently.

## File map
```
agent.py                 Daily booking run: scrape → notify once → Hanterad (no parts) or stage order
approve.py               Daily poller: Approve / Cancel / Byt / Klar taps, 48 h expiry, Klar
                         withdrawal, order checker (verifies placed orders in "Skickade Beställningar"),
                         order status tracking (track_statuses), one shared list read (_SentOrders)
summary.py               Daily Telegram overview (stdlib + notifier only; reads the state files, writes
                         nothing, no browser; exit 1 if the send failed)
order_match.py           Order checker matching (pure): car model / date / registration → one row
notifier.py              Telegram Bot API client (stdlib): messages, inline buttons (one row or rows),
                         getUpdates
portal_scraper.py        MECA portal (Playwright): login, Nya tab, detail page, Hanterad → Bekräfta
parts_order.py           Plan, stage (cart), approval message, swap restage, execute_order (checkout),
                         _verify_placed -> _verified_row (immediate order lookup; verify_now is
                         the test-facing wrapper of the same search)
parts_order_helpers.py   Pure helpers: quantities, cart/total checks, Swedish message text, alternatives,
                         margin warning (parts_price, margin_line, VAT_FACTOR)
parts_matcher.py         Pure matching: brand rank, ProMeister/fluid/placement filters, Claude picker,
                         stock-aware choice and alternatives (choose_with_alternatives)
parts_portal.py          pro.meca.se (Playwright): login, load vehicle, search, cart, checkout
parts_portal_parse.py    Pure HTML parsers for pro.meca.se pages (fixture-tested)
orders_state.py          config/orders.json: one status per booking, never order twice
rules.py                 Loads + validates parts_rules.yaml (fails closed)
parts_rules.yaml         Owner-edited rules: brand_prefer, brand_forbid, ceiling_sek, mode, plus optional
                         brand_prefer_by_part, prefer_in_stock, expensive_*, max_alternatives,
                         min_margin_pct
config_loader.py         Env-first config (GitHub Secrets) with config/config.yaml fallback
alerts.py                Alert store config/alerts.json: Telegram + dashboard, 24 h de-dup, never raises
preflight.py             Fail-closed guards: state_problems / check_state_or_alert, guarded(name, fn)
alerts_cli.py            Workflow-level failure alert (stdlib only), called by the workflows
watchdog.py              Dead-man switch (stdlib only): stale bookings / approve / summary run -> alert
web/                     Read-only owner dashboard (Node): active alerts, last successful run age,
                         four count tiles, orders list with search box + status filter
state.py                 config/seen_ids.txt (notified once) + no-Hanterad-button bookkeeping
run_logger.py            Appends one structured line per run to logs/run_history.jsonl
improve.py               Weekly self-improve → PR (file allowlist enforced in code; rest = suggestions)
inspect_portal.py        Dev helper: inspect the portal's HTML when selectors break
config/config.example.yaml   Template; env var name for each field is in its comments
tests/                   Offline pytest suite (fixtures for both portals' HTML)
```
Not present any more: `sms_sender.py` (46elks SMS), `email_trigger.py` (Gmail gate),
`extractor.py`, `parts_agent.py`, `email_agent.py`, Windows Task Scheduler jobs. Do not
reference or recreate them.

## Booking loop rules (agent.py)
- **The portal is the truth for a `Bokning`.** Listed on the Nya tab = not handled. Local files
  never decide "handled"; `config/seen_ids.txt` only means "the owner was notified" (log text
  "Already notified").
- **Notify once.** Every run works on every listed `Bokning`, quietly: no new message unless the
  outcome changes. A failed Telegram send leaves the booking unseen (retried next run) and makes
  the run exit 1.
- **No spare parts** (no non-fluid part lines; fluids are only listed): message once, then
  Hanterad → Bekräfta immediately, retried every run until it succeeds. A failed Hanterad is
  reported and the run exits 1.
- **With spare parts:** Hanterad is NOT pressed by agent.py. The order flow runs (staging, then
  approval in approve.py); only the owner's **Klar** tap marks it Hanterad.
- A `blocked` order whose cart was never touched (e.g. "ingen säker träff") is re-staged
  automatically each run, at most 10 tries, with a message only when the outcome changes.
  Blocked-after-cart-touched, failed, cancelled, expired, pending, attempted and ordered bookings
  are never retried automatically.
- Specification not read completely (`spec_fallback` / `spec_dropped`): Hanterad is not pressed;
  the owner is told to check the booking.
- **Offert / Förfrågan** (no Hanterad button): local storage handles them — notified once, then
  only a PENDING line in the log while still listed. A booking whose type was only guessed and
  that shows no Hanterad button on two runs in a row gets one notice and is then handled like
  Offert/Förfrågan (`config/no_button_seen_once.txt` → `config/no_button_ids.txt`).
- Crash recovery: a booking is queued in `config/orders.json` right after its notification;
  any record still `attempted` at the start of the next run is reported and marked `failed`.

### Telegram message (booking notification)
```
Ny Bokning - OLRD-0001
Kund: Test Kund
Tel: +46700000001
Fordon: TST001 VOLKSWAGEN PASSAT
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

## Approval and Klar (approve.py)
- An order is placed only if the tap comes from the owner (`telegram.chat_id`, a **private** chat,
  never a group), on that booking's own approval message (stored message id), the booking is
  still `pending`, and the approval is not older than **48 h** (checked at poll time).
- `execute_order` re-checks the live cart against the stored plan and the ceiling, records
  `attempted` before checkout, then places the order and reports the result (see "Immediate
  verification" for what follows the Beställd message).
- Cancel clears the vehicle's cart (best effort). An expired approval leaves the cart for the
  owner to clear.
- **Klar** is accepted with the same owner checks, only once the order is resolved (never while
  `pending` or `attempted`). A failed Hanterad keeps the button; a missing Hanterad button counts
  as already handled by hand. Klar buttons older than 14 days are withdrawn once with a notice.
- **Byt <part>** (`swap:<booking_id>:<index>`, index = position in the plan's parts) is accepted
  with the same owner, message-id, `pending` and 48 h checks, at most **2 swaps per booking**
  and only for a line with a stored alternative that the matcher's filters still allow (and
  whose swapped total stays under the ceiling). See "Alternatives and the swap".
- `python approve.py --check` (stdlib only) prints `has_pending=true|false`; the workflow skips
  installing Playwright when nothing is pending.

## Alerts and guards (alerts.py, preflight.py, alerts_cli.py, watchdog.py)
- **Alert store** `config/alerts.json` (`{"alerts": {key: {text, severity, first_seen, last_seen,
  count, last_sent, resolved_at}}}`, no customer data). `alerts.raise_alert` records the alert and
  sends one Telegram message at most every 24 h per key (an unresolved alert is re-sent after 24 h);
  `alerts.resolve` closes it. Nothing in `alerts.py` ever raises. A corrupt file is renamed to
  `alerts.json.corrupt` and starts empty.
- **State guard** (`preflight.check_state_or_alert`, alert key `state_unreadable`): a missing or
  unreadable `config/orders.json` stops ALL ordering before the portal is contacted and the run
  exits 1 (`orders_state._load()` alone treats a missing file as empty, which once re-staged an
  order that was already placed). agent.py calls it first in `run()` and logs
  `state_unreadable: true` in run_history. approve.py checks it only in the full run ("Handle
  approvals"), which runs only when `approve.py --check` printed `has_pending=true`. `--check` runs
  first, outside `guarded`: with a MISSING orders.json it sees an empty state, prints
  `has_pending=false` and the approve run ends green without any alert (the next booking run's
  guard raises `state_unreadable`); with a CORRUPT file `--check` exits 1, the step fails and the
  owner gets only the generic `workflow_failed_MECA Approval Poller`. The full run resolves
  `state_unreadable` once the file is back.
- **Run guard** (`preflight.guarded(name, fn)`, wired around `agent.py` as `booking_agent` and
  `approve.py` as `approve`, but not around `approve.py --check`): an exception raises
  `crash_<name>`, a non-zero exit raises `run_failed_<name>` unless this run already raised
  `state_unreadable` or `crash_<name>` (one fault, one message); a clean exit resolves both and
  `workflow_failed_<workflow name>` ("MECA Booking Agent" / "MECA Approval Poller") without
  loading the config. A config that cannot load (`load_config` calls `sys.exit`, contained by
  `preflight._config`) only means no alert can be sent. Alert texts are fixed Swedish strings
  with the exception class name only, never the message.
- **Workflow alert** (`alerts_cli.py workflow-failed <name>`, step "Alert on failure",
  `if: failure() || cancelled()`, in bookings.yml and approve.yml): key
  `workflow_failed_<workflow name>`; stdlib only. It covers a failed install, the main step's own
  `timeout-minutes` ("Run booking agent" 13, "Handle approvals" 8: the step fails, so `failure()`
  fires), the job timeout (20 / 15 min: that is a cancel, hence `cancelled()`; a manual cancel
  alerts too) and a crash before our code starts. Suppressed when this workflow's
  `state_unreadable`, `crash_<short>` or `run_failed_<short>` is active and was last seen within
  20 min (this run already told the owner). It runs before the persist step; always exits 0.
- **Mark OK** (`alerts_cli.py workflow-ok <name>`, `if: success()`, after the agent/approve step and
  before the persist step, env WORKFLOW_NAME only): resolves `workflow_failed_<name>`,
  `run_failed_<short>`, `crash_<short>`. Needed because most approve runs stop after `--check`,
  which runs outside `guarded`.
- **Watchdog** (`watchdog.py`, watchdog.yml, cron `17 */6 * * *`, own concurrency group
  `meca-watchdog`): asks the GitHub API when bookings.yml, approve.yml and daily-summary.yml last
  succeeded. Alerts
  `stale_bookings` (expected Mon-Sat 15:00 UTC), `stale_approve` (daily 06:00 UTC) and
  `stale_summary` (daily-summary.yml, daily 05:45 UTC; `Spec.minute` = 45) when the
  last slot is more than 3 h past with no success; it resolves them when fine. A GitHub API error
  never raises an alert. It writes ONLY `config/watchdog_alerts.json` (so its state commit cannot
  conflict with bookings/approve, which write `config/alerts.json`). Limit: it cannot detect
  GitHub disabling scheduled workflows after 60 days of repo inactivity (the watchdog is itself
  scheduled).
- **Dashboard** (`web/`): shows a banner with the active alerts from BOTH files and warns when the
  last successful run (run_history entry with neither `login_failed` nor `state_unreadable`; agent.py
  logs `state_unreadable: true` when the state guard stops it) is older than
  `LAST_SUCCESS_STALE_HOURS` = 52 h (Saturday 15:00 to Monday 15:00 is 48 h with no Sunday run).
  The commit-age warning keeps `STALE_HOURS` = 36. Both reach the page in the view model.
  Orders list, tiles and search: see "Dashboard: tiles and search" below.

## Alternatives and the swap (parts_matcher.py, parts_order.py, approve.py)
- **Optional rules** in `parts_rules.yaml` (defaults apply when absent; strict validation, a bad
  value makes the whole file invalid and fails closed): `brand_prefer_by_part` (part word → brand
  list, tried before `brand_prefer`; a key matches on whole normalised words of the booking line,
  Pollenfilter = Kupéfilter, first key in file order wins; duplicate keys rejected),
  `prefer_in_stock` (true), `expensive_ratio` (1.5, a finite number > 1), `expensive_min_diff_sek`
  (100, integer ≥ 0), `max_alternatives` (1, integer 0-3). **At most ONE alternative is ever
  offered** whatever `max_alternatives` says (one picker call confirms one candidate); 0 turns
  alternatives off.
- **Stock-aware choice** (`choose_with_alternatives`, only in `approve` mode): when the pick is
  out of stock (`in_stock` is False; unknown stock is never "slut") and `prefer_in_stock` is on,
  it is replaced by an in-stock candidate that passes the same filters and the picker's
  confidence gate, in two stages (preferred brands first, then every in-stock candidate). The
  message line then ends `Bytt från <title> (slut)`. The owner still approves; the replacement
  goes through the ceiling and cart checks like any part. `auto` mode never replaces and never
  offers alternatives.
- **Alternatives:** a part is flagged **slut** (out of stock and no replacement) or **dyr** (a
  confirmed in-stock candidate costs ≤ price / `expensive_ratio` and saves at least
  `expensive_min_diff_sek` and more than 0). The alternative has to pass the usual filters (never
  ProMeister, fluids, forbidden brands, non-Latin titles, wrong placement) and the picker's
  confidence (≥ 0.8). Message: `Alternativ för <del>: <titel> (<märke>) à <sek>[ – i lager]
  (slut | dyr: sparar N SEK/st)`. It is stored in the plan (`alternative`, `replaced_from`) and is
  never added to the cart.
- **Buttons:** `[Godkänn, Avbryt]`, plus one row `Byt <part, 20 tecken>` per flagged line. With no
  flagged line the message and its single button row are exactly as before. No swap rows once the
  booking has been swapped twice, when a button's callback data would exceed 64 bytes, or when
  the alternative could not be pinned. A message over 4000 characters drops the alternative block
  and swap rows (then the `Bytt från` notes); Godkänn/Avbryt always stay.
- **Swap flow** (`approve._swap` → `parts_order.restage_with_pins`): checks (owner, record
  `pending`, message id, `swaps` < 2, line + alternative, ceiling estimate) happen BEFORE
  anything is touched. Refusal answers differ per case (each changes nothing): `swap_problem`
  refusals `SWAP_REFUSED` ("Bytet är inte längre möjligt."), an old message "Knappen är inte
  längre giltig.", a non-pending record "Redan hanterad.", an expired one "Godkännandet har gått
  ut.". Taps are handled once a day, so the callback answer may arrive late: swap refusals,
  deferred swaps and stale approval-message buttons are ALSO sent as a plain message
  (`Byte för <nr>: …`, `STALE_TEXT`). **Run cap:** a swap only starts if
  `_run_start + SWAP_RUN_S (400 s) - now >= SWAP_BUDGET_S (240 s)`; otherwise it is deferred
  (`SWAP_DEFERRED`, "Bytet hinns inte med i den här körningen – tryck Byt igen vid nästa
  körning."), nothing changes, the tap is consumed (offset advanced) and the old buttons stay
  live for a new tap. Then: clear the vehicle's cart (a failure withdraws the old approval: its
  buttons are cleared, the record becomes `blocked` with `cart_touched` (never re-staged) and
  `SWAP_CLEAR_FAILED` is sent with Klar: the cart may be partly emptied), clear the
  old message's buttons, record `attempted` + `swaps + 1` + `cart_touched`, restage with the
  alternative pinned for that line and every other line as staged (no new picking), verify the
  cart and ceiling as in normal staging, and send a NEW approval message (new `message_id`; the
  record is `pending` again). A swap **never orders**. Its own budget is **240 s** from the
  start of its handling (`SWAP_BUDGET_S`, cart clear included), capped at run start + 400 s.
  After the cart check, the swapped line's live cart title (matched by `url_or_id`) must equal
  the alternative's (normalised); otherwise the swap fails closed (another article). Each swap refreshes the approval's 48 h window (two
  swaps can keep one approval open for up to about 3 × 48 h). The new message shows the unit
  price from the live cart and `Kund: se bokning <nr>` (no customer data is stored).
- **A failed swap** ends in a truthful Swedish message and the booking is no longer `pending`
  (never retried automatically): `Bytet misslyckades – varukorgen är tömd eller delvis fylld,
  kontrollera pro.meca.se` (emptied or unknown), `… varukorgen innehåller delarna, kontrollera
  pro.meca.se` (every add worked but the ceiling, cart check or the message failed), or `Bytet
  misslyckades – kontrollera pro.meca.se` followed by the lines for what is in the cart and what
  was not added, closed by "Varukorgen tömdes för bytet och innehåller nu bara det som står ovan
  – kontrollera den på pro.meca.se." The owner then orders by hand.

## Immediate verification (parts_order._verify_placed, after checkout)
(Production path: `execute_order` → `_verify_placed` → `_verified_row`. `verify_now` runs the
same `_verified_row` search as a standalone, test-facing function.)
- After `checkout()` and the **Beställd** message (sent first and unchanged, "bekräftas senare"
  when only an `UNVERIFIED-…` reference was read), `execute_order` looks the order up in
  "Skickade Beställningar" in the same session: 3 reads 10 s apart, within 90 s. It is a
  separate step: a match gives a SECOND message, `Beställning verifierad: …` (the checker's
  text), and stores `verified_at` + `verified_numbers`.
- **Only by plate:** the plan needs `reg_nr`, the table needs the registration column, the row
  must have the same plate, be dated on or after the order and not be claimed by another
  record. When the real order reference was read, it must also appear in the row (as order
  number or supplier number). Without plate or column nothing is verified now (the daily
  checker still can, by model).
- Never fails the order (it is already placed): errors are logged without page text, an
  undelivered verified message is only logged and not resent, and the run does not exit 1 for it
  (the daily checker sets the problem flag). Not started after `approve` run start + 270 s
  (`VERIFY_START_S`) or, in `auto` mode, past the staging deadline.
- **Residual risk:** a same-plate row dated on or after the order can be taken as this order when
  the reference is UNVERIFIED and the earlier row is not claimed by an unverified `ordered`
  record. The duplicate guard makes a second order for the same plate within 7 days rare.
- The daily order checker (below) remains the fallback.

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

## Order checker (approve.py, after the taps)
(The immediate check in `_verified_row` uses the same matching rules for a plate; this daily
checker is the fallback for everything it did not verify.)
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

## Daily summary (summary.py, .github/workflows/daily-summary.yml)
- Workflow `MECA Daily Summary`: cron `45 5 * * *` (05:45 UTC every day, 07:45 Swedish summer
  time) plus `workflow_dispatch`. `permissions: contents: read`, own concurrency group
  `meca-summary`, `timeout-minutes: 5`. It installs nothing: `python3 summary.py` is stdlib +
  `notifier`. It persists nothing and has no "Alert on failure" step: the watchdog covers it
  (`stale_summary`, label "dagssammanfattningen"). Run it by hand from the Actions tab.
- Reads `config/orders.json`, `config/alerts.json`, `config/watchdog_alerts.json` (optional) and
  `logs/run_history.jsonl` (last 5 rows) as plain JSON, sends ONE message, never writes state,
  never opens a browser, and exits 1 when the send failed or building crashed (a short fallback
  `Dagssammanfattningen kunde inte skapas` is tried first), so the workflow shows red.
- Only booking numbers and plates (`<id> · <plate>`), never customer data. Sections, in order, each
  booking in at most ONE of the four booking sections (an elif chain over the records, sorted by id):
  1. `Väntar på ditt godkännande`: status `pending`.
  2. `Väntar på Klar`: any status except `pending`/`attempted` with `klar_outstanding`, the mirror of
     `orders_state._open_klar` (int `klar_message_id`, not `handled`, not `klar_closed`,
     `klar_sent_at` not older than 14 days; a missing or unparseable time never expires). The line
     is annotated `(blockerad)`, `(avbruten)`, `(misslyckad)`, `(utgången)` or `(ej verifierad)`;
     such a booking is listed only here.
  3. `Beställt, ej verifierat`: the order checker's predicate (`approve._to_verify`: `ordered`, no
     `verified_at`, not `verify_warned`), not in section 2.
  4. `Kräver åtgärd`: `blocked`/`failed`/`expired`, `updated_at` within 7 days or missing/unparseable
     (fail-open), shown as `<id> (blockerad|misslyckad|utgången)`.
  5. `Larm`: active (unresolved) alerts from both alert files, newest first, text whitespace-collapsed
     and cut to 120 characters.
  Each section shows at most 10 items then `+N till`; the whole text is capped at 3000 characters
  (the longest section is collapsed one item at a time). An empty day gives one line:
  `Inget väntar. Senaste lyckade körning: <date> UTC`. The footer is `Senaste lyckade körning:
  <YYYY-MM-DD> UTC` (last history row with neither `login_failed` nor `state_unreadable`) or `Ingen körning registrerad`.
- A file that is missing (orders.json, run_history.jsonl) or unreadable gives empty data plus a line
  `OBS: <fil> kunde inte läsas` at the top (a missing alerts.json / watchdog_alerts.json is normal and
  silent; a corrupt one gets the note). The notes travel in `orders["_notes"]`. When orders.json is
  unreadable and nothing else is listed, the text is the note plus `Läget är okänt.`, never `Inget väntar`.
- `ordered` + verified records that are not waiting for Klar appear in no section (nothing to do).

## First run after pushing
Run MECA Booking Agent, MECA Approval Poller and MECA Daily Summary once each from the Actions tab
(`workflow_dispatch`) and check they are green. The watchdog treats a workflow with NO runs at all
as not overdue (logged, no alert, nothing resolved), so a new workflow such as daily-summary.yml does
not raise `stale_summary` before its first run; only a known-old last success is overdue.

## Order status tracking (approve.py: track_statuses, orders_state: tracked_orders)
- The checker already reads "Skickade Beställningar"; its "Beställningsstatus" column is the row's
  `order_status`. Record fields (whitelisted in `orders_state`): `portal_status` (text, at most
  80 chars, markup and control/format characters stripped) and `status_changed_at` (ISO time).
- **Tracked** = status `ordered` + `verified_at` and `verified_numbers` + not `handled` and not
  `klar_closed` + younger than 21 days (`orders_state.TRACK_MAX_AGE`, a safety bound, counted from
  `verified_at`, else `updated_at`). In practice tracking ends when Klar is done or the Klar button
  expires (14 days; `_close_expired_klar` runs before tracking). The row is found with
  `order_match.find_row(rows, verified_numbers)`; none or several rows: nothing happens.
- First sighting is silent (stored only). A different status: message `Orderstatus <label>: <old> →
  <new>` and the new status is stored ONLY after the send succeeded, so an undelivered message is
  sent again next run (never lost, never twice). Texts are never logged, only their length.
- The portal's placeholder (`-`, en dash, em dash: `STATUS_PLACEHOLDERS`) counts as empty: it is never
  stored or sent, and after a real status it leaves that status and its timestamp unchanged.
- One `list_sent_orders` read per run: `_SentOrders` is shared by `_verify_orders` and
  `track_statuses` (a portal error gives `None` for both, logged by class and code only).
- An undelivered status message or a crash inside tracking (e.g. the state write after a send) makes
  the approve run exit 1; the rest of the run is not affected.
- `approve.py --check` prints `has_pending=true` while `orders_state.outstanding_tracking()` is true,
  so the daily approve run installs Chromium and opens the portal (about one browser run a day, for
  about 14 days per order, shared with all other approve work).

## Margin warning (parts_order_helpers.margin_line, rules.min_margin_pct)
- Shown as one line in the approval message right after `Totalt:` (before the fluids); it never
  blocks or changes an order and is only informational. `auto` mode sends no approval text.
- Prices: the MECA booking page lists prices **ex VAT**, and a part row's `price` is the **row total**
  (unit x qty), so quantities are NOT multiplied again. The pro.meca.se cart total is **incl. 25 %
  VAT**, so the purchase is `cart_total / VAT_FACTOR` (1.25) before comparing. Only the ordered rows
  count (`parts_order.split_lines(booking)[0]`); fluid rows are excluded. Fees added at checkout are
  not in the cart total, so a loss can be slightly understated.
- Texts: `OBS: inköp exkl. moms X SEK över bokningens delpris Y SEK` when the purchase exceeds the
  booking's parts price; otherwise, when `min_margin_pct` > 0 and the margin is below it,
  `OBS: marginal N,N % (under M %)` (margin = (price - purchase) / price, rounded DOWN to one decimal).
  Equal prices give no line.
- No line when anything is unknown (a row without a readable price or quantity, a missing cart total).
- `min_margin_pct` (rules key): integer 0-90, default 0 = warn only on a loss; invalid values make the
  whole rules file invalid (fail closed).
- The ordered-rows price is stored as `plan["parts_price_sek"]` at the first staging and reused by a swap
  restage (the swap booking carries no prices). Plans staged before this change have none, so they show
  no margin line after a swap.

## Dashboard: tiles and search (web/)
- `summarize.js` whitelists, per order, `portalStatus` (<= 80), `verifiedNumbers` (<= 120) and
  `klarOpen` (same predicate as the Klar section above). `vm.counts` holds the per-status counts plus
  `waiting`, `klar`, `needsAction`; the four tiles (`render.tiles`, headline too) read only these:
  `waiting` = pending; `klar` = ordered/blocked with `klarOpen`; `ordered` = status ordered and NOT
  under Klar (so `counts.ordered` is not the plain per-status total); `needsAction` = blocked/failed/
  expired updated within 7 days or of unknown age, not under Klar. They add up without overlap and
  have the meaning of the Telegram summary's sections, except that the summary has no section for
  plain verified `ordered` records.
- Above the orders list: a search box and a status filter (`render.filterOrders`, pure, client-side
  over the loaded orders, limit 200): case-insensitive substring over booking id and number, plate
  (spaces ignored for the plate only), status key and Swedish label, `portalStatus` and
  `verifiedNumbers`. No fetch while typing; `Inga träffar` when nothing matches. Text is set with
  `textContent`, no inline script. The demo data (`demo.js`) includes a `portal_status`.

## Parts rules
- Only parts under **"Kopplade reservdelar"** (linked to the loaded vehicle) are candidates.
  Discontinued ("Produkten har utgått") and replaced ("Ersätts av") items are excluded.
- **Bosch preferred** (`brand_prefer`; per part word `brand_prefer_by_part` goes first), then
  another linked brand. **Never ProMeister** (hard-coded in `parts_matcher.py`, also in
  `brand_forbid`).
- **Never fluids or oils** (any kind). Fluid-looking lines are listed under "Förbered", never
  searched. Some real parts are over-blocked (e.g. "Fettnippel", "Oil pump"): order those by hand.
- **Placement:** when the booking line names an axle (fram/bak) or side (vä/hö), the candidate
  must match it; unknown placement is dropped. **Axle-specific parts** (belägg, bromsskiva,
  stötdämpare, …) need an axle in the booking line, otherwise the booking is blocked, not guessed.
- Candidate titles/brands with non-Latin letters are dropped; the Claude picker (`claude-sonnet-5-5`)
  must be confident (≥ 0.8) or nothing is ordered. Only the first 15 linked results are considered.
- **Ceiling 10,000 SEK** per booking order, compared against the higher of the cart total and the
  checkout-page total, at staging, at approval and inside `checkout()` before the final click.
  A missing or unreadable total means no order.
- The pro.meca.se cart is **per vehicle**; staging requires that vehicle's cart to be empty.
- Stock and price: see "Alternatives and the swap" (in-stock replacement, `slut`/`dyr`
  alternatives, one-tap swap). A part with unknown stock is never treated as out of stock.
- `parts_rules.yaml` `mode: approve` (ask the owner first) is the current setting; `auto` would
  order directly under the same limits (no replacement, no alternatives, no swap) and is not to be
  switched on before the checks listed in the handoff doc are done.

## Parts vocabulary and lookup rules (owner-confirmed 2026-10-21)
- **Pollenfilter = Kupéfilter.** Searching "pollenfilter" on pro.meca.se returns NOTHING; "kupéfilter" returns the linked parts. Treat both words (and "kupefilter") as the same part and search "kupéfilter".
- **Artikel-ID.** On the booking page the "Delar" table has the columns name, OEM No, Artikel-ID, Antal, Beställda, Pris, Rabatt, Totalt (e.g. `Pollenfilter | 1025-PC40293 | 1025-PC40293 | 1`). The Artikel-ID is the pro.meca.se "Art. Nr" (searching it finds exactly that article, in the extra "Artikelnummer" tab and in "Kopplade reservdelar" when it fits the car). Fluids/oils ("Vätskor / Oljor") have no Artikel-ID.
- The Artikel-ID is **usually a ProMeister part**: ProMeister is never ordered, so use it as the REFERENCE (its title and "Produktinformation" such as filter type and dimensions) and find a similar linked part from another brand (Bosch preferred). It is not always ProMeister (the oil filter on the same booking was a Bosch `1025-F026407142`).
- Searches are only trusted from the "Kopplade reservdelar" tab (parts linked to the vehicle).

## Workflows and schedules (cron is UTC; Sweden is UTC+1 winter / UTC+2 summer)
| Workflow | Schedule | Runs |
|---|---|---|
| `.github/workflows/bookings.yml` | daily 15:00 UTC, Mon–Sat (`0 15 * * 1-6`) | `python agent.py`, commits `config/seen_ids.txt`, `config/orders.json`, `logs/run_history.jsonl`, `config/no_button_*.txt`, `config/alerts.json` |
| `.github/workflows/approve.yml` | daily 06:00 UTC, every day (`0 6 * * *`) | `python approve.py --check`, then `python approve.py` if anything is pending; commits `config/orders.json`, `config/alerts.json` |
| `.github/workflows/watchdog.yml` | every 6 h at :17 (`17 */6 * * *`) | `python watchdog.py`, commits `config/watchdog_alerts.json` only |
| `.github/workflows/daily-summary.yml` | daily 05:45 UTC, every day (`45 5 * * *`) | `python3 summary.py` (stdlib only), sends one Telegram overview, persists nothing |
| `.github/workflows/weekly-improve.yml` | Mondays 06:00 UTC | `python improve.py --apply-all`, opens a PR |

bookings.yml and approve.yml share the concurrency group `meca-bookings`, so they never overlap
(single writer of `orders.json`, one cart per vehicle). Run approve.yml by hand from the Actions
tab to process a tap immediately. Telegram keeps unread taps for 24 h only, so the poller runs
every day; tap within ~36 h of the staging message.
daily-summary.yml has its own group `meca-summary`, `contents: read`, writes nothing and has no
failure-alert step (the watchdog watches it); run it by hand from the Actions tab.
watchdog.yml has its own group `meca-watchdog` and writes only `config/watchdog_alerts.json`;
`config/alerts.json` is written only by bookings/approve (see "Alerts and guards").
**Every file the workflows read must be committed in the repo:** the first GitHub run once lost
order state because `config/orders.json` had never been committed.

### Weekly self-improve
`python improve.py --apply-all` sends source + SKILL.md + recent logs to Claude
(`claude-opus-5-5`), applies the proposals in the working tree, runs `py_compile` on each changed
`.py`, reverts any file that does not compile, writes `improve_reports/latest.md`, and the
workflow opens a **Pull Request**. It never pushes to `main`; a human reviews and merges.

**What the improve agent may change** (enforced in code in `improve.py`, not only in the prompt;
every PR still needs the owner's review before merging):
- `state.py` and `run_logger.py` (`ALLOWED_FILES`, existing files only — low-risk bookkeeping);
- NEW test files `tests/test_<lowercase_name>.py` (never an existing test, `conftest.py`,
  `__init__.py`, `sitecustomize.py` or `*.pth`).

**Everything else is suggestion-only.** A proposal for any other path — `portal_scraper.py`,
`parts_portal.py`, `parts_portal_parse.py`, `notifier.py`, `approve.py`, `parts_matcher.py`,
`parts_order*.py`, `orders_state.py`, `rules.py`, `agent.py`, `config_loader.py`, `improve.py`,
`parts_rules.yaml`, `.git/`, `.github/`, `.claude/`, `config/`, docs, existing tests, new
modules — is never written. It is listed as "skipped (not on the allowlist)" and its reason and
proposed code are shown under "Suggestions (not applied — need a developer)" in
`improve_reports/latest.md` (the PR body). The model can also return free-text `suggestions`.
Selector and search-word fixes will move to a validated data file (planned
`portal_selectors.json`, which will then join the allowlist). Dot-components, `..`, absolute
paths, backslashes and symlinks are always refused, and the path is checked again at write time.
New test files are flagged in the report: read them before running pytest locally (LLM-written
code). The workflow checks out with `persist-credentials: false`.

## Secrets (GitHub Secrets; env var overrides config.yaml)
`PORTAL_USERNAME`, `PORTAL_PASSWORD` (MECA portal), `PARTS_PORTAL_USERNAME`,
`PARTS_PORTAL_PASSWORD` (pro.meca.se), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (the owner's
private chat id), `ANTHROPIC_API_KEY` (part picker + weekly improve). Optional env:
`PORTAL_BOOKINGS_PATH`, `PORTAL_DEBUG`. Secrets live only in GitHub Secrets or the gitignored
`config/config.yaml`. Required by `config_loader`: portal username/password and telegram
bot_token/chat_id; ordering additionally needs the parts portal login and the Anthropic key.

## Scraper selectors and gotchas
- Cookie popups on both portals must be dismissed before any click works.
- MECA portal: the Authenticate JSON response decides login success; a failed login is never
  retried (bad attempts can lock the account) and is reported to the owner.
- Booking links: `a[href*='BookingInfo']`, deduplicated by BookingID param.
- Hanterad: exact text match, then wait for the Bekräfta dialog; Bekräfta is also exact text.
- Specification scraping stops at **Händelselogg** so event-log rows are not parsed.
- pro.meca.se login: TWO email fields — use `#ShopLoginForm_Login` / `#ShopLoginForm_Password`.
- pro.meca.se checkout: cart → "Gå vidare" → "Beställning <REG>" → "Skicka beställning" (the only
  click that places an order). The confirmation page has not been seen yet; it is handled
  defensively (an order without a readable reference is stored as `UNVERIFIED-…`; the order
  checker finds the real number later in "Skickade Beställningar").

## Safety invariants (never break these)
- A booking is never ordered twice (`orders_state`; `attempted` is written before checkout and a
  crashed attempt is never re-ordered automatically).
- Nothing is ordered above the ceiling, from ProMeister, for fluids/oils, or outside "Kopplade
  reservdelar"; ambiguity means no order.
- Only the owner's private chat can approve, cancel, swap or Klar, on the booking's own message.
  A swap and an in-stock replacement never order anything: they only change what the owner is
  asked to approve, under the same ceiling, brand, fluid and cart checks.
- Every booking with parts ends in a message (ordered, blocked or failed); a message that cannot
  be delivered turns the run red.
- A missing or unreadable `config/orders.json` stops all ordering before any portal contact
  (alert `state_unreadable`); an order for the same vehicle already in "Skickade Beställningar"
  within 7 days blocks staging (see the duplicate-order guard). Failures are never silent: a crash,
  a non-zero exit, a failed or timed-out workflow or a stale schedule each raise an alert (one
  message per fault; the watchdog's `stale_*` is a separate later reminder).
- Owner messages are Swedish and never carry raw exception text; customer data is never written
  to `config/orders.json` (it is committed to git).
- No secrets in git. The improve agent only opens PRs, only writes `state.py`, `run_logger.py`
  and new test files, and every PR needs the owner's review.

## Known limitations
- The order confirmation page, quantity semantics ("x4" pieces vs sets), the real format of
  `data-placements`, and the 0-stock policy are unverified until the first supervised live order
  (see `docs/superpowers/plans/2026-10-21-parts-ordering-handoff.md`).
- `max_alternatives` 2-3 behave like 1. Immediate verification is by plate only and has the
  residual risk described above. A failed swap (or a swap whose cart clear failed, which
  withdraws the approval) leaves an empty or partly filled cart that the owner fixes by hand. Exact duplicate keys in `parts_rules.yaml` are not detected.
- The axle-specific part list is incomplete (bromsbackar, bromsslang, English lines); title-only
  matching is used when the portal gives no ids; brand preference is enforced within one search
  term only.
- The watchdog cannot see GitHub disabling scheduled workflows after 60 days without repo
  activity; the duplicate guard reads only the first ~50 sent rows and, to proceed by hand, the
  owner orders in pro.meca.se manually.
- An approve run killed mid-checkout is only reported by the next daily booking run's sweep.
- The portal's order-status vocabulary is unknown, so nothing depends on specific words; tracking
  costs about one browser run a day per order for about 14 days (21 is only a safety bound). The margin warning ignores fees at
  checkout and hard-codes 25 % VAT. The Telegram summary shows at most 10 items per section.
- `approve.py` repeats the "invalid rules" warning every poll; a missing secret turns every poll red.
- Cancel clears the whole vehicle cart, including anything the owner added by hand.
- `logs/agent.log` (a 14-day workflow artifact) can contain full booking details.

## Logs monitored by improve.py
- `logs/run_history.jsonl`: structured per-run summary (the main signal, tracked in git).
- `logs/agent.log`: only exists inside a CI run or a local run (gitignored), so usually absent.
- `logs/improve_auto.log`: written by `improve.py --auto` (local only).

## Rules for proposed changes
- Do not add dependencies or touch workflows/secrets handling without a clear reason.
- Do not reference or recreate files listed under "Not present any more".
- Keep changes small and syntax-checkable; every changed `.py` must compile and the offline test
  suite (`python -m pytest`) must stay green.
