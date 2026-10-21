// Builds the handlers once per process from the environment. The Vercel
// functions in api/ and dev-server.js both use this.
import { createHandlers } from './handlers.js';
import { createGitHub } from './github.js';
import { createDemoSource } from './demo.js';

export const DEMO_PASSWORD = 'demo';
// Fixed demo secret: only ever used when isDemo() is true, which is never on Vercel.
const DEMO_SECRET = 'demo-only-session-secret-not-for-production-use';

/** Demo mode needs DEMO=1 and is refused on Vercel, so a stray env var cannot open the site. */
export function isDemo(env = process.env) {
  return env.DEMO === '1' && !env.VERCEL;
}

export function buildHandlers(env = process.env) {
  if (isDemo(env)) {
    return createHandlers({
      env: { DASHBOARD_PASSWORD: DEMO_PASSWORD, SESSION_SECRET: DEMO_SECRET },
      source: () => createDemoSource(),
      demo: true,
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
  });
}

let cached = null;
export function handlers() {
  cached ??= buildHandlers();
  return cached;
}
