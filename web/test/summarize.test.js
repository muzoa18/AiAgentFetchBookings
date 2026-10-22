import { test } from 'node:test';
import assert from 'node:assert/strict';
import { summarize, HISTORY_LIMIT, STALE_HOURS, LAST_SUCCESS_STALE_HOURS } from '../lib/summarize.js';

const NOW = Date.parse('2026-10-21T12:00:00Z');
const commit = { sha: '0123456789abcdef', date: '2026-10-21T10:00:00Z' };

function orders(bookings) {
  return JSON.stringify({ last_update_id: 5, bookings });
}

const rec = (status, extra = {}, plan = {}) => ({
  status, message_id: 1, updated_at: '2026-10-21T09:00:00+00:00',
  plan: { booking_id: 'X', booking_nr: 'B-1', reg_nr: 'ABC123', ...plan }, ...extra,
});

test('every status lands in the right tile', () => {
  const vm = summarize({
    ordersText: orders({
      a: rec('pending'),
      b: rec('pending'),
      c: rec('ordered', { verified_at: '2026-10-21T10:00:00Z', verified_numbers: ['100200'] }),
      d: rec('ordered'),
      e: rec('blocked', {}, { reason: 'ingen säker träff' }),
      f: rec('failed'),
      g: rec('expired'),
      h: rec('cancelled'),
      i: rec('attempted'),
      k: rec('ordered', { klar_message_id: 9 }),
      l: rec('ordered', { klar_message_id: 9, handled: true }),
      m: rec('ordered', { klar_message_id: 9, klar_closed: true }),
    }),
    historyText: '', seenText: '', commit, now: NOW,
  });
  assert.equal(vm.tiles, undefined, 'one number per concept: vm.counts only');
  assert.equal(vm.orders.length, 12);
  assert.deepEqual(vm.counts, {
    attempted: 1, pending: 2, ordered: 4, blocked: 1, failed: 1, cancelled: 1, expired: 1,
    waiting: 2, klar: 1, needsAction: 3,
  });
});

test('verified orders carry their order numbers; unverified do not', () => {
  const vm = summarize({
    ordersText: orders({
      v: rec('ordered', { verified_at: '2026-10-21T10:00:00Z', verified_numbers: ['100200', 300400, { x: 1 }] }),
      u: rec('ordered'),
      p: rec('pending'),
    }),
    commit, now: NOW,
  });
  const by = Object.fromEntries(vm.orders.map((o) => [o.bookingId, o]));
  assert.deepEqual(by.v.verification, { state: 'verified', at: '2026-10-21T10:00:00Z', numbers: ['100200', '300400'] });
  assert.equal(by.u.verification.state, 'unverified');
  assert.equal(by.p.verification.state, 'none');
});

test('order rows expose parts, fluids, total, reason, model and a safe link', () => {
  const vm = summarize({
    ordersText: orders({
      x: rec('blocked', {
        detail_url: 'https://meca.promeisterportal.com/booking/1', last_reason: 'gammal orsak',
        vehicle_model: 'Volvo V70', tries: 2,
      }, {
        reason: 'ingen säker träff',
        expected_lines: [{ title: 'Oljefilter', qty: 1, price_sek: 89.5 }],
        fluids: [{ name: 'Motorolja 5W-30', qty: '4,5 l' }],
        total_sek: 412.25,
      }),
      y: rec('pending', { detail_url: 'javascript:alert(1)' }, { cart_total: '1 234,50 kr' }),
    }),
    commit, now: NOW,
  });
  const by = Object.fromEntries(vm.orders.map((o) => [o.bookingId, o]));
  assert.equal(by.x.vehicleModel, 'Volvo V70');
  assert.equal(by.x.regNr, 'ABC123');
  assert.equal(by.x.bookingNr, 'B-1');
  assert.deepEqual(by.x.lines, [{ title: 'Oljefilter', qty: 1, price: 89.5 }]);
  assert.deepEqual(by.x.fluids, [{ name: 'Motorolja 5W-30', qty: '4,5 l' }]);
  assert.equal(by.x.total, 412.25);
  assert.equal(by.x.reason, 'ingen säker träff');
  assert.equal(by.x.detailUrl, 'https://meca.promeisterportal.com/booking/1');
  assert.equal(by.x.tries, 2);
  assert.equal(by.y.detailUrl, null, 'non-https links are dropped');
  assert.equal(by.y.total, 1234.5);
});

