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
// Newest commit older than this: the state files are stale.
export const STALE_HOURS = 36;
// Newest successful run older than this: warn. Not 36: Saturday 15:00 UTC to Monday
// 15:00 UTC is 48 h with no Sunday booking run, which false-alarmed every Monday.
// Sent to the page in the view model (lastSuccessStaleHours) so the two cannot drift.
export const LAST_SUCCESS_STALE_HOURS = 52;
const STR_MAX = 300;
const LIST_MAX = 50;
export const ALERTS_LIMIT = 20;

export const STATUSES = ['attempted', 'pending', 'ordered', 'blocked', 'failed', 'cancelled', 'expired'];
const NEEDS_ACTION = new Set(['blocked', 'failed', 'expired']);
// Same windows as the daily Telegram summary (summary.py ACTION_WINDOW, KLAR_MAX_AGE).
const ACTION_WINDOW_MS = 7 * 86_400_000;
const KLAR_MAX_AGE_MS = 14 * 86_400_000;
const PORTAL_STATUS_MAX = 80;
const VERIFIED_NUMBERS_MAX = 120;
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

// Mirror of orders_state._open_klar "outstanding" (and summary.klar_outstanding): int
// klar_message_id, not handled, not klar_closed, and klar_sent_at not older than 14 days.
// A missing or zone-less klar_sent_at never expires.
function isKlarOpen(r, now) {
  if (!isWaitingKlar(r)) return false;
  const sent = r.klar_sent_at;
  if (typeof sent === 'string' && /([zZ]|[+-]\d\d:?\d\d)$/.test(sent)) {
    const ms = Date.parse(sent);
    if (Number.isFinite(ms) && now - ms > KLAR_MAX_AGE_MS) return false;
  }
  return true;
}

// verified_numbers is a list from the agent's order check, or text; shown as one string.
function verifiedNumbersText(value) {
  if (Array.isArray(value)) {
    return str(value.map((n) => str(n, 40)).filter(Boolean).slice(0, 10).join(', '), VERIFIED_NUMBERS_MAX);
  }
  return str(value, VERIFIED_NUMBERS_MAX);
}

function order(id, r, now) {
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
    klarOpen: isKlarOpen(r, now),
    portalStatus: str(r.portal_status, PORTAL_STATUS_MAX),
    verifiedNumbers: verifiedNumbersText(r.verified_numbers),
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

  const all = parseOrders(ordersText, problems).map(([id, r]) => [order(id, r, now), r]);
  const tiles = { pending: 0, orderedVerified: 0, orderedUnverified: 0, needsAction: 0, waitingKlar: 0 };
  // Per-status counts plus the daily summary's four numbers: waiting (pending), klar
  // (ordered/blocked with an open Klar button; each booking once, so a blocked one with an
  // open Klar is not also needsAction), ordered (status ordered, Klar or not) and needsAction
  // (blocked/failed/expired, updated within 7 days or of unknown age).
  const counts = Object.fromEntries([...STATUSES, 'waiting', 'klar', 'needsAction'].map((s) => [s, 0]));
  for (const [o] of all) {
    if (o.status === 'pending') counts.waiting += 1;
    if ((o.status === 'ordered' || o.status === 'blocked') && o.klarOpen) counts.klar += 1;
    else if (NEEDS_ACTION.has(o.status)
      && (o.updatedAt == null || now - Date.parse(o.updatedAt) <= ACTION_WINDOW_MS)) counts.needsAction += 1;
    if (STATUSES.includes(o.status)) counts[o.status] += 1;
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
  // A run that failed to log in or stopped at the state guard is not a success.
  const lastSuccess = runs.findLast((r) => !r.entry.login_failed && !r.entry.state_unreadable);
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
      staleHours: STALE_HOURS,
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
    lastSuccessStaleHours: LAST_SUCCESS_STALE_HOURS,
    problems,
  };
}
