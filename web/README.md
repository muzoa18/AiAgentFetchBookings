# MECA-agenten dashboard

A small, read-only web page that shows what the MECA booking agent is doing:
orders waiting for approval in Telegram, placed and verified orders, blocked or
failed orders and why, bookings waiting for Klar, and the recent run history.
It also has two buttons that start the `approve.yml` and `bookings.yml`
workflows immediately instead of waiting for the schedule.

Approving, cancelling and Klar stay in Telegram. The dashboard never changes
the agent's state files.

No frameworks and no runtime dependencies: static files in `public/`, Node
serverless functions in `api/`, pure modules in `lib/`, Node built-ins only.

## Try the demo

Requires Node 20 or newer. Works the same on Windows, macOS and Linux.

```bash
cd web
npm run demo
```

Open http://localhost:3000 and log in with the password `demo`. The demo uses
invented data (every status, a verified and an unverified order, a blocked
order with a reason, failed runs in the history). The "Kör nu" buttons
pretend to succeed and nothing is sent to GitHub. Add `?stale=1` to the URL to
see the stale-data warning.

Run the tests (no network needed):

```bash
cd web
npm test
```

## Deploy on Vercel

1. In Vercel, choose **Add New → Project** and import this GitHub repository.
2. Set **Root Directory** to `web`. Leave the framework preset as **Other**,
   with no build command. `vercel.json` sets the output directory to `public`,
   and the functions in `api/` are picked up automatically.
3. Add the environment variables below (Production, plus Preview if you use
   preview deployments), then deploy.

| Variable | Required | Value |
|---|---|---|
| `DASHBOARD_PASSWORD` | yes | The shared login password. At least 16 characters with at least 6 different characters, otherwise the API refuses every request. Use a passphrase of four random words, e.g. `kaffe ratt vinter lampa`. |
| `SESSION_SECRET` | yes | Random, at least 32 characters. Generate with `node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"`. If it is missing or weak, the API refuses every request. |
| `GITHUB_TOKEN` | yes | Fine-grained personal access token (see below). |
| `GITHUB_REPO` | yes | `owner/name` of this repository. |
| `GITHUB_BRANCH` | no | Branch the workflows commit state to. Default `main`. |

The deployed functions always run in live mode. Demo mode can only be started
by `dev-server.js --demo` on your own machine, and an environment variable
called `DEMO` has no effect.

### The GitHub token

GitHub → Settings → Developer settings → Personal access tokens →
**Fine-grained tokens** → Generate new token:

- **Resource owner:** the account that owns the repo.
- **Repository access:** *Only select repositories* → this repository only.
- **Repository permissions:**
  - **Contents: Read-only**, to read `config/orders.json`,
    `logs/run_history.jsonl`, `config/seen_ids.txt` and the latest commit.
  - **Actions: Read and write**, to start `workflow_dispatch` runs.
  - Metadata: Read-only (added automatically).
  - Everything else: no access.
- **Expiration:** pick a date and put a reminder in your calendar.

With this token, anyone holding it could read this repository's files and
manage its Actions: start, cancel or re-run workflows, enable or disable them,
and delete run logs. It only works on this one repository. It cannot push code, change
secrets, or touch other repositories. The server only ever asks GitHub to
start `approve.yml` or `bookings.yml`.

### Rotating secrets

- **Password:** change `DASHBOARD_PASSWORD` in Vercel → Settings →
  Environment Variables, then redeploy. All sessions end, because the session
  key is derived from the password, so everyone has to log in again.
- **Session secret:** set a new random `SESSION_SECRET` and redeploy. This
  also ends every existing session.
- **GitHub token:** create a new token, update `GITHUB_TOKEN`, redeploy, then
  revoke the old token on GitHub.

## Security model

- **One shared password.** It must be at least 16 characters with 6 or more
  different characters. It is compared with HMAC and `timingSafeEqual`, and
  errors are generic.
- **Login throttling.** Failed logins are limited to 5 per 10 minutes per
  client. IPv4 is counted per address and IPv6 per /64 network. On Vercel the
  client IP comes from the platform's `x-vercel-forwarded-for` / `x-real-ip`
  headers. Locally these headers are ignored and the socket address is used.
  After 100 failed logins in 10 minutes across all clients, every login
  attempt waits about 2 s. That slows guessing but never locks the owner out.
  **Caveat:** these counters live in memory in each Vercel function instance.
  They are reset on a cold start and are not shared between instances, so
  they are best-effort. The real protection is a long passphrase.
- **Session cookie.** It holds an expiry and a nonce signed with HMAC-SHA256.
  The key is derived from `SESSION_SECRET` and a hash of the current
  password. The cookie is `HttpOnly; Secure; SameSite=Strict` with a 12 h
  lifetime. No user data is stored in it. Logging out clears it. The local
  demo leaves out `Secure` because it runs on plain `http://`.
- **Read-only data.** `/api/status` returns only a summarised view model:
  whitelisted fields with types and lengths checked. It never returns raw
  `orders.json`, seen booking IDs, Telegram message IDs, GitHub's response
  bodies or the token.
- **Run-now allow-list.** `POST /api/run` accepts exactly
  `{"workflow": "approve" | "bookings"}` and nothing else. It needs a
  session, `Content-Type: application/json`, the header
  `X-Requested-With: dashboard`, and an `Origin`/`Referer` whose scheme,
  host and port match the request when one is sent. `Origin: null` is
  rejected and `X-Forwarded-Host` is ignored. Bodies are capped at 4 KB. Each workflow has a 30 s server-side cooldown
  (best-effort) and the button is disabled for 30 s.
- **Browser hardening.** A strict CSP (`default-src 'none'`, scripts and
  styles from self only, no inline code), `nosniff`, `no-referrer`,
  `frame-ancestors 'none'`, `Cross-Origin-Resource-Policy: same-origin`,
  `X-Robots-Tag: noindex`, and `no-store` on the API. Every value is put into
  the page with `textContent` and DOM APIs, never `innerHTML`. A link is only
  rendered when it is `https:` to `meca.promeisterportal.com`, `pro.meca.se`
  or `github.com`. Anything else is shown as plain text.
- **GitHub client.** It never follows redirects, refuses responses over
  2 MB, times out after 8 s, and never passes GitHub's error bodies on.
- **Data shown.** Vehicle identifiers only: registration numbers, booking
  numbers and car models. No names or contact details. The state files hold
  none, and the dashboard adds none.

## Data freshness

The dashboard shows the state files as they are in the latest commit on
`GITHUB_BRANCH`. They are only as fresh as the last workflow run that pushed
them. If the agent runs locally and does not push, the page shows old data.
It says so when the newest commit is older than 36 hours. After you press
"Kör nu", the result appears once the workflow has committed. The page
refreshes every 60 s while the tab is visible.

## Files

```
web/
  api/        login.js logout.js status.js run.js  (Vercel functions, thin wrappers)
  lib/        auth.js github.js summarize.js ratelimit.js http.js handlers.js runtime.js demo.js
  public/     index.html app.js render.js links.js style.css robots.txt
  test/       node:test suites (no network)
  dev-server.js  local server for the demo, or for live mode with real env vars
  vercel.json    headers and output directory
```

## Not in v1

- Approve, cancel or Klar from the dashboard. These stay in Telegram, which is
  tied to the owner's account.
- Multi-user login, per-user audit or roles.
- Live status of a started workflow run (follow it on the Actions page).
- Persistent rate limiting across function instances.
