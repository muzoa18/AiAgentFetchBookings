// Session cookie and password check. Pure functions, Node built-ins only.
//
// Token format: v1.<expiry ms>.<random nonce>.<HMAC-SHA256(key, "v1.<expiry>.<nonce>")>
// where key = HMAC-SHA256(SESSION_SECRET, "session|" + SHA-256(DASHBOARD_PASSWORD)).
// Binding the key to the password means changing the password ends every session.
// The token carries no user data: there is one shared password, so a valid
// signature plus an unexpired timestamp is the whole session.
import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';

export const COOKIE_NAME = 'dash_session';
export const SESSION_TTL_MS = 12 * 60 * 60 * 1000;
export const MIN_SECRET_LENGTH = 32;
export const MIN_PASSWORD_LENGTH = 16;
export const MIN_PASSWORD_DISTINCT = 6;

export function assertSecret(secret) {
  if (typeof secret !== 'string' || secret.length < MIN_SECRET_LENGTH) {
    throw new Error(`SESSION_SECRET must be at least ${MIN_SECRET_LENGTH} characters`);
  }
  if (new Set(secret).size < 8) {
    throw new Error('SESSION_SECRET is too weak (too few distinct characters)');
  }
}

export function assertPassword(password) {
  if (typeof password !== 'string' || password.length < MIN_PASSWORD_LENGTH
      || new Set(password).size < MIN_PASSWORD_DISTINCT) {
    throw new Error(`DASHBOARD_PASSWORD must be at least ${MIN_PASSWORD_LENGTH} characters `
      + `with at least ${MIN_PASSWORD_DISTINCT} different characters`);
  }
}

function sessionKey(secret, password) {
  const pwHash = createHash('sha256').update(String(password ?? '')).digest('hex');
  return createHmac('sha256', secret).update('session|' + pwHash).digest();
}

function mac(key, data) {
  return createHmac('sha256', key).update(data).digest('base64url');
}

function safeEqual(a, b) {
  const ab = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  if (ab.length !== bb.length) return false;
  return timingSafeEqual(ab, bb);
}

export function signSession(secret, now, password) {
  assertSecret(secret);
  const body = `v1.${now + SESSION_TTL_MS}.${randomBytes(12).toString('base64url')}`;
  return `${body}.${mac(sessionKey(secret, password), body)}`;
}

export function verifySession(token, secret, now, password) {
  assertSecret(secret);
  if (typeof token !== 'string' || token.length > 200) return false;
  const parts = token.split('.');
  if (parts.length !== 4 || parts[0] !== 'v1') return false;
  const [, exp, nonce, sig] = parts;
  if (!/^\d{1,16}$/.test(exp) || !/^[\w-]{1,64}$/.test(nonce)) return false;
  if (!safeEqual(sig, mac(sessionKey(secret, password), `v1.${exp}.${nonce}`))) return false;
  const expiry = Number(exp);
  // An expiry further out than one TTL cannot have been issued by signSession.
  return expiry > now && expiry <= now + SESSION_TTL_MS;
}

// Both sides are HMAC'd first so the comparison is fixed-length and timing-safe
// whatever the input length.
export function checkPassword(input, expected, secret) {
  assertSecret(secret);
  if (typeof input !== 'string' || typeof expected !== 'string' || !expected) return false;
  if (input.length > 1024) return false;
  const a = createHmac('sha256', secret).update('pw:' + input).digest();
  const b = createHmac('sha256', secret).update('pw:' + expected).digest();
  return timingSafeEqual(a, b);
}

// `secure: false` is only for the local demo on http://localhost.
export function sessionCookie(token, { secure = true } = {}) {
  return `${COOKIE_NAME}=${token}; Path=/; HttpOnly${secure ? '; Secure' : ''}; SameSite=Strict; Max-Age=${SESSION_TTL_MS / 1000}`;
}

export function clearedCookie({ secure = true } = {}) {
  return `${COOKIE_NAME}=; Path=/; HttpOnly${secure ? '; Secure' : ''}; SameSite=Strict; Max-Age=0`;
}

export function readSessionCookie(req) {
  const header = req?.headers?.cookie;
  if (typeof header !== 'string') return null;
  for (const part of header.split(';')) {
    const i = part.indexOf('=');
    if (i > 0 && part.slice(0, i).trim() === COOKIE_NAME) return part.slice(i + 1).trim();
  }
  return null;
}
