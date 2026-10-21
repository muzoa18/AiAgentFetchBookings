// The four API handlers, built from injected dependencies so tests and the demo
// server use exactly the same code as the Vercel functions.
import {
  assertSecret, checkPassword, signSession, verifySession,
  sessionCookie, clearedCookie, readSessionCookie,
} from './auth.js';
import { createLimiter } from './ratelimit.js';
import { GitHubError } from './github.js';
import { summarize } from './summarize.js';
import {
  sendJson, sendError, RequestError, assertDashboardPost, readJsonBody, assertKeys, clientIp,
} from './http.js';

export const STATE_FILES = {
  orders: 'config/orders.json',
  history: 'logs/run_history.jsonl',
  seen: 'config/seen_ids.txt',
};

// The only workflows the dashboard may start. Key = request value, value = file.
export const WORKFLOWS = { approve: 'approve.yml', bookings: 'bookings.yml' };
export const RUN_COOLDOWN_MS = 30_000;
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
 *   now?: () => ms, log?: (msg) => void, demo?: boolean
 * }
 */
export function createHandlers({ env, source, now = Date.now, log, demo = false }) {
  log ??= (m) => console.error(m);
  const loginLimiter = createLimiter({ limit: 5, windowMs: 10 * 60_000, clock: now });
  const lastRun = new Map(); // workflow -> ms

  function configured(res) {
    try {
      assertSecret(env.SESSION_SECRET);
    } catch {
      log('dashboard: SESSION_SECRET missing or weak; refusing to serve');
      sendError(res, 500, NOT_CONFIGURED);
      return false;
    }
    if (typeof env.DASHBOARD_PASSWORD !== 'string' || env.DASHBOARD_PASSWORD.length < 1) {
      log('dashboard: DASHBOARD_PASSWORD missing; refusing to serve');
      sendError(res, 500, NOT_CONFIGURED);
      return false;
    }
    return true;
  }

  function authed(req) {
    return verifySession(readSessionCookie(req), env.SESSION_SECRET, now());
  }

  function fail(res, err) {
    if (err instanceof RequestError) return sendError(res, err.status, err.message);
    const [status, message] = githubMessage(err);
    log(`dashboard: request failed (${err instanceof GitHubError ? err.code : 'internal'})`);
    return sendError(res, status, message);
  }

  async function login(req, res) {
    if (!configured(res)) return;
    try {
      assertDashboardPost(req);
      const ip = clientIp(req);
      const limit = loginLimiter.hit(ip);
      if (!limit.allowed) {
        return sendError(res, 429, 'För många försök. Vänta en stund och försök igen.',
          { 'Retry-After': String(limit.retryAfterS) });
      }
      const body = await readJsonBody(req);
      assertKeys(body, ['password']);
      if (typeof body.password !== 'string' || !checkPassword(body.password, env.DASHBOARD_PASSWORD, env.SESSION_SECRET)) {
        return sendError(res, 401, LOGIN_FAILED);
      }
      loginLimiter.reset(ip); // only failed attempts accumulate
      return sendJson(res, 200, { ok: true }, { 'Set-Cookie': sessionCookie(signSession(env.SESSION_SECRET, now())) });
    } catch (err) {
      return fail(res, err);
    }
  }

  async function logout(req, res) {
    if (!configured(res)) return;
    try {
      assertDashboardPost(req);
    } catch (err) {
      return fail(res, err);
    }
    const ok = authed(req);
    return sendJson(res, ok ? 200 : 401, ok ? { ok: true } : { error: 'Inte inloggad' },
      { 'Set-Cookie': clearedCookie() });
  }

  async function status(req, res) {
    if (!configured(res)) return;
    if (req.method !== 'GET') return sendError(res, 405, 'Metoden stöds inte');
    // The demo flag only lets the login page show the demo password hint.
    if (!authed(req)) return sendJson(res, 401, demo ? { error: 'Inte inloggad', demo: true } : { error: 'Inte inloggad' });
    try {
      const src = source();
      const [ordersText, historyText, seenText, commit] = await Promise.all([
        src.getFile(STATE_FILES.orders),
        src.getFile(STATE_FILES.history),
        src.getFile(STATE_FILES.seen),
        src.getLatestCommit(),
      ]);
      const vm = summarize({ ordersText, historyText, seenText, commit, now: now() });
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
      assertDashboardPost(req);
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
