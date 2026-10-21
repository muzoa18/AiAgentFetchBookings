# Parts ordering — handoff (2026-10-21)

State: Tasks 1–8 and 6b are built and reviewed (final whole-branch review + one fix wave, 560 tests pass). Task 9 (cleanup) is deliberately not done. Nothing has run against the real Telegram bot or a real order yet. All commits are local until the owner pushes.

## Before the first supervised live order (owner actions)
1. **Disable or neuter the local Windows scheduler.** If it keeps running this checkout it keeps its own `config/orders.json` / `seen_ids.txt`, can stage carts and send Approve buttons that nothing processes, and clicks Hanterad so GitHub never sees the booking. Also: the local `config/config.yaml` needs a `telegram:` section (`bot_token`, `chat_id`) or `load_config` exits.
2. **GitHub secrets:** `PORTAL_USERNAME`, `PORTAL_PASSWORD` (rotate), `PARTS_PORTAL_USERNAME`, `PARTS_PORTAL_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (private chat id), `ANTHROPIC_API_KEY`. Settings → Actions → "Allow GitHub Actions to create and approve pull requests" (weekly improve PR).
3. **Confirm the cron:** daily booking run `7 15 * * 1-6` UTC (17:07 summer / 16:07 winter); approval poller `*/20 5-19 * * 1-6` UTC, `timeout-minutes: 10` (kept inside the 2,000-minute Free quota; a Sunday tick would avoid taps being lost over Saturday night — optional).
4. Push the commits (`git pull --rebase` first if the workflows already pushed state).
5. **Run the first order as a supervised live test** (rules file stays `mode: approve`): follow the Telegram message, tap Approve, watch the order appear on pro.meca.se, cancel it there if anything is wrong. Things to check and report back:
   - the **confirmation page** after "Skicka beställning": does the order reference come back (not `UNVERIFIED-…`), and does the page title keep "Beställning <REG>" (that would make a real order be reported as failed — safe direction, nothing re-orders — but needs a heuristic fix);
   - `checkout()` opens the order overview before reading the cart — verify this does not unload the vehicle (if it does every approval fails closed);
   - the real format of `data-placements` on result cards (front/rear matching), and the unseen "Tilläggstjänster"/"Fritext" cart rows;
   - quantity semantics ("x4" pieces vs sets), 0-stock parts (the message now shows "OBS: 0 i lager"; policy not decided).

## Before switching to `auto` mode (rules file), not before the supervised order
Confirmation-page handling verified; 0-stock policy; quantity semantics; extend the axle-specific part list (bromsbackar, bromsslang, English lines); title-only matching when the portal gives no ids; picker is the sole judge of "same part" and only the first ~15 linked results are considered; approve-run killed mid-checkout is only reported by the next daily sweep; brand preference is enforced within one search term only; `approve.py` repeats the "invalid rules" warning every poll and a missing secret turns every poll red; Cancel clears the whole vehicle cart including anything the owner added by hand.

## Known behaviour worth knowing
- Cart on pro.meca.se is **per vehicle**; staging requires that vehicle's cart to be empty (else the booking is blocked with a message). An expired approval leaves the cart filled for the owner to clear (spec); Cancel clears it.
- Only parts under "Kopplade reservdelar" are candidates; discontinued ("utgått") and replaced ("Ersätts av") items are excluded; ProMeister is hard-coded as forbidden; fluids and oils are never ordered (some real parts are over-blocked, e.g. "Fettnippel", "Smörjnippel", English "Oil pump" — order those by hand).
- Axle-specific parts (belägg, bromsskiva, stötdämpare, …) without an axle in the booking line are blocked, not guessed.
- Ceiling 10,000 SEK is compared against the higher of the cart total and the checkout-page total, at staging, at approval and inside `checkout()` before the final click.
- `logs/agent.log` (uploaded as a 14-day workflow artifact) can contain full booking details — scrub before trusting it with more than a small workshop's data.

## Rulings made on the owner's behalf (full text in the SDD ledger, not committed)
run() returns an int exit code; Hanterad only for `Bokning`; fluid-looking part lines are listed, never searched; `attempted` written before checkout and swept as `failed` at the next run; quantity parsing fails closed; implementers never ran live checks; ProMeister hard-coded; picker model `claude-sonnet-5-5`; `execute_order` owns the attempted→ordered/failed state and result message; undeliverable approval message ⇒ booking `failed` and red run; ordering step isolated per booking; `clear_cart()` best-effort on Cancel; approve honours only owner chat id + user id + stored message id; expiry does not clear the cart (spec wins); axle-specific parts need an axle in the line; approval poll every 20 min in working hours (Actions quota); customer data is never persisted to `orders.json`.

## Task 9 (when Telegram has run cleanly)
Delete `sms_sender.py`, `email_trigger.py`, `GMAIL_SETUP.md`, `config/seen_email_ids.txt`; update `improve.py` (`SOURCE_FILES`/`LOG_FILES` list files that no longer exist; `IMPROVE_MODEL` should be a current model id); update root `SKILL.md`, `.claude/skills/meca-agent/SKILL.md`, `README.md`, `config/config.example.yaml` for Telegram, the rules file and the two workflows.
