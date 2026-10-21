// Minimal GitHub REST client. Errors are GitHubError with a short code; the
// token, request URLs with secrets and GitHub's response bodies never end up in
// an error message or a log line.
const API = 'https://api.github.com';
const REPO_RE = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/;
const BRANCH_RE = /^[A-Za-z0-9_./-]{1,200}$/;
const WORKFLOW_RE = /^[A-Za-z0-9_-]{1,100}\.ya?ml$/;

export class GitHubError extends Error {
  constructor(code, status = 0) {
    super(`GitHub request failed (${code})`);
    this.name = 'GitHubError';
    this.code = code;
    this.status = status;
  }
}

export function createGitHub({ token, repo, branch = 'main', fetchImpl = globalThis.fetch, timeoutMs = 8000 }) {
  if (typeof token !== 'string' || token.length < 10) throw new GitHubError('config');
  if (typeof repo !== 'string' || !REPO_RE.test(repo) || repo.includes('..')) throw new GitHubError('config');
  if (typeof branch !== 'string' || !BRANCH_RE.test(branch) || branch.includes('..')) throw new GitHubError('config');

  const [owner, name] = repo.split('/');
  const repoPath = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;

  async function request(path, { accept = 'application/vnd.github+json', method = 'GET', body } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new GitHubError('timeout')), timeoutMs);
    try {
      let res;
      try {
        res = await fetchImpl(API + path, {
          method,
          headers: {
            Accept: accept,
            Authorization: `Bearer ${token}`,
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'meca-agent-dashboard',
            ...(body ? { 'Content-Type': 'application/json' } : {}),
          },
          body: body ? JSON.stringify(body) : undefined,
          signal: controller.signal,
        });
      } catch {
        // The caught error may echo request details; it is deliberately dropped.
        throw new GitHubError(controller.signal.aborted ? 'timeout' : 'network');
      }
      if (!res.ok) {
        res.body?.cancel?.().catch(() => {}); // GitHub's error body is never read or kept
        const remaining = res.headers.get('x-ratelimit-remaining');
        if (res.status === 429 || (res.status === 403 && (remaining === '0' || res.headers.has('retry-after')))) {
          throw new GitHubError('rate_limited', res.status);
        }
        if (res.status === 401) throw new GitHubError('unauthorized', 401);
        if (res.status === 403) throw new GitHubError('forbidden', 403);
        if (res.status === 404) throw new GitHubError('not_found', 404);
        if (res.status === 422) throw new GitHubError('rejected', 422);
        throw new GitHubError('upstream', res.status);
      }
      try {
        return await res.text();
      } catch {
        throw new GitHubError(controller.signal.aborted ? 'timeout' : 'network');
      }
    } finally {
      clearTimeout(timer);
    }
  }

  return {
    /** File contents as text, or null when the file (or path) does not exist. */
    async getFile(path) {
      const clean = String(path).split('/').map(encodeURIComponent).join('/');
      try {
        return await request(`${repoPath}/contents/${clean}?ref=${encodeURIComponent(branch)}`,
          { accept: 'application/vnd.github.raw+json' });
      } catch (err) {
        if (err instanceof GitHubError && err.code === 'not_found') return null;
        throw err;
      }
    },

    /** { sha, date } of the newest commit on the branch. */
    async getLatestCommit() {
      const text = await request(`${repoPath}/commits/${encodeURIComponent(branch)}`);
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        throw new GitHubError('upstream');
      }
      const sha = typeof data?.sha === 'string' ? data.sha : null;
      const date = data?.commit?.committer?.date ?? data?.commit?.author?.date ?? null;
      return { sha, date: typeof date === 'string' ? date : null };
    },

    /** Start a workflow_dispatch run of a workflow file on the configured branch. */
    async dispatch(workflowFile) {
      if (typeof workflowFile !== 'string' || !WORKFLOW_RE.test(workflowFile)) throw new GitHubError('config');
      await request(`${repoPath}/actions/workflows/${workflowFile}/dispatches`, {
        method: 'POST', body: { ref: branch },
      });
    },

    actionsUrl(workflowFile) {
      return `https://github.com/${owner}/${name}/actions` + (workflowFile ? `/workflows/${workflowFile}` : '');
    },
  };
}
