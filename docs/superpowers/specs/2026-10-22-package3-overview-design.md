# Package 3: overview (design)

Date: 2026-10-22. Scope approved by the owner in chat (2026-10-21): "daily summary on Telegram",
"delivery tracking after ordering", "price and margin warning", "history and search per booking in
the dashboard". The owner works from the phone, wants few messages, and said the final goal is a
system that can later also draft quotes (not part of this package).

## Why

After packages 1 and 2 the system is safe and decides well, but the owner still has to open
Telegram threads, the dashboard and pro.meca.se to know "what is waiting, what is on its way, did
anything cost more than the job pays". Package 3 gives one calm overview and tells the owner when
something changes.

## Goals

1. **Daily summary (Telegram).** One message at about 07:45 Swedish summer time (05:45 UTC), every
   day, from a small stdlib-only workflow that reads the state files: waiting for the owner's
   approval, orders waiting for Klar, placed orders not yet verified or still on their way, blocked
   orders needing action, active alerts, last successful run. A quiet day gives a one-line message
   ("Inget väntar"). It writes no state and never opens a browser.
2. **Order status tracking.** For placed orders the order checker also stores the portal's order
   status text (the "Beställningsstatus" column of "Skickade Beställningar") and tells the owner
   once per change ("Orderstatus <label>: <old> → <new>"). The vocabulary of that column is not
   known to us, so nothing depends on specific words: tracking stops after 21 days or when the
   record is marked Klar. Tracking only runs while there is something to track (it joins
   `has_pending` for orders younger than 21 days that are not Klar).
