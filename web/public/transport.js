// Network transport for app.js. The offline preview (preview.js) swaps this
// module for lib/preview-transport.js, which answers from embedded demo data.
export async function send(path, init) {
  const res = await fetch(path, init);
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  return { status: res.status, ok: res.ok, data: data ?? {} };
}