test('parts fall back to plan.parts when expected_lines is absent', () => {
  const vm = summarize({
    ordersText: orders({ x: rec('ordered', {}, { parts: [{ title: 'Bromsskiva', qty: 2, price_sek: 500 }] }) }),
    commit, now: NOW,
  });
  assert.deepEqual(vm.orders[0].lines, [{ title: 'Bromsskiva', qty: 2, price: 500 }]);
});

test('freshness: short sha, age and the 36 h stale flag', () => {
  const fresh = summarize({ commit, now: NOW });
  assert.equal(fresh.freshness.sha, '0123456');
  assert.equal(fresh.freshness.stale, false);
  const old = summarize({ commit: { sha: 'abcdef0123', date: '2026-10-19T23:00:00Z' }, now: NOW });
  assert.equal(old.freshness.stale, true);
  const none = summarize({ commit: null, now: NOW });
  assert.equal(none.freshness.sha, null);
  assert.equal(none.freshness.stale, true);
  assert.equal(fresh.freshness.staleHours, STALE_HOURS);
});

test('missing files give an empty but valid view model', () => {
  const vm = summarize({ ordersText: null, historyText: null, seenText: null, commit, now: NOW });
  assert.deepEqual(vm.orders, []);
  assert.deepEqual(vm.history, []);
  assert.equal(vm.lastRun, null);
  assert.equal(vm.seenCount, 0);
  assert.deepEqual(vm.problems, []);
});

test('a corrupt orders.json is reported, not thrown', () => {
  for (const text of ['{nope', '[]', '"x"', JSON.stringify({ bookings: [] })]) {
    const vm = summarize({ ordersText: text, commit, now: NOW });
    assert.deepEqual(vm.orders, []);
    assert.ok(vm.problems.includes('orders_unreadable'), text);
  }
});

test('corrupt history lines are skipped and counted', () => {
  const history = [
    JSON.stringify({ timestamp: '2026-10-21T06:00:00', agent: 'booking_agent', bookings_found: 2, notified: 2 }),
    '{broken',
    '42',
    '',
    JSON.stringify({ timestamp: '2026-10-21T08:00:00', agent: 'booking_agent', bookings_found: 1, notify_failed: 1, login_failed: true }),
  ].join('\n');
  const vm = summarize({ historyText: history, commit, now: NOW });
  assert.equal(vm.history.length, 2);
  assert.equal(vm.skippedHistoryLines, 2);
  assert.equal(vm.lastRun.timestamp, '2026-10-21T08:00:00Z', 'naive timestamps are read as UTC');
  assert.equal(vm.lastRun.failed, true);
  assert.equal(vm.lastRun.loginFailed, true);
  assert.equal(vm.lastRun.failures, 1);
  assert.equal(vm.history[0].failed, false);
  assert.equal(vm.history[0].counts.notified, 2);
});

test('unknown fields are ignored and never leak into the view model', () => {
  const vm = summarize({
    ordersText: orders({ a: rec('pending', { secret_thing: 'leak-me', message_id: 77 }, { customer: 'leak-me' }) }),
    historyText: JSON.stringify({ timestamp: '2026-10-21T08:00:00', agent: 'x', mystery: 'leak-me', notified: 1 }),
    commit, now: NOW,
  });
  assert.ok(!JSON.stringify(vm).includes('leak-me'));
  assert.ok(!JSON.stringify(vm).includes('77'), 'telegram message ids are not exposed');
});

test('an unknown status is shown as unknown and counted nowhere', () => {
  const vm = summarize({ ordersText: orders({ a: rec('weird') }), commit, now: NOW });
  assert.equal(vm.orders[0].status, 'unknown');
  assert.equal(Object.values(vm.counts).reduce((a, b) => a + b, 0), 0);
});

test('huge history is truncated to the newest runs', () => {
  const lines = [];
  for (let i = 0; i < 5000; i++) {
    lines.push(JSON.stringify({ timestamp: new Date(NOW - (5000 - i) * 60000).toISOString(), agent: 'booking_agent', notified: i }));
  }
  const vm = summarize({ historyText: lines.join('\n'), commit, now: NOW });
  assert.equal(vm.history.length, HISTORY_LIMIT);
  assert.equal(vm.historyTotal, 5000);
  assert.equal(vm.history.at(-1).counts.notified, 4999, 'newest last');
});

