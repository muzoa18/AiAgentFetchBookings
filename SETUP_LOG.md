# Setup Log — Hosting the MECA Agent on GitHub Actions

Date: 2026-06-16

This log records every change made to move the MECA Booking Agent from
local Windows Task Scheduler to GitHub Actions, and the reasoning behind each.

---

## Decisions taken (with the user)

1. **Host:** GitHub Actions (not Vercel). Vercel serverless can't run the
   Playwright/Chromium browser automation this agent depends on (Chromium size,
   function timeouts, read-only filesystem). Actions = full Linux VM, native
   Chromium, cron, free secrets, no rewrite.
2. **Trigger:** email-gated. MECA emails "a booking was made" (no details), so
   that email is used as the signal; the agent then scrapes the portal for the
   details. Falls back to "always scrape" if Gmail isn't configured.
3. **Confirm step:** unchanged — keeps auto `Hanterad → Bekräfta` for `Bokning`.
4. **Improve agent:** opens a **Pull Request** for review (changed from the
   initial auto-commit idea) — unattended AI edits should not hit `main` and run
   against the live portal unreviewed.

---

## Files added

| File | Purpose |
|---|---|
| `.gitignore` | Excludes secrets (config.yaml, Gmail token/creds, browser context), `.idea/`, venvs, logs/screenshots. Keeps `logs/run_history.jsonl` tracked. |
| `config/config.example.yaml` | Config template with the env-var name for each field. |
| `email_trigger.py` | Gmail check — was a MECA booking email received? Fails open on any error. |
| `.github/workflows/bookings.yml` | Booking agent: every 30 min (business hrs, UTC), email-gated, commits state back, uploads logs as artifacts. |
| `.github/workflows/weekly-improve.yml` | Weekly: runs improve agent, opens a PR with proposed changes. |
| `SETUP_LOG.md` | This file. |

## Files changed

| File | Change |
|---|---|
| `config_loader.py` | Rewritten to be **env-first**: reads `config.yaml` if present (local), then overlays env vars (GitHub Secrets). Works with no config.yaml in CI. Added defaults + required-field validation. |
| `agent.py` | Added a **trigger gate** at the top of `run()` (`email_trigger.should_run`). Updated the module docstring (no longer Windows Task Scheduler). |
| `improve.py` | Model `claude-sonnet-4-20250514` → **`claude-opus-4-8`**; switched to **structured outputs** (JSON-schema, no more fragile markdown-fence parsing); `load_config()` no longer exits when config.yaml is missing (falls back to env); added **`--apply-all` PR mode** (applies all proposals, syntax-checks each changed `.py`, reverts anything that won't compile, writes `improve_reports/latest.md`). |
| `requirements.txt` | Bumped `anthropic`; added Gmail libraries (`google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `google-auth-httplib2`). |
| `README.md` | Rewritten for the GitHub Actions deployment, secrets setup, and architecture rationale. |

---

## Security actions

- Real credentials that were sitting in `config/config.yaml` are **not** committed
  (gitignored before `git init`). They must move to GitHub Secrets.
- **Recommended:** rotate the MECA password, 46elks API keys, and Anthropic key,
  since they previously lived in a plaintext local file.

---

## Manual steps still required by the user

1. Add the GitHub Secrets / Variables listed in `README.md`.
2. Settings → Actions → General → enable
   *"Allow GitHub Actions to create and approve pull requests"*.
3. (Optional) Set up the Gmail trigger per `GMAIL_SETUP.md` and paste the token /
   credentials JSON into the `GMAIL_TOKEN_JSON` / `GMAIL_CREDENTIALS_JSON` secrets,
   then set the `BOOKING_TRIGGER_MODE=email` variable.
4. Verify the cron times in `bookings.yml` (UTC) match the desired local hours.
