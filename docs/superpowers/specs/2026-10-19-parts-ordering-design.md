# Automatic spare-parts ordering from digital bookings — design

Date: 2026-10-20. Status: draft for approval.

## Intent and why
The workshop (MD Bilreparationer, Eskilstuna) receives digital bookings through the MECA
ProMeister portal. Today the agent scrapes new bookings and SMSes the owner; the owner then
orders the listed spare parts by hand on pro.meca.se. The goal is for the agent to prepare that
parts order itself and, once the owner approves with one tap on their phone, place it. The owner
is a mechanic, not a developer: it must be safe by default and adjustable without editing code.

## Goals
- For each new `Bokning` that lists parts, log into pro.meca.se, load the vehicle by reg number,
  find each part, put it in the cart, and ask the owner to approve via Telegram.
- On approval, place the order. Notify the owner of the result, or why nothing was ordered.
- Keep the existing flow: booking notification and Hanterad → Bekräfta for `Bokning`.
- Replace SMS (46elks, paid) with a free Telegram bot for all notifications and for approval.
- Remove the Gmail trigger (code, secrets, dependencies). The job is time-triggered.
- Code lives in the owner's private GitHub repo.

## Non-goals
- Ordering for `Förfrågan` or `Offert` bookings (no confirmed work yet).
- Ordering any fluids or oils (owner stocks them). Fluids on a booking are only mentioned in the message.
- Customer-facing emails or quotes (the old parts_agent quote-email flow is not being rebuilt).
- Vercel hosting (Playwright/Chromium does not fit serverless limits).
- Handling replies by SMS (would need a paid inbound number and a webhook server).

## Anti-goals (a failure even if it technically works)
- Ordering the same booking twice.
- Ordering an unrelated, wrong or ProMeister-branded part.
- An order above the value ceiling being placed.
- Anyone other than the owner being able to approve an order.
- A silent failure: every booking with parts must end in a message saying ordered, blocked or failed.

## Decisions made with the owner
- **Value ceiling:** 10,000 SEK per booking order. Above it nothing is ordered; the message says blocked.
- **Brand rule:** prefer Bosch; if Bosch has no match use another brand; never ProMeister.
- **Fluids and oils:** never ordered, any kind. The message lists them so the owner knows to prepare them.
- **Ambiguity:** a part that cannot be matched confidently means nothing is ordered for that
  booking, and the message says what failed.
- **Login:** pro.meca.se uses a plain username + password (no 2FA/captcha known).
- **Schedule:** the booking job runs once a day, around 17:00 local, as today.
- **Hosting:** GitHub Actions recommended; the Windows scheduled task stays as a fallback until
  Actions has run cleanly for a couple of weeks. If a portal blocks datacenter IPs on first
  runs, stay on Windows.
- **Notification and approval:** a Telegram bot. The owner already has a Telegram account and
  creates the bot (via @BotFather) and supplies the bot token and their chat id.
- **Order semantics:** an order on pro.meca.se is cancellable and involves no payment, so a wrong
  order is recoverable. The limits still apply.
- **Checkout path:** the owner will supply screenshots of the manual order flow; the builder uses
  them to write the checkout steps.
- **No test account:** development runs against production. A small real cart or order is accepted
  by the owner (can be cancelled/returned).
- **Credentials:** the owner updates them (portal, pro.meca.se, Telegram bot token + chat id,
  Anthropic key).

## Rollout (owner-decided)
- **Phase 1, approve-first:** the agent fills the cart and sends a Telegram message with the
  booking, parts and prices, cart total and fluids to prepare, plus Approve and Cancel buttons.
  Nothing is ordered until the owner taps Approve.
- **Phase 2, automatic:** once the owner trusts phase 1, one setting in `parts_rules.yaml` makes the
  agent place the order itself and just report it, under the same limits.

## Approval mechanics (my calls, not owner-decided)
- **Pending state:** the daily run stores a plan per booking (matched parts, prices, cart total)
  with status `pending`, committed to the repo like the other state.
- **Polling workflow:** a second, light GitHub Actions workflow runs every 20 minutes within working
  hours (05–19 UTC, Mon–Sat) and first checks the state file; if nothing is pending it exits
  immediately. Otherwise it reads Telegram updates. Approval can therefore lag by up to about
  20 minutes inside the window.
