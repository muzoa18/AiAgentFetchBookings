import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createLimiter, createBrake } from '../lib/ratelimit.js';

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

test('eviction drops expired keys first and never flushes a blocked key by spraying', () => {
  let now = 0;
  const rl = createLimiter({ limit: 2, windowMs: 1000, maxKeys: 5, clock: () => now });
  for (let i = 0; i < 3; i++) rl.hit('attacker');
  assert.equal(rl.hit('attacker').allowed, false);
  for (let i = 0; i < 50; i++) rl.hit('spray' + i);
  assert.ok(rl.size() <= 5);
  assert.ok(rl.has('attacker'), 'the blocked key survives');
  assert.equal(rl.hit('attacker').allowed, false);
  now = 1001;
  rl.hit('fresh');
  assert.equal(rl.size(), 1, 'expired keys are pruned when room is needed');
});

test('the brake trips at the threshold inside the window and recovers after it', () => {
  let now = 0;
  const brake = createBrake({ threshold: 100, windowMs: 600_000, clock: () => now });
  for (let i = 0; i < 99; i++) brake.record();
  assert.equal(brake.tripped(), false);
  brake.record();
  assert.equal(brake.tripped(), true);
  now = 700_000;
  assert.equal(brake.tripped(), false);
});
