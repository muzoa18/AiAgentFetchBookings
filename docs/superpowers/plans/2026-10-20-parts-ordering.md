# Parts Ordering + Telegram Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace SMS/Gmail with a Telegram bot, then have the agent fill a pro.meca.se cart for each new `Bokning` with parts and place the order after the owner taps Approve.

**Architecture:** The daily run scrapes bookings (unchanged), notifies via Telegram, builds a parts plan per booking, fills the pro.meca.se cart, and stores the plan as `pending`. A light polling workflow reads Telegram button taps from the owner's chat id only, re-verifies the cart against the stored plan, and checks out. Pure logic (rules, matching, state, message building) is unit-tested; the two Playwright adapters are verified against the live site.

**Tech Stack:** Python 3.11 (CI) / 3.10 (local WSL), Playwright, `urllib` for the Telegram HTTP API (no new runtime dependency), `anthropic` for part matching, `pytest` (dev only), GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-19-parts-ordering-design.md`

## Global Constraints

- Value ceiling 10,000 SEK per booking order, compared with the checkout cart total incl. VAT; missing/unreadable total → do not order.
- Brand rule: prefer Bosch; else another brand; never ProMeister.
- Never order any fluid or oil; fluids on a booking are only listed in the message.
- Only `Bokning` bookings with at least one part; reg number required, else blocked.
- Each booking handled at most once; its id is recorded before the cart/checkout step.
- Only callbacks from `TELEGRAM_CHAT_ID` are acted on.
- Approvals expire after 48 h with a notification; poll interval 10–15 min; poller exits immediately when nothing is pending.
- Fail closed on any ambiguity; every booking with parts ends in a Telegram message (ordered, blocked, failed, expired, cancelled).
- Secrets only via env/GitHub Secrets/gitignored `config/config.yaml`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `PARTS_PORTAL_USERNAME`, `PARTS_PORTAL_PASSWORD`, `PORTAL_*`, `ANTHROPIC_API_KEY`.
- Messages are in Swedish, matching the existing SMS (my call).
- Daily booking job around 17:00 local; cron in UTC.

## Review Focus

1. Telegram message longer than 4096 chars (many parts) → split, never silently truncated or rejected.
2. Booking with fluids only / no parts → no cart touched; message lists fluids.
3. Double tap on Approve, or Approve after Cancel/expiry → order placed at most once; later taps answered "already handled".
4. Tap from a foreign chat id → ignored, nothing logged to the owner as an order event.
5. Telegram API down during the daily run → booking is NOT marked seen, so the next run retries (deliberately different from the old SMS behaviour; my call), and the run exits non-zero.

---

## File Structure

| File | Responsibility |
|---|---|
| `notifier.py` (new) | Telegram HTTP client: send, send with buttons, read updates, answer callback |
| `parts_rules.yaml` + `rules.py` (new) | Rules file and loader (`brand_prefer`, `brand_forbid`, `ceiling_sek`, `mode`) |
| `parts_matcher.py` (new) | Brand/fluid filtering + confidence-gated pick of a catalogue candidate |
| `orders_state.py` (new) | Per-booking order status store `config/orders.json` |
| `parts_portal.py` (new) | Playwright adapter for pro.meca.se: login, vehicle, search, cart, checkout |
| `parts_order.py` (new) | Orchestration: build plan, message, pending store, phase-2 auto order |
| `approve.py` (new) | Polling entry point: updates → verify → checkout → notify; expiry |
| `agent.py`, `config_loader.py`, workflows (modify) | Switch to Telegram, drop Gmail gate, daily cron |
| `tests/` (new), `requirements-dev.txt` (new) | pytest suite |
| `sms_sender.py`, `email_trigger.py` (delete at end) | Removed after Telegram runs cleanly |

---

### Task 1: Telegram notifier

**Files:**
- Create: `notifier.py`, `tests/test_notifier.py`, `requirements-dev.txt` (`pytest>=8`)
- Modify: `config_loader.py` (add `("telegram","bot_token")→TELEGRAM_BOT_TOKEN`, `("telegram","chat_id")→TELEGRAM_CHAT_ID` to `ENV_MAP`; add both to `REQUIRED_FIELDS`)

**Interfaces:**
- Produces:
  - `send_message(config: dict, text: str) -> bool` — splits text into ≤4000-char chunks on line boundaries; True only if all chunks sent.
  - `send_with_buttons(config: dict, text: str, buttons: list[tuple[str, str]]) -> int | None` — `(label, callback_data)` pairs in one row; returns Telegram `message_id` or None.
  - `get_updates(config: dict, offset: int | None) -> list[dict]` — raw updates, each callback normalised to `{"update_id", "chat_id", "callback_id", "data", "message_id"}`; non-callback updates dropped.
  - `answer_callback(config: dict, callback_id: str, text: str) -> None`
  - `clear_buttons(config: dict, message_id: int) -> None`

- [ ] **Step 1: Write failing tests** with `urllib.request.urlopen` monkeypatched: `test_send_message_posts_to_bot_token_url`, `test_long_text_split_into_chunks_under_4096` (assert every chunk ≤4096 and joined text equals input), `test_send_with_buttons_builds_inline_keyboard` (assert `reply_markup.inline_keyboard[0]` has the labels and callback data), `test_get_updates_drops_non_callbacks_and_normalises`, `test_send_failure_returns_false` (HTTP error → False, no exception).
- [ ] **Step 2: Run** `pytest tests/test_notifier.py -v` — Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the five functions in `notifier.py` with `urllib` + JSON (same style as `sms_sender.py`); never log the token (log only the method name).
- [ ] **Step 4: Run** `pytest tests/test_notifier.py -v` — Expected: PASS.
- [ ] **Step 5: Live check** (owner's real bot): `python -c "import config_loader, notifier; c=config_loader.load_config(); print(notifier.send_message(c,'Test'))"` — Expected: `True` and the message arrives in Telegram.
- [ ] **Step 6: Commit** `feat: add Telegram notifier`.

### Task 2: Switch the daily job to Telegram and drop Gmail

**Files:**
- Modify: `agent.py` (replace `send_sms`/`should_run`; see behaviour below), `config_loader.py` (remove `sms.*` from REQUIRED_FIELDS, remove `trigger.*`), `requirements.txt` (drop the four `google-*` libs), `.github/workflows/bookings.yml` (cron `0 15 * * 1-6` ≈ 17:00 Swedish summer time/16:00 winter — owner to confirm; remove Gmail restore step and trigger env vars; add Telegram + parts secrets; keep the state-commit step and add `config/orders.json`)
- Test: `tests/test_agent_notify.py`

**Interfaces:**
- Consumes: Task 1 `send_message`.
- Produces: `build_message(booking: dict) -> str` in `agent.py` (existing `build_sms` renamed, same content, Markdown-free).

- [ ] **Step 1: Write failing tests**: `test_build_message_matches_existing_sms_format` (reuse the format shown in `SKILL.md`), `test_notify_failure_does_not_mark_seen` (fake `send_message` False → id absent from saved seen set, `run()` returns non-zero exit), `test_notify_success_marks_seen_and_calls_hanterad_for_bokning_only`.
- [ ] **Step 2: Run** `pytest tests/test_agent_notify.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** the swap in `agent.py`: remove the trigger gate; on notify failure leave the id unseen, skip Hanterad, and `sys.exit(1)` after the loop; `log_run` keys `sms_sent/sms_failed` become `notified/notify_failed`.
- [ ] **Step 4: Run** the full suite `pytest -v` — Expected: PASS.
- [ ] **Step 5: Live dry check** `python agent.py` against the real portal with one test booking, confirming one Telegram message and no duplicate on a second run.
- [ ] **Step 6: Commit** `feat: Telegram notifications, daily schedule, remove Gmail gate`.

