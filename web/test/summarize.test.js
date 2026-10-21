import { test } from 'node:test';
import assert from 'node:assert/strict';
import { summarize, HISTORY_LIMIT } from '../lib/summarize.js';

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
  assert.deepEqual(vm.tiles, {
    pending: 2, orderedVerified: 1, orderedUnverified: 4, needsAction: 3, waitingKlar: 1,
  });
  assert.equal(vm.orders.length, 12);
  assert.deepEqual(vm.counts, {
    attempted: 1, pending: 2, ordered: 5, blocked: 1, failed: 1, cancelled: 1, expired: 1,
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
        detail_url: 'https://portal.example.test/booking/1', last_reason: 'gammal orsak',
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
  assert.equal(by.x.detailUrl, 'https://portal.example.test/booking/1');
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
  assert.equal(Object.values(vm.tiles).reduce((a, b) => a + b, 0), 0);
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
