import { test } from 'node:test';
import assert from 'node:assert/strict';
import { unauthorizedMessage } from '../public/session.js';
import { DEFAULT_PORT, isAllowedHost, parseArgs, parsePort, portInUseMessage } from '../lib/cli.js';

test('a 401 right after a successful login explains the cookie problem', () => {
  const msg = unauthorizedMessage({ justLoggedIn: true, location: { hostname: 'localhost', port: '3737' } });
  assert.equal(msg, 'Webbläsaren sparade inte inloggningen (cookie). Prova http://127.0.0.1:3737 eller en annan webbläsare.');
  const from127 = unauthorizedMessage({ justLoggedIn: true, location: { hostname: '127.0.0.1', port: '3001' } });
  assert.match(from127, /Prova http:\/\/localhost:3001 /);
});

test('an ordinary 401 just shows the login form', () => {
  assert.equal(unauthorizedMessage({ justLoggedIn: false, location: { hostname: 'localhost', port: '3737' } }), null);
});

test('dev-server arguments: --demo, --port N, PORT env, validation', () => {
  assert.equal(DEFAULT_PORT, 3737);
  assert.deepEqual(parseArgs([]), { demo: false, port: 3737 });
  assert.deepEqual(parseArgs(['--demo']), { demo: true, port: 3737 });
  assert.deepEqual(parseArgs(['--demo', '--port', '3001']), { demo: true, port: 3001 });
  assert.deepEqual(parseArgs(['--port=8080', '--demo']), { demo: true, port: 8080 });
  assert.deepEqual(parseArgs([], { PORT: '4000' }), { demo: false, port: 4000 });
  for (const bad of [['--port', '80'], ['--port', '70000'], ['--port'], ['--port', 'abc'], ['--port', '3000.5'], ['--evil']]) {
    assert.throws(() => parseArgs(bad), Error, bad.join(' '));
  }
  assert.equal(parsePort('1024'), 1024);
  assert.equal(parsePort('65536'), null);
  assert.match(portInUseMessage(3737), /Port 3737 används redan \/ is already in use .*--demo --port 3738/);
});

test('the dev server only answers to its own loopback Host names (DNS rebinding)', () => {
  for (const host of ['127.0.0.1:3737', 'localhost:3737', 'LOCALHOST:3737', '[::1]:3737', 'LocalHost:3737']) {
    assert.equal(isAllowedHost(host, 3737), true, host);
  }
  for (const host of [undefined, '', 'localhost', '127.0.0.1', 'localhost:3738', 'evil.example:3737',
    'localhost.evil.example:3737', '127.0.0.1.nip.io:3737', '192.168.1.10:3737', '[::1]', 'localhost:3737 ', 'localhost:03737']) {
    assert.equal(isAllowedHost(host, 3737), false, String(host));
  }
});
