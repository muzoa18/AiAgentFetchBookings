// Turns the agent's state files into the dashboard's view model.
//
// Input is the raw text of config/orders.json, logs/run_history.jsonl,
// config/seen_ids.txt, config/alerts.json and config/watchdog_alerts.json (null
// when a file is missing). Output contains only the
// fields the UI shows: whitelisted, type-checked and length-limited. Nothing
// here throws on bad input; problems are listed in `problems`.
import { safeHref } from '../public/links.js';

export const HISTORY_LIMIT = 30;
export const ORDERS_LIMIT = 200;
export const STALE_HOURS = 36;
const STR_MAX = 300;
const LIST_MAX = 50;
export const ALERTS_LIMIT = 20;

export const STATUSES = ['attempted', 'pending', 'ordered', 'blocked', 'failed', 'cancelled', 'expired'];
const NEEDS_ACTION = new Set(['blocked', 'failed', 'expired']);
// Run counters shown in the history, in display order.
const RUN_COUNTERS = ['bookings_found', 'notified', 'orders_staged', 'orders_failed',
  'notify_failed', 'handled_failed'];

function str(value, max = STR_MAX) {
  if (typeof value === 'number' && Number.isFinite(value)) value = String(value);
  if (typeof value !== 'string') return null;
  // Strip control characters (keeps normal whitespace as a space).
  const clean = value.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim();
  if (!clean) return null;
  return clean.length > max ? clean.slice(0, max - 1) + '…' : clean;
}

function num(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string') {
    // Swedish formatting: "1 234,50 kr"
    const cleaned = value.replace(/kr|sek|\s| /gi, '').replace(',', '.');
    if (/^-?\d+(\.\d+)?$/.test(cleaned)) return Number(cleaned);
  }
  return null;
}

function obj(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

// ISO string -> normalised ISO in UTC, or null. A timestamp without a zone
// (run_history.jsonl uses datetime.now().isoformat()) is read as UTC, which is
// what GitHub Actions runners use.
function time(value) {
  if (typeof value !== 'string' || value.length > 40) return null;
  const hasZone = /([zZ]|[+-]\d\d:?\d\d)$/.test(value);
  const ms = Date.parse(hasZone ? value : value + 'Z');
  return Number.isFinite(ms) ? new Date(ms).toISOString().replace('.000Z', 'Z') : null;
}


function parseOrders(text, problems) {
  if (text == null) return [];
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    problems.push('orders_unreadable');
    return [];
  }
  const bookings = obj(obj(data)?.bookings);
  if (!bookings) {
    problems.push('orders_unreadable');
    return [];
  }
  return Object.entries(bookings).filter(([, r]) => obj(r));
}

// One alerts file ({"alerts": {key: entry}}) -> unresolved entries, or [] plus a problem.
function parseAlerts(text, problemCode, problems) {
  if (text == null) return [];
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    problems.push(problemCode);
    return [];
  }
  const entries = obj(obj(data)?.alerts);
  if (!entries) {
    problems.push(problemCode);
    return [];
  }
  const out = [];
  for (const [key, raw] of Object.entries(entries)) {
    const e = obj(raw);
    if (!e || e.resolved_at) continue;
    const text = str(e.text);
    const cleanKey = str(key, 80);
    if (!text || !cleanKey) continue;
    out.push({
      key: cleanKey,
      text,
      severity: e.severity === 'warning' ? 'warning' : 'error',
      firstSeen: time(e.first_seen),
      lastSeen: time(e.last_seen),
      count: Number.isInteger(e.count) && e.count > 0 ? e.count : 1,
    });
  }
  return out;
}

// Both files, merged: the same key in both keeps the later last_seen.
function mergeAlerts(files, problems) {
  const byKey = new Map();
  for (const [text, code] of files) {
    for (const a of parseAlerts(text, code, problems)) {
      const prev = byKey.get(a.key);
      if (!prev || (a.lastSeen ?? '') > (prev.lastSeen ?? '')) byKey.set(a.key, a);
    }
  }
  return [...byKey.values()]
    .sort((a, b) => (b.lastSeen ?? '').localeCompare(a.lastSeen ?? '') || a.key.localeCompare(b.key))
    .slice(0, ALERTS_LIMIT);
}

function lines(list) {
  if (!Array.isArray(list)) return [];
  const out = [];
  for (const item of list.slice(0, LIST_MAX)) {
    const o = obj(item);
    const title = str(o?.title) ?? str(o?.name);
    if (!title) continue;
    const qty = num(o.qty);
    out.push({ title, qty: qty ?? 1, price: num(o.price_sek) ?? num(o.price) });
  }
  return out;
}

function fluids(list) {
  if (!Array.isArray(list)) return [];
  const out = [];
  for (const item of list.slice(0, LIST_MAX)) {
    const o = obj(item);
    const name = str(o?.name);
    if (name) out.push({ name, qty: str(o.qty, 40) });
  }
  return out;
}

function isWaitingKlar(r) {
  return Number.isInteger(r.klar_message_id) && r.handled !== true && r.klar_closed !== true;
}

