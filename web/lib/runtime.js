// Builds the handlers once per process. The Vercel functions in api/ call
// handlers(), which always runs in live mode and ignores any DEMO variable.
// Demo mode exists only when dev-server.js passes { demo: true } explicitly.
import { createHandlers } from './handlers.js';
import { createGitHub } from './github.js';
import { createDemoSource } from './demo.js';

export const DEMO_PASSWORD = 'demo';
// Fixed demo secret: only reachable through buildHandlers(env, { demo: true }) off Vercel.
const DEMO_SECRET = 'demo-only-session-secret-not-for-production-use';

export function buildHandlers(env = process.env, { demo = false, log } = {}) {
  if (demo) {
    if (env.VERCEL) throw new Error('Demo mode is not allowed on Vercel');
    return createHandlers({
      env: { DASHBOARD_PASSWORD: DEMO_PASSWORD, SESSION_SECRET: DEMO_SECRET },
      source: () => createDemoSource(),
      demo: true,
      log,
    });
  }
  let github = null;
  return createHandlers({
    env: { DASHBOARD_PASSWORD: env.DASHBOARD_PASSWORD, SESSION_SECRET: env.SESSION_SECRET },
    source: () => {
      github ??= createGitHub({
        token: env.GITHUB_TOKEN, repo: env.GITHUB_REPO, branch: env.GITHUB_BRANCH || 'main',
      });
      return github;
    },
    // On Vercel the platform sets the client-IP and proto headers; elsewhere they are spoofable.
    trustProxy: Boolean(env.VERCEL),
    log,
  });
}

let cached = null;
/** Entry point for api/*.js: live mode only. */
export function handlers() {
  cached ??= buildHandlers(process.env);
  return cached;
}
