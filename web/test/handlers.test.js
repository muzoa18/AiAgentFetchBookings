import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHandlers, WORKFLOWS } from '../lib/handlers.js';
import { GitHubError } from '../lib/github.js';
import { signSession } from '../lib/auth.js';
import { buildHandlers, isDemo } from '../lib/runtime.js';

const env = { DASHBOARD_PASSWORD: 'test-password', SESSION_SECRET: 'test-secret-0123456789-abcdefghijklmnop' };
const HOST = 'dash.example.test';
const NOW = Date.parse('2026-10-21T12:00:00Z');

function fakeSource(overrides = {}) {
  const calls = [];
  return {
    calls,
    async getFile(path) {
      calls.push(['getFile', path]);
      if (path === 'config/orders.json') {
        return JSON.stringify({ bookings: { a: { status: 'pending', updated_at: '2026-10-21T10:00:00Z',
          plan: { booking_nr: '1', reg_nr: 'QAA111', expected_lines: [{ title: '<img src=x onerror=alert(1)>', qty: 1 }] } } } });
      }
      return null;
    },
    async getLatestCommit() { return { sha: 'abcdef1234567', date: '2026-10-21T11:00:00Z' }; },
    async dispatch(file) { calls.push(['dispatch', file]); },
    actionsUrl(file) { return 'https://github.com/o/r/actions' + (file ? '/workflows/' + file : ''); },
    ...overrides,
  };
}

function setup({ source = fakeSource(), envOverride = env } = {}) {
  const logs = [];
  const h = createHandlers({ env: envOverride, source: () => source, now: () => NOW, log: (m) => logs.push(m) });
  return { h, source, logs };
}

function req({ method = 'POST', body, headers = {}, url = '/api/x', ip = '10.0.0.1' } = {}) {
  return {
    method, url,
    body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body),
    headers: {
      host: HOST,
      ...(method === 'POST' ? { 'content-type': 'application/json', 'x-requested-with': 'dashboard', origin: `https://${HOST}` } : {}),
      'x-forwarded-for': ip,
      ...headers,
    },
  };
}

function res() {
  return {
    statusCode: 0, headers: {}, body: '',
    setHeader(k, v) { this.headers[k.toLowerCase()] = v; },
    end(b) { this.body = b ?? ''; },
    get json() { return JSON.parse(this.body); },
  };
}

const cookie = () => `dash_session=${signSession(env.SESSION_SECRET, NOW)}`;
const authed = (opts) => req({ ...opts, headers: { cookie: cookie(), ...(opts?.headers ?? {}) } });

test('login with the right password sets a secure session cookie', async () => {
  const { h } = setup();
  const r = res();
  await h.login(req({ body: { password: 'test-password' } }), r);
  assert.equal(r.statusCode, 200);
  assert.match(r.headers['set-cookie'], /^dash_session=v1\..*HttpOnly; Secure; SameSite=Strict/);
  assert.equal(r.headers['cache-control'], 'no-store');
  assert.match(r.headers['content-security-policy'], /default-src 'none'/);
});

test('login with a wrong password fails generically and sets no cookie', async () => {
  const { h } = setup();
  const r = res();
  await h.login(req({ body: { password: 'nope' } }), r);
  assert.equal(r.statusCode, 401);
  assert.equal(r.json.error, 'Inloggningen misslyckades');
  assert.equal(r.headers['set-cookie'], undefined);
});

test('login rejects malformed bodies and extra fields', async () => {
  const { h } = setup();
  for (const body of ['{bad', '[]', { password: 1 }, { password: 'test-password', admin: true }, {}]) {
    const r = res();
    await h.login(req({ body }), r);
    assert.equal(r.statusCode, body?.password === 1 ? 401 : 400, JSON.stringify(body));
  }
});

