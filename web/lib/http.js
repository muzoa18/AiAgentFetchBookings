// Response helpers and request checks shared by the API handlers.
import { isIP } from 'node:net';

export const SECURITY_HEADERS = {
  'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    + "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY',
  'X-Robots-Tag': 'noindex, nofollow',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), interest-cohort=()',
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Resource-Policy': 'same-origin',
  'Strict-Transport-Security': 'max-age=31536000',
};

export const MAX_BODY_BYTES = 4096;
export const BODY_TIMEOUT_MS = 5000;
const BAD_REQUEST = 'Ogiltig begäran';
const DENIED = 'Förfrågan nekades';

export function applySecurityHeaders(res) {
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) res.setHeader(k, v);
}

export function sendJson(res, status, body, headers = {}) {
  applySecurityHeaders(res);
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  for (const [k, v] of Object.entries(headers)) res.setHeader(k, v);
  res.statusCode = status;
  res.end(JSON.stringify(body));
}

export function sendError(res, status, message, headers) {
  sendJson(res, status, { error: message }, headers);
}

export class RequestError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function header(req, name) {
  const v = req.headers?.[name];
  return Array.isArray(v) ? v[0] : v;
}

/**
 * The origin this request was addressed to, from the Host header (never
 * X-Forwarded-Host). The scheme comes from X-Forwarded-Proto only behind a
 * trusted proxy (Vercel); otherwise from the socket.
 */
export function requestOrigin(req, { trustProxy = false } = {}) {
  const host = header(req, 'host');
  if (typeof host !== 'string' || !host) return null;
  let proto;
  if (trustProxy) {
    const fwd = String(header(req, 'x-forwarded-proto') ?? 'https').split(',')[0].trim().toLowerCase();
    proto = fwd === 'http' ? 'http' : 'https';
  } else {
    proto = req.socket?.encrypted ? 'https' : 'http';
  }
  try {
    const u = new URL(`${proto}://${host}`);
    if (u.username || u.password || u.pathname !== '/' || u.search || u.hash) return null;
    return u.origin.toLowerCase();
  } catch {
    return null;
  }
}

function originOf(value) {
  try {
    const u = new URL(value);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return u.origin.toLowerCase();
  } catch {
    return null;
  }
}

/** Throws RequestError unless this is a same-origin JSON POST from the dashboard's own script. */
export function assertDashboardPost(req, { trustProxy = false } = {}) {
  if (req.method !== 'POST') throw new RequestError(405, 'Metoden stöds inte');
  const type = String(header(req, 'content-type') ?? '').split(';')[0].trim().toLowerCase();
  if (type !== 'application/json') throw new RequestError(415, 'Fel innehållstyp');
  if (header(req, 'x-requested-with') !== 'dashboard') throw new RequestError(403, DENIED);
  if (header(req, 'sec-fetch-site') === 'cross-site') throw new RequestError(403, DENIED);
  const expected = requestOrigin(req, { trustProxy });
  for (const name of ['origin', 'referer']) {
    const value = header(req, name);
    if (value === undefined || value === '') continue;
    // "null" (sandboxed frames, some redirects) is never accepted.
    if (!expected || originOf(value) !== expected) throw new RequestError(403, DENIED);
  }
}

/**
 * Parsed JSON object body. Accepts a body Vercel already parsed (req.body, a
 * lazy getter that throws on invalid JSON) or reads the stream (node:http).
 * Rejects anything but a small plain object.
 */
