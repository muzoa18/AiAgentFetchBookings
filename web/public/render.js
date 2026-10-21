// Pure DOM building for the dashboard. Every dynamic value goes in through
// createTextNode / textContent / setAttribute, never through an HTML parser.
// Functions take the document as their first argument so tests can pass a fake.

export const STATUS = {
  attempted: { label: 'Förbereds', tone: 'neutral', mark: '○' },
  pending: { label: 'Väntar på godkännande', tone: 'wait', mark: '◷' },
  ordered: { label: 'Beställd', tone: 'ok', mark: '✓' },
  blocked: { label: 'Stoppad', tone: 'alert', mark: '!' },
  failed: { label: 'Misslyckad', tone: 'alert', mark: '✕' },
  expired: { label: 'Utgången', tone: 'alert', mark: '⊘' },
  cancelled: { label: 'Avbruten', tone: 'neutral', mark: '–' },
  unknown: { label: 'Okänd status', tone: 'neutral', mark: '?' },
};

const AGENTS = {
  booking_agent: 'Bokningskontroll',
  approve_agent: 'Godkännande',
  email_agent: 'E-post (gammal)',
};

const COUNTER_LABELS = {
  bookings_found: ['bokning', 'bokningar'],
  notified: ['avisering', 'aviseringar'],
  orders_staged: ['förberedd order', 'förberedda ordrar'],
  orders_failed: ['misslyckad order', 'misslyckade ordrar'],
  notify_failed: ['misslyckad avisering', 'misslyckade aviseringar'],
  handled_failed: ['misslyckad Hanterad', 'misslyckade Hanterad'],
};

/** h(doc, 'tag', { class, text, attrs... }, ...children) */
export function h(doc, tag, props = {}, ...children) {
  const el = doc.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = String(value);
    else if (key === 'hidden') el.hidden = true;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  append(doc, el, children);
  return el;
}

function append(doc, el, children) {
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(typeof child === 'string' || typeof child === 'number' ? doc.createTextNode(String(child)) : child);
  }
}

/** Only absolute https links are ever rendered as links. */
export function safeHref(url) {
  if (typeof url !== 'string') return null;
  try {
    const u = new URL(url);
    return u.protocol === 'https:' ? u.href : null;
  } catch {
    return null;
  }
}

function link(doc, url, text, cls = 'link') {
  const href = safeHref(url);
  if (!href) return null;
  return h(doc, 'a', { class: cls, href, target: '_blank', rel: 'noopener noreferrer' }, text);
}

export function plural(n, [one, many]) {
  return `${n} ${n === 1 ? one : many}`;
}

const rtf = new Intl.RelativeTimeFormat('sv', { numeric: 'auto' });

export function timeAgo(iso, now = Date.now()) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return 'okänd tid';
  const s = Math.round((t - now) / 1000);
  const abs = Math.abs(s);
  if (abs < 45) return 'just nu';
  if (abs < 3600) return rtf.format(Math.round(s / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(s / 3600), 'hour');
  return rtf.format(Math.round(s / 86400), 'day');
}

const dateFmt = new Intl.DateTimeFormat('sv-SE', {
  dateStyle: 'medium', timeStyle: 'short', timeZone: 'Europe/Stockholm',
});

export function formatDate(iso) {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? dateFmt.format(t) : '';
}

const money = new Intl.NumberFormat('sv-SE', { style: 'currency', currency: 'SEK', maximumFractionDigits: 2 });

export function sek(value) {
  return typeof value === 'number' && Number.isFinite(value) ? money.format(value) : null;
}

function time(doc, iso, now) {
  return h(doc, 'time', { datetime: iso, title: formatDate(iso) }, timeAgo(iso, now));
}

/** Swedish registration plate. */
export function plate(doc, reg) {
  const text = typeof reg === 'string' && reg ? reg.replace(/^([A-ZÅÄÖ]{3})(\w{3})$/i, '$1 $2').toUpperCase() : '—';
  return h(doc, 'span', { class: 'plate', 'aria-label': `Registreringsnummer ${text}` },
    h(doc, 'span', { class: 'plate-eu', 'aria-hidden': 'true' }, 'S'),
    h(doc, 'span', { class: 'plate-text' }, text));
}

export function badge(doc, status) {
  const s = STATUS[status] ?? STATUS.unknown;
  return h(doc, 'span', { class: `badge tone-${s.tone}` },
    h(doc, 'span', { class: 'badge-mark', 'aria-hidden': 'true' }, s.mark),
    s.label);
}

export function headline(vm) {
  const t = vm.tiles;
  if (t.needsAction > 0) return `${plural(t.needsAction, ['bokning behöver', 'bokningar behöver'])} din åtgärd`;
  if (t.pending > 0) return `${plural(t.pending, ['beställning väntar', 'beställningar väntar'])} på ditt godkännande i Telegram`;
  if (t.waitingKlar > 0) return `${plural(t.waitingKlar, ['bokning väntar', 'bokningar väntar'])} på Klar i Telegram`;
  return 'Inget väntar på dig just nu';
}

