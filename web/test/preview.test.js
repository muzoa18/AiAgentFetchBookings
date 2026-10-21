import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { buildPreview, embedJson, wrapModule } from '../preview.js';

const page = await buildPreview();

test('the preview has no external URLs or resources', () => {
  assert.equal(page.match(/https?:\/\/(?!127\.0\.0\.1|localhost)[^\s"'`<]*/g), null);
  assert.ok(!/\b(src|href)=["'](?!#|data:)/i.test(page.replace(/<a [^>]*href="#main"/, '')), 'no src/href to files');
  assert.ok(!/@import|url\(\s*['"]?http/i.test(page));
  assert.match(page, /<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-[^']+'; style-src 'sha256-[^']+';/);
  assert.match(page, /connect-src 'none'/);
});

test('the preview embeds the demo bookings and skips the network', () => {
  for (const nr of ['880142', '880133', '880097', 'QXA112']) assert.ok(page.includes(nr), nr);
  assert.ok(!/fetch\(/.test(page), 'no fetch( at all');
  assert.ok(!/XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts/.test(page), 'no other network APIs');
  assert.ok(page.includes('Startad (demo)'));
});

test('the bundled script compiles and keeps the no-innerHTML rule', () => {
  const script = page.match(/<script>([\s\S]*)<\/script>/)[1];
  assert.doesNotThrow(() => new vm.Script(script));
  assert.ok(!/\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b/.test(script));
});

test('wrapModule rewrites imports and exports', () => {
  const out = wrapModule('x', "import { a } from './links.js';\nimport * as R from './render.js';\nexport function f() {}\nexport const C = 1;\nexport { a };\n");
  assert.match(out, /const \{ a \} = __mod_links;/);
  assert.match(out, /const R = __mod_render;/);
  assert.match(out, /return \{ a, f, C \};/);
  assert.throws(() => wrapModule('y', "export default 1;\n"), /unsupported/);
});

test('the build works the same from a CRLF checkout', async () => {
  const { mkdtemp, mkdir, readFile, writeFile, rm } = await import('node:fs/promises');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const dir = await mkdtemp(join(tmpdir(), 'preview-crlf-'));
  try {
    const files = ['public/index.html', 'public/style.css', 'public/links.js', 'public/render.js',
      'public/session.js', 'public/app.js', 'lib/preview-transport.js'];
    for (const f of files) {
      const text = await readFile(new URL('../' + f, import.meta.url), 'utf8');
      await mkdir(join(dir, f, '..'), { recursive: true });
      await writeFile(join(dir, f), text.replace(/\r?\n/g, '\r\n'));
    }
    const status = { tiles: {}, orders: [], history: [], freshness: {}, counts: {}, preview: true };
    const lf = await buildPreview({ status });
    const crlf = await buildPreview({ root: dir, status });
    assert.equal(crlf, lf);
    assert.ok(!crlf.includes('\r'));
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('embedded JSON escapes <, U+2028 and U+2029', () => {
  const out = embedJson({ t: '</script><b>\u2028x\u2029' });
  assert.ok(!out.includes('<') && !out.includes('\u2028') && !out.includes('\u2029'));
  assert.deepEqual(JSON.parse(out), { t: '</script><b>\u2028x\u2029' });
  assert.ok(!/[\u2028\u2029]/.test(page));
});
