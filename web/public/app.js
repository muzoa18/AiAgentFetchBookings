// Dashboard controller: fetches /api/status, swaps views, wires buttons.
// All rendering goes through render.js (DOM APIs only, no HTML strings).
import * as R from './render.js';
import { unauthorizedMessage } from './session.js';
import { send } from './transport.js';

const REFRESH_MS = 60_000;
const RUN_LOCK_MS = 30_000;
const $ = (id) => document.getElementById(id);

const RUNS = {
  approve: {
    title: 'Kör godkännande nu?',
    text: 'Agenten läser dina Godkänn-, Avbryt- och Klar-tryck i Telegram och lägger godkända beställningar på pro.meca.se.',
  },
  bookings: {
    title: 'Kör bokningskontroll nu?',
    text: 'Agenten hämtar nya bokningar från portalen, skickar aviseringar och förbereder delbeställningar.',
  },
};

let lastLoad = 0;
let loading = false;
let timer = null;

function api(path, options = {}) {
  const init = { credentials: 'same-origin', cache: 'no-store', ...options };
  if (options.body !== undefined) {
    init.method = 'POST';
    init.headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'dashboard' };
    init.body = JSON.stringify(options.body);
  }
  return send(path, init);
}

function replace(id, node) {
  $(id).replaceChildren(node);
}

function show(view) {
  $('boot').hidden = true;
  $('login-view').hidden = view !== 'login';
  $('dash-view').hidden = view !== 'dash';
  $('bar-actions').hidden = view !== 'dash';
}

function showLoadError(message) {
  const el = $('load-error');
  el.textContent = message ?? '';
  el.hidden = !message;
}

function render(vm) {
  const now = Date.now();
  replace('freshness', R.freshness(document, vm, now));
  $('headline').textContent = R.headline(vm);
  replace('tiles', R.tiles(document, vm));
  replace('orders', R.orders(document, vm, now));
  $('orders-count').textContent = vm.totalOrders > vm.orders.length
    ? `${vm.orders.length} av ${vm.totalOrders}` : R.plural(vm.orders.length, ['bokning', 'bokningar']);
  replace('lastrun', R.lastRun(document, vm, now));
  replace('history', R.history(document, vm, now));
  const foot = [`${vm.seenCount} bokningar sedda totalt`];
  if (vm.demo) foot.push('Demoläge med påhittade data');
  if (vm.problems?.includes('orders_unreadable')) foot.push('orders.json kunde inte läsas');
  $('foot').textContent = foot.join('. ') + '.';
  document.body.classList.toggle('is-demo', Boolean(vm.demo));
}

async function load({ announce = false, justLoggedIn = false } = {}) {
  if (loading) return;
  loading = true;
  $('refresh').disabled = true;
  try {
    const stale = new URLSearchParams(location.search).get('stale') === '1' ? '?stale=1' : '';
    const { status, ok, data } = await api('/api/status' + stale);
    if (status === 401) {
      $('demo-note').hidden = !data.demo;
      show('login');
      showLoadError(null);
      // Right after a successful login a 401 means the cookie was not kept: say so, never loop silently.
      $('login-error').textContent = unauthorizedMessage({ justLoggedIn, location }) ?? '';
      $('password').focus();
      return;
    }
    if (!ok) {
      showLoadError(data.error || 'Kunde inte hämta status. Försök igen.');
      return;
    }
    showLoadError(null);
    lastLoad = Date.now();
    render(data);
    show('dash');
    if (announce) {
      $('announce').textContent = `Uppdaterad ${new Date().toLocaleTimeString('sv-SE', { hour: '2-digit', minute: '2-digit' })}`;
    }
  } catch {
    showLoadError('Ingen kontakt med servern. Kontrollera nätverket och försök igen.');
  } finally {
    loading = false;
    $('refresh').disabled = false;
  }
}

function schedule() {
  clearInterval(timer);
  timer = setInterval(() => {
    if (document.visibilityState === 'visible' && !$('dash-view').hidden) load({ announce: true });
  }, REFRESH_MS);
}

async function onLogin(event) {
  event.preventDefault();
  const input = $('password');
  const error = $('login-error');
  const button = $('login-submit');
  error.textContent = '';
  if (!input.value) {
    error.textContent = 'Skriv lösenordet.';
    input.focus();
    return;
  }
  button.disabled = true;
  try {
    const { ok, data } = await api('/api/login', { body: { password: input.value } });
    if (!ok) {
      error.textContent = data.error || 'Inloggningen misslyckades';
      input.select();
      return;
    }
    input.value = '';
    await load({ justLoggedIn: true });
    $('main').focus();
  } catch {
    error.textContent = 'Ingen kontakt med servern.';
  } finally {
    button.disabled = false;
  }
}

async function onLogout() {
  try {
    await api('/api/logout', { body: {} });
  } finally {
    show('login');
    $('password').focus();
  }
}

function confirmRun(workflow) {
  const dialog = $('confirm');
  $('confirm-title').textContent = RUNS[workflow].title;
  $('confirm-text').textContent = RUNS[workflow].text;
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
    dialog.returnValue = '';
    dialog.showModal();
    $('confirm-ok').focus();
  });
}

function lockButton(button) {
  const label = button.textContent;
  button.disabled = true;
  let left = Math.round(RUN_LOCK_MS / 1000);
  button.textContent = `${label} (${left} s)`;
  const t = setInterval(() => {
    left -= 1;
    if (left <= 0) {
      clearInterval(t);
      button.disabled = false;
      button.textContent = label;
    } else {
      button.textContent = `${label} (${left} s)`;
    }
  }, 1000);
}

async function onRun(event) {
  const button = event.currentTarget;
  const workflow = button.dataset.workflow;
  if (!Object.hasOwn(RUNS, workflow) || button.disabled) return;
  if (!(await confirmRun(workflow))) return;
  const status = $('run-status');
  status.replaceChildren('Startar…');
  status.className = 'run-status';
  lockButton(button);
  try {
    const { ok, status: code, data } = await api('/api/run', { body: { workflow } });
    if (code === 401) {
      show('login');
      return;
    }
    if (!ok) {
      status.className = 'run-status is-failed';
      status.replaceChildren(data.error || 'Kunde inte starta körningen.');
      return;
    }
    status.className = 'run-status is-ok';
    if (data.preview) {
      status.replaceChildren('Startad (demo). Förhandsvisningen startar ingenting på GitHub.');
      return;
    }
    const href = R.safeHref(data.actionsUrl, { allowGithub: true });
    status.replaceChildren(
      'Startad – se ',
      href ? R.h(document, 'a', { href, target: '_blank', rel: 'noopener noreferrer', class: 'link' }, 'Actions') : 'Actions',
      '. Resultatet syns här när körningen har pushat sina filer.');
  } catch {
    status.className = 'run-status is-failed';
    status.replaceChildren('Ingen kontakt med servern.');
  }
}

$('login-form').addEventListener('submit', onLogin);
$('logout').addEventListener('click', onLogout);
$('refresh').addEventListener('click', () => load({ announce: true }));
for (const b of document.querySelectorAll('[data-workflow]')) b.addEventListener('click', onRun);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && !$('dash-view').hidden && Date.now() - lastLoad > REFRESH_MS) {
    load({ announce: true });
  }
});

schedule();
load();
