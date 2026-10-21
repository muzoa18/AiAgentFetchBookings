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

### Option 1: preview file, no server and no Node at runtime

```bash
cd web
npm run preview
```

This writes `web/preview/dashboard-preview.html`, a single file with the
dashboard and invented demo data built in. Double-click it to open it in any
browser. It makes no network requests and has no login step. The "Kör nu"
buttons only show "Startad (demo)". Add `?stale=1` to the address to see the
stale-data warning. The file is gitignored, and it has to be built once on a
machine with Node.

### Option 2: local demo server

Requires Node 20 or newer. On Windows, install **Node.js LTS** from
https://nodejs.org (the default options are fine). Then open **PowerShell** or
**cmd**, not WSL, and run:

```powershell
cd C:\Users\<you>\PycharmProjects\AiAgentFetchBookings\web
npm run demo
```

Open the address the server prints. `http://127.0.0.1:3737` is the
recommended one. Log in with the password `demo`. The demo uses invented data:
every status, a verified and an unverified order, a blocked order with a
reason, and failed runs in the history. The "Kör nu" buttons pretend to
succeed and nothing is sent to GitHub. Add `?stale=1` to the URL to see the
stale-data warning.

Use a different port with `npm run demo -- --port 3738`, which works in
PowerShell, cmd and bash, or with `node dev-server.js --demo --port 3738`.
The server listens only on this computer: 127.0.0.1 and ::1, never the
network.

Run the tests (no network needed):

```bash
cd web
npm test
```

### Felsökning (troubleshooting)

- **Blank page or "Laddar instrumentpanelen…" that never goes away:** press
  F12 and open the **Console** tab. A red error there names the problem. Check
  that you opened the address printed by `npm run demo`, not the
  `index.html` file directly. The server prints `OK – öppna …` once it has
  checked itself.
- **`ERR_CONNECTION_RESET` / "connection refused":** the server is not running
  on this machine. A server started inside WSL or a Claude Code `!` command is
  not always reachable from Windows browsers. Start it in PowerShell or cmd
  instead, or use `npm run preview`.
- **The login form comes back, or a message says the browser did not keep
  the cookie:** use `http://127.0.0.1:PORT`, or try another browser.
  Shields or strict privacy settings, for example in Brave, can block
  cookies on localhost.
- **`Port 3737 används redan / is already in use`:** another program, or an
  earlier demo, is using the port. Stop it with Ctrl+C in its window, or
  start on another port: `npm run demo -- --port 3738`.
- **`Misdirected request` (HTTP 421):** the local server only answers to
  `127.0.0.1:PORT`, `localhost:PORT` and `[::1]:PORT`. This protects
  against DNS rebinding. Open one of those addresses, not the PC's network
  name or IP.
- **`Instrumentpanelen är inte konfigurerad` (live mode):**
  `SESSION_SECRET` must be at least 32 characters, and
  `DASHBOARD_PASSWORD` at least 16 characters with 8 different characters,
  and not an obvious pattern (see the table under Deploy).

## Deploy on Vercel

1. In Vercel, choose **Add New → Project** and import this GitHub repository.
2. Set **Root Directory** to `web`. Leave the framework preset as **Other**,
   with no build command. `vercel.json` sets the output directory to `public`,
   and the functions in `api/` are picked up automatically.
3. Add the environment variables below (Production, plus Preview if you use
   preview deployments), then deploy.

| Variable | Required | Value |
|---|---|---|
| `DASHBOARD_PASSWORD` | yes | The shared login password. It must be at least 16 characters with at least 8 different characters, and must not contain common words or patterns such as `password`, `qwerty`, `123456`, `letmein` or repeated blocks like `aaaa`. Otherwise the API refuses every request. Use **five random words** chosen by dice or a password manager's passphrase generator, not words you pick yourself. Example of the format, *exempel – använd inte detta* (it is rejected on purpose): `granit moln sax tulpan väska`. |
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

- **One shared password.** It must be at least 16 characters (counted as
  characters, not bytes) with 8 or more different characters, and must not be
  an obvious pattern. It is compared with HMAC and `timingSafeEqual`, and
  errors are generic.
- **Login throttling.** Failed logins are limited to 5 per 10 minutes per
  client. IPv4 is counted per address and IPv6 per /64 network. On Vercel the
  client IP comes from the platform's `x-vercel-forwarded-for` / `x-real-ip`
  headers. Locally these headers are ignored and the socket address is used.
  The per-client limit is checked before the password, so a limited request
  is answered with 429 at once and its password is never looked at. After
  100 failed logins in 10 minutes across all clients, the brake trips:
  - Passwords are then checked for at most 2 login requests at a time, each
    only after a 2 s wait.
  - Every other login request gets the same `429` with `Retry-After: 2` at
    once, and its password is **not checked at all**.
  - Because of that, a 429 says nothing about whether a guess was right, and
    guesses per second stay bounded.
  - **Trade-off:** during an active guessing flood, a fresh login by the owner
    may also get 429 ("För många försök") for a while. A session that is
    already logged in (valid for 12 h) is not affected, and the status page
    and "Kör nu" keep working.
  - If you are locked out of a fresh login, retry a minute later. If the
    flood continues, temporarily turn on Vercel Deployment Protection or
    change `DASHBOARD_PASSWORD`.
  A login body that has not arrived within 5 s is dropped (408).
  **Be honest about the limits:** on Vercel, each function instance keeps
  its own counters in memory. They are not shared between instances and are
  reset on every cold start, so an attacker spread across many instances gets
  more tries. **Password strength is the real protection.** If your Vercel
  plan offers it, also turn on Vercel's own **Deployment Protection**
  (Settings → Deployment Protection), so the platform asks for
  authentication before the dashboard is even reached.
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
              cli.js preview-transport.js
  public/     index.html app.js render.js links.js session.js transport.js style.css robots.txt
  test/       node:test suites (no network)
  dev-server.js  local server for the demo (--demo, --port N), or live mode with real env vars
  preview.js     builds preview/dashboard-preview.html (offline, self-contained)
  vercel.json    headers and output directory
```

## Not in v1

- Approve, cancel or Klar from the dashboard. These stay in Telegram, which is
  tied to the owner's account.
- Multi-user login, per-user audit or roles.
- Live status of a started workflow run (follow it on the Actions page).
- Persistent rate limiting across function instances.
