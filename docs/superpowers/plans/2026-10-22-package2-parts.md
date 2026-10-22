# Package 2: the parts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Smarter parts proposals (rules as data, stock/price-aware choice with an alternative), a one-tap swap in Telegram, and immediate order-number verification after checkout, without widening ordering authority.

**Architecture:** Extend `Rules` with optional keys, add `choose_with_alternatives` beside `choose`, store each flagged part's alternative in the order plan, add a `swap:` callback handled by `approve.py` that re-stages with a pinned alternative, and add a same-session sent-orders lookup after checkout.

**Tech Stack:** Python 3.11, pytest, Playwright (fakes only in tests), PyYAML.

**Spec:** `docs/superpowers/specs/2026-10-22-package2-parts-design.md`

## Global Constraints

- Python suite `pytest -q` (1475 pass now) and web `node --test` (122) stay green; tests are offline with fakes, fake customer data only, no live portals or Telegram, never place a real order.
- Swedish fixed owner texts; no raw exception/page text; customer data never in logs/messages beyond booking number, plate, model, order numbers.
- Never widen ordering authority: ceiling, ProMeister ban, fluid ban, "Kopplade reservdelar" only, owner-only approval (private chat id + user id + stored message id), `attempted` before checkout, duplicate guard and cart verification all unchanged.
- `agent.py` keeps CRLF. Commit trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not push.
- Backwards compatibility: the current `parts_rules.yaml` and all existing tests keep working unchanged.

## Review Focus

- A swap tapped twice, from an old message, from another chat/user, on a non-pending record, or after 2 swaps: nothing changes, owner told why, never a second cart fill.
- A swap that fails halfway (cart cleared but staging fails): the owner is told the truth ("varukorgen är tömd / delvis fylld"), the record is not left `pending` with stale buttons.
- The in-stock replacement or an alternative that is a forbidden brand, a fluid, wrong axle/side, or above the ceiling must never be proposed or pinned.
- Alternative picked with low picker confidence: not proposed (same gate as the main choice).
- `verify_now` ambiguity (two candidate rows) or portal errors: never stores a guess, never fails an order that is already placed.
- Rules file with bad new keys fails closed (no ordering, owner told "Regelfilen är ogiltig").

---

### Task 1: Rules as data

**Files:** Modify `rules.py`, `parts_rules.yaml`, `tests/test_rules.py`.

**Interfaces:**
- Produces: `Rules` gains `brand_prefer_by_part: tuple[tuple[str, tuple[str, ...]], ...] = ()`, `prefer_in_stock: bool = True`, `expensive_ratio: float = 1.5`, `expensive_min_diff_sek: int = 100`, `max_alternatives: int = 1` (new fields last, defaults keep positional construction valid). YAML: `brand_prefer_by_part` is a mapping of part word -> list of brand names.
- Validation (ValueError, fail closed): types; `expensive_ratio > 1`; `expensive_min_diff_sek >= 0` int, not bool; `max_alternatives` int 0..3; part keys non-empty strings; brand lists lists of non-empty strings. Unknown keys are ignored as today (do not start rejecting them).

- [ ] **Step 1: Failing tests** (`tests/test_rules.py`): the shipped `parts_rules.yaml` loads with defaults; a file with all new keys loads into the expected tuple/values; each invalid value above raises `ValueError` with the key name in the message; a file with only the old four keys still loads with the defaults.
- [ ] **Step 2: Run** `pytest tests/test_rules.py -q` -> FAIL. **Step 3: Implement** in `rules.py`; add commented examples (not active values beyond the defaults) to `parts_rules.yaml`. **Step 4:** full suite PASS. **Step 5: Commit** `feat(rules): per-part brands, stock and price thresholds as data`.

---

### Task 2: Matching with stock, price and alternatives

**Files:** Modify `parts_matcher.py`, `tests/test_parts_matcher.py`.

