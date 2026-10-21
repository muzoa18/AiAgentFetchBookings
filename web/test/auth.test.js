import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  SESSION_TTL_MS, assertSecret, assertPassword, checkPassword, signSession, verifySession,
  sessionCookie, clearedCookie, readSessionCookie,
} from '../lib/auth.js';

const SECRET = 'test-secret-0123456789-abcdefghijklmnop';
const OTHER = 'other-secret-0123456789-abcdefghijklmnop';
const NOW = Date.UTC(2026, 9, 21, 12, 0, 0);
const PW = 'test-password-long-enough';

test('a freshly signed session verifies', () => {
  const token = signSession(SECRET, NOW, PW);
  assert.equal(verifySession(token, SECRET, NOW + 1000, PW), true);
});

test('a session expires after 12 hours', () => {
  assert.equal(SESSION_TTL_MS, 12 * 60 * 60 * 1000);
  const token = signSession(SECRET, NOW, PW);
  assert.equal(verifySession(token, SECRET, NOW + SESSION_TTL_MS - 1, PW), true);
  assert.equal(verifySession(token, SECRET, NOW + SESSION_TTL_MS + 1, PW), false);
});

test('a tampered session is rejected', () => {
  const token = signSession(SECRET, NOW, PW);
  const [v, exp, nonce, sig] = token.split('.');
  const later = String(Number(exp) + 10 * SESSION_TTL_MS);
  assert.equal(verifySession([v, later, nonce, sig].join('.'), SECRET, NOW, PW), false);
  const flipped = sig.slice(0, -1) + (sig.endsWith('A') ? 'B' : 'A');
  assert.equal(verifySession([v, exp, nonce, flipped].join('.'), SECRET, NOW, PW), false);
  assert.equal(verifySession(token + 'x', SECRET, NOW, PW), false);
});

test('a session signed with another secret is rejected', () => {
  assert.equal(verifySession(signSession(OTHER, NOW, PW), SECRET, NOW, PW), false);
});

test('garbage and missing tokens are rejected without throwing', () => {
  for (const t of [undefined, null, '', 'a.b', 'v1.x.y.z', 'v1..', 42, {}, 'v1.9999999999999.n.' + 'A'.repeat(43)]) {
    assert.equal(verifySession(t, SECRET, NOW, PW), false);
  }
});

test('weak or missing secrets are refused', () => {
  for (const s of [undefined, '', 'short', 'a'.repeat(64), 'abababababababababababababababab']) {
    assert.throws(() => assertSecret(s), /SESSION_SECRET/);
  }
  assert.doesNotThrow(() => assertSecret(SECRET));
  assert.throws(() => signSession('short', NOW, PW), /SESSION_SECRET/);
  assert.throws(() => verifySession('v1.1.2.3', 'short', NOW, PW), /SESSION_SECRET/);
});

test('password check is exact and length-independent', () => {
  assert.equal(checkPassword('test-password', 'test-password', SECRET), true);
  assert.equal(checkPassword('test-passwor', 'test-password', SECRET), false);
  assert.equal(checkPassword('test-password-longer', 'test-password', SECRET), false);
  assert.equal(checkPassword('', 'test-password', SECRET), false);
  assert.equal(checkPassword(undefined, 'test-password', SECRET), false);
  assert.equal(checkPassword('x', '', SECRET), false, 'an unset password never matches');
});

test('session cookie is HttpOnly, Secure, SameSite=Strict with a 12 h max age', () => {
  const c = sessionCookie('tok');
  assert.match(c, /^dash_session=tok;/);
  for (const part of ['HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/', 'Max-Age=43200']) {
    assert.ok(c.includes(part), part);
  }
  assert.match(clearedCookie(), /dash_session=;.*Max-Age=0/);
});

test('the session cookie is read from the Cookie header', () => {
  assert.equal(readSessionCookie({ headers: { cookie: 'a=1; dash_session=abc.def; b=2' } }), 'abc.def');
  assert.equal(readSessionCookie({ headers: {} }), null);
});

test('changing the password ends every session', () => {
  const token = signSession(SECRET, NOW, PW);
  assert.equal(verifySession(token, SECRET, NOW, PW), true);
  assert.equal(verifySession(token, SECRET, NOW, 'another-password-long-enough'), false);
});

test('short or low-variety passwords are refused', () => {
  for (const pw of [undefined, '', 'short', 'fifteen-chars-x', 'aaaaaaaaaaaaaaaaaaaa', 'abababababababababab', 'abcabcabcabcabcabc']) {
    assert.throws(() => assertPassword(pw), /DASHBOARD_PASSWORD/, String(pw));
  }
  for (const pw of ['sixteen-chars-ok', 'häst batteri fönster kaffe', PW]) {
    assert.doesNotThrow(() => assertPassword(pw), pw);
  }
});

test('the demo cookie can drop Secure for http://localhost; the default keeps it', () => {
  assert.ok(sessionCookie('t').includes('; Secure'));
  assert.ok(!sessionCookie('t', { secure: false }).includes('Secure'));
  assert.ok(!clearedCookie({ secure: false }).includes('Secure'));
  assert.ok(clearedCookie().includes('; Secure'));
});
