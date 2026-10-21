// Stand-in for public/transport.js inside the offline preview file. It answers
// from PREVIEW_STATUS, which preview.js embeds; nothing goes over the network.
/* global PREVIEW_STATUS */
export async function send(path) {
  if (path.startsWith('/api/status')) {
    const data = structuredClone(PREVIEW_STATUS);
    if (path.includes('stale=1')) data.freshness = { ...data.freshness, stale: true };
    return { status: 200, ok: true, data };
  }
  if (path === '/api/run') return { status: 202, ok: true, data: { ok: true, preview: true } };
  return { status: 200, ok: true, data: { ok: true } };
}