- **Ruling (final review):** the poll interval is 20 min within working hours to stay inside the
  GitHub Actions free-minutes quota (adjustable by the owner); approvals tapped outside the window
  are processed the next morning, and the 48 h expiry still applies.
- **On Approve:** a fresh browser session logs in to pro.meca.se, verifies the cart still matches the
  stored plan and the total is within the ceiling, then checks out. Any mismatch fails closed and
  notifies the owner.
- **Who can approve:** only callbacks from the owner's chat id are accepted; everything else is ignored.
- **Expiry:** a pending approval expires after 48 h with a notification; the cart is left for the
  owner to deal with by hand.
- **SMS:** Telegram replaces SMS for all notifications. `sms_sender.py` and the 46elks secrets are
  removed once Telegram has run cleanly.

## Constraints
- Playwright against two JS portals; selectors will need tuning against the live site.
- The agent runs unattended and spends real money, so it must fail closed.
- State must survive between runs (committed back to the repo, as today).
- No secrets in git (bot token, chat id and portal logins live in GitHub Secrets / local config).

## Builder-check answers (calls marked "my call" were made by the designer, not the owner)
- **Booking data shape:** already scraped today. `portal_scraper.py` returns `reg_nr`, `services`,
  `parts` and `fluids` as lists of `{name, qty}`. Names are free text from the portal, so matching
  is fuzzy; a real sample is read from the live portal while building (`inspect_portal.py`).
- **Fitment and quantity (my call):** load the vehicle by reg number so the catalogue filters to
  compatible parts, take quantity from the booking line, and treat a missing reg number or a part
  with no confident match as "do not order" (fail closed).
- **Value ceiling (my call):** compared against the cart total shown at checkout, including VAT
  (the stricter reading). If the total is missing or unreadable, do not order.
- **Retries (my call):** state records a status per booking (pending, ordered, blocked, failed,
  cancelled, expired). Blocked or failed bookings are not retried automatically; the message tells
  the owner to order by hand. Later edits to a booking's parts list are ignored once it has a status.
- **Crash between "order" and confirmation (my call):** the booking id is recorded as "attempted"
  before checkout. An attempted-but-unconfirmed booking is never re-ordered automatically and is
  flagged in a message for the owner to check on pro.meca.se.
- **Infrastructure failures (my call):** a failed pro.meca.se login or a blocked IP sends a Telegram
  message ("parts ordering failed: login") so it is never silent.

## Decisions after live recon of pro.meca.se (2026-10-21, owner-confirmed)
- **Cart is per vehicle**, not per session; several sessions may exist. Every cart operation runs with the booking's vehicle loaded, and the empty-cart precondition applies per reg number.
- **Fitment:** only parts under "Kopplade reservdelar" (linked to the loaded vehicle) are candidates. Prefer Bosch among those; if no linked Bosch part exists for the same part, another linked brand is acceptable (never ProMeister). No linked candidate -> nothing ordered for that booking and the message says so.
- **Exclusions:** discontinued items ("Produkten har utgått") and items marked "Ersätts av" are never candidates.
- **Value ceiling:** compared against the HIGHER of the totals visible on the cart page and the checkout page (not what is actually paid); a missing/unreadable total means no order. The checkout step re-checks this before the final click.
- **Checkout path:** cart page -> "Gå vidare" -> checkout page ("Beställning <REG>") -> "Skicka beställning" (the order is placed here). The confirmation page has not been seen yet.
- **First real order is a supervised live test:** the owner follows it and cancels if anything is wrong; the confirmation-page handling is verified and adjusted against it. Rollout stays approve-first.

## Left to the builder
- Part-matching method (the repo already uses the Anthropic API; model choice, prompt and
  confidence threshold are the builder's call, as long as ambiguity fails closed).
- Mapping from a booking's service/part lines to search terms.
- Module layout, test strategy, state schema details, and how the rules file is wired.
- Exact polling interval (decided: 20 min within working hours, see Approval mechanics).
- What to do about `improve.py` stale file lists and model ID (small cleanup, in scope).
