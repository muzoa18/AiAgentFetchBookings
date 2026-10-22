# Package 2: the parts (design)

Date: 2026-10-22. Scope approved by the owner in chat (2026-10-21): "out of stock or expensive part:
the Telegram message proposes an alternative you can accept or reject", "read the order number
right at checkout", "rules in parts_rules.yaml as data". The owner also said: out-of-stock and
expensive parts "must be handled" (answer to question 6 of the improvement list).

## Why

Today a part that is out of stock only gets a note ("OBS: 0 i lager") and is still put in the
cart; an expensive part is not flagged at all; a part with no confident match blocks the whole
order; brand preference is one global list; and a placed order only gets a real order number the
next morning (the order checker). Package 2 makes the proposal smarter and the owner's decision
easier, without ever ordering anything the owner has not approved.

## Goals

1. **Rules as data.** `parts_rules.yaml` gains optional keys with safe defaults (existing file
   keeps working): `brand_prefer_by_part` (mapping part word -> brand list, tried before the
   global `brand_prefer`), `prefer_in_stock` (default true), `expensive_ratio` (default 1.5),
   `expensive_min_diff_sek` (default 100), `max_alternatives` (default 1; allowed 0-3).
2. **Stock- and price-aware choice.** With `prefer_in_stock`, an out-of-stock choice is replaced by
   an in-stock, allowed, picker-confirmed candidate when there is one. A chosen part is flagged
   `expensive` when an allowed, in-stock, picker-confirmed alternative costs at most
   `1/expensive_ratio` of it and the saving is at least `expensive_min_diff_sek`.
3. **Alternatives in the approval message.** Per flagged line the message lists the alternative
   (title, brand, price, stock) and why ("slut" / "dyr: sparar X SEK"), and offers a button
   "Byt <part>" per flagged line next to Godkänn / Avbryt.
4. **One-tap swap.** Tapping "Byt" (owner chat only, only while the record is `pending`, at most
   2 swaps per booking) clears that vehicle's cart, re-stages with the alternative pinned for that
   line, re-verifies the cart exactly as a normal staging does, and sends a NEW approval message
   (the old message's buttons are removed). Nothing is ordered by a swap.
5. **Immediate order verification.** After a successful checkout the agent looks the order up in
   "Skickade Beställningar" in the same session (a few polls); a unique match gives
   `verified_at`/`verified_numbers` and the Beställd message carries the real order numbers. If it
   is not found, the existing "bekräftas senare" flow and the daily checker remain the fallback.

## Non-goals

No new ordering authority: the ceiling, ProMeister ban, fluid ban, "Kopplade reservdelar only",
owner-only approval, `attempted`-before-checkout and the duplicate guard are unchanged. No
Artikel-ID reference lookup (separate), no change to the confirmation-page parsing (never seen),
no auto mode.

## Design

### Rules (`rules.py`, `parts_rules.yaml`)
`Rules` gains the five fields with defaults (frozen dataclass; positional construction in existing
tests keeps working because the new fields come last with defaults). Strict validation, fail closed:
wrong types, negative numbers, `expensive_ratio <= 1`, `max_alternatives` outside 0-3, brand lists of
non-strings -> `ValueError` (the existing "invalid rules file" path). `brand_prefer_by_part` keys are
compared with the same normalisation the matcher uses for line names (`_norm`); a key matches when
it is a whole word of the normalised line name or its synonym term (Pollenfilter = Kupéfilter).

### Matching (`parts_matcher.py`)
`_brand_rank(cand, rules, line_name=None)`: per-part brands (in listed order) rank before the global
list. New `choose_with_alternatives(line, candidates, rules, picker) -> Choice` where
`Choice = (chosen: dict | None, alternatives: list[dict])`; each alternative carries
`alt_reason` ("slut" or "dyr") and `saving_sek`. `choose()` stays and returns `.chosen` semantics
unchanged for callers that do not need alternatives. Candidate order and confidence rules are
unchanged: every alternative must pass the same `_pick` confidence gate (at most one extra picker call
per flagged line). The alternative never comes from a forbidden brand, a fluid, a non-Latin title or
a wrong placement (same filters as `choose`).

### Staging and the message (`parts_order.py`, `parts_order_helpers.py`)
`plan["parts"][i]` gains `alternative` (dict with the fields `add_to_cart` needs plus `alt_reason`,
`saving_sek`) when flagged. `build_summary` renders an "Alternativ" block under the parts.
`notifier.send_with_buttons` accepts rows (`[[(label, data), ...], ...]`); the existing single-row
call stays valid. Buttons: row 1 Godkänn / Avbryt, row 2.. one `Byt <short part name>` per flagged
line with `swap:<booking_id>:<line_index>` (callback data <= 64 bytes; booking ids are short).

### Swap (`approve.py`, `parts_order.py`)
`_swap(config, session, booking_id, record, line_index, rules, cb)`: owner chat, record `pending`,
`message_id` equal to the tapped message, `swaps < 2` and the line has an alternative, else the tap
is answered with a short Swedish reason and nothing changes. Then: clear the cart (existing
`_clear_cart`; a failure stops the swap and says so), `parts_order.restage_with_pins` stages the same
booking with `{line_index: alternative}` pinned (no search for that line, normal matching for the
others, same cart verification, same ceiling), stores `swaps + 1`, and sends the new approval message.
The old message's buttons are cleared (`clear_buttons`). Any failure leaves the usual truthful
Swedish message and the existing "cart may hold parts" handling.

### Immediate verification (`parts_order.py`, `order_match.py`)
After `checkout()` returns, `verify_now(portal, record, others)` polls `portal.list_sent_orders()`
(3 tries, 10 s apart, injectable sleep) and applies the same matching as the order checker
(`order_match.resolve_orders` with the other records' `verified_numbers` as claimed). A unique match
stores `verified_at` + `verified_numbers` and the Beställd text shows the numbers; ambiguous or
none -> nothing stored, text unchanged ("bekräftas senare"). Errors never fail the order (it is
already placed): logged and left to the daily checker.

## Constraints (all tasks)

As package 1 (offline tests, Swedish fixed owner texts, no customer data, no live portals, never place
real orders, no push, commit trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`,
`agent.py` stays CRLF). Python suite 1475 + web 122 must stay green. Update both SKILL.md files, README,
`parts_rules.yaml` comments. Every behaviour that can lead to an order (swap, in-stock replacement)
needs a test that the ceiling, forbidden brands, cart verification and the "owner approval" step
still apply.
