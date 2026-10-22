import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHandlers, WORKFLOWS } from '../lib/handlers.js';
import { GitHubError } from '../lib/github.js';
import { checkPassword, signSession } from '../lib/auth.js';
import { buildHandlers } from '../lib/runtime.js';

const PW = 'fake-horse-lamp-river-test';
const env = { DASHBOARD_PASSWORD: PW, SESSION_SECRET: 'test-secret-0123456789-abcdefghijklmnop' };
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

function setup({ source = fakeSource(), envOverride = env, sleep = async () => {} } = {}) {
  const logs = [];
  const sleeps = [];
  const checks = []; // passwords actually evaluated
  const h = createHandlers({
    env: envOverride, source: () => source, now: () => NOW, log: (m) => logs.push(m),
    trustProxy: true, sleep: async (ms) => { sleeps.push(ms); await sleep(ms); },
    checkPasswordImpl: (...a) => { checks.push(a[0]); return checkPassword(...a); },
  });
  return { h, source, logs, sleeps, checks };
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

const cookie = () => `dash_session=${signSession(env.SESSION_SECRET, NOW, PW)}`;
const authed = (opts) => req({ ...opts, headers: { cookie: cookie(), ...(opts?.headers ?? {}) } });

test('login with the right password sets a secure session cookie', async () => {
  const { h } = setup();
  const r = res();
  await h.login(req({ body: { password: PW } }), r);
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
  for (const body of ['{bad', '[]', { password: 1 }, { password: PW, admin: true }, {}]) {
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
  await h.login(req({ body: { password: PW }, ip: '10.9.9.9' }), r);
  assert.equal(r.statusCode, 429);
  assert.ok(Number(r.headers['retry-after']) > 0);
  const other = res();
  await h.login(req({ body: { password: PW }, ip: '10.1.1.1' }), other);
  assert.equal(other.statusCode, 200);
});

test('successful logins do not use up the attempt budget', async () => {
  const { h } = setup();
  for (let i = 0; i < 8; i++) {
    const r = res();
    await h.login(req({ body: { password: PW }, ip: '10.7.7.7' }), r);
    assert.equal(r.statusCode, 200);
  }
});

test('a weak secret or missing password refuses to serve', async () => {
  for (const e of [{ ...env, SESSION_SECRET: 'short' }, { ...env, SESSION_SECRET: undefined }, { ...env, DASHBOARD_PASSWORD: '' },
    { ...env, DASHBOARD_PASSWORD: 'test-password' }, { ...env, DASHBOARD_PASSWORD: 'aaaaaaaaaaaaaaaaaaaaaaaa' }]) {
    const { h } = setup({ envOverride: e });
    const r = res();
    await h.login(req({ body: { password: PW } }), r);
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
  const cases = [['rate_limited', 503], ['timeout', 504], ['unauthorized', 502], ['upstream', 502], ['too_large', 502]];
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

test('the password-strength refusal never reveals the password', async () => {
  const { h } = setup({ envOverride: { ...env, DASHBOARD_PASSWORD: 'short-secret' } });
  const r = res();
  await h.login(req({ body: { password: 'short-secret' } }), r);
  assert.equal(r.statusCode, 500);
  assert.equal(r.json.error, 'Instrumentpanelen är inte konfigurerad');
  assert.ok(!r.body.includes('short-secret'));
  const good = setup({ envOverride: { ...env, DASHBOARD_PASSWORD: 'sixteen-chars-ok' } });
  const g = res();
  await good.h.login(req({ body: { password: 'sixteen-chars-ok' } }), g);
  assert.equal(g.statusCode, 200);
});

test('changing the password logs every session out', async () => {
  const { h } = setup({ envOverride: { ...env, DASHBOARD_PASSWORD: 'a-brand-new-fake-phrase' } });
  const r = res();
  await h.status(authed({ method: 'GET' }), r);
  assert.equal(r.statusCode, 401, 'a cookie minted under the old password is rejected');
});

test('rate limit keys IPv6 clients by /64', async () => {
  const { h } = setup();
  for (let i = 1; i <= 5; i++) {
    const r = res();
    await h.login(req({ body: { password: 'nope' }, ip: `2001:db8:5:6::${i}` }), r);
    assert.equal(r.statusCode, 401);
  }
  const same = res();
  await h.login(req({ body: { password: PW }, ip: '2001:db8:5:6:ffff:1:2:3' }), same);
  assert.equal(same.statusCode, 429, 'another address in the same /64 is limited too');
  const other = res();
  await h.login(req({ body: { password: PW }, ip: '2001:db8:5:7::1' }), other);
  assert.equal(other.statusCode, 200);
});

test('off Vercel, a spoofed X-Forwarded-For does not dodge the limit', async () => {
  const h = createHandlers({ env, source: () => fakeSource(), now: () => NOW, log: () => {} });
  for (let i = 0; i < 5; i++) {
    const r = res();
    await h.login({ ...req({ body: { password: 'nope' }, ip: `198.51.100.${i}`, headers: { origin: undefined } }), socket: { remoteAddress: '127.0.0.1' } }, r);
  }
  const r = res();
  await h.login({ ...req({ body: { password: PW }, ip: '198.51.100.99', headers: { origin: undefined } }), socket: { remoteAddress: '127.0.0.1' } }, r);
  assert.equal(r.statusCode, 429);
});

async function tripBrake(h) {
  for (let i = 0; i < 100; i++) {
    const r = res();
    await h.login(req({ body: { password: 'nope' }, ip: `10.${i >> 8}.${i & 255}.1` }), r);
    assert.equal(r.statusCode, 401);
  }
}

const flush = () => new Promise((r) => setImmediate(r));

test('brake: a flood of 200 gets at most 2 evaluations and 198 immediate 429s', async () => {
  const pending = [];
  const { h, sleeps, checks } = setup({ sleep: () => new Promise((resolve) => pending.push(resolve)) });
  await tripBrake(h);
  checks.length = 0;
  const results = Array.from({ length: 200 }, () => res());
  const calls = results.map((r, i) => h.login(req({ body: { password: `guess-${i}` }, ip: `172.16.${i >> 8}.${i & 255}` }), r));
  await flush();
  assert.equal(sleeps.length, 2, 'at most two requests hold a slot');
  assert.equal(checks.length, 0, 'nothing is evaluated before the delay');
  const answered = results.filter((r) => r.statusCode !== 0);
  assert.equal(answered.length, 198);
  for (const r of answered) {
    assert.equal(r.statusCode, 429);
    assert.equal(r.headers['retry-after'], '2');
  }
  pending.forEach((resolve) => resolve());
  await Promise.all(calls);
  assert.equal(checks.length, 2, 'exactly the two slot holders were evaluated');
  assert.equal(results.filter((r) => r.statusCode === 401).length, 2);
});

test('brake: 429s look identical for correct and wrong passwords (no oracle)', async () => {
  const pending = [];
  const { h, checks } = setup({ sleep: () => new Promise((resolve) => pending.push(resolve)) });
  await tripBrake(h);
  const holders = [res(), res()];
  const held = holders.map((r, i) => h.login(req({ body: { password: 'x' }, ip: `172.20.0.${i}` }), r));
  await flush();
  checks.length = 0;
  const right = res();
  const wrong = res();
  await h.login(req({ body: { password: PW }, ip: '192.0.2.10' }), right);
  await h.login(req({ body: { password: 'wrong-guess' }, ip: '192.0.2.11' }), wrong);
  assert.equal(right.statusCode, 429, 'documented trade-off: a fresh owner login may be refused during a flood');
  assert.equal(right.body, wrong.body);
  assert.equal(right.statusCode, wrong.statusCode);
  assert.deepEqual(right.headers, wrong.headers);
  assert.equal(checks.length, 0, 'neither password was evaluated');
  pending.forEach((resolve) => resolve());
  await Promise.all(held);
});

test('brake: a correct password succeeds after the delay when a slot is free', async () => {
  const { h, sleeps, checks } = setup();
  await tripBrake(h);
  checks.length = 0;
  const owner = res();
  await h.login(req({ body: { password: PW }, ip: '192.0.2.50' }), owner);
  assert.equal(owner.statusCode, 200);
  assert.deepEqual(sleeps, [2000], 'evaluated only after the delay');
  assert.equal(checks.length, 1);
});

test('without the brake a correct password logs in at once', async () => {
  const { h, sleeps } = setup();
  const owner = res();
  await h.login(req({ body: { password: PW } }), owner);
  assert.equal(owner.statusCode, 200);
  assert.deepEqual(sleeps, []);
});

test('brake: a wrong password from a normal client gets 401 after the delay', async () => {
  const { h, sleeps } = setup();
  await tripBrake(h);
  const r = res();
  await h.login(req({ body: { password: 'typo' }, ip: '192.0.2.51' }), r);
  assert.equal(r.statusCode, 401);
  assert.deepEqual(sleeps, [2000]);
});

test('brake: an existing session keeps working during a flood', async () => {
  const pending = [];
  const { h } = setup({ sleep: () => new Promise((resolve) => pending.push(resolve)) });
  await tripBrake(h);
  const flood = Array.from({ length: 20 }, (_, i) => h.login(req({ body: { password: 'x' }, ip: `172.30.0.${i}` }), res()));
  await flush();
  const s = res();
  await h.status(authed({ method: 'GET' }), s);
  assert.equal(s.statusCode, 200);
  const run = res();
  await h.run(authed({ body: { workflow: 'bookings' } }), run);
  assert.equal(run.statusCode, 202);
  pending.forEach((resolve) => resolve());
  await Promise.all(flood);
});

test('the per-client limit is checked before the password', async () => {
  const { h } = setup();
  for (let i = 0; i < 5; i++) await h.login(req({ body: { password: 'nope' }, ip: '198.51.100.201' }), res());
  const r = res();
  await h.login(req({ body: { password: PW }, ip: '198.51.100.201' }), r);
  assert.equal(r.statusCode, 429, 'even the right password is refused once this client is limited');
  const other = res();
  await h.login(req({ body: { password: PW }, ip: '198.51.100.202' }), other);
  assert.equal(other.statusCode, 200);
  const still = res();
  await h.login(req({ body: { password: PW }, ip: '198.51.100.201' }), still);
  assert.equal(still.statusCode, 429, "another client's success does not reset this client's counter");
});

test('a stalled login body times out', async () => {
  const h = createHandlers({ env, source: () => fakeSource(), now: () => NOW, log: () => {}, trustProxy: true, bodyTimeoutMs: 20 });
  const stalled = req({ headers: {} });
  delete stalled.body;
  let destroyed = false;
  stalled.on = () => {}; // never emits data or end
  stalled.destroy = () => { destroyed = true; };
  const r = res();
  await h.login(stalled, r);
  assert.equal(r.statusCode, 408);
  assert.ok(destroyed);
});

test('rate-limited requests are answered at once, even while the brake is on', async () => {
  const { h, sleeps, checks } = setup();
  await tripBrake(h);
  for (let i = 0; i < 5; i++) await h.login(req({ body: { password: 'nope' }, ip: '198.51.100.200' }), res());
  const before = sleeps.length;
  const evaluated = checks.length;
  const r = res();
  await h.login(req({ body: { password: PW }, ip: '198.51.100.200' }), r);
  assert.equal(r.statusCode, 429);
  assert.equal(sleeps.length, before, 'no sleep for a rate-limited request');
  assert.equal(checks.length, evaluated, 'the password of a rate-limited request is not evaluated');
});

test('the api entry path ignores DEMO; demo needs the explicit option', async () => {
  const live = buildHandlers({ ...env, DEMO: '1' }, { log: () => {} });
  const d = res();
  await live.login(req({ body: { password: 'demo' }, headers: { origin: undefined } }), d);
  assert.equal(d.statusCode, 401, 'DEMO=1 does not enable the demo password');
  const real = res();
  await live.login(req({ body: { password: PW }, headers: { origin: undefined } }), real);
  assert.equal(real.statusCode, 200);
  assert.match(real.headers['set-cookie'], /; Secure;/, 'production cookies are Secure');
  const s = res();
  await live.status(req({ method: 'GET', headers: { cookie: real.headers['set-cookie'].split(';')[0] } }), s);
  assert.notEqual(s.json.demo, true);
  assert.throws(() => buildHandlers({ VERCEL: '1' }, { demo: true }), /not allowed on Vercel/);
});

test('demo mode: password "demo", cookie without Secure, all statuses present', async () => {
  const h = buildHandlers({}, { demo: true });
  const login = res();
  await h.login(req({ body: { password: 'demo' }, headers: { origin: undefined } }), login);
  assert.equal(login.statusCode, 200);
  assert.ok(!login.headers['set-cookie'].includes('Secure'), 'http://localhost cannot store Secure cookies everywhere');
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
  assert.ok(s.json.orders.some((o) => o.detailUrl), 'demo links pass the allow-list');
});

test('status fetches both alert files; a missing alert file is just null', async () => {
  const { h, source } = setup();
  const r = res();
  await h.status(authed({ method: 'GET' }), r);
  const paths = source.calls.filter((c) => c[0] === 'getFile').map((c) => c[1]);
  assert.ok(paths.includes('config/alerts.json'));
  assert.ok(paths.includes('config/watchdog_alerts.json'));
  assert.equal(r.statusCode, 200);
  assert.deepEqual(r.json.alerts, []);
});

test('status merges alerts from both files', async () => {
  const entry = (text) => ({ text, severity: 'warning', first_seen: '2026-10-21T08:00:00+00:00',
    last_seen: '2026-10-21T09:00:00+00:00', count: 1, last_sent: null, resolved_at: null });
  const source = fakeSource({
    async getFile(path) {
      if (path === 'config/alerts.json') return JSON.stringify({ alerts: { a: entry('<b>A</b>') } });
      if (path === 'config/watchdog_alerts.json') return JSON.stringify({ alerts: { w: entry('W') } });
      return null;
    },
  });
  const { h } = setup({ source });
  const r = res();
  await h.status(authed({ method: 'GET' }), r);
  assert.deepEqual(r.json.alerts.map((a) => a.text).sort(), ['<b>A</b>', 'W']);
});

test('demo mode shows one warning and one error alert', async () => {
  const h = buildHandlers({}, { demo: true });
  const login = res();
  await h.login(req({ body: { password: 'demo' }, headers: { origin: undefined } }), login);
  const token = login.headers['set-cookie'].split(';')[0];
  const s = res();
  await h.status(req({ method: 'GET', headers: { cookie: token } }), s);
  assert.deepEqual(s.json.alerts.map((a) => a.severity).sort(), ['error', 'warning']);
  assert.equal(typeof s.json.lastSuccessAgeHours, 'number');
});

test('demo data: one order carries a portal status', async () => {
  const { createDemoSource } = await import('../lib/demo.js');
  const { summarize } = await import('../lib/summarize.js');
  const src = createDemoSource();
  const vm = summarize({ ordersText: await src.getFile('config/orders.json') });
  assert.ok(vm.orders.some((o) => typeof o.portalStatus === 'string' && o.portalStatus));
});
