// Best-effort fixed-window counter kept in memory. On Vercel each warm function
// instance has its own copy, so this slows down guessing but is not a hard limit.
export function createLimiter({ limit, windowMs, maxKeys = 5000, clock = Date.now }) {
  const hits = new Map(); // key -> { count, resetAt }

  function prune(now) {
    for (const [key, entry] of hits) if (entry.resetAt <= now) hits.delete(key);
    // Still too many keys: drop the oldest insertions.
    while (hits.size >= maxKeys) hits.delete(hits.keys().next().value);
  }

  return {
    hit(key) {
      const now = clock();
      let entry = hits.get(key);
      if (!entry || entry.resetAt <= now) {
        if (hits.size >= maxKeys) prune(now);
        entry = { count: 0, resetAt: now + windowMs };
        hits.set(key, entry);
      }
      entry.count += 1;
      const allowed = entry.count <= limit;
      return { allowed, retryAfterS: allowed ? 0 : Math.ceil((entry.resetAt - now) / 1000) };
    },
    reset(key) {
      hits.delete(key);
    },
    size() {
      return hits.size;
    },
  };
}
