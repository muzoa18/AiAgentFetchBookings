// Link allow-list shared by the server (summarize.js) and the browser (render.js).
// A link is only ever rendered when it is plain https to one of these exact hosts.
export const ALLOWED_LINK_HOSTS = Object.freeze([
  'meca.promeisterportal.com', // booking detail pages
  'pro.meca.se', // Orderöversikt
  'github.com', // Actions page
]);

/** The normalised URL when it is https, has no userinfo or port, and its host is allow-listed; otherwise null. */
export function safeHref(value) {
  if (typeof value !== 'string' || value.length > 500) return null;
  let u;
  try {
    u = new URL(value);
  } catch {
    return null;
  }
  if (u.protocol !== 'https:' || u.username || u.password || u.port) return null;
  if (!ALLOWED_LINK_HOSTS.includes(u.hostname.toLowerCase())) return null;
  return u.href;
}