test('login is rate limited after 5 attempts per IP', async () => {
  const { h } = setup();
  for (let i = 0; i < 5; i++) {
    const r = res();
    await h.login(req({ body: { password: 'nope' }, ip: '10.9.9.9' }), r);
    assert.equal(r.statusCode, 401);
  }
  const r = res();
  await h.login(req({ body: { password: 'test-password' }, ip: '10.9.9.9' }), r);
  assert.equal(r.statusCode, 429);
  assert.ok(Number(r.headers['retry-after']) > 0);
  const other = res();
  await h.login(req({ body: { password: 'test-password' }, ip: '10.1.1.1' }), other);
  assert.equal(other.statusCode, 200);
});

test('a weak secret or missing password refuses to serve', async () => {
  for (const e of [{ ...env, SESSION_SECRET: 'short' }, { ...env, SESSION_SECRET: undefined }, { ...env, DASHBOARD_PASSWORD: '' }]) {
    const { h } = setup({ envOverride: e });
    const r = res();
    await h.login(req({ body: { password: 'test-password' } }), r);
    assert.equal(r.statusCode, 500);
    const s = res();
    await h.status(authed({ method: 'GET' }), s);
    assert.equal(s.statusCode, 500);
  }
});

test('status requires a valid session', async () => {
  const { h, source } = setup();
  for (const headers of [{}, { cookie: 'dash_session=v1.1.2.3' }]) {
    const r = res();
    await h.status(req({ method: 'GET', headers }), r);
    assert.equal(r.statusCode, 401);
  }
  assert.equal(source.calls.length, 0, 'GitHub is not touched without a session');
});

test('status returns only the view model, XSS payload as plain data', async () => {
  const { h } = setup();
  const r = res();
  await h.status(authed({ method: 'GET' }), r);
  assert.equal(r.statusCode, 200);
  const vm = r.json;
  assert.equal(vm.tiles.pending, 1);
  assert.equal(vm.freshness.sha, 'abcdef1');
  assert.equal(vm.orders[0].lines[0].title, '<img src=x onerror=alert(1)>');
  assert.equal(vm.bookings, undefined, 'no raw orders.json');
  assert.equal(r.headers['cache-control'], 'no-store');
});

test('status maps GitHub errors to short generic messages', async () => {
  const cases = [['rate_limited', 503], ['timeout', 504], ['unauthorized', 502], ['upstream', 502]];
  for (const [code, status] of cases) {
    const { h, logs } = setup({ source: fakeSource({ getLatestCommit: async () => { throw new GitHubError(code); } }) });
    const r = res();
    await h.status(authed({ method: 'GET' }), r);
    assert.equal(r.statusCode, status);
    assert.deepEqual(Object.keys(r.json), ['error']);
    assert.ok(!r.body.includes('GitHub request failed'));
    assert.ok(logs.every((l) => !l.includes('fake')));
  }
});

test('an unexpected error is not echoed to the client', async () => {
  const { h } = setup({ source: fakeSource({ getFile: async () => { throw new Error('secret internals'); } }) });
  const r = res();
  await h.status(authed({ method: 'GET' }), r);
  assert.equal(r.statusCode, 502);
  assert.ok(!r.body.includes('secret internals'));
});

test('run dispatches only allow-listed workflows', async () => {
  assert.deepEqual(WORKFLOWS, { approve: 'approve.yml', bookings: 'bookings.yml' });
  const { h, source } = setup();
  const r = res();
  await h.run(authed({ body: { workflow: 'approve' } }), r);
  assert.equal(r.statusCode, 202);
  assert.equal(r.json.actionsUrl, 'https://github.com/o/r/actions/workflows/approve.yml');
  for (const workflow of ['weekly-improve', 'approve.yml', '../bookings', 'constructor', '__proto__', 'toString', 1, null]) {
    const x = res();
    await h.run(authed({ body: { workflow } }), x);
    assert.equal(x.statusCode, 400, String(workflow));
  }
  const extra = res();
  await h.run(authed({ body: { workflow: 'bookings', ref: 'evil' } }), extra);
  assert.equal(extra.statusCode, 400);
  assert.deepEqual(source.calls.filter((c) => c[0] === 'dispatch'), [['dispatch', 'approve.yml']]);
});

