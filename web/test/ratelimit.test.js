import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createLimiter } from '../lib/ratelimit.js';

test('allows up to the limit inside the window, then blocks', () => {
  let now = 0;
  const rl = createLimiter({ limit: 5, windowMs: 600_000, clock: () => now });
  for (let i = 0; i < 5; i++) assert.equal(rl.hit('1.2.3.4').allowed, true);
  const blocked = rl.hit('1.2.3.4');
  assert.equal(blocked.allowed, false);
  assert.equal(blocked.retryAfterS, 600);
  assert.equal(rl.hit('5.6.7.8').allowed, true, 'other keys are independent');
  now = 600_001;
  assert.equal(rl.hit('1.2.3.4').allowed, true, 'the window resets');
});

test('reset clears a key', () => {
  const rl = createLimiter({ limit: 1, windowMs: 1000, clock: () => 0 });
  rl.hit('k');
  assert.equal(rl.hit('k').allowed, false);
  rl.reset('k');
  assert.equal(rl.hit('k').allowed, true);
});

test('memory is bounded', () => {
  const rl = createLimiter({ limit: 1, windowMs: 1000, maxKeys: 10, clock: () => 0 });
  for (let i = 0; i < 100; i++) rl.hit('k' + i);
  assert.ok(rl.size() <= 10);
});