**Interfaces:**
- Consumes: `Rules` from Task 1.
- Produces:
  - `_brand_rank(cand, rules, line_name: str = "") -> int`: if `line_name` contains (whole normalised word, synonyms honoured via `search_terms`) a `brand_prefer_by_part` key, that key's brands rank first in listed order, then the global `brand_prefer`, then the rest; with no `line_name` behaviour is exactly today's.
  - `@dataclass(frozen=True) class Choice: chosen: dict | None; alternatives: tuple[dict, ...]`.
  - `choose_with_alternatives(line: dict, candidates: list[dict], rules: Rules, picker) -> Choice`. Order of work: same filtering/ranking as `choose`; main pick as today; then (a) if `rules.prefer_in_stock` and the pick has `in_stock is False` and in-stock ranked candidates exist, pick again among the in-stock ones (same confidence gate) and use that as `chosen` when confident, otherwise keep the original; (b) build alternatives only when `rules.max_alternatives > 0` and the chosen part is out of stock (`alt_reason` "slut") or an in-stock, allowed, picker-confirmed (`_pick` on the remaining ranked, in-stock, cheaper-enough list) candidate costs `<= chosen_price / expensive_ratio` with saving `>= expensive_min_diff_sek` (`alt_reason` "dyr", `saving_sek`); each alternative is a copy of the candidate dict plus `alt_reason` and `saving_sek` (0 for "slut"), at most `max_alternatives`. At most ONE extra picker call per line for alternatives.
  - `choose(...)` keeps its signature and behaviour (callers unchanged).

- [ ] **Step 1: Failing tests** (fake picker returning scripted `(index, confidence)`; candidates with `in_stock` True/False/None, prices): per-part brand beats global brand; global preference unchanged without a per-part key; out-of-stock chosen is replaced by confident in-stock candidate; replaced candidate never forbidden/fluid/placement-wrong; no in-stock candidate -> keeps original, alternatives empty, `alt` only if another allowed part exists (none here); expensive flagged at ratio 1.5 and diff 100 (boundary: exactly 1.5x and exactly 100 SEK qualifies, 99 does not); alternative rejected when picker confidence < `MIN_CONFIDENCE`; `max_alternatives=0` -> no alternatives and no extra picker call (assert call count); ProMeister never appears as chosen or alternative; `in_stock None` (unknown) is neither "slut" nor preferred.
- [ ] **Step 2-4:** FAIL, implement, PASS + full suite. **Step 5: Commit** `feat(matcher): stock-aware choice with price/stock alternatives and per-part brands`.

---

### Task 3: Stage with alternatives and show them

**Files:** Modify `parts_order.py`, `parts_order_helpers.py`, `notifier.py`, `tests/test_parts_order.py`, `tests/test_notifier.py`.

**Interfaces:**
- Consumes: `choose_with_alternatives`.
- Produces:
  - `notifier.send_with_buttons(config, text, buttons)` accepts either `[(label, data), ...]` (one row, as today) or `[[(label, data), ...], ...]` (rows); detect by element type; returns `message_id | None` as before.
  - `plan["parts"][i]["alternative"]`: `{"title","brand","price_sek","url_or_id","in_stock","article_nr"?,"alt_reason","saving_sek"}` only when the choice has one (keep exactly the fields `portal.add_to_cart` reads; check it and keep the stored dict minimal: no page text).
  - `build_summary` appends, per part with an alternative, `Alternativ för <name>: <title> (<brand>) à <sek>[ – i lager] (<slut|dyr: sparar N SEK>)`.
  - Buttons in `_stage`: row 1 `Godkänn`/`Avbryt` (unchanged data), then one row per flagged line `Byt <name[:20]>` with data `swap:<booking_id>:<index>` (assert `len(data.encode()) <= 64`; skip the swap button, not the alternative text, when it would be longer). No swap buttons when `swaps >= 2` in the record.
  - `_stage` uses `choose_with_alternatives(...)`; when the out-of-stock replacement happened the summary says so ("Bytt till artikel i lager"), fixed Swedish text.

- [ ] **Step 1: Failing tests:** staging with a flagged line stores `alternative`, sends rows (fake notifier captures), message text contains the alternative block; no flagged lines -> exactly today's message and one row; cart verification and ceiling unchanged (an alternative is never in the cart at staging); `send_with_buttons` row detection (both shapes); callback data length guard.
- [ ] **Step 2-4:** FAIL, implement, PASS + full suite. **Step 5: Commit** `feat(parts): show alternatives for out-of-stock or expensive parts with a swap button`.

---

### Task 4: One-tap swap

**Files:** Modify `approve.py`, `parts_order.py`, `orders_state.py` (only for the `swaps` field), `tests/test_approve.py`, `tests/test_parts_order.py`, `tests/test_orders_state.py`.

