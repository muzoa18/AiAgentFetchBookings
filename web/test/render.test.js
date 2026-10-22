import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import * as R from '../public/render.js';
import { summarize } from '../lib/summarize.js';

// Minimal fake DOM: elements with attributes and children, text nodes. It has
// no HTML parser at all, so anything that tried to parse markup would fail.
class Text {
  constructor(data) { this.nodeType = 3; this.data = data; }
}
class Element {
  constructor(tag) { this.nodeType = 1; this.tagName = tag; this.attributes = {}; this.children = []; this.hidden = false; this.className = ''; }
  setAttribute(k, v) {
    assert.ok(!/^on/i.test(k), `event handler attribute ${k}`);
    this.attributes[k] = String(v);
  }
  append(...nodes) {
    for (const n of nodes) {
      assert.ok(n instanceof Element || n instanceof Text, 'only nodes are appended');
      this.children.push(n);
    }
  }
  set textContent(v) { this.children = [new Text(String(v))]; }
  get textContent() { return this.children.map((c) => (c instanceof Text ? c.data : c.textContent)).join(''); }
  set innerHTML(_) { throw new Error('innerHTML used'); }
}
const doc = {
  createElement: (t) => new Element(t),
  createTextNode: (d) => new Text(d),
};

function all(node, out = []) {
  if (node instanceof Element) {
    out.push(node);
    node.children.forEach((c) => all(c, out));
  }
  return out;
}

const EVIL = '<img src=x onerror=alert(1)>';
const NOW = Date.parse('2026-10-21T12:00:00Z');

function vmWith(extra = {}) {
  return summarize({
    ordersText: JSON.stringify({ bookings: {
      a: { status: 'blocked', updated_at: '2026-10-21T10:00:00Z', detail_url: 'https://meca.promeisterportal.com/b/1',
        vehicle_model: EVIL,
        plan: { booking_nr: EVIL, reg_nr: 'QAB123', reason: EVIL,
          expected_lines: [{ title: EVIL, qty: 1, price_sek: 10 }], fluids: [{ name: EVIL, qty: EVIL }] }, ...extra },
      b: { status: 'ordered', updated_at: '2026-10-21T09:00:00Z', verified_at: '2026-10-21T09:30:00Z',
        verified_numbers: [EVIL], plan: { reg_nr: 'QCD45E' } },
    } }),
    historyText: JSON.stringify({ timestamp: '2026-10-21T08:00:00', agent: EVIL, orders_failed: 1 }),
    commit: { sha: 'abcdef1234', date: '2026-10-21T11:00:00Z' },
    now: NOW,
  });
}

