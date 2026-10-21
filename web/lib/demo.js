// Invented demo data for `npm run demo`. Nothing here comes from the real state
// files. Registration numbers use the letter Q, which Swedish plates never carry.
const HOUR = 3_600_000;

function iso(ms) {
  return new Date(ms).toISOString();
}

function ordersFile(now) {
  const t = (h) => iso(now - h * HOUR);
  const bookings = {
    'DEMO-A1': {
      status: 'pending', message_id: 1, updated_at: t(3), cart_touched: true,
      detail_url: 'https://meca.promeisterportal.com/demo/booking/DEMO-A1', vehicle_model: 'Volvo V60 D4 2017',
      plan: {
        booking_id: 'DEMO-A1', booking_nr: '880142', reg_nr: 'QXA112', total_sek: 1486.0,
        expected_lines: [
          { title: 'Oljefilter Mann HU 816 x', qty: 1, price_sek: 189.0 },
          { title: 'Luftfilter Mann C 27 125', qty: 1, price_sek: 312.0 },
          { title: 'Kupéfilter med kol', qty: 1, price_sek: 285.0 },
          { title: 'Torkarblad fram, sats', qty: 1, price_sek: 700.0 },
        ],
        fluids: [{ name: 'Motorolja 0W-20', qty: '5,2 l' }],
      },
    },
    'DEMO-A2': {
      status: 'pending', message_id: 2, updated_at: t(5), cart_touched: true,
      detail_url: 'https://meca.promeisterportal.com/demo/booking/DEMO-A2',
      plan: {
        booking_id: 'DEMO-A2', booking_nr: '880151', reg_nr: 'QLM48K', total_sek: 2390.5,
        expected_lines: [
          { title: 'Bromsklossar fram ATE', qty: 1, price_sek: 845.5 },
          { title: 'Bromsskiva fram ventilerad', qty: 2, price_sek: 772.5 },
        ],
        fluids: [{ name: 'Bromsvätska DOT 4', qty: '1 l' }],
      },
    },
    'DEMO-B1': {
      status: 'ordered', message_id: 3, updated_at: t(20), cart_touched: true,
      detail_url: 'https://meca.promeisterportal.com/demo/booking/DEMO-B1', vehicle_model: 'Volkswagen Golf 1.4 TSI 2015',
      verified_at: t(14), verified_numbers: ['4471093'],
      klar_message_id: 31, klar_sent_at: t(14),
      plan: {
        booking_id: 'DEMO-B1', booking_nr: '880097', reg_nr: 'QRT305', total_sek: 1032.0, confirmation: '4471093',
        expected_lines: [
          { title: 'Tändstift NGK, sats om 4', qty: 1, price_sek: 512.0 },
          { title: 'Oljefilter Bosch', qty: 1, price_sek: 145.0 },
          { title: 'Drivrem 6PK1050', qty: 1, price_sek: 375.0 },
        ],
        fluids: [{ name: 'Motorolja 5W-30', qty: '4 l' }, { name: 'Spolarvätska', qty: '2 l' }],
      },
    },
    'DEMO-B2': {
      status: 'ordered', message_id: 4, updated_at: t(30), cart_touched: true, handled: true,
      detail_url: 'https://meca.promeisterportal.com/demo/booking/DEMO-B2', vehicle_model: 'Toyota Corolla Hybrid 2020',
      verified_at: t(26), verified_numbers: ['4470811', '4470812'],
      klar_message_id: 32, klar_sent_at: t(26),
      plan: {
        booking_id: 'DEMO-B2', booking_nr: '880064', reg_nr: 'QPD71C', total_sek: 2210.0, confirmation: '4470811',
        expected_lines: [
          { title: 'Stötdämpare fram', qty: 2, price_sek: 1105.0 },
        ],
        fluids: [],
      },
    },
    'DEMO-C1': {
      status: 'ordered', message_id: 5, updated_at: t(1.2), cart_touched: true,
      detail_url: 'https://meca.promeisterportal.com/demo/booking/DEMO-C1', vehicle_model: 'Škoda Octavia Combi 2.0 TDI 2019',
      plan: {
        booking_id: 'DEMO-C1', booking_nr: '880160', reg_nr: 'QHN920', total_sek: 668.0, confirmation: 'väntar',
        expected_lines: [
          { title: 'Bränslefilter', qty: 1, price_sek: 428.0 },
          { title: 'Oljeplugg med packning', qty: 1, price_sek: 40.0 },
          { title: 'Pollenfilter', qty: 1, price_sek: 200.0 },
        ],
        fluids: [{ name: 'Motorolja 5W-30 VW 507.00', qty: '4,7 l' }],
      },
    },
    'DEMO-D1': {
      status: 'blocked', message_id: 6, updated_at: t(9), cart_touched: false, tries: 3,
      last_reason: 'ingen säker träff för "Bromsskiva bak"',
      detail_url: 'https://meca.promeisterportal.com/demo/booking/DEMO-D1', vehicle_model: 'Ford Focus 1.0 EcoBoost 2016',
      plan: {
        booking_id: 'DEMO-D1', booking_nr: '880133', reg_nr: 'QBF66T',
        reason: 'ingen säker träff för "Bromsskiva bak"',
        fluids: [{ name: 'Bromsvätska DOT 4', qty: '1 l' }],
      },
    },
    'DEMO-E1': {
      status: 'failed', message_id: 7, updated_at: t(28), cart_touched: true,
      detail_url: 'https://meca.promeisterportal.com/demo/booking/DEMO-E1',
      plan: {
        booking_id: 'DEMO-E1', booking_nr: '880120', reg_nr: 'QKS402',
        reason: 'pro.meca.se svarade inte vid beställning',
        expected_lines: [{ title: 'Kamremssats med vattenpump', qty: 1, price_sek: 2890.0 }],
        total_sek: 2890.0, fluids: [{ name: 'Kylarvätska G12', qty: '3 l' }],
      },
    },
    'DEMO-F1': {
      status: 'expired', message_id: 8, updated_at: t(70),
      plan: {
        booking_id: 'DEMO-F1', booking_nr: '880011', reg_nr: 'QZE13P', total_sek: 455.0,
        reason: 'godkännandet gick ut efter 48 h',
        expected_lines: [{ title: 'Glödlampa H7, par', qty: 1, price_sek: 155.0 }, { title: 'Torkarblad bak', qty: 1, price_sek: 300.0 }],
        fluids: [],
      },
    },
    'DEMO-G1': {
      status: 'cancelled', message_id: 9, updated_at: t(46),
      plan: {
        booking_id: 'DEMO-G1', booking_nr: '880034', reg_nr: 'QWA580', total_sek: 980.0,
        expected_lines: [{ title: 'Batteri 70 Ah', qty: 1, price_sek: 980.0 }],
        fluids: [],
      },
    },
    'DEMO-H1': {
      status: 'attempted', message_id: null, updated_at: t(0.3),
      plan: { booking_id: 'DEMO-H1', booking_nr: '880171', reg_nr: 'QNU24D', queued: true },
    },
  };
  return JSON.stringify({ last_update_id: 1000, bookings }, null, 2);
}