test('long strings are truncated and control characters stripped', () => {
  const vm = summarize({
    ordersText: orders({ a: rec('blocked', {}, { reason: 'x\u0000y'.padEnd(5000, 'z') }) }),
    commit, now: NOW,
  });
  assert.ok(vm.orders[0].reason.length <= 300);
  assert.ok(!vm.orders[0].reason.includes('\u0000'));
});

test('seen ids are counted, not listed', () => {
  const vm = summarize({ seenText: 'AAAA-1\nBBBB-2\n\nCCCC-3\n', commit, now: NOW });
  assert.equal(vm.seenCount, 3);
  assert.ok(!JSON.stringify(vm).includes('AAAA-1'));
});

test('orders are sorted newest update first', () => {
  const vm = summarize({
    ordersText: orders({
      old: rec('pending', { updated_at: '2026-10-01T00:00:00+00:00' }),
      new: rec('pending', { updated_at: '2026-10-21T00:00:00+00:00' }),
    }),
    commit, now: NOW,
  });
  assert.deepEqual(vm.orders.map((o) => o.bookingId), ['new', 'old']);
});

test('an XSS payload survives as plain data', () => {
  const evil = '<img src=x onerror=alert(1)>';
  const vm = summarize({ ordersText: orders({ a: rec('pending', {}, { expected_lines: [{ title: evil, qty: 1 }] }) }), commit, now: NOW });
  assert.equal(vm.orders[0].lines[0].title, evil);
});

// ---- alerts (config/alerts.json + config/watchdog_alerts.json) ----
const alertEntry = (extra = {}) => ({
  text: 'Inloggningen misslyckades', severity: 'error', first_seen: '2026-10-21T08:00:00+00:00',
  last_seen: '2026-10-21T09:00:00+00:00', count: 2, last_sent: null, resolved_at: null, ...extra,
});
const alertsFile = (alerts) => JSON.stringify({ alerts });

test('alerts: unresolved only, sorted by lastSeen desc, shaped for the UI', () => {
  const vm = summarize({
    alertsText: alertsFile({
      old: alertEntry({ text: 'Gammal', last_seen: '2026-10-20T09:00:00+00:00' }),
      done: alertEntry({ text: 'Löst', resolved_at: '2026-10-21T09:30:00+00:00' }),
      newer: alertEntry({ text: 'Nyare', severity: 'warning', last_seen: '2026-10-21T11:00:00+00:00' }),
    }),
    now: NOW,
  });
  assert.deepEqual(vm.alerts.map((a) => a.key), ['newer', 'old']);
  assert.deepEqual(vm.alerts[0], {
    key: 'newer', text: 'Nyare', severity: 'warning', firstSeen: '2026-10-21T08:00:00Z',
    lastSeen: '2026-10-21T11:00:00Z', count: 2,
  });
  assert.deepEqual(vm.problems, []);
});

test('alerts: both files are merged; the same key keeps the later last_seen', () => {
  const vm = summarize({
    alertsText: alertsFile({
      shared: alertEntry({ text: 'från alerts', last_seen: '2026-10-21T09:00:00+00:00' }),
      a: alertEntry({ text: 'bara A', last_seen: '2026-10-21T07:00:00+00:00' }),
    }),
    watchdogAlertsText: alertsFile({
      shared: alertEntry({ text: 'från watchdog', last_seen: '2026-10-21T10:00:00+00:00' }),
      w: alertEntry({ text: 'bara W', last_seen: '2026-10-21T08:00:00+00:00' }),
    }),
    now: NOW,
  });
  assert.deepEqual(vm.alerts.map((a) => [a.key, a.text]), [['shared', 'från watchdog'], ['w', 'bara W'], ['a', 'bara A']]);
  const flipped = summarize({
    alertsText: alertsFile({ shared: alertEntry({ text: 'från alerts', last_seen: '2026-10-21T11:00:00+00:00' }) }),
    watchdogAlertsText: alertsFile({ shared: alertEntry({ text: 'från watchdog' }) }),
    now: NOW,
  });
  assert.equal(flipped.alerts.length, 1);
  assert.equal(flipped.alerts[0].text, 'från alerts');
});

