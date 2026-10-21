import { test } from 'node:test';
import assert from 'node:assert/strict';
import { unauthorizedMessage } from '../public/session.js';
import { parseArgs, parsePort, portInUseMessage } from '../lib/cli.js';

test('a 401 right after a successful login explains the cookie problem', () => {
  const msg = unauthorizedMessage({ justLoggedIn: true, location: { hostname: 'localhost', port: '3000' } });
  assert.equal(msg, 'Webbläsaren sparade inte inloggningen (cookie). Prova http://127.0.0.1:3000 eller en annan webbläsare.');
  const from127 = unauthorizedMessage({ justLoggedIn: true, location: { hostname: '127.0.0.1', port: '3001' } });
  assert.match(from127, /Prova http:\/\/localhost:3001 /);
});

test('an ordinary 401 just shows the login form', () => {
  assert.equal(unauthorizedMessage({ justLoggedIn: false, location: { hostname: 'localhost', port: '3000' } }), null);
});

test('dev-server arguments: --demo, --port N, PORT env, validation', () => {
  assert.deepEqual(parseArgs([]), { demo: false, port: 3000 });
  assert.deepEqual(parseArgs(['--demo']), { demo: true, port: 3000 });
  assert.deepEqual(parseArgs(['--demo', '--port', '3001']), { demo: true, port: 3001 });
  assert.deepEqual(parseArgs(['--port=8080', '--demo']), { demo: true, port: 8080 });
  assert.deepEqual(parseArgs([], { PORT: '4000' }), { demo: false, port: 4000 });
  for (const bad of [['--port', '80'], ['--port', '70000'], ['--port'], ['--port', 'abc'], ['--port', '3000.5'], ['--evil']]) {
    assert.throws(() => parseArgs(bad), Error, bad.join(' '));
  }
  assert.equal(parsePort('1024'), 1024);
  assert.equal(parsePort('65536'), null);
  assert.match(portInUseMessage(3000), /Port 3000 används redan \/ is already in use .*--demo --port 3001/);
});
