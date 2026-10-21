// Local server: serves public/ and the same API handlers as Vercel.
//   node dev-server.js --demo [--port N]   invented demo data, password "demo", no GitHub calls
//   node dev-server.js [--port N]          real env vars (DASHBOARD_PASSWORD, SESSION_SECRET, GITHUB_*)
// Listens on the loopback addresses only (127.0.0.1 and ::1), never on the LAN.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { applySecurityHeaders } from './lib/http.js';
import { buildHandlers } from './lib/runtime.js';
import { isAllowedHost, parseArgs, portInUseMessage } from './lib/cli.js';

let options;
try {
  options = parseArgs(process.argv.slice(2), process.env);
} catch (err) {
  console.error(err.message);
  process.exit(2);
}
const { demo, port: PORT } = options;

const ROOT = fileURLToPath(new URL('./public/', import.meta.url));
const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
};
const handlers = buildHandlers(process.env, { demo });
const routes = {
  '/api/login': handlers.login,
  '/api/logout': handlers.logout,
  '/api/status': handlers.status,
  '/api/run': handlers.run,
};

async function serveStatic(pathname, res) {
  let rel;
  try {
    rel = pathname === '/' ? 'index.html' : decodeURIComponent(pathname).replace(/^\/+/, '');
  } catch {
    res.statusCode = 400;
    return res.end();
  }
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

async function handle(req, res) {
  applySecurityHeaders(res);
  if (!isAllowedHost(req.headers.host, PORT)) {
    res.statusCode = 421; // Misdirected Request: not addressed to this loopback server
    return res.end('Misdirected request');
  }
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
    if (!res.headersSent) res.statusCode = 500;
    res.end();
  }
}

function listen(host) {
  return new Promise((resolve, reject) => {
    const server = createServer(handle);
    server.once('error', reject);
    server.listen(PORT, host, () => resolve(server));
  });
}

async function selfCheck(base) {
  try {
    for (const path of ['/', '/app.js']) {
      const res = await fetch(base + path, { signal: AbortSignal.timeout(3000) });
      if (!res.ok) throw new Error(`${path} gav / returned HTTP ${res.status}`);
      await res.arrayBuffer();
    }
    console.log(`OK – öppna ${base} i webbläsaren / open ${base} in your browser.`);
  } catch (err) {
    console.error(`Självtest misslyckades / self-check failed: ${err.message}`);
  }
}

try {
  await listen('127.0.0.1');
} catch (err) {
  console.error(err.code === 'EADDRINUSE' ? portInUseMessage(PORT) : `Kunde inte starta / could not start: ${err.message}`);
  process.exit(1);
}
let ipv6 = true;
try {
  await listen('::1');
} catch {
  ipv6 = false; // IPv6 loopback unavailable, or the port is taken there: 127.0.0.1 still works
}

const mode = demo ? 'demoläge, lösenord "demo" / demo mode, password "demo"' : 'live (GitHub)';
console.log(`Instrumentpanelen körs / dashboard running (${mode}):`);
console.log(`  http://127.0.0.1:${PORT}   (rekommenderas / recommended)`);
if (ipv6) console.log(`  http://localhost:${PORT}   (http://[::1]:${PORT})`);
console.log('Stoppa med / stop with Ctrl+C.');
await selfCheck(`http://127.0.0.1:${PORT}`);