test('alerts: a resolved copy of a key in one file an unresolved copy still shows when the other file has it resolved', () => {
  const vm = summarize({
    alertsText: alertsFile({ k: alertEntry({ resolved_at: '2026-10-21T09:30:00+00:00', last_seen: '2026-10-21T11:00:00+00:00' }) }),
    watchdogAlertsText: alertsFile({ k: alertEntry({ text: 'aktiv' }) }),
    now: NOW,
  });
  assert.deepEqual(vm.alerts.map((a) => a.text), ['aktiv']);
});

test('alerts: capped at 20, newest first', () => {
  const many = {};
  for (let i = 0; i < 25; i++) {
    many[`k${i}`] = alertEntry({ text: `a${i}`, last_seen: new Date(Date.parse('2026-10-21T00:00:00Z') + i * 60000).toISOString() });
  }
  const vm = summarize({ alertsText: alertsFile(many), now: NOW });
  assert.equal(vm.alerts.length, 20);
  assert.equal(vm.alerts[0].key, 'k24');
  assert.equal(vm.alerts[19].key, 'k5');
});

test('alerts: corrupt JSON in each file is its own problem and never throws', () => {
  const good = alertsFile({ ok: alertEntry({ text: 'ok' }) });
  const a = summarize({ alertsText: '{kaputt', watchdogAlertsText: good, now: NOW });
  assert.deepEqual(a.problems, ['alerts_unreadable']);
  assert.deepEqual(a.alerts.map((x) => x.key), ['ok']);
  const w = summarize({ alertsText: good, watchdogAlertsText: '[]', now: NOW });
  assert.deepEqual(w.problems, ['watchdog_alerts_unreadable']);
  assert.deepEqual(w.alerts.map((x) => x.key), ['ok']);
  const both = summarize({ alertsText: 'x', watchdogAlertsText: '{"alerts": 5}', now: NOW });
  assert.deepEqual(both.problems, ['alerts_unreadable', 'watchdog_alerts_unreadable']);
  assert.deepEqual(both.alerts, []);
});

test('alerts: missing files are not an error', () => {
  const vm = summarize({ alertsText: null, watchdogAlertsText: null, now: NOW });
  assert.deepEqual(vm.alerts, []);
  assert.deepEqual(vm.problems, []);
  assert.deepEqual(summarize({ now: NOW }).alerts, []);
});

test('alerts: control characters stripped, long text limited, severity coerced', () => {
  const vm = summarize({
    alertsText: alertsFile({
      a: alertEntry({ text: 'rad1\u0000\u001b[31m\nrad2', severity: 'critical' }),
      b: alertEntry({ text: 'x'.repeat(1000), severity: 'warning', last_seen: '2026-10-21T08:30:00Z' }),
      c: alertEntry({ text: '   ', last_seen: '2026-10-21T08:20:00Z' }),
      d: alertEntry({ text: 'ok', severity: null, last_seen: '2026-10-21T08:10:00Z', count: 'many' }),
    }),
    now: NOW,
  });
  const byKey = Object.fromEntries(vm.alerts.map((x) => [x.key, x]));
  assert.ok(!/[\u0000-\u001f]/.test(byKey.a.text));
  assert.equal(byKey.a.severity, 'error');
  assert.ok(byKey.b.text.length <= 300);
  assert.equal(byKey.b.severity, 'warning');
  assert.equal(byKey.c, undefined, 'an alert without text is dropped');
  assert.equal(byKey.d.severity, 'error');
  assert.equal(byKey.d.count, 1);
});

test('lastSuccessAgeHours: newest run without login_failed, from a fixed now', () => {
  const history = [
    { timestamp: '2026-10-21T06:00:00', agent: 'booking_agent', login_failed: false },
    { timestamp: '2026-10-21T09:00:00', agent: 'approve_agent' },
    { timestamp: '2026-10-21T10:00:00', agent: 'booking_agent', login_failed: true },
  ].map((e) => JSON.stringify(e)).join('\n');
  assert.equal(summarize({ historyText: history, now: NOW }).lastSuccessAgeHours, 3);
  const allFailed = JSON.stringify({ timestamp: '2026-10-21T10:00:00', login_failed: true });
  assert.equal(summarize({ historyText: allFailed, now: NOW }).lastSuccessAgeHours, null);
  assert.equal(summarize({ now: NOW }).lastSuccessAgeHours, null);
});