function historyFile(now) {
  const lines = [];
  for (let i = 34; i >= 0; i--) {
    const at = now - i * 12 * HOUR - 0.6 * HOUR;
    const bookings = i % 2 === 0;
    const entry = bookings
      ? { agent: 'booking_agent', bookings_found: (i * 7) % 4, notified: (i * 7) % 4, notify_failed: 0,
        orders_staged: (i * 3) % 3, orders_failed: 0, handled_failed: 0, login_failed: false }
      : { agent: 'approve_agent', approved: i % 3, ordered: i % 3, orders_failed: 0 };
    if (i === 6) Object.assign(entry, { login_failed: true, bookings_found: 0, notified: 0, orders_staged: 0 });
    if (i === 13) Object.assign(entry, { orders_failed: 1 });
    entry.timestamp = new Date(at).toISOString().replace('Z', '');
    lines.push(JSON.stringify({ timestamp: entry.timestamp, ...entry }));
  }
  lines.splice(10, 0, '{"timestamp": "trasig rad');
  return lines.join('\n') + '\n';
}

function seenFile() {
  return Array.from({ length: 214 }, (_, i) => `DEMO-${1000 + i}`).join('\n') + '\n';
}

/** A data source with the same shape as the GitHub client, serving demo data. */
export function createDemoSource(now = Date.now) {
  return {
    async getFile(path) {
      const t = now();
      if (path === 'config/orders.json') return ordersFile(t);
      if (path === 'logs/run_history.jsonl') return historyFile(t);
      if (path === 'config/seen_ids.txt') return seenFile();
      return null;
    },
    async getLatestCommit() {
      return { sha: 'de3a0c0ffee123456', date: iso(now() - 0.5 * HOUR) };
    },
    async dispatch() {},
    actionsUrl() {
      return 'https://github.com/';
    },
  };
}
