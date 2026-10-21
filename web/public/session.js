// Pure decisions for the login flow (tested in test/session.test.js).

/**
 * The message to show when /api/status answers 401. Right after a successful
 * login that means the browser did not keep the session cookie: say so instead
 * of silently showing the login form again. Otherwise null (plain login form).
 */
export function unauthorizedMessage({ justLoggedIn, location }) {
  if (!justLoggedIn) return null;
  const port = location?.port ? `:${location.port}` : '';
  const alt = location?.hostname === '127.0.0.1' ? `http://localhost${port}` : `http://127.0.0.1${port}`;
  return `Webbläsaren sparade inte inloggningen (cookie). Prova ${alt} eller en annan webbläsare.`;
}
