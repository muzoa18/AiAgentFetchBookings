import { test } from 'node:test';
import assert from 'node:assert/strict';
import { assertDashboardPost, clientIp, rateLimitKey, readJsonBody, requestOrigin } from '../lib/http.js';

const base = (headers = {}, extra = {}) => ({
  method: 'POST',
  headers: { host: 'dash.example.test', 'content-type': 'application/json', 'x-requested-with': 'dashboard', ...headers },
  socket: { remoteAddress: '203.0.113.9' },
  ...extra,
});
const ok = (req, opts) => assert.doesNotThrow(() => assertDashboardPost(req, opts));
const denied = (req, opts) => assert.throws(() => assertDashboardPost(req, opts), (e) => e.status === 403);
const VERCEL = { trustProxy: true };

test('IPv6 is keyed by /64, IPv4 by full address', () => {
  assert.equal(rateLimitKey('2001:db8:1:2:aaaa:bbbb:cccc:dddd'), '2001:0db8:0001:0002::/64');
  assert.equal(rateLimitKey('2001:db8:1:2::1'), '2001:0db8:0001:0002::/64');
  assert.equal(rateLimitKey('2001:DB8:1:2:ffff::'), '2001:0db8:0001:0002::/64');
  assert.notEqual(rateLimitKey('2001:db8:1:3::1'), rateLimitKey('2001:db8:1:2::1'));
  assert.equal(rateLimitKey('[2001:db8::1%eth0]'), '2001:0db8:0000:0000::/64');
  assert.equal(rateLimitKey('::1'), '0000:0000:0000:0000::/64');
  assert.equal(rateLimitKey('203.0.113.7'), '203.0.113.7');
  assert.notEqual(rateLimitKey('203.0.113.7'), rateLimitKey('203.0.113.8'));
  assert.equal(rateLimitKey('::ffff:203.0.113.7'), '203.0.113.7');
  assert.equal(rateLimitKey('garbage'), 'other:garbage');
});

test('client IP trusts Vercel headers only behind Vercel', () => {
  const req = base({ 'x-vercel-forwarded-for': '198.51.100.1', 'x-real-ip': '198.51.100.2', 'x-forwarded-for': '198.51.100.3, 10.0.0.1' });
  assert.equal(clientIp(req, VERCEL), '198.51.100.1');
  assert.equal(clientIp(req), '203.0.113.9', 'off Vercel the spoofable headers are ignored');
  assert.equal(clientIp(base({ 'x-forwarded-for': '198.51.100.3, 10.0.0.1' }), VERCEL), '198.51.100.3');
});

test('origin must match scheme, host and port, case-insensitively', () => {
  ok(base({ origin: 'https://dash.example.test' }), VERCEL);
  ok(base({ origin: 'HTTPS://DASH.Example.TEST' }), VERCEL);
  ok(base({ host: 'DASH.example.test', origin: 'https://dash.example.test' }), VERCEL);
  ok(base({ origin: 'https://dash.example.test:443' }), VERCEL);
  denied(base({ origin: 'http://dash.example.test' }), VERCEL);
  denied(base({ origin: 'https://dash.example.test:8443' }), VERCEL);
  denied(base({ origin: 'null' }), VERCEL);
  denied(base({ origin: 'https://evil.example' }), VERCEL);
  denied(base({ referer: 'http://dash.example.test/' }), VERCEL);
  ok(base({ referer: 'https://dash.example.test/some/page?x=1' }), VERCEL);
  // Local dev server: plain http, scheme from the socket.
  ok(base({ host: 'localhost:3000', origin: 'http://localhost:3000' }));
  denied(base({ host: 'localhost:3000', origin: 'http://localhost:3001' }));
  denied(base({ host: 'localhost:3000', origin: 'https://localhost:3000' }));
});

test('X-Forwarded-Host is ignored when matching the origin', () => {
  denied(base({ 'x-forwarded-host': 'evil.example', origin: 'https://evil.example' }), VERCEL);
  assert.equal(requestOrigin(base({ 'x-forwarded-host': 'evil.example' }), VERCEL), 'https://dash.example.test');
});

test('missing Origin still requires the custom header and JSON', () => {
  ok(base({}), VERCEL);
  denied(base({ 'x-requested-with': undefined }), VERCEL);
  assert.throws(() => assertDashboardPost(base({ 'content-type': 'text/plain' }), VERCEL), (e) => e.status === 415);
});

test('a pre-parsed body getter that throws becomes a 400', async () => {
  const req = { get body() { throw new SyntaxError('Unexpected token'); } };
  await assert.rejects(readJsonBody(req), (e) => e.status === 400 && e.message === 'Ogiltig begäran');
});

test('the 4 KB limit also applies to a pre-parsed body', async () => {
  await assert.rejects(readJsonBody({ body: { password: 'x'.repeat(5000) } }), (e) => e.status === 413);
  assert.deepEqual(await readJsonBody({ body: { password: 'ok' } }), { password: 'ok' });
  await assert.rejects(readJsonBody({ body: 'x'.repeat(5000) }), (e) => e.status === 413);
});