**Interfaces:**
- Produces:
  - `orders_state`: optional record field `swaps: int` (0-2) preserved by `put`/`update_fields` like the other extras; validated.
  - `parts_order.restage_with_pins(config, portal, booking, pins: dict[int, dict], rules, picker) -> str`: the `_stage` flow with `pins` giving, for those line indexes, the candidate to use instead of searching/choosing (the pinned dict must be one stored in the previous plan's `alternative`); everything else (empty-cart check, matching of the other lines, add, cart verification, ceiling, new `pending` plan, new approval message with a fresh message id) as `_stage`. A pinned candidate is re-validated with the same filters (`rank_candidates` on a one-element list must keep it) and refused otherwise.
  - `approve._swap(config, session, booking_id, record, line_index, rules, cb)` as in the spec; tap handling added to `_handle_one` for `swap:<id>:<n>` using the same owner/message-id/age checks as `_approve`/`_cancel`. Sets `swaps + 1`. The booking dict needed by staging is rebuilt from the stored plan (`booking_id`, `booking_nr`, `reg_nr`, `parts` names/qty, `fluids`, `detail_url`, vehicle) without scraping MECA; the pinned part replaces the old part at `line_index`.
- Failure texts (Swedish, fixed): swap refused (`inte längre möjligt`), cart not cleared (`CART_MANUAL`), staging failed after clearing (`Bytet misslyckades – varukorgen är tömd eller delvis fylld, kontrollera pro.meca.se`).

- [ ] **Step 1: Failing tests** with the fake portal/notifier already used in `tests/test_approve.py`: happy path (swap clears cart, restages with the alternative, new pending record with new message_id, old buttons cleared, `swaps == 1`, nothing ordered, ceiling re-checked); refused: wrong chat, wrong user, stale message id, status not pending, `swaps == 2`, line without alternative, malformed callback data, expired record; double-tap on the same message (second tap sees `swaps`/message id changed and does nothing); cart-clear failure keeps the old record untouched; staging failure after clear leaves a truthful message and a record that is not `pending` with live buttons (blocked/failed per existing semantics, `cart_touched` true); an alternative that is forbidden/fluid/over ceiling is refused.
- [ ] **Step 2-4:** FAIL, implement, PASS + full suite. **Step 5: Commit** `feat(approve): one-tap swap to the proposed alternative`.

---

### Task 5: Immediate order verification

**Files:** Modify `parts_order.py`, `order_match.py` only if a helper is needed, `parts_order_helpers.py`, `tests/test_parts_order.py`.

**Interfaces:**
- Produces `verify_now(portal, booking_id: str, record: dict, sleep=time.sleep, tries: int = 3, wait_s: int = 10) -> str | None`: polls `portal.list_sent_orders()`, runs the same matching as the daily checker (`order_match.resolve_orders` with all other records' `verified_numbers` as claimed, the record's `updated_at` date as order date, plate/model as the checker does) and returns the numbers text for a unique match, else None. Never raises (all errors logged by exception class only, return None).
- `execute_order`: after `orders_state.put(booking_id, "ordered", ...)`, call `verify_now`; on a number, store `verified_at` + `verified_numbers` (same shape and 120-char cut as the checker, via the existing `orders_state` setter the checker uses) and make the Beställd text `Ordernummer: <numbers> (verifierad)`; otherwise unchanged. A failure or timeout in `verify_now` must not change `execute_order`'s return value or skip the Beställd message.

- [ ] **Step 1: Failing tests:** unique match on the 2nd poll -> stored + text; none after 3 polls -> None, text unchanged, sleep called twice; two candidate rows -> None (ambiguous); another record's claimed numbers excluded; `list_sent_orders` raising -> None and the order is still `ordered` with the normal message; checker later skips a record that already has `verified_at`.
- [ ] **Step 2-4:** FAIL, implement, PASS + full suite. **Step 5: Commit** `feat(parts): verify the placed order right after checkout`.

---

### Task 6: Docs and package verification

**Files:** Modify root `SKILL.md`, `.claude/skills/meca-agent/SKILL.md`, `README.md`, `parts_rules.yaml` comments, the handoff doc (append "Package 2").

- [ ] **Step 1:** Document the new rules keys with defaults and examples, alternatives and the swap flow (limits, owner-only, what happens on failure), immediate verification, and the Telegram button layout. Keep root `SKILL.md` accurate (the improve agent reads it).
- [ ] **Step 2:** Run `pytest -q`, `cd web && node --test`, `py_compile` on changed Python files, YAML parse of the workflows; `git status` clean; `file agent.py` CRLF. Report outputs. No code changes here; list inconsistencies instead of fixing them.
- [ ] **Step 3: Commit** `docs: package 2 (rules as data, alternatives, swap, immediate verification)`.
