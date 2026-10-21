// Response helpers and request checks shared by the API handlers.
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

function hostOf(value) {
  try {
    return new URL(value).host.toLowerCase();
  } catch {
    return null;
  }
}

/** Throws RequestError unless this is a same-origin JSON POST from the dashboard's own script. */
export function assertDashboardPost(req) {
  if (req.method !== 'POST') throw new RequestError(405, 'Metoden stöds inte');
  const type = String(header(req, 'content-type') ?? '').split(';')[0].trim().toLowerCase();
  if (type !== 'application/json') throw new RequestError(415, 'Fel innehållstyp');
  if (header(req, 'x-requested-with') !== 'dashboard') throw new RequestError(403, 'Förfrågan nekades');
  if (header(req, 'sec-fetch-site') === 'cross-site') throw new RequestError(403, 'Förfrågan nekades');
  const host = String(header(req, 'host') ?? '').toLowerCase();
  for (const name of ['origin', 'referer']) {
    const value = header(req, name);
    if (value === undefined || value === '') continue;
    if (!host || hostOf(value) !== host) throw new RequestError(403, 'Förfrågan nekades');
  }
}

/**
 * Parsed JSON object body. Accepts a body Vercel already parsed (req.body) or
 * reads the stream (plain node:http). Rejects anything but a small plain object.
 */
export async function readJsonBody(req, maxBytes = MAX_BODY_BYTES) {
  let value;
  let raw;
  if (req.body !== undefined && req.body !== null) {
    if (typeof req.body === 'string' || Buffer.isBuffer(req.body)) raw = String(req.body);
    else value = req.body;
  } else if (typeof req.on === 'function') {
    raw = await new Promise((resolve, reject) => {
      const chunks = [];
      let size = 0;
      req.on('data', (c) => {
        size += c.length;
        if (size > maxBytes) {
          reject(new RequestError(413, 'För stor förfrågan'));
          req.destroy?.();
        } else chunks.push(c);
      });
      req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
      req.on('error', () => reject(new RequestError(400, 'Ogiltig förfrågan')));
    });
  } else {
    raw = '';
  }
  if (raw !== undefined) {
    if (Buffer.byteLength(raw) > maxBytes) throw new RequestError(413, 'För stor förfrågan');
    try {
      value = JSON.parse(raw);
    } catch {
      throw new RequestError(400, 'Ogiltig förfrågan');
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new RequestError(400, 'Ogiltig förfrågan');
  return value;
}

/** Throws unless `body` has exactly the given keys. */
export function assertKeys(body, keys) {
  const got = Object.keys(body);
  if (got.length !== keys.length || !keys.every((k) => Object.hasOwn(body, k))) {
    throw new RequestError(400, 'Ogiltig förfrågan');
  }
}

export function clientIp(req) {
  const real = header(req, 'x-real-ip');
  if (real) return String(real).trim();
  const fwd = header(req, 'x-forwarded-for');
  if (fwd) return String(fwd).split(',')[0].trim();
  return req.socket?.remoteAddress ?? 'unknown';
}