export async function readJsonBody(req, maxBytes = MAX_BODY_BYTES, { timeoutMs = BODY_TIMEOUT_MS } = {}) {
  let pre;
  try {
    pre = req.body;
  } catch {
    throw new RequestError(400, BAD_REQUEST);
  }
  let value;
  let raw;
  if (pre !== undefined && pre !== null) {
    if (typeof pre === 'string' || Buffer.isBuffer(pre)) {
      raw = String(pre);
    } else {
      value = pre;
      let size;
      try {
        size = Buffer.byteLength(JSON.stringify(value) ?? '');
      } catch {
        throw new RequestError(400, BAD_REQUEST);
      }
      if (size > maxBytes) throw new RequestError(413, 'För stor förfrågan');
    }
  } else if (typeof req.on === 'function') {
    raw = await new Promise((resolve, reject) => {
      const chunks = [];
      let size = 0;
      // A client that never finishes its body must not hold the function open.
      const timer = setTimeout(() => {
        reject(new RequestError(408, 'Förfrågan tog för lång tid'));
        req.destroy?.();
      }, timeoutMs);
      const done = (fn) => (arg) => { clearTimeout(timer); fn(arg); };
      resolve = done(resolve);
      reject = done(reject);
      req.on('data', (c) => {
        size += c.length;
        if (size > maxBytes) {
          reject(new RequestError(413, 'För stor förfrågan'));
          req.destroy?.();
        } else chunks.push(c);
      });
      req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
      req.on('error', () => reject(new RequestError(400, BAD_REQUEST)));
    });
  } else {
    raw = '';
  }
  if (raw !== undefined) {
    if (Buffer.byteLength(raw) > maxBytes) throw new RequestError(413, 'För stor förfrågan');
    try {
      value = JSON.parse(raw);
    } catch {
      throw new RequestError(400, BAD_REQUEST);
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new RequestError(400, BAD_REQUEST);
  return value;
}

/** Throws unless `body` has exactly the given keys. */
export function assertKeys(body, keys) {
  const got = Object.keys(body);
  if (got.length !== keys.length || !keys.every((k) => Object.hasOwn(body, k))) {
    throw new RequestError(400, BAD_REQUEST);
  }
}

/**
 * Client IP. Behind Vercel (trustProxy) the platform sets x-vercel-forwarded-for
 * / x-real-ip and overwrites x-forwarded-for, so they can be trusted there.
 * Anywhere else those headers are client-controlled and ignored: the socket
 * address is used.
 */
export function clientIp(req, { trustProxy = false } = {}) {
  if (trustProxy) {
    for (const name of ['x-vercel-forwarded-for', 'x-real-ip', 'x-forwarded-for']) {
      const v = header(req, name);
      if (v) {
        const first = String(v).split(',')[0].trim();
        if (first) return first;
      }
    }
  }
  return req.socket?.remoteAddress ?? 'unknown';
}

function expandIPv6(addr) {
  let a = addr.toLowerCase();
  // Embedded IPv4 tail (e.g. ::ffff:192.0.2.1) -> two hextets.
  const v4 = a.match(/(\d+\.\d+\.\d+\.\d+)$/);
  if (v4) {
    const [p, q, r, s] = v4[1].split('.').map(Number);
    a = a.slice(0, -v4[1].length) + ((p << 8) | q).toString(16) + ':' + ((r << 8) | s).toString(16);
  }
  const [head, tail] = a.includes('::') ? a.split('::') : [a, null];
  const h = head ? head.split(':') : [];
  const t = tail ? tail.split(':') : [];
  const fill = tail === null ? [] : Array(8 - h.length - t.length).fill('0');
  return [...h, ...fill, ...t].map((x) => x.padStart(4, '0'));
}

/**
 * Rate-limit key for an IP: IPv4 by full address, IPv6 by its /64 prefix (one
 * customer network usually gets a whole /64). IPv4-mapped IPv6 counts as IPv4.
 */
export function rateLimitKey(ip) {
  const raw = String(ip ?? '').trim().replace(/^\[|\]$/g, '').split('%')[0];
  const kind = isIP(raw);
  if (kind === 4) return raw;
  if (kind === 6) {
    const parts = expandIPv6(raw);
    if (parts.slice(0, 6).join(':') === '0000:0000:0000:0000:0000:ffff') {
      const n = parseInt(parts[6], 16);
      const m = parseInt(parts[7], 16);
      return `${n >> 8}.${n & 255}.${m >> 8}.${m & 255}`;
    }
    return parts.slice(0, 4).join(':') + '::/64';
  }
  return 'other:' + raw.slice(0, 64);
}
