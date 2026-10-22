// The four API handlers, built from injected dependencies so tests and the demo
// server use exactly the same code as the Vercel functions.
import {
  assertSecret, assertPassword, checkPassword, signSession, verifySession,
  sessionCookie, clearedCookie, readSessionCookie,
} from './auth.js';
import { createLimiter, createBrake } from './ratelimit.js';
import { GitHubError } from './github.js';
import { summarize } from './summarize.js';
import {
  sendJson, sendError, RequestError, assertDashboardPost, readJsonBody, assertKeys, clientIp, rateLimitKey,
  BODY_TIMEOUT_MS,
} from './http.js';

export const STATE_FILES = {
  orders: 'config/orders.json',
  history: 'logs/run_history.jsonl',
  seen: 'config/seen_ids.txt',
  alerts: 'config/alerts.json',
  watchdogAlerts: 'config/watchdog_alerts.json',
};

// The only workflows the dashboard may start. Key = request value, value = file.
export const WORKFLOWS = { approve: 'approve.yml', bookings: 'bookings.yml' };
export const RUN_COOLDOWN_MS = 30_000;
export const LOGIN_LIMIT = { attempts: 5, windowMs: 10 * 60_000 };
// Per instance: after `failures` failed logins in the window, a password is only evaluated
// after `delayMs`, for at most `maxWaiting` requests at once; the rest get 429 immediately
// without evaluation.
export const BRAKE = { failures: 100, windowMs: 10 * 60_000, delayMs: 2000, maxWaiting: 2 };
const TOO_MANY = 'För många försök. Vänta en stund och försök igen.';
export const ORDER_OVERVIEW_URL = 'https://pro.meca.se/INTERSHOP/web/WFS/Mekonomen-MecaB2BSE-Site/sv_SE/-/SEK/ViewOrderOverview-Start';

const NOT_CONFIGURED = 'Instrumentpanelen är inte konfigurerad';
const LOGIN_FAILED = 'Inloggningen misslyckades';

function githubMessage(err) {
  switch (err instanceof GitHubError ? err.code : 'other') {
    case 'rate_limited': return [503, 'GitHub begränsar anropen just nu. Försök igen om en stund.'];
    case 'timeout': return [504, 'GitHub svarade inte i tid. Försök igen.'];
    case 'unauthorized':
    case 'forbidden': return [502, 'Åtkomst till repot nekades. Kontrollera GITHUB_TOKEN.'];
    case 'not_found': return [502, 'Repot eller workflowen hittades inte. Kontrollera GITHUB_REPO.'];
    case 'rejected': return [502, 'GitHub avvisade starten av körningen.'];
    case 'config': return [500, NOT_CONFIGURED];
    default: return [502, 'Kunde inte nå GitHub. Försök igen.'];
  }
}

/**
 * deps: {
 *   env: { DASHBOARD_PASSWORD, SESSION_SECRET },
 *   source: () => { getFile, getLatestCommit, dispatch, actionsUrl }   (lazy: may throw GitHubError('config'))
 *   now?: () => ms, log?: (msg) => void,
 *   demo?: boolean        local demo only: short password allowed, cookie without Secure
 *   trustProxy?: boolean  true on Vercel: trust the platform's client-IP and proto headers
 *   sleep?: (ms) => Promise
 * }
 */
