// npm run preview: writes preview/dashboard-preview.html, one self-contained file
// that shows the dashboard with the invented demo data. No server, no network:
// open it by double-clicking. The UI code is the real public/*.js, bundled here.
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { buildHandlers } from './lib/runtime.js';

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
export const OUT_FILE = here('./preview/dashboard-preview.html');

// Module order = dependency order. transport.js is replaced by the preview transport.
const MODULES = [
  ['links', 'public/links.js'],
  ['render', 'public/render.js'],
  ['session', 'public/session.js'],
  ['transport', 'lib/preview-transport.js'],
  ['app', 'public/app.js'],
];

function modName(spec) {
  const name = spec.replace(/^.*\//, '').replace(/\.js$/, '');
  return name === 'preview-transport' ? 'transport' : name;
}

/** Turns one of our ES modules into an IIFE that returns its exports. Only the syntax our modules use is supported. */
export function wrapModule(name, source) {
  const exported = [];
  let body = source
    .replace(/^import \* as (\w+) from '([^']+)';$/gm, (_, local, spec) => `const ${local} = __mod_${modName(spec)};`)
    .replace(/^import \{([^}]+)\} from '([^']+)';$/gm, (_, names, spec) => `const {${names}} = __mod_${modName(spec)};`)
    .replace(/^export \{([^}]+)\};$/gm, (_, names) => {
      exported.push(...names.split(',').map((n) => n.trim()).filter(Boolean));
      return '';
    })
    .replace(/^export (async function|function|const|let|class) (\w+)/gm, (_, kind, id) => {
      exported.push(id);
      return `${kind} ${id}`;
    });
  if (/^\s*(import|export)\b/m.test(body)) throw new Error(`${name}: unsupported import/export syntax`);
  body = body.replace(/\r\n/g, '\n');
  return `const __mod_${name} = (() => {\n${body}\nreturn { ${exported.join(', ')} };\n})();\n`;
}

async function demoStatus() {
  const h = buildHandlers({}, { demo: true, log: () => {} });
  const call = (fn, req) => new Promise((resolve) => {
    const res = {
      headers: {}, statusCode: 0,
      setHeader(k, v) { this.headers[k.toLowerCase()] = v; },
      end(body) { resolve({ status: this.statusCode, headers: this.headers, body: JSON.parse(body) }); },
    };
    fn(req, res);
  });
  const login = await call(h.login, {
    method: 'POST', url: '/api/login', body: JSON.stringify({ password: 'demo' }),
    headers: { host: 'preview', 'content-type': 'application/json', 'x-requested-with': 'dashboard' },
  });
  const cookie = login.headers['set-cookie'].split(';')[0];
  const status = await call(h.status, { method: 'GET', url: '/api/status', headers: { cookie } });
  if (status.status !== 200) throw new Error('demo status failed');
  // The preview is fully offline: no outbound links either (they would only point at demo pages).
  const vm = status.body;
  return {
    ...vm,
    preview: true,
    links: {},
    orders: vm.orders.map((o) => ({ ...o, detailUrl: null })),
  };
}

const sha = (text) => `'sha256-${createHash('sha256').update(text, 'utf8').digest('base64')}'`;

export async function buildPreview() {
  const [html, css, ...sources] = await Promise.all([
    readFile(here('./public/index.html'), 'utf8'),
    readFile(here('./public/style.css'), 'utf8'),
    ...MODULES.map(([, p]) => readFile(here('./' + p), 'utf8')),
  ]);
  const data = JSON.stringify(await demoStatus()).replace(/</g, '\\u003c');
  const script = `"use strict";\nconst PREVIEW_STATUS = ${data};\n`
    + MODULES.map(([name], i) => wrapModule(name, sources[i])).join('\n');
  if (/<\/(script|style)/i.test(script + css)) throw new Error('inline content would close its tag');
  const csp = `default-src 'none'; script-src ${sha(script)}; style-src ${sha(css)}; img-src data:; `
    + "connect-src 'none'; base-uri 'none'; form-action 'none'";
  const page = html
    .replace(/[ \t]*<link rel="stylesheet" href="\/style.css">\n/, '')
    .replace(/[ \t]*<script type="module" src="\/app.js"><\/script>\n/, '')
    .replace('<meta charset="utf-8">', () => `<meta charset="utf-8">\n  <meta http-equiv="Content-Security-Policy" content="${csp}">`)
    .replace('<title>MECA-agenten</title>', () => '<title>MECA-agenten (förhandsvisning)</title>')
    .replace('</head>', () => `  <style>${css}</style>\n</head>`)
    .replace('</body>', () => `  <script>${script}</script>\n</body>`);
  if (page.includes('src="/app.js"') || page.includes('href="/style.css"')) throw new Error('index.html changed: update preview.js');
  return page;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const page = await buildPreview();
  await mkdir(here('./preview/'), { recursive: true });
  await writeFile(OUT_FILE, page, 'utf8');
  console.log(`Skrev / wrote ${OUT_FILE}`);
  console.log('Dubbelklicka på filen för att öppna den / double-click the file to open it.');
}