### Task 3: Rules file

**Files:**
- Create: `parts_rules.yaml`, `rules.py`, `tests/test_rules.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Rules: brand_prefer: tuple[str,...]; brand_forbid: tuple[str,...]; ceiling_sek: int; mode: str`; `load_rules(path: Path = Path("parts_rules.yaml")) -> Rules` (raises `ValueError` on unknown `mode` or non-positive ceiling).
- `parts_rules.yaml` values: `brand_prefer: [Bosch]`, `brand_forbid: [ProMeister]`, `ceiling_sek: 10000`, `mode: approve` (allowed: `approve` | `auto`).

- [ ] **Step 1: Write failing tests**: `test_defaults_from_repo_file` (asserts the four values above), `test_unknown_mode_rejected`, `test_zero_ceiling_rejected`.
- [ ] **Step 2: Run** `pytest tests/test_rules.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `Rules` and `load_rules` with `yaml.safe_load`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `feat: parts rules file`.

### Task 4: Part matching

**Files:**
- Create: `parts_matcher.py`, `tests/test_parts_matcher.py`

**Interfaces:**
- Consumes: Task 3 `Rules`.
- Produces:
  - `is_fluid(name: str) -> bool` — true for oil/olja, brake fluid/bromsvätska, coolant/kylarvätska, spolarvätska, and similar.
  - `rank_candidates(candidates: list[dict], rules: Rules) -> list[dict]` — candidate dicts have `{"title","brand","price_sek","url_or_id"}`; drops forbidden brands, orders preferred brands first.
  - `choose(line: dict, candidates: list[dict], rules: Rules, picker) -> dict | None` — `line` is `{"name","qty"}`; `picker(line, ranked) -> tuple[int, float]` (index, confidence); returns the chosen candidate only if confidence ≥ 0.8, else None.
  - `make_claude_picker(api_key: str)` — returns a `picker` using the Anthropic API with JSON-schema output (model from a module constant `PICK_MODEL`, set to a current model id; builder confirms).

- [ ] **Step 1: Write failing tests** (fake picker only, no network): `test_fluids_are_never_matched` (`is_fluid("Motorolja 5W-30")`, `"Bromsvätska DOT4"` → True; `"Oljefilter"` → False), `test_promeister_dropped_even_if_only_candidate` (→ `choose` returns None), `test_bosch_ranked_before_other_brands`, `test_low_confidence_returns_none`, `test_picker_index_out_of_range_returns_none`.
- [ ] **Step 2: Run** `pytest tests/test_parts_matcher.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** the four pure functions; the Claude picker passes only the ranked candidates' titles/brands and the booking line, and returns `(idx, confidence)`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `feat: part matching with brand/fluid rules`.

