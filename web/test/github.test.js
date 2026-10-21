import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createGitHub, GitHubError } from '../lib/github.js';

const TOKEN = 'fake-token-for-tests-only';
const base = { token: TOKEN, repo: 'example-owner/example-repo', branch: 'main' };

function response(status, body, headers = {}) {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), { status, headers });
}

function mockFetch(handler) {
  const calls = [];
  const fn = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return handler(String(url), init);
  };
  fn.calls = calls;
  return fn;
}

function assertNoToken(err) {
  assert.ok(err instanceof GitHubError);
  assert.ok(!String(err.message).includes(TOKEN));
  assert.ok(!String(err.stack).includes(TOKEN));
  assert.ok(!JSON.stringify(err).includes(TOKEN));
}

test('reads a file as raw text with the token in the header only', async () => {
  const fetchImpl = mockFetch(() => response(200, '{"bookings":{}}'));
  const gh = createGitHub({ ...base, fetchImpl });
  assert.equal(await gh.getFile('config/orders.json'), '{"bookings":{}}');
  const { url, init } = fetchImpl.calls[0];
  assert.equal(url, 'https://api.github.com/repos/example-owner/example-repo/contents/config/orders.json?ref=main');
  assert.equal(init.headers.Accept, 'application/vnd.github.raw+json');
  assert.equal(init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.ok(!url.includes(TOKEN));
});

test('a missing file reads as null', async () => {
  const gh = createGitHub({ ...base, fetchImpl: mockFetch(() => response(404, { message: 'Not Found' })) });
  assert.equal(await gh.getFile('logs/run_history.jsonl'), null);
});

test('403 with an exhausted rate limit maps to rate_limited', async () => {
  const gh = createGitHub({
    ...base,
    fetchImpl: mockFetch(() => response(403, { message: `API rate limit exceeded ${TOKEN}` },
      { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1700000000' })),
  });
  await assert.rejects(gh.getFile('config/orders.json'), (err) => {
    assert.equal(err.code, 'rate_limited');
    assertNoToken(err);
    assert.ok(!err.message.includes('API rate limit exceeded'), 'GitHub body is not echoed');
    return true;
  });
});

test('401 maps to unauthorized and 500 to upstream', async () => {
  for (const [status, code] of [[401, 'unauthorized'], [403, 'forbidden'], [500, 'upstream'], [429, 'rate_limited']]) {
    const gh = createGitHub({ ...base, fetchImpl: mockFetch(() => response(status, 'nope')) });
    await assert.rejects(gh.getLatestCommit(), (err) => err.code === code);
  }
});

test('a hanging request times out', async () => {
  const fetchImpl = (url, init) => new Promise((_, reject) => {
    init.signal.addEventListener('abort', () => reject(init.signal.reason));
  });
  const gh = createGitHub({ ...base, fetchImpl, timeoutMs: 20 });
  await assert.rejects(gh.getFile('config/orders.json'), (err) => {
    assert.equal(err.code, 'timeout');
    assertNoToken(err);
    return true;
  });
});

test('a network error carrying the token is mapped without the token', async () => {
  const logged = [];
  const orig = console.error;
  console.error = (...a) => logged.push(a.join(' '));
  try {
    const gh = createGitHub({ ...base, fetchImpl: async () => { throw new Error(`boom ${TOKEN}`); } });
    await assert.rejects(gh.getFile('x'), (err) => {
      assert.equal(err.code, 'network');
      assertNoToken(err);
      return true;
    });
  } finally {
    console.error = orig;
  }
  assert.ok(!logged.join('\n').includes(TOKEN));
});

test('latest commit returns the sha and committer date', async () => {
  const fetchImpl = mockFetch(() => response(200, {
    sha: 'abc1234def5678', commit: { committer: { date: '2026-10-20T08:00:00Z' } },
  }));
  const gh = createGitHub({ ...base, fetchImpl });
  assert.deepEqual(await gh.getLatestCommit(), { sha: 'abc1234def5678', date: '2026-10-20T08:00:00Z' });
  assert.equal(fetchImpl.calls[0].url, 'https://api.github.com/repos/example-owner/example-repo/commits/main');
});

test('dispatch posts the ref to the workflow file', async () => {
  const fetchImpl = mockFetch(() => new Response(null, { status: 204 }));
  const gh = createGitHub({ ...base, fetchImpl });
  await gh.dispatch('approve.yml');
  const { url, init } = fetchImpl.calls[0];
  assert.equal(url, 'https://api.github.com/repos/example-owner/example-repo/actions/workflows/approve.yml/dispatches');
  assert.equal(init.method, 'POST');
  assert.deepEqual(JSON.parse(init.body), { ref: 'main' });
});

test('invalid configuration is refused', () => {
  assert.throws(() => createGitHub({ ...base, token: '' }), (e) => e.code === 'config');
  assert.throws(() => createGitHub({ ...base, repo: 'not a repo' }), (e) => e.code === 'config');
  assert.throws(() => createGitHub({ ...base, repo: '../x/y' }), (e) => e.code === 'config');
  assert.throws(() => createGitHub({ ...base, branch: 'a..b' }), (e) => e.code === 'config');
});

test('dispatch refuses a workflow file name that is not plain', async () => {
  const gh = createGitHub({ ...base, fetchImpl: mockFetch(() => response(204, '')) });
  await assert.rejects(gh.dispatch('../../x'), (e) => e.code === 'config');
});

test('redirects are never followed', async () => {
  const fetchImpl = mockFetch(() => response(200, 'x'));
  const gh = createGitHub({ ...base, fetchImpl });
  await gh.getFile('a');
  await gh.getLatestCommit().catch(() => {});
  await gh.dispatch('approve.yml');
  assert.ok(fetchImpl.calls.every((c) => c.init.redirect === 'error'));
  const failing = createGitHub({ ...base, fetchImpl: async () => { throw new TypeError('fetch failed: redirect mode is set to error'); } });
  await assert.rejects(failing.getFile('a'), (e) => e.code === 'network');
});

test('response bodies over 2 MB are refused', async () => {
  const big = 'x'.repeat(2 * 1024 * 1024 + 1);
  const streamed = createGitHub({ ...base, fetchImpl: mockFetch(() => response(200, big)) });
  await assert.rejects(streamed.getFile('config/orders.json'), (e) => e.code === 'too_large' && !e.message.includes('x'.repeat(10)));
  const declared = createGitHub({ ...base, fetchImpl: mockFetch(() => new Response('small', { status: 200, headers: { 'content-length': String(50 * 1024 * 1024) } })) });
  await assert.rejects(declared.getFile('config/orders.json'), (e) => e.code === 'too_large');
  const ok = createGitHub({ ...base, fetchImpl: mockFetch(() => response(200, 'x'.repeat(1024 * 1024))) });
  assert.equal((await ok.getFile('x')).length, 1024 * 1024);
});
