// Local server: serves public/ and the same API handlers as Vercel.
//   node dev-server.js --demo     invented demo data, password "demo", no GitHub calls
//   node dev-server.js            real env vars (DASHBOARD_PASSWORD, SESSION_SECRET, GITHUB_*)
// Listens on 127.0.0.1 only. Not used in production.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { applySecurityHeaders } from './lib/http.js';

if (process.argv.includes('--demo')) process.env.DEMO = '1';
const { buildHandlers, isDemo } = await import('./lib/runtime.js');

const PORT = Number(process.env.PORT) || 3000;
const ROOT = fileURLToPath(new URL('./public/', import.meta.url));
const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
};
const handlers = buildHandlers();
const routes = {
  '/api/login': handlers.login,
  '/api/logout': handlers.logout,
  '/api/status': handlers.status,
  '/api/run': handlers.run,
};

async function serveStatic(pathname, res) {
  const rel = pathname === '/' ? 'index.html' : decodeURIComponent(pathname).replace(/^\/+/, '');
  const file = normalize(join(ROOT, rel));
  if (!file.startsWith(ROOT.endsWith(sep) ? ROOT : ROOT + sep) || !TYPES[extname(file)]) {
    res.statusCode = 404;
    return res.end('Not found');
  }
  try {
    const data = await readFile(file);
    res.setHeader('Content-Type', TYPES[extname(file)]);
    res.setHeader('Cache-Control', 'no-cache');
    res.end(data);
  } catch {
    res.statusCode = 404;
    res.end('Not found');
  }
}

const server = createServer(async (req, res) => {
  applySecurityHeaders(res);
  let pathname;
  try {
    pathname = new URL(req.url, 'http://localhost').pathname;
  } catch {
    res.statusCode = 400;
    return res.end();
  }
  const route = routes[pathname];
  try {
    if (route) return await route(req, res);
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.statusCode = 405;
      return res.end();
    }
    return await serveStatic(pathname, res);
  } catch {
    res.statusCode = 500;
    res.end();
  }
});

server.listen(PORT, '127.0.0.1', () => {
  const mode = isDemo() ? 'demo mode, password "demo"' : 'live mode (GitHub)';
  console.log(`Dashboard on http://localhost:${PORT} (${mode}). Ctrl+C to stop.`);
});