export function createHandlers({ env, source, now = Date.now, log, demo = false, trustProxy = false,
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)), bodyTimeoutMs = BODY_TIMEOUT_MS,
  checkPasswordImpl = checkPassword }) {
  log ??= (m) => console.error(m);
  const loginLimiter = createLimiter({ limit: LOGIN_LIMIT.attempts, windowMs: LOGIN_LIMIT.windowMs, clock: now });
  const brake = createBrake({ threshold: BRAKE.failures, windowMs: BRAKE.windowMs, clock: now });
  const cookieOpts = { secure: !demo };
  const postOpts = { trustProxy };
  let waiting = 0; // logins currently held by the brake
  const lastRun = new Map(); // workflow -> ms

  function configured(res) {
    try {
      assertSecret(env.SESSION_SECRET);
    } catch {
      log('dashboard: SESSION_SECRET missing or weak; refusing to serve');
      sendError(res, 500, NOT_CONFIGURED);
      return false;
    }
    try {
      if (!demo) assertPassword(env.DASHBOARD_PASSWORD);
    } catch {
      log('dashboard: DASHBOARD_PASSWORD missing or too weak; refusing to serve');
      sendError(res, 500, NOT_CONFIGURED);
      return false;
    }
    return true;
  }

  function authed(req) {
    return verifySession(readSessionCookie(req), env.SESSION_SECRET, now(), env.DASHBOARD_PASSWORD);
  }

  function fail(res, err) {
    if (err instanceof RequestError) return sendError(res, err.status, err.message);
    const [status, message] = githubMessage(err);
    log(`dashboard: request failed (${err instanceof GitHubError ? err.code : 'internal'})`);
    return sendError(res, status, message);
  }

  // A correct password resets only this client's counter; a wrong one counts towards the brake.
  function evaluate(res, password, ip) {
    if (typeof password === 'string' && checkPasswordImpl(password, env.DASHBOARD_PASSWORD, env.SESSION_SECRET)) {
      loginLimiter.reset(ip);
      const token = signSession(env.SESSION_SECRET, now(), env.DASHBOARD_PASSWORD);
      return sendJson(res, 200, { ok: true }, { 'Set-Cookie': sessionCookie(token, cookieOpts) });
    }
    brake.record();
    return sendError(res, 401, LOGIN_FAILED);
  }

  async function login(req, res) {
    if (!configured(res)) return;
    try {
      assertDashboardPost(req, postOpts);
      const body = await readJsonBody(req, undefined, { timeoutMs: bodyTimeoutMs });
      assertKeys(body, ['password']);
      // Per-client limit first; a limited request is answered at once, password not evaluated.
      const ip = rateLimitKey(clientIp(req, { trustProxy }));
      const limit = loginLimiter.hit(ip);
      if (!limit.allowed) return sendError(res, 429, TOO_MANY, { 'Retry-After': String(limit.retryAfterS) });
      if (!brake.tripped()) return evaluate(res, body.password, ip);
      // Brake tripped (many failures across all clients): the password is evaluated for at most
      // BRAKE.maxWaiting requests at a time, each only after BRAKE.delayMs. Everyone else gets
      // the same 429 at once WITHOUT evaluation, so a 429 reveals nothing about the password
      // and guesses per second stay bounded. Trade-off, by design: during an active flood a
      // fresh owner login may get 429 for a while too. Existing sessions (12 h) keep working,
      // /api/status and /api/run are unaffected; retry later, or enable Vercel Deployment
      // Protection / rotate the password.
      if (waiting >= BRAKE.maxWaiting) return sendError(res, 429, TOO_MANY, { 'Retry-After': '2' });
      waiting += 1;
      try {
        await sleep(BRAKE.delayMs);
        return evaluate(res, body.password, ip);
      } finally {
        waiting -= 1;
      }
    } catch (err) {
      return fail(res, err);
    }
  }

  async function logout(req, res) {
    if (!configured(res)) return;
    try {
      assertDashboardPost(req, postOpts);
    } catch (err) {
      return fail(res, err);
    }
    const ok = authed(req);
    return sendJson(res, ok ? 200 : 401, ok ? { ok: true } : { error: 'Inte inloggad' },
      { 'Set-Cookie': clearedCookie(cookieOpts) });
  }

  async function status(req, res) {
    if (!configured(res)) return;
    if (req.method !== 'GET') return sendError(res, 405, 'Metoden stöds inte');
    // The demo flag only lets the login page show the demo password hint.
    if (!authed(req)) return sendJson(res, 401, demo ? { error: 'Inte inloggad', demo: true } : { error: 'Inte inloggad' });
    try {
      const src = source();
      const [ordersText, historyText, seenText, alertsText, watchdogAlertsText, commit] = await Promise.all([
        src.getFile(STATE_FILES.orders),
        src.getFile(STATE_FILES.history),
        src.getFile(STATE_FILES.seen),
        src.getFile(STATE_FILES.alerts),
        src.getFile(STATE_FILES.watchdogAlerts),
        src.getLatestCommit(),
      ]);
      const vm = summarize({ ordersText, historyText, seenText, alertsText, watchdogAlertsText, commit, now: now() });
      if (demo && /[?&]stale=1(&|$)/.test(req.url ?? '')) {
        vm.freshness = { ...vm.freshness, ageHours: 52, stale: true,
          committedAt: new Date(now() - 52 * 3_600_000).toISOString() };
      }
      vm.links = { actions: src.actionsUrl(), orderOverview: ORDER_OVERVIEW_URL };
      vm.demo = demo;
      return sendJson(res, 200, vm);
    } catch (err) {
      return fail(res, err);
    }
  }

  async function run(req, res) {
    if (!configured(res)) return;
    try {
      assertDashboardPost(req, postOpts);
      if (!authed(req)) return sendError(res, 401, 'Inte inloggad');
      const body = await readJsonBody(req);
      assertKeys(body, ['workflow']);
      if (typeof body.workflow !== 'string' || !Object.hasOwn(WORKFLOWS, body.workflow)) {
        return sendError(res, 400, 'Okänd körning');
      }
      const file = WORKFLOWS[body.workflow];
      const last = lastRun.get(file) ?? -Infinity;
      if (now() - last < RUN_COOLDOWN_MS) {
        const wait = Math.ceil((RUN_COOLDOWN_MS - (now() - last)) / 1000);
        return sendError(res, 429, 'Körningen startades nyss. Vänta en stund.', { 'Retry-After': String(wait) });
      }
      const src = source();
      lastRun.set(file, now());
      try {
        await src.dispatch(file);
      } catch (err) {
        lastRun.delete(file);
        throw err;
      }
      return sendJson(res, 202, { ok: true, workflow: body.workflow, actionsUrl: src.actionsUrl(file) });
    } catch (err) {
      return fail(res, err);
    }
  }

  return { login, logout, status, run };
}