### Task 5: Order state

**Files:**
- Create: `orders_state.py`, `tests/test_orders_state.py`
- Modify: `.gitignore` is NOT changed; `config/orders.json` is committed back by the workflows.

**Interfaces:**
- Produces:
  - `Status = Literal["attempted","pending","ordered","blocked","failed","cancelled","expired"]`
  - `get(booking_id: str) -> dict | None`; `put(booking_id: str, status: Status, plan: dict | None = None, message_id: int | None = None) -> None` (stamps `updated_at`, ISO UTC)
  - `claim(booking_id: str) -> bool` — atomically sets `attempted` only if no record exists; False if one does.
  - `pending() -> list[tuple[str, dict]]`; `last_update_id() -> int | None` / `set_last_update_id(n: int) -> None`

- [ ] **Step 1: Write failing tests** (tmp file via monkeypatched path): `test_claim_only_once`, `test_put_overwrites_status_and_keeps_plan`, `test_pending_lists_only_pending`, `test_last_update_id_roundtrip`.
- [ ] **Step 2: Run** `pytest tests/test_orders_state.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** with a JSON file `config/orders.json` and write-temp-then-rename.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `feat: per-booking order state`.

### Task 6: pro.meca.se adapter (needs the owner's screenshots)

**Files:**
- Create: `parts_portal.py`, `tests/test_parts_portal_parse.py`, `tests/fixtures/` (saved HTML of search results and cart)

**Interfaces:**
- Produces:
  - `class PartsPortal(config)` context manager (headless Chromium; `debug` flag as in `portal_scraper.py`).
  - `login() -> None` (raises `PartsPortalError` on failure); `load_vehicle(reg_nr: str) -> None`
  - `search(term: str) -> list[dict]` — candidate dicts as in Task 4.
  - `add_to_cart(candidate: dict, qty: int) -> None`; `read_cart() -> dict` → `{"lines": [{"title","qty","price_sek"}], "total_sek": float | None}`
  - `checkout() -> str` — returns the order confirmation reference; raises `PartsPortalError` if no confirmation is seen.
  - Pure parsers `parse_search_results(html: str) -> list[dict]`, `parse_cart(html: str) -> dict`.

- [ ] **Step 1: Collect inputs** — ask the owner for the screenshots of the manual order flow and a saved page of a search result and of the cart (browser "Save as HTML"); put the HTML under `tests/fixtures/`.
- [ ] **Step 2: Write failing tests** against the fixtures: `test_parse_search_results_extracts_title_brand_price`, `test_parse_cart_total_and_lines`, `test_parse_cart_missing_total_is_none`.
- [ ] **Step 3: Run** `pytest tests/test_parts_portal_parse.py -v` — Expected: FAIL.
- [ ] **Step 4: Implement** the parsers and the Playwright methods; selectors come from the screenshots/fixtures, preferring exact-text matching (as in `portal_scraper.py`); save a screenshot to `logs/` on any failure.
- [ ] **Step 5: Run** the parser tests — Expected: PASS.
- [ ] **Step 6: Live check** with `mode: approve`: run a script that logs in, loads a real reg number, searches one part, adds it to the cart, reads the cart, and prints it. Do NOT call `checkout()` yet.
- [ ] **Step 7: Commit** `feat: pro.meca.se cart adapter`.

### Task 7: Order planning and approval message

**Files:**
- Create: `parts_order.py`, `tests/test_parts_order.py`
- Modify: `agent.py` (after a successful notify of a `Bokning` with parts, call `plan_and_stage`)

**Interfaces:**
- Consumes: Tasks 1, 3, 4, 5, 6.
- Produces: `plan_and_stage(config: dict, booking: dict, portal, rules: Rules, picker) -> str` — returns the final status (`pending`, `ordered`, `blocked`, `failed`). Steps: `claim` the booking id (False → return existing status, no work); block if no reg number, no parts, or any matched part is None; fill the cart; read it; block if the total is None or > `ceiling_sek`; store the plan; in `mode: approve` send the summary with buttons `("Godkänn","approve:<id>")` and `("Avbryt","cancel:<id>")` and set `pending`; in `mode: auto` call the same `execute_order` used by Task 8 and set `ordered`.
- `build_summary(booking: dict, cart: dict, fluids: list[dict]) -> str`; `execute_order(config, portal, booking_id: str, plan: dict, rules) -> str` — re-reads the cart, fails closed if it differs from `plan` or exceeds the ceiling, then `checkout()`.

- [ ] **Step 1: Write failing tests** with a fake portal/picker/notifier: `test_no_reg_number_is_blocked_and_messaged`, `test_fluids_only_booking_never_touches_cart`, `test_unmatched_part_blocks_whole_booking`, `test_over_ceiling_blocked`, `test_missing_total_blocked`, `test_second_call_for_same_booking_does_nothing`, `test_approve_mode_sends_buttons_and_sets_pending`, `test_auto_mode_orders_without_buttons`, `test_summary_lists_fluids_to_prepare`.
- [ ] **Step 2: Run** `pytest tests/test_parts_order.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `plan_and_stage`, `build_summary`, `execute_order`; any exception inside → status `failed` + a Telegram message, never propagates silently.
- [ ] **Step 4: Run** the full suite — Expected: PASS.
- [ ] **Step 5: Live check** in `approve` mode with a real small booking: confirm the cart is filled and the message with buttons arrives; do not tap yet.
- [ ] **Step 6: Commit** `feat: stage parts orders for Telegram approval`.

