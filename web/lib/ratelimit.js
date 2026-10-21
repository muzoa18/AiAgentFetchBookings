// Best-effort fixed-window counters kept in memory. On Vercel each warm function
// instance has its own copy, so this slows down guessing but is not a hard limit.

export function createLimiter({ limit, windowMs, maxKeys = 5000, clock = Date.now }) {
  const hits = new Map(); // key -> { count, resetAt }, in insertion order

  // Make room for one new key: drop expired entries first, then the oldest key
  // that is not currently blocked, so spraying new keys cannot flush a blocked one.
  function makeRoom(now) {
    for (const [key, entry] of hits) if (entry.resetAt <= now) hits.delete(key);
    if (hits.size < maxKeys) return;
    for (const [key, entry] of hits) {
      if (entry.count <= limit) {
        hits.delete(key);
        return;
      }
    }
    hits.delete(hits.keys().next().value); // everything is blocked: drop the oldest
  }

  return {
    hit(key) {
      const now = clock();
      let entry = hits.get(key);
      if (!entry || entry.resetAt <= now) {
        hits.delete(key);
        if (hits.size >= maxKeys) makeRoom(now);
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
    has(key) {
      return hits.has(key);
    },
    size() {
      return hits.size;
    },
  };
}

/** Counts events in a rolling window of fixed buckets; `tripped()` once `threshold` is reached. */
export function createBrake({ threshold, windowMs, clock = Date.now, buckets = 10 }) {
  const size = windowMs / buckets;
  let counts = new Map(); // bucket index -> count

  function current(now) {
    const oldest = Math.floor((now - windowMs) / size);
    for (const b of counts.keys()) if (b <= oldest) counts.delete(b);
    let total = 0;
    for (const c of counts.values()) total += c;
    return total;
  }

  return {
    record() {
      const b = Math.floor(clock() / size);
      counts.set(b, (counts.get(b) ?? 0) + 1);
    },
    tripped() {
      return current(clock()) >= threshold;
    },
    reset() {
      counts = new Map();
    },
  };
}
