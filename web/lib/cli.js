// Argument parsing for dev-server.js (kept separate so it can be tested).
export const DEFAULT_PORT = 3737; // 3000 is often taken by other dev tools

export function parsePort(value) {
  const s = String(value ?? '').trim();
  if (!/^\d{1,5}$/.test(s)) return null;
  const n = Number(s);
  return n >= 1024 && n <= 65535 ? n : null;
}

/** { demo, port } from argv (without node and the script) and env; throws Error with a printable message. */
export function parseArgs(argv, env = {}) {
  let demo = false;
  let port = env.PORT !== undefined && env.PORT !== '' ? parsePort(env.PORT) : DEFAULT_PORT;
  if (port === null) throw new Error(`PORT=${env.PORT} är ogiltig / is invalid (1024-65535)`);
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--demo') demo = true;
    else if (arg === '--port' || arg.startsWith('--port=')) {
      const value = arg === '--port' ? argv[++i] : arg.slice('--port='.length);
      port = parsePort(value);
      if (port === null) throw new Error(`--port ${value ?? ''} är ogiltig / is invalid: välj 1024-65535 / use 1024-65535`);
    } else {
      throw new Error(`Okänt argument / unknown argument: ${arg}. Användning / usage: node dev-server.js [--demo] [--port N]`);
    }
  }
  return { demo, port };
}

export function portInUseMessage(port) {
  const next = port < 65535 ? port + 1 : DEFAULT_PORT;
  return `Port ${port} används redan / is already in use — starta med / start with: `
    + `node dev-server.js --demo --port ${next}   (eller / or: npm run demo -- --port ${next})`;
}

/** DNS-rebinding defence: only Host values that name this server on a loopback address. */
export function isAllowedHost(host, port) {
  if (typeof host !== 'string') return false;
  const h = host.toLowerCase();
  return h === `127.0.0.1:${port}` || h === `localhost:${port}` || h === `[::1]:${port}`;
}