test('lastSuccessAgeHours: a run that stopped at the state guard is not a success', () => {
  const history = [
    { timestamp: '2026-10-21T06:00:00', agent: 'booking_agent', login_failed: false, state_unreadable: false },
    { timestamp: '2026-10-21T10:00:00', agent: 'booking_agent', login_failed: false, state_unreadable: true },
  ].map((e) => JSON.stringify(e)).join('\n');
  assert.equal(summarize({ historyText: history, now: NOW }).lastSuccessAgeHours, 6);
  const onlyState = JSON.stringify({ timestamp: '2026-10-21T10:00:00', state_unreadable: true });
  assert.equal(summarize({ historyText: onlyState, now: NOW }).lastSuccessAgeHours, null);
});

test('last-success threshold: 52 h (no Sunday run), separate from the 36 h commit age', () => {
  assert.equal(LAST_SUCCESS_STALE_HOURS, 52);
  assert.equal(STALE_HOURS, 36);
  assert.equal(summarize({ now: NOW }).lastSuccessStaleHours, LAST_SUCCESS_STALE_HOURS);
});

test('orders expose portalStatus and verifiedNumbers, whitelisted and length-limited', () => {
  const vm = summarize({
    ordersText: orders({
      a: rec('ordered', { portal_status: 'Skickad\u0000från lager', verified_numbers: 'L-123 / 456' }),
      b: rec('ordered', { portal_status: 'x'.repeat(200), verified_numbers: 'y'.repeat(500) }),
      c: rec('ordered', { portal_status: { evil: 1 }, verified_numbers: 42 }),
      d: rec('pending'),
      e: rec('ordered', { verified_at: '2026-10-21T10:00:00Z', verified_numbers: ['100200', 300400] }),
    }),
    commit, now: NOW,
  });
  const by = Object.fromEntries(vm.orders.map((o) => [o.bookingId, o]));
  assert.equal(by.a.portalStatus, 'Skickad från lager');
  assert.equal(by.a.verifiedNumbers, 'L-123 / 456');
  assert.equal(by.b.portalStatus.length, 80);
  assert.equal(by.b.verifiedNumbers.length, 120);
  assert.equal(by.c.portalStatus, null);
  assert.equal(by.c.verifiedNumbers, '42');
  assert.equal(by.d.portalStatus, null);
  assert.equal(by.d.verifiedNumbers, null);
  assert.equal(by.e.verifiedNumbers, '100200, 300400', 'a list is joined');
});

test('counts follow the daily Telegram summary rules', () => {
  const day = (d) => new Date(NOW - d * 86_400_000).toISOString();
  const klar = (extra = {}) => ({ klar_message_id: 9, klar_sent_at: day(1), ...extra });
  const vm = summarize({
    ordersText: orders({
      p1: rec('pending'),
      p2: rec('pending'),
      o1: rec('ordered'),
      o2: rec('ordered', klar()),
      o3: rec('ordered', klar({ klar_sent_at: day(15) })),
      o4: rec('ordered', klar({ handled: true })),
      o5: rec('ordered', klar({ klar_closed: true })),
      b1: rec('blocked', klar()),                       // Klar only, not needs-action
      b2: rec('blocked', { updated_at: day(2) }),       // needs action
      b3: rec('blocked', { updated_at: day(8) }),       // too old
      f1: rec('failed', { updated_at: day(6) }),
      x1: rec('expired', { updated_at: day(7.5) }),
      x2: rec('expired', { updated_at: undefined }),    // unknown age: counted
      c1: rec('cancelled'),
    }),
    commit, now: NOW,
  });
  assert.equal(vm.counts.waiting, 2);
  assert.equal(vm.counts.klar, 2, 'o2 and b1');
  assert.equal(vm.counts.ordered, 4, 'status ordered and not counted under Klar (o1, o3, o4, o5)');
  assert.equal(vm.counts.needsAction, 3, 'b2, f1, x2');
  const by = Object.fromEntries(vm.orders.map((o) => [o.bookingId, o]));
  assert.equal(by.o2.klarOpen, true);
  assert.equal(by.o3.klarOpen, false);
  assert.equal(by.o4.klarOpen, false);
  assert.equal(by.o1.klarOpen, false);
  // Existing per-status counts are kept.
  assert.equal(vm.counts.pending, 2);
  assert.equal(vm.counts.blocked, 3);
  const empty = summarize({ ordersText: null, now: NOW });
  assert.deepEqual([empty.counts.waiting, empty.counts.klar, empty.counts.ordered, empty.counts.needsAction], [0, 0, 0, 0]);
});