test('run has a server-side cooldown per workflow', async () => {
  const { h } = setup();
  const a = res();
  await h.run(authed({ body: { workflow: 'bookings' } }), a);
  const b = res();
  await h.run(authed({ body: { workflow: 'bookings' } }), b);
  assert.equal(a.statusCode, 202);
  assert.equal(b.statusCode, 429);
});

test('run requires a session, the custom header, JSON and same origin', async () => {
  const { h, source } = setup();
  const cases = [
    [req({ body: { workflow: 'approve' } }), 401],
    [authed({ body: { workflow: 'approve' }, headers: { 'x-requested-with': undefined } }), 403],
    [authed({ body: { workflow: 'approve' }, headers: { 'content-type': 'text/plain' } }), 415],
    [authed({ body: { workflow: 'approve' }, headers: { origin: 'https://evil.example' } }), 403],
    [authed({ body: { workflow: 'approve' }, headers: { origin: undefined, referer: 'https://evil.example/x' } }), 403],
    [authed({ body: { workflow: 'approve' }, headers: { 'sec-fetch-site': 'cross-site' } }), 403],
    [authed({ method: 'GET' }), 405],
  ];
  for (const [r0, status] of cases) {
    const r = res();
    await h.run(r0, r);
    assert.equal(r.statusCode, status);
  }
  assert.equal(source.calls.length, 0);
});

test('run maps a GitHub failure to a generic message and allows a retry', async () => {
  let fail = true;
  const { h } = setup({ source: fakeSource({ dispatch: async () => { if (fail) throw new GitHubError('rejected'); } }) });
  const r = res();
  await h.run(authed({ body: { workflow: 'approve' } }), r);
  assert.equal(r.statusCode, 502);
  assert.equal(r.json.error, 'GitHub avvisade starten av körningen.');
  fail = false;
  const again = res();
  await h.run(authed({ body: { workflow: 'approve' } }), again);
  assert.equal(again.statusCode, 202, 'a failed dispatch does not start the cooldown');
});

test('logout clears the cookie', async () => {
  const { h } = setup();
  const r = res();
  await h.logout(authed({ body: {} }), r);
  assert.equal(r.statusCode, 200);
  assert.match(r.headers['set-cookie'], /Max-Age=0/);
  const anon = res();
  await h.logout(req({ body: {} }), anon);
  assert.equal(anon.statusCode, 401);
  assert.match(anon.headers['set-cookie'], /Max-Age=0/);
});

test('demo mode needs DEMO=1 and is never enabled on Vercel', async () => {
  assert.equal(isDemo({ DEMO: '1' }), true);
  assert.equal(isDemo({ DEMO: '1', VERCEL: '1' }), false);
  assert.equal(isDemo({}), false);
  const h = buildHandlers({ DEMO: '1' });
  const login = res();
  await h.login(req({ body: { password: 'demo' } }), login);
  assert.equal(login.statusCode, 200);
  const token = login.headers['set-cookie'].split(';')[0];
  const s = res();
  await h.status(req({ method: 'GET', headers: { cookie: token }, url: '/api/status?stale=1' }), s);
  assert.equal(s.statusCode, 200);
  assert.equal(s.json.demo, true);
  assert.equal(s.json.freshness.stale, true);
  for (const st of ['attempted', 'pending', 'ordered', 'blocked', 'failed', 'cancelled', 'expired']) {
    assert.ok(s.json.counts[st] > 0, st);
  }
  assert.ok(s.json.history.some((r) => r.failed));
  const v = s.json.orders.filter((o) => o.status === 'ordered').map((o) => o.verification.state);
  assert.ok(v.includes('verified') && v.includes('unverified'));
  const prod = buildHandlers({ DEMO: '1', VERCEL: '1' });
  const refused = res();
  await prod.login(req({ body: { password: 'demo' } }), refused);
  assert.equal(refused.statusCode, 500, 'no demo password on Vercel');
});