test('XSS payloads are rendered as text, never as elements or attributes', () => {
  const vm = { ...vmWith(), links: { orderOverview: 'https://pro.meca.se/orders' } };
  const roots = [R.orders(doc, vm, NOW), R.lastRun(doc, vm, NOW), R.history(doc, vm, NOW), R.freshness(doc, vm, NOW), R.tiles(doc, vm)];
  for (const root of roots) {
    for (const el of all(root)) {
      assert.notEqual(el.tagName, 'img');
      assert.notEqual(el.tagName, 'script');
      for (const [k, v] of Object.entries(el.attributes)) {
        assert.ok(!/^on/i.test(k));
        if (k === 'href') assert.match(v, /^https:\/\//);
      }
    }
  }
  const text = roots.map((r) => r.textContent).join('\n');
  assert.ok(text.includes(EVIL), 'payload is shown verbatim as text');
});

test('javascript: links are never rendered', () => {
  const vm = vmWith();
  vm.orders[0].detailUrl = 'javascript:alert(1)';
  const vm2 = { ...vm, links: { orderOverview: 'data:text/html,x' } };
  const hrefs = all(R.orders(doc, vm2, NOW)).filter((e) => e.tagName === 'a');
  assert.equal(hrefs.length, 0);
  assert.equal(R.safeHref('JAVASCRIPT:alert(1)'), null);
  assert.equal(R.safeHref('https://pro.meca.se/'), 'https://pro.meca.se/');
});

test('status badges always carry text, not only colour', () => {
  for (const status of Object.keys(R.STATUS)) {
    const b = R.badge(doc, status);
    assert.ok(b.textContent.includes(R.STATUS[status].label));
  }
  assert.ok(R.badge(doc, 'nonsense').textContent.includes('Okänd status'));
});

test('verified orders show their order numbers; stale data shows a warning', () => {
  const vm = vmWith();
  const text = R.orders(doc, vm, NOW).textContent;
  assert.ok(text.includes('Verifierad i Skickade beställningar, order'));
  const stale = R.freshness(doc, { freshness: { sha: 'abc1234', committedAt: '2026-10-19T00:00:00Z', stale: true } }, NOW).textContent;
  assert.ok(stale.includes('Körs agenten lokalt? Pusha statefilerna för att se dem här'));
  assert.ok(stale.includes('abc1234'));
  const withHours = R.freshness(doc, { freshness: { sha: 'abc1234', stale: true, staleHours: 36 } }, NOW).textContent;
  assert.ok(withHours.includes('Över 36 timmar gammal.'));
  assert.ok(!stale.includes('undefined'));
});

test('headline reflects what needs attention', () => {
  assert.equal(R.headline({ tiles: { needsAction: 1, pending: 2, waitingKlar: 0 } }), '1 bokning behöver din åtgärd');
  assert.equal(R.headline({ tiles: { needsAction: 0, pending: 2, waitingKlar: 0 } }), '2 beställningar väntar på ditt godkännande i Telegram');
  assert.equal(R.headline({ tiles: { needsAction: 0, pending: 0, waitingKlar: 0 } }), 'Inget väntar på dig just nu');
});

test('relative times are Swedish', () => {
  assert.equal(R.timeAgo('2026-10-21T11:59:50Z', NOW), 'just nu');
  assert.equal(R.timeAgo('2026-10-21T09:00:00Z', NOW), 'för 3 timmar sedan');
  assert.equal(R.timeAgo('nope', NOW), 'okänd tid');
});

test('front-end source never uses HTML-parsing sinks or eval', () => {
  const dir = new URL('../public/', import.meta.url);
  const sinks = /\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval|new Function|setHTMLUnsafe|createContextualFragment)\b|setTimeout\(\s*['"`]/;
  for (const name of readdirSync(dir).filter((f) => f.endsWith('.js'))) {
    const src = readFileSync(new URL(name, dir), 'utf8');
    assert.ok(!sinks.test(src), `${name} uses a forbidden sink: ${src.match(sinks)?.[0]}`);
  }
});

test('index.html has no inline scripts, styles or event handlers', () => {
  const html = readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
  assert.ok(!/<script(?![^>]*\bsrc=)[^>]*>/i.test(html), 'inline <script>');
  assert.ok(!/<style\b/i.test(html), 'inline <style>');
  assert.ok(!/\sstyle=/i.test(html), 'style attribute');
  assert.ok(!/\son[a-z]+=/i.test(html), 'event handler attribute');
  assert.ok(!/https?:\/\//i.test(html), 'no external resources');
});

test('secrets are never in client code', () => {
  const dir = new URL('../public/', import.meta.url);
  for (const name of readdirSync(dir)) {
    const src = readFileSync(new URL(name, dir), 'utf8');
    assert.ok(!/GITHUB_TOKEN|SESSION_SECRET|DASHBOARD_PASSWORD|ghp_|github_pat_/.test(src), name);
  }
});

test('alerts banner: error and warning items, text set via textContent, relative time', () => {
  const vm = { alerts: [
    { key: 'a', text: '<img src=x onerror=alert(1)> inloggning', severity: 'error', firstSeen: '2026-10-21T08:00:00Z', lastSeen: '2026-10-21T09:00:00Z', count: 3 },
    { key: 'b', text: 'Varning', severity: 'warning', firstSeen: '2026-10-21T08:00:00Z', lastSeen: '2026-10-21T09:00:00Z', count: 1 },
  ] };
  const el = R.alerts(doc, vm, NOW);
  const nodes = all(el);
  assert.ok(nodes.every((n) => n.tagName !== 'img'));
  assert.ok(nodes.some((n) => /\balert-error\b/.test(n.className)));
  assert.ok(nodes.some((n) => /\balert-warning\b/.test(n.className)));
  assert.ok(el.textContent.includes('<img src=x onerror=alert(1)> inloggning'));
  assert.match(el.textContent, /sedan /);
  assert.equal(R.alerts(doc, { alerts: [] }, NOW), null);
  assert.equal(R.alerts(doc, {}, NOW), null);
});

test('stale warning appears when the last successful run is older than the view model threshold', () => {
  const vm = (age) => ({ freshness: { sha: 'abc1234', committedAt: '2026-10-21T10:00:00Z', stale: false },
    lastSuccessAgeHours: age, lastSuccessStaleHours: 52 });
  // Saturday 15:00 to Monday 15:00 is 48 h with no Sunday run: no warning.
  assert.ok(!R.freshness(doc, vm(48), NOW).textContent.includes('Senaste lyckade körning'));
  assert.ok(!R.freshness(doc, vm(5), NOW).textContent.includes('Senaste lyckade körning'));
  const late = R.freshness(doc, vm(53), NOW).textContent;
  assert.ok(late.includes('Senaste lyckade körning för över 52 timmar sedan.'));
  // No threshold in the view model (older server): no warning rather than a guessed one.
  const noThreshold = { ...vm(100), lastSuccessStaleHours: undefined };
  assert.ok(!R.freshness(doc, noThreshold, NOW).textContent.includes('Senaste lyckade körning'));
});

test('the render thresholds come from the server view model', () => {
  const vm = summarize({ historyText: JSON.stringify({ timestamp: '2026-10-19T00:00:00', agent: 'booking_agent' }),
    now: Date.parse('2026-10-21T12:00:00Z') });
  assert.equal(vm.lastSuccessStaleHours, 52);
  assert.ok(R.freshness(doc, vm, Date.parse('2026-10-21T12:00:00Z')).textContent.includes('över 52 timmar'));
});

const ORDERS = [
  { bookingId: 'B-1', bookingNr: '880142', regNr: 'QXA112', status: 'pending', portalStatus: null, verifiedNumbers: null },
  { bookingId: 'B-2', bookingNr: '880151', regNr: 'QLM48K', status: 'ordered', portalStatus: 'Levererad', verifiedNumbers: '4471093, 4471094' },
  { bookingId: 'B-3', bookingNr: null, regNr: null, status: 'blocked', portalStatus: 'Restad', verifiedNumbers: null },
];
const ids = (list) => list.map((o) => o.bookingId);

test('filterOrders: query is a case-insensitive substring over id, number, plate, status, portal status and numbers', () => {
  assert.deepEqual(ids(R.filterOrders(ORDERS, {})), ['B-1', 'B-2', 'B-3']);
  assert.deepEqual(ids(R.filterOrders(ORDERS, { query: '  ' })), ['B-1', 'B-2', 'B-3']);
  assert.deepEqual(ids(R.filterOrders(ORDERS, { query: 'b-2' })), ['B-2']);
  assert.deepEqual(ids(R.filterOrders(ORDERS, { query: '88014' })), ['B-1']);
  assert.deepEqual(ids(R.filterOrders(ORDERS, { query: 'qlm' })), ['B-2']);
  assert.deepEqual(ids(R.filterOrders(ORDERS, { query: 'QLM 48K' })), ['B-2'], 'plate with a space');
  assert.deepEqual(ids(R.filterOrders(ORDERS, { query: 'levererad' })), ['B-2']);
  assert.deepEqual(ids(R.filterOrders(ORDERS, { query: '4471094' })), ['B-2']);
  assert.deepEqual(ids(R.filterOrders(ORDERS, { query: 'blocked' })), ['B-3']);
  assert.deepEqual(ids(R.filterOrders(ORDERS, { query: 'stoppad' })), ['B-3'], 'Swedish status label');
});

test('filterOrders: status filter, both together, no match, and purity', () => {
  const copy = JSON.stringify(ORDERS);
  assert.deepEqual(ids(R.filterOrders(ORDERS, { status: 'ordered' })), ['B-2']);
  assert.deepEqual(ids(R.filterOrders(ORDERS, { status: '' })), ['B-1', 'B-2', 'B-3']);
  assert.deepEqual(ids(R.filterOrders(ORDERS, { status: 'ordered', query: 'levererad' })), ['B-2']);
  assert.deepEqual(R.filterOrders(ORDERS, { status: 'pending', query: 'levererad' }), []);
  assert.deepEqual(R.filterOrders(ORDERS, { query: 'finns inte' }), []);
  assert.deepEqual(R.filterOrders(null, { query: 'x' }), []);
  assert.equal(JSON.stringify(ORDERS), copy, 'input is not mutated');
});

test('orders list shows "Inga träffar" when the filter matches nothing, and portal status as text', () => {
  const vm = vmWith({ portal_status: EVIL });
  const none = R.orders(doc, vm, NOW, { query: 'zzzz' });
  assert.equal(none.textContent, 'Inga träffar');
  assert.equal(R.orders(doc, { orders: [] }, NOW, { query: 'zzzz' }).textContent.includes('Inga delbeställningar'), true);
  const some = R.orders(doc, vm, NOW, { query: 'qab' });
  assert.equal(all(some).filter((e) => e.tagName === 'li' && /\border\b/.test(e.className)).length, 1);
  assert.ok(some.textContent.includes(EVIL), 'portal status shown verbatim as text');
  assert.ok(all(some).every((e) => e.tagName !== 'img'));
});

test('counts row renders the four counts', () => {
  const vm = { counts: { waiting: 2, klar: 1, ordered: 5, needsAction: 0 } };
  const el = R.countsRow(doc, vm);
  assert.match(el.textContent, /2/);
  assert.equal(all(el).filter((e) => e.tagName === 'li').length, 4);
  assert.equal(R.countsRow(doc, {}).textContent.includes('NaN'), false);
});

test('search UI: inputs exist, no re-fetch on typing, only DOM APIs', () => {
  const html = readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
  assert.match(html, /id="order-search"/);
  assert.match(html, /id="order-status-filter"/);
  assert.match(html, /id="counts"/);
  const app = readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
  const handler = app.match(/function onFilter[\s\S]*?\n}\n/)?.[0] ?? '';
  assert.ok(handler && !/load\(|api\(|fetch\(/.test(handler), 'filtering does not fetch');
  assert.match(app, /addEventListener\('input'/);
});