### Task 8: Approval poller

**Files:**
- Create: `approve.py`, `.github/workflows/approve.yml`, `tests/test_approve.py`

**Interfaces:**
- Consumes: Tasks 1, 5, 6, 7.
- Produces: `handle_updates(config, portal_factory, rules) -> int` (number of actions taken) and `expire_stale(config, now: datetime) -> int`; `main()` exits 0 immediately if `orders_state.pending()` is empty (before touching Telegram or Playwright).

- [ ] **Step 1: Write failing tests**: `test_nothing_pending_exits_without_telegram_call`, `test_foreign_chat_id_ignored`, `test_approve_executes_once_and_second_tap_says_already_handled`, `test_cancel_sets_cancelled_and_clears_buttons`, `test_approve_after_expiry_refused`, `test_expire_stale_after_48h_notifies`, `test_cart_mismatch_marks_failed_and_notifies`, `test_last_update_id_advanced_even_for_ignored_updates`.
- [ ] **Step 2: Run** `pytest tests/test_approve.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `handle_updates`/`expire_stale`/`main`; always `answer_callback` and `clear_buttons`; record `ordered` with the confirmation reference before notifying.
- [ ] **Step 4: Add** `approve.yml`: cron `*/15 * * * *`, `concurrency` group shared with the bookings job, installs deps and Chromium only after the pending check passes (use a first step that runs `python approve.py --check` and sets an output), commits `config/orders.json`.
- [ ] **Step 5: Run** the full suite — Expected: PASS.
- [ ] **Step 6: Live check** — tap Approve on the staged booking from Task 7, confirm the order appears on pro.meca.se and one "Beställd" message arrives; cancel the order afterwards by hand.
- [ ] **Step 7: Commit** `feat: Telegram approval poller and checkout`.

### Task 9: Cleanup and docs

**Files:**
- Delete: `sms_sender.py`, `email_trigger.py`, `GMAIL_SETUP.md`, `config/seen_email_ids.txt`
- Modify: `improve.py` (`SOURCE_FILES` to the real files incl. the new modules, `LOG_FILES` to existing logs, `IMPROVE_MODEL` to a current model id), `SKILL.md` (root), `.claude/skills/meca-agent/SKILL.md`, `README.md`, `config/config.example.yaml`

- [ ] **Step 1:** Only after Telegram has run cleanly for the owner-agreed period, remove the files above and every reference (`grep -rn "sms\|email_trigger\|gmail" --include=*.py --include=*.yml --include=*.md`).
- [ ] **Step 2:** Update the docs to describe Telegram, the rules file, the two workflows, and the new secrets.
- [ ] **Step 3: Run** `pytest -v` and `python -m py_compile *.py` — Expected: all pass, no output from py_compile.
- [ ] **Step 4: Commit** `chore: remove SMS and Gmail, update docs and improve.py lists`.