function order(id, r) {
  const plan = obj(r.plan) ?? {};
  const status = STATUSES.includes(r.status) ? r.status : 'unknown';
  const verifiedAt = time(r.verified_at);
  const numbers = Array.isArray(r.verified_numbers)
    ? r.verified_numbers.map((n) => str(n, 40)).filter(Boolean).slice(0, 10) : [];
  let verification = { state: 'none', at: null, numbers: [] };
  if (status === 'ordered') {
    verification = verifiedAt
      ? { state: 'verified', at: verifiedAt, numbers }
      : { state: 'unverified', at: null, numbers: [] };
  }
  const partLines = lines(plan.expected_lines);
  return {
    bookingId: str(id, 80),
    bookingNr: str(plan.booking_nr, 40),
    regNr: str(plan.reg_nr, 20),
    vehicleModel: str(r.vehicle_model, 80),
    status,
    updatedAt: time(r.updated_at),
    total: num(plan.cart_total) ?? num(plan.total_sek),
    confirmation: str(plan.confirmation, 80),
    verification,
    lines: partLines.length ? partLines : lines(plan.parts),
    fluids: fluids(plan.fluids),
    reason: status === 'blocked' || status === 'failed' || status === 'expired'
      ? str(plan.reason) ?? str(r.last_reason) : null,
    lastReason: str(r.last_reason),
    tries: Number.isInteger(r.tries) ? r.tries : 0,
    handled: r.handled === true,
    waitingKlar: isWaitingKlar(r),
    detailUrl: safeHref(r.detail_url),
  };
}

function parseHistory(text) {
  const runs = [];
  let skipped = 0;
  if (typeof text !== 'string') return { runs, skipped };
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    let entry;
    try {
      entry = obj(JSON.parse(line));
    } catch {
      entry = null;
    }
    const timestamp = entry && time(entry.timestamp);
    if (!timestamp) {
      skipped += 1;
      continue;
    }
    runs.push({ entry, timestamp });
  }
  return { runs, skipped };
}

function run({ entry, timestamp }) {
  const counts = {};
  for (const key of RUN_COUNTERS) {
    if (Number.isInteger(entry[key])) counts[key] = entry[key];
  }
  // Any "*_failed" counter (older agents logged e.g. sms_failed) counts as a failure.
  let failures = 0;
  for (const [key, value] of Object.entries(entry)) {
    if (/_failed$/.test(key) && Number.isInteger(value) && value > 0) failures += value;
  }
  const loginFailed = entry.login_failed === true;
  return {
    timestamp,
    agent: str(entry.agent, 40),
    counts,
    failures,
    loginFailed,
    failed: failures > 0 || loginFailed,
  };
}

export function summarize({ ordersText = null, historyText = null, seenText = null, alertsText = null,
  watchdogAlertsText = null, commit = null,
  now = Date.now(), historyLimit = HISTORY_LIMIT } = {}) {
  const problems = [];

  const all = parseOrders(ordersText, problems).map(([id, r]) => [order(id, r), r]);
  const tiles = { pending: 0, orderedVerified: 0, orderedUnverified: 0, needsAction: 0, waitingKlar: 0 };
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0]));
  for (const [o] of all) {
    if (o.status in counts) counts[o.status] += 1;
    if (o.status === 'pending') tiles.pending += 1;
    if (o.verification.state === 'verified') tiles.orderedVerified += 1;
    if (o.verification.state === 'unverified') tiles.orderedUnverified += 1;
    if (NEEDS_ACTION.has(o.status)) tiles.needsAction += 1;
    if (o.waitingKlar) tiles.waitingKlar += 1;
  }
  const orders = all.map(([o]) => o)
    .sort((a, b) => (b.updatedAt ?? '').localeCompare(a.updatedAt ?? '') || String(a.bookingId).localeCompare(String(b.bookingId)))
    .slice(0, ORDERS_LIMIT);

  const { runs, skipped } = parseHistory(historyText);
  runs.sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  const history = runs.slice(-historyLimit).map(run);

  const alerts = mergeAlerts([[alertsText, 'alerts_unreadable'],
    [watchdogAlertsText, 'watchdog_alerts_unreadable']], problems);
  const lastSuccess = runs.findLast((r) => !r.entry.login_failed);
  const lastSuccessAgeHours = lastSuccess
    ? Math.round(Math.max(0, (now - Date.parse(lastSuccess.timestamp)) / 3_600_000) * 10) / 10 : null;

  const committedAt = time(commit?.date);
  const ageHours = committedAt ? Math.max(0, (now - Date.parse(committedAt)) / 3_600_000) : null;
  const sha = typeof commit?.sha === 'string' && /^[0-9a-f]{7,40}$/i.test(commit.sha) ? commit.sha.slice(0, 7) : null;

  const seenCount = typeof seenText === 'string'
    ? seenText.split('\n').filter((l) => l.trim()).length : 0;

  return {
    generatedAt: new Date(now).toISOString(),
    freshness: {
      sha,
      committedAt,
      ageHours: ageHours == null ? null : Math.round(ageHours * 10) / 10,
      stale: ageHours == null || ageHours > STALE_HOURS,
    },
    tiles,
    counts,
    orders,
    totalOrders: all.length,
    lastRun: history.at(-1) ?? null,
    history,
    historyTotal: runs.length,
    skippedHistoryLines: skipped,
    seenCount,
    alerts,
    lastSuccessAgeHours,
    problems,
  };
}
