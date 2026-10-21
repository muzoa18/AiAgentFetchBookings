import { test } from 'node:test';
import assert from 'node:assert/strict';
import { safeHref } from '../public/links.js';
import * as R from '../public/render.js';
import { summarize } from '../lib/summarize.js';

const GOOD = [
  'https://meca.promeisterportal.com/booking/123',
  'https://pro.meca.se/INTERSHOP/web/WFS/Mekonomen-MecaB2BSE-Site/sv_SE/-/SEK/ViewOrderOverview-Start',
  'https://PRO.MECA.SE/x',
];
const BAD = [
  'javascript:alert(1)',
  'JavaScript:alert(1)',
  'data:text/html,<script>alert(1)</script>',
  'http://pro.meca.se/',
  'https://pro.meca.se.evil.com/',
  'https://evil.com/pro.meca.se',
  'https://pro.meca.se@evil.com/',
  'https://user:pw@pro.meca.se/',
  'https://pro.meca.se:8443/',
  'https://evilpro.meca.se/',
  'https://meca.promeisterportal.com.evil.com/',
  '//pro.meca.se/',
  'pro.meca.se',
  '',
  null,
  42,
];

test('only https links to the allow-listed hosts pass', () => {
  for (const url of GOOD) assert.ok(safeHref(url), url);
  for (const url of BAD) assert.equal(safeHref(url), null, String(url));
});

test('github.com is only allowed when asked for (the Actions link)', () => {
  assert.equal(safeHref('https://github.com/owner/repo/actions'), null);
  assert.equal(safeHref('https://github.com/owner/repo/actions', { allowGithub: true }), 'https://github.com/owner/repo/actions');
  assert.equal(safeHref('https://github.com.evil.com/', { allowGithub: true }), null);
  assert.equal(safeHref('http://github.com/', { allowGithub: true }), null);
  assert.equal(safeHref('https://x@github.com/', { allowGithub: true }), null);
  const vm = summarize({ ordersText: JSON.stringify({ bookings: { a: { status: 'pending', detail_url: 'https://github.com/x', plan: {} } } }) });
  assert.equal(vm.orders[0].detailUrl, null, 'detail_url can never point at github.com');
});

test('render.js uses the same allow-list', () => {
  assert.equal(R.safeHref, safeHref);
});

test('summarize drops detail_url values that are not allow-listed', () => {
  for (const url of BAD.filter((u) => typeof u === 'string')) {
    const vm = summarize({ ordersText: JSON.stringify({ bookings: { a: { status: 'pending', detail_url: url, plan: {} } } }) });
    assert.equal(vm.orders[0].detailUrl, null, url);
  }
  const ok = summarize({ ordersText: JSON.stringify({ bookings: { a: { status: 'pending', detail_url: GOOD[0], plan: {} } } }) });
  assert.equal(ok.orders[0].detailUrl, GOOD[0]);
});