3. **Price and margin warning.** At staging, compare the parts' purchase total (cart, incl. VAT) with
   what the booking charges the customer for those parts (the booking's part rows carry `price`).
   Add an informational line to the approval message when purchase exceeds the customer price
   (`OBS: inköp X SEK över bokningens delpris Y SEK`) or the margin is under `min_margin_pct`
   (new optional rules key, default 0 = only warn on a loss). Never blocks an order.
4. **Dashboard: history and search.** The orders list gets a search box (booking number, plate,
   status text, order number) and a status filter, filtering client-side over the already loaded
   `orders` (limit 200 as today), plus the new `portalStatus` and verified numbers per order. The
   daily summary's counts also appear at the top of the page.

## Non-goals

No new ordering or approval authority, no change to checkout, no quote drafting, no new portals,
no multi-user roles. No scraping of delivery pages we have not seen: only the status column already
read.

## Design

### Daily summary (`summary.py`, `.github/workflows/daily-summary.yml`)
`build_summary(orders, alerts, history, now) -> str` (pure; Swedish fixed text; booking numbers,
plates and order numbers only; at most 3000 characters, extra items collapsed to "+N till").
`main(env)` reads `config/orders.json`, `config/alerts.json`, `config/watchdog_alerts.json` and
`logs/run_history.jsonl` from the checkout, sends one `send_message`, exits 1 when the send failed (always after trying a fallback if building crashed). The workflow:
cron `45 5 * * *` + `workflow_dispatch`, permissions `contents: read`, stdlib only, no persist,
no concurrency group conflict (own group `meca-summary`), step timeout 3 min, plus the `Alert on
failure` step as the other workflows (it writes alerts.json: so it needs `contents: write` and the
standard persist; keep it simple: NO failure alert step here; the watchdog checks the summary workflow
too via a third `SPECS` entry `summary` (daily 05:45 UTC, key `stale_summary`)).

### Status tracking (`order_match.py`, `approve.py`, `orders_state.py`)
Record fields `portal_status` (str, <= 80 chars, sanitised: control characters and page markup
stripped) and `status_changed_at`. `track_statuses(config, session, now)` in approve.py: for each
`ordered` record with `verified_at`, younger than 21 days and not `klar_closed`, find its sent row
(by its stored `verified_numbers`, the supplier/order numbers) in the freshly read list; if the
status differs from `portal_status` (first sighting only stores, no message), store it and send the
fixed message. Rows not found: nothing. Portal errors: log and skip. One `list_sent_orders()` read
per run, shared with the existing checker. `outstanding_tracking()` feeds `--check`'s `has_pending`.

### Margin (`parts_order_helpers.py`, `rules.py`)
`margin_line(booking, cart_total, rules) -> str | None`; booking part rows' `price` is parsed
with the same SEK parser as the scraper (`parse_sek`), quantity-aware (`price` is per unit unless the
row has a total: check the scraper's field semantics). `min_margin_pct` (int 0-90, default 0)
joins `Rules` (new field last). Unknown/unparsable prices -> no line (never a false alarm).

### Dashboard (`web/`)
`summarize.js`: orders get `portalStatus`, `verifiedNumbers`; view model adds `counts`
(`waiting`, `klar`, `ordered`, `needsAction`) already derivable from `orders`. `render.js`/`app.js`:
a search input and a status select above the list, filtering by case-insensitive substring over the
whitelisted fields; all text via `textContent`; no inline script.

## Constraints (all tasks)
As packages 1-2 (offline tests, Swedish fixed owner texts, no customer data beyond booking number,
plate, model, order numbers, never place real orders, no push, commit trailer
`Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`, `agent.py` stays CRLF). Python suite
1760 + web 122 stay green. Update both SKILL.md files, README, `parts_rules.yaml` comments.

## Status (2026-10-22)
Built and reviewed. The code is the truth where this design differs:
- **Tiles, not a counts row.** The dashboard shows four tiles read from `vm.counts` (waiting, klar, ordered, needsAction; the headline uses the same numbers); there is no separate counts row. `counts.ordered` is non-overlapping (status `ordered` and no open Klar button), so the four add up, and each booking is in one of them, like the summary's elif chain. Search: the whitespace-insensitive match applies to the plate only.
- **Margin figures are ex VAT.** Booking prices are ex VAT and a part row's `price` is the row total (not per unit, no quantity multiplication); the cart total (incl. 25 % VAT) is divided by `VAT_FACTOR` 1.25. Only ordered rows count (fluids excluded). Text: `OBS: inköp exkl. moms X SEK över bokningens delpris Y SEK`; the margin is rounded down to one decimal (`OBS: marginal N,N % (under M %)`). The ordered-rows price is stored as `plan["parts_price_sek"]` for swap restages; older plans show no line after a swap.
- **Tracking ends at Klar.** Tracked = ordered, verified (`verified_at` and `verified_numbers`), not `handled`/`klar_closed`, under 21 days from `verified_at` (a safety bound: effectively tracking ends at Klar or when the Klar button expires after 14 days, because `_close_expired_klar` runs first). The row is found with `order_match.find_row`. `-` (also en/em dash) counts as empty. A status is stored only after its message was sent; an undelivered message or a tracking crash exits 1. The shared read is `approve._SentOrders`.
- **No failure-alert step in the summary workflow**; the watchdog covers it (`stale_summary`, `Spec.minute` = 45). The job has `timeout-minutes: 5`, not a 3-minute step timeout.
- **Summary details:** the Klar predicate mirrors `orders_state._open_klar` in stdlib-only code (a missing or unparseable `klar_sent_at` never expires); a Klar-waiting booking (any status except pending/attempted) is listed only under `Väntar på Klar` (annotated with its Swedish status), `Beställt, ej verifierat` mirrors `approve._to_verify`, 'Kräver åtgärd' uses Swedish status words, each section shows at most 10 items; unreadable files add an `OBS: <fil> kunde inte läsas` line; the footer is the date of the last successful run.
- The Python suite is 1902 tests and the web suite 131 (the design said 1760 and 122).
- **Final review round:** `is_overdue` treats "no successful run at all" as not overdue (logged, no alert, nothing resolved), so a new workflow does not false-alarm before its first run; the summary says `Läget är okänt` instead of `Inget väntar` when orders.json is unreadable; its footer skips runs with `state_unreadable`.