export function tiles(doc, vm) {
  const items = [
    ['pending', 'Väntar på godkännande', 'wait', 'Godkänn eller avbryt i Telegram.'],
    ['orderedVerified', 'Beställda', 'ok', 'Finns i Skickade beställningar.'],
    ['orderedUnverified', 'Beställda – väntar på verifiering', 'info', 'Agenten kontrollerar orderlistan.'],
    ['needsAction', 'Behöver åtgärd', 'alert', 'Stoppade, misslyckade eller utgångna.'],
    ['waitingKlar', 'Väntar på Klar', 'neutral', 'Tryck Klar i Telegram när bilen är klar.'],
  ];
  return h(doc, 'ul', { class: 'tiles', role: 'list' }, items.map(([key, label, tone, hint]) => {
    const n = Number(vm.tiles[key]) || 0;
    return h(doc, 'li', { class: `tile tone-${tone}${n === 0 ? ' is-zero' : ''}` },
      h(doc, 'span', { class: 'tile-n' }, String(n)),
      h(doc, 'span', { class: 'tile-label' }, label),
      h(doc, 'span', { class: 'tile-hint' }, hint));
  }));
}

function runCounts(run) {
  const parts = [];
  for (const [key, forms] of Object.entries(COUNTER_LABELS)) {
    const n = run.counts?.[key];
    if (typeof n === 'number' && (n > 0 || key === 'bookings_found')) parts.push(plural(n, forms));
  }
  return parts.join(', ');
}

function agentName(agent) {
  return AGENTS[agent] ?? agent ?? 'Okänd körning';
}

export function lastRun(doc, vm, now) {
  const run = vm.lastRun;
  if (!run) {
    return h(doc, 'p', { class: 'muted' }, 'Inga körningar loggade ännu.');
  }
  const failText = run.loginFailed
    ? 'Inloggningen i portalen misslyckades'
    : run.failed ? `${plural(run.failures, ['fel', 'fel'])} i körningen` : 'Inga fel';
  return h(doc, 'div', { class: 'lastrun' },
    h(doc, 'p', { class: 'lastrun-when' },
      h(doc, 'strong', {}, agentName(run.agent)), ' ', time(doc, run.timestamp, now)),
    h(doc, 'p', { class: `lastrun-state ${run.failed ? 'is-failed' : 'is-ok'}` },
      h(doc, 'span', { class: 'state-mark', 'aria-hidden': 'true' }, run.failed ? '✕' : '✓'), failText),
    runCounts(run) ? h(doc, 'p', { class: 'muted small' }, runCounts(run)) : null);
}

export function history(doc, vm, now) {
  const runs = vm.history ?? [];
  if (!runs.length) return h(doc, 'p', { class: 'muted' }, 'Ingen historik.');
  const failedCount = runs.filter((r) => r.failed).length;
  const strip = h(doc, 'ol', { class: 'strip', 'aria-hidden': 'true' }, runs.map((r) =>
    h(doc, 'li', { class: `strip-run ${r.failed ? 'is-failed' : 'is-ok'}`, title: `${agentName(r.agent)}, ${formatDate(r.timestamp)}${r.failed ? ' – fel' : ''}` })));
  const recent = runs.slice(-8).reverse();
  return h(doc, 'div', { class: 'history' },
    h(doc, 'p', { class: 'history-summary' },
      runs.length === 1 ? 'Senaste körningen: ' : `Senaste ${runs.length} körningarna: `,
      h(doc, 'strong', { class: failedCount ? 'text-alert' : '' }, failedCount ? `${failedCount} med fel` : 'alla utan fel')),
    strip,
    h(doc, 'p', { class: 'strip-axis muted small', 'aria-hidden': 'true' },
      h(doc, 'span', {}, 'äldst'), h(doc, 'span', {}, 'senast')),
    h(doc, 'ul', { class: 'runs', role: 'list' }, recent.map((r) =>
      h(doc, 'li', { class: `run ${r.failed ? 'is-failed' : ''}` },
        h(doc, 'span', { class: 'run-mark', 'aria-hidden': 'true' }, r.failed ? '✕' : '✓'),
        h(doc, 'span', { class: 'run-main' },
          h(doc, 'span', { class: 'run-agent' }, agentName(r.agent), ' ',
            h(doc, 'span', { class: 'run-when' }, time(doc, r.timestamp, now))),
          h(doc, 'span', { class: 'run-detail muted' },
            r.loginFailed ? 'inloggning misslyckades' : r.failed ? plural(r.failures, ['fel', 'fel']) : (runCounts(r) || 'ok')))))));
}

