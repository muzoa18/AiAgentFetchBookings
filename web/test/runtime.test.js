// The real api/* entry point (runtime.handlers()) must ignore DEMO completely.
import { test } from 'node:test';
import assert from 'node:assert/strict';

const fakeRes = () => ({
  statusCode: 0, headers: {}, body: '',
  setHeader(k, v) { this.headers[k.toLowerCase()] = v; },
  end(b) { this.body = b ?? ''; },
});
const post = (body) => ({
  method: 'POST', url: '/api/login', body: JSON.stringify(body), socket: { remoteAddress: '127.0.0.1' },
  headers: { host: 'dash.example.test', 'content-type': 'application/json', 'x-requested-with': 'dashboard' },
});

test('api entry with DEMO=1 rejects the demo password and never serves demo data', async () => {
  const saved = { ...process.env };
  Object.assign(process.env, {
    DEMO: '1',
    DASHBOARD_PASSWORD: 'fake-horse-lamp-river-test',
    SESSION_SECRET: 'test-secret-0123456789-abcdefghijklmnop',
  });
  try {
    const { handlers } = await import('../lib/runtime.js');
    const login = fakeRes();
    await handlers().login(post({ password: 'demo' }), login);
    assert.ok([401, 500].includes(login.statusCode), String(login.statusCode));
    assert.equal(login.headers['set-cookie'], undefined);

    const status = fakeRes();
    await handlers().status({ method: 'GET', url: '/api/status', headers: { host: 'dash.example.test' } }, status);
    assert.equal(status.statusCode, 401);
    assert.ok(!status.body.includes('"demo":true'), 'no demo hint on the live path');
    assert.ok(!/DEMO-|QXA112/.test(status.body), 'no demo data');

    // With the real password the live path logs in, but it is still not demo mode.
    const real = fakeRes();
    await handlers().login(post({ password: 'fake-horse-lamp-river-test' }), real);
    assert.equal(real.statusCode, 200);
    assert.match(real.headers['set-cookie'], /; Secure;/);
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
    Object.assign(process.env, saved);
  }
});