function verification(doc, o) {
  const v = o.verification ?? {};
  if (v.state === 'verified') {
    const nums = (v.numbers ?? []).join(', ');
    return h(doc, 'p', { class: 'verify is-verified' },
      h(doc, 'span', { class: 'state-mark', 'aria-hidden': 'true' }, '✓'),
      nums ? `Verifierad i Skickade beställningar, order ${nums}` : 'Verifierad i Skickade beställningar');
  }
  if (v.state === 'unverified') {
    return h(doc, 'p', { class: 'verify is-pending' },
      h(doc, 'span', { class: 'state-mark', 'aria-hidden': 'true' }, '◷'),
      'Väntar på verifiering i Skickade beställningar');
  }
  return null;
}

function partsList(doc, o) {
  const lines = o.lines ?? [];
  const fluids = o.fluids ?? [];
  if (!lines.length && !fluids.length) return null;
  const summary = [lines.length ? plural(lines.length, ['reservdel', 'reservdelar']) : null,
    fluids.length ? plural(fluids.length, ['vätska', 'vätskor']) : null].filter(Boolean).join(' och ');
  return h(doc, 'details', { class: 'parts' },
    h(doc, 'summary', {}, `Visa ${summary}`),
    lines.length ? h(doc, 'ul', { class: 'lines', role: 'list' }, lines.map((l) =>
      h(doc, 'li', { class: 'line' },
        h(doc, 'span', { class: 'line-title' }, l.title),
        h(doc, 'span', { class: 'line-qty' }, `× ${l.qty}`),
        h(doc, 'span', { class: 'line-price' }, sek(l.price) ?? '')))) : null,
    fluids.length ? h(doc, 'div', { class: 'fluids' },
      h(doc, 'p', { class: 'fluids-head' }, 'Vätskor att ta fram'),
      h(doc, 'ul', { class: 'lines', role: 'list' }, fluids.map((f) =>
        h(doc, 'li', { class: 'line' },
          h(doc, 'span', { class: 'line-title' }, f.name),
          h(doc, 'span', { class: 'line-qty' }, f.qty ?? ''))))) : null);
}

export function orderCard(doc, o, vm, now) {
  const tone = (STATUS[o.status] ?? STATUS.unknown).tone;
  const title = o.bookingNr ? `Bokning ${o.bookingNr}` : 'Bokning';
  const reason = o.reason ?? (o.status === 'blocked' ? o.lastReason : null);
  return h(doc, 'li', { class: `order tone-${tone}` },
    h(doc, 'article', { class: 'order-body', 'aria-label': `${title}, ${(STATUS[o.status] ?? STATUS.unknown).label}` },
      h(doc, 'div', { class: 'order-top' },
        plate(doc, o.regNr),
        h(doc, 'div', { class: 'order-id' },
          h(doc, 'h3', { class: 'order-title' }, title),
          o.vehicleModel ? h(doc, 'p', { class: 'order-model' }, o.vehicleModel) : null),
        h(doc, 'div', { class: 'order-total' }, sek(o.total) ?? '')),
      h(doc, 'div', { class: 'order-meta' },
        badge(doc, o.status),
        o.waitingKlar ? h(doc, 'span', { class: 'chip' }, 'Väntar på Klar') : null,
        o.handled ? h(doc, 'span', { class: 'chip' }, 'Hanterad') : null,
        o.updatedAt ? h(doc, 'span', { class: 'muted small' }, 'Uppdaterad ', time(doc, o.updatedAt, now)) : null),
      verification(doc, o),
      reason ? h(doc, 'p', { class: 'reason' },
        h(doc, 'strong', {}, o.status === 'blocked' ? 'Orsak: ' : 'Anledning: '), reason,
        o.tries ? h(doc, 'span', { class: 'muted' }, ` (försök ${o.tries})`) : null) : null,
      partsList(doc, o),
      h(doc, 'p', { class: 'order-links' },
        link(doc, o.detailUrl, 'Bokningen i portalen'),
        (o.status === 'ordered' || o.status === 'pending') ? link(doc, vm.links?.orderOverview, 'Orderöversikt på pro.meca.se') : null)));
}

export function orders(doc, vm, now) {
  if (!vm.orders?.length) {
    return h(doc, 'p', { class: 'empty' }, 'Inga delbeställningar ännu. Nya bokningar med reservdelar dyker upp här.');
  }
  return h(doc, 'ul', { class: 'orders', role: 'list' }, vm.orders.map((o) => orderCard(doc, o, vm, now)));
}

export function freshness(doc, vm, now) {
  const f = vm.freshness ?? {};
  const line = f.sha
    ? h(doc, 'span', {}, 'Data från repot, senaste commit ', h(doc, 'code', {}, f.sha), ' ', time(doc, f.committedAt, now))
    : h(doc, 'span', {}, 'Data från repot, ingen commit hittades');
  return h(doc, 'div', { class: `freshness ${f.stale ? 'is-stale' : ''}` },
    line,
    f.stale ? h(doc, 'span', { class: 'stale-warn', role: 'note' },
      h(doc, 'span', { class: 'state-mark', 'aria-hidden': 'true' }, '!'),
      'Över 36 timmar gammal. Körs agenten lokalt? Pusha statefilerna för att se dem här.') : null);
}
