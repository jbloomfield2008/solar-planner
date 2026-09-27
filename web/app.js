// solar01 local console.  No build step: Preact + htm and uPlot are vendored in ./vendor,
// so the page works with no internet and no Home Assistant.
import { html, render, useState, useEffect, useRef, useMemo } from './vendor/preact-htm.js';
import uPlot from './vendor/uPlot.esm.js';

const FONT = 'Barlow, system-ui, sans-serif';
let TZ;

// ---------- formatting ----------------------------------------------------------------------
const formats = new Map();
function dtf(opts) {
  const key = `${TZ}|${JSON.stringify(opts)}`;
  if (!formats.has(key)) formats.set(key, new Intl.DateTimeFormat('en-US', { timeZone: TZ, ...opts }));
  return formats.get(key);
}
const hhmm = (ts) => (ts == null ? '–' : dtf({ hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(ts * 1000));
const dayHhmm = (ts) => (ts == null ? '–' : dtf({ weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(ts * 1000));
function weekdayDay(parts) {
  const get = (type) => parts.find((p) => p.type === type)?.value;
  return `${get('weekday')} ${get('day')}`;
}
const dayLabel = (ts) => weekdayDay(dtf({ weekday: 'short', day: 'numeric' }).formatToParts(ts * 1000));
const localHour = (ts) => Number(dtf({ hour: 'numeric', hourCycle: 'h23' }).format(ts * 1000)) % 24;
const isoTs = (v) => (v ? Date.parse(v) / 1000 : null);

function num(v, digits = 0) {
  if (v == null || Number.isNaN(Number(v))) return '–';
  return Number(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function money(v) {
  if (v == null || Number.isNaN(Number(v))) return '–';
  return `${v < 0 ? '−' : ''}$${num(Math.abs(v), 2)}`;
}
function kwh(v, digits = 1) {
  return v == null ? '–' : `${num(v, digits)} kWh`;
}
const dateLabel = (iso) => {
  const [yy, mm, dd] = iso.split('-').map(Number);
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' }).format(Date.UTC(yy, mm - 1, dd, 12));
};
function watts(w) {
  if (w == null) return '–';
  return Math.abs(w) >= 1000 ? `${num(w / 1000, 2)} kW` : `${num(w)} W`;
}
function ago(s) {
  if (s == null) return 'never';
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} days`;
}
function duration(s) {
  if (s == null) return '–';
  if (s >= 86400) return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function barPath(x, w, yTop, base, radius = 4) {
  const r = Math.max(0, Math.min(radius, w / 2, base - yTop));
  return `M${x},${base}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + w - r}Q${x + w},${yTop} ${x + w},${yTop + r}V${base}Z`;
}
function niceStep(max, count = 4) {
  const raw = max / count;
  const p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw);
}

// ---------- hooks ----------------------------------------------------------------------------
function useWidth() {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return undefined;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

function useTheme() {
  const query = window.matchMedia('(prefers-color-scheme: dark)');
  const [dark, setDark] = useState(query.matches);
  useEffect(() => {
    const onChange = (e) => setDark(e.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);
  return dark ? 'dark' : 'light';
}

function usePoll(url, ms) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    let timer;
    const load = () => {
      setLoading(true);
      fetch(url)
        .then((r) => { if (!r.ok) throw new Error(r.statusText); return r.json(); })
        .then((d) => { if (!alive) return; setData(d); setLoading(false); timer = setTimeout(load, ms); })
        .catch(() => { if (!alive) return; setLoading(false); timer = setTimeout(load, 5000); });   // retry soon
    };
    load();
    return () => { alive = false; clearTimeout(timer); };
  }, [url, ms]);
  return [data, loading];
}

function useLive() {
  const [state, setState] = useState(null);
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState([]);
  const [lastRx, setLastRx] = useState(0);
  const [, tick] = useState(0);
  useEffect(() => {
    fetch('/api/events?limit=100').then((r) => r.json()).then((d) => setEvents(d.events || [])).catch(() => {});
    const es = new EventSource('/api/stream');
    es.addEventListener('state', (e) => { setState(JSON.parse(e.data)); setConnected(true); setLastRx(Date.now()); });
    es.addEventListener('event', (e) => setEvents((list) => [JSON.parse(e.data), ...list].slice(0, 300)));
    es.onerror = () => setConnected(false);
    const timer = setInterval(() => tick((n) => n + 1), 1000);
    return () => { es.close(); clearInterval(timer); };
  }, []);
  return { state, connected, events, lastRx };
}

// ---------- small pieces ---------------------------------------------------------------------
function Icon({ kind }) {
  switch (kind) {
    case 'ok':
      return html`<svg class="ic" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.5" /><path d="M5 8.3l2.1 2.1L11 6.2" /></svg>`;
    case 'warn':
      return html`<svg class="ic" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.9 14.6 13.6H1.4Z" /><path d="M8 6.2v3.4M8 11.4v.3" /></svg>`;
    case 'error':
      return html`<svg class="ic" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.5" /><path d="M5.7 5.7l4.6 4.6M10.3 5.7l-4.6 4.6" /></svg>`;
    case 'info':
      return html`<svg class="ic" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.5" /><path d="M8 7.2v4M8 4.9v.2" /></svg>`;
    default:
      return html`<svg class="ic" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.5" /><path d="M5.2 8h5.6" /></svg>`;
  }
}
const STATUS_WORD = { ok: 'OK', warn: 'needs attention', error: 'fault', off: 'off' };
const HEALTH = [['core', 'Core process'], ['inverter', 'Inverter link'], ['jk', 'BMS link'], ['emulator', 'BMS bridge'],
  ['ct', 'CT calibration'], ['planner', 'Planner'], ['weather', 'Forecast'], ['mqtt', 'Home Assistant'], ['clock', 'Clock'],
  ['pi', 'Pi']];

function Panel({ id, title, className = '', aside, stale, children }) {
  return html`<section class=${`panel ${className}${stale ? ' is-stale' : ''}`} aria-labelledby=${id}>
    <div class="panel-head"><h2 id=${id}>${title}</h2>${aside}</div>
    ${children}
  </section>`;
}

function Facts({ rows }) {
  return html`<dl class="facts">${rows.filter(Boolean).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>`;
}

// ---------- header ----------------------------------------------------------------------------
function Header({ s, connected, lastRx }) {
  const since = (Date.now() - lastRx) / 1000;
  const live = connected && since < 10;
  return html`<header class="top">
    <div class="ident">
      <h1>solar01</h1>
      <p>Local console for the FlexBoss21, the JK BMS bridge and the charge planner</p>
    </div>
    <ul class="health" aria-label="System health">
      ${HEALTH.map(([key, label]) => {
        const h = s.health[key] || { status: 'off', msg: '' };
        return html`<li class=${`st st-${h.status}`} title=${h.msg}><${Icon} kind=${h.status} /><span>${label}</span><span class="vh">${`: ${STATUS_WORD[h.status]}. ${h.msg}`}</span></li>`;
      })}
    </ul>
    <p class=${live ? 'rx' : 'rx rx-bad'} aria-live="polite">${live ? `Live at ${hhmm(s.ts)}` : `No update for ${ago(since)}, reconnecting`}</p>
  </header>`;
}

// ---------- headline numbers ---------------------------------------------------------------------
function Tile({ label, value, sub, action }) {
  return html`<div class="tile">
    <dt>${label}</dt>
    <dd class="tile-value">${value}</dd>
    ${sub || action ? html`<dd class="tile-sub">${sub}${action}</dd>` : null}
  </div>`;
}

function Headline({ s, summary, onSettings }) {
  const inv = s.inverter.data || {};
  const socNow = s.jk?.data?.soc ?? inv.soc;
  const t = summary?.today || {};
  const c = summary?.cycle;
  const prior = summary?.soc?.prior;
  const delta = socNow != null && prior != null ? socNow - prior : null;
  const batt = inv.battery_power ?? 0;
  const pct = t.pv_forecast_kwh ? Math.round((t.pv_kwh / t.pv_forecast_kwh) * 100) : null;
  const setRates = html`<button class="link" onClick=${onSettings}>Set your rates</button>`;
  let cycleSub = '–';
  let costSub = null;
  let savedSub = null;
  if (c) {
    const tier = c.tier != null && (c.by_tier || []).length > 1 ? `, tier ${c.tier + 1}` : '';
    cycleSub = `Day ${c.day} of ${c.days} since ${dateLabel(c.start)}${tier}`;
    if (c.configured) {
      costSub = c.projected_cost != null && c.day >= 3 ? `About ${money(c.projected_cost)} by ${dateLabel(c.end)}` : `Average ${c.avg_rate != null ? `${money(c.avg_rate)}/kWh` : '–'}`;
      savedSub = `${kwh(c.saved_kwh, c.saved_kwh < 100 ? 1 : 0)} not bought, ${money(c.home_cost)} without solar and battery`;
    }
    if (c.partial && c.grid_since) {
      const since = `Grid data from ${dayLabel(c.grid_since)}`;
      cycleSub = `${cycleSub}. ${since}`;
    }
  }
  const signed = (v) => (v == null ? '–' : `${v > 0.05 ? '▲ ' : v < -0.05 ? '▼ ' : ''}${num(Math.abs(v))} pts`);
  return html`<section class="headline" aria-labelledby="headline-title">
    <div class="headline-head">
      <h2 id="headline-title" class="vh">Today and this billing cycle</h2>
      <button class="link" onClick=${onSettings}>Tariff and billing cycle</button>
    </div>
    <dl class="tiles">
      <${Tile} label="Solar forecast today" value=${kwh(t.pv_forecast_kwh)} sub=${`Tomorrow ${kwh(t.pv_forecast_tomorrow_kwh)}`} />
      <${Tile} label="Solar so far today" value=${kwh(t.pv_kwh)} sub=${`${pct != null ? `${pct} % of forecast, ` : ''}${watts(inv.pv_power)} now`} />
      <${Tile} label="Home use today" value=${kwh(t.load_kwh)} sub=${`${watts(inv.load_power)} now`} />
      <${Tile} label="Battery" value=${socNow == null ? '–' : `${num(socNow)} %`}
        sub=${batt >= 20 ? `Charging ${watts(batt)}` : batt <= -20 ? `Discharging ${watts(-batt)}` : 'Idle'} />
      <${Tile} label="Battery vs 24 h ago" value=${signed(delta)}
        sub=${prior != null ? `${num(prior)} % at ${hhmm(summary.soc.prior_ts)} yesterday` : 'No reading from 24 hours ago'} />
      <${Tile} label="Grid import today" value=${kwh(t.grid_kwh)} sub=${t.export_kwh ? `${kwh(t.export_kwh)} exported` : 'Nothing exported'} />
      <${Tile} label="Grid import this cycle" value=${c ? kwh(c.grid_kwh, c.grid_kwh < 100 ? 1 : 0) : '–'} sub=${cycleSub} />
      <${Tile} label="Cost this cycle" value=${c?.configured ? money(c.cost) : '–'} sub=${costSub} action=${c && !c.configured ? setRates : null} />
      <${Tile} label="Saved this cycle" value=${c?.configured ? money(c.saved) : c ? kwh(c.saved_kwh, 0) : '–'}
        sub=${c?.configured ? savedSub : c ? 'Home use minus grid import' : null} />
    </dl>
  </section>`;
}

const PERIOD_FIELDS = [['super_off_peak', 'Super off-peak'], ['off_peak', 'Off-peak'], ['on_peak', 'On-peak']];

function TariffDialog({ open, onClose, onSaved }) {
  const ref = useRef(null);
  const [form, setForm] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      setError('');
      setForm(null);
      fetch('/api/tariff').then((r) => r.json()).then((t) => setForm({
        day: String(t.cycle_start_day),
        rates: Object.fromEntries(PERIOD_FIELDS.map(([k]) => [k, String(t.rates[k] ?? 0)])),
        tiers: t.tiers.map((x) => ({ up: x.up_to_kwh == null ? null : String(x.up_to_kwh), adder: String(x.adder) })),
      })).catch((err) => setError(`Could not load the settings: ${err.message}`));
      d.showModal();
    } else if (!open && d.open) {
      d.close();
    }
  }, [open]);
  const set = (patch) => setForm((f) => ({ ...f, ...patch }));
  const setTier = (i, patch) => set({ tiers: form.tiers.map((t, k) => (k === i ? { ...t, ...patch } : t)) });
  const addTier = () => {
    if (!form.tiers.length) set({ tiers: [{ up: '', adder: '' }, { up: null, adder: '0' }] });
    else set({ tiers: [...form.tiers.slice(0, -1), { up: '', adder: '' }, form.tiers[form.tiers.length - 1]] });
  };
  const removeTier = (i) => {
    const rest = form.tiers.filter((_, k) => k !== i);
    if (rest.length === 1) return set({ tiers: [] });
    rest[rest.length - 1] = { ...rest[rest.length - 1], up: null };
    return set({ tiers: rest });
  };
  async function save(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    const body = {
      cycle_start_day: Number(form.day),
      rates: Object.fromEntries(PERIOD_FIELDS.map(([k]) => [k, Number(form.rates[k] || 0)])),
      tiers: form.tiers.map((t) => ({ up_to_kwh: t.up == null ? null : Number(t.up), adder: Number(t.adder || 0) })),
    };
    try {
      const r = await fetch('/api/tariff', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (!r.ok) throw new Error((await r.json()).error || r.statusText);
      onSaved();
      onClose();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }
  return html`<dialog class="dialog" ref=${ref} onClose=${onClose} aria-labelledby="tariff-title">
    <form method="dialog" onSubmit=${save}>
      <h2 id="tariff-title">Tariff and billing cycle</h2>
      ${!form ? html`<p class="empty">${error || 'Loading'}</p>` : html`
      <label class="field"><span>Billing cycle starts on day</span>
        <input type="number" min="1" max="31" step="1" required value=${form.day} onInput=${(e) => set({ day: e.target.value })} />
        <small>Of each month. Days past the end of a short month use its last day.</small></label>
      <fieldset>
        <legend>Rate per kWh, by time of use</legend>
        <div class="rate-grid">
          ${PERIOD_FIELDS.map(([k, label]) => html`<label class="field"><span>${label}</span>
            <span class="input-unit"><span aria-hidden="true">$</span><input type="number" min="0" max="10" step="0.00001" inputmode="decimal" required
              value=${form.rates[k]} onInput=${(e) => set({ rates: { ...form.rates, [k]: e.target.value } })} /></span></label>`)}
        </div>
        <small>Super off-peak and on-peak hours come from the planner calendar; every other hour is off-peak.</small>
      </fieldset>
      <fieldset>
        <legend>Usage tiers</legend>
        <small>Optional. An amount per kWh added to the time-of-use rate, chosen by how much you have imported so far this cycle. Use a negative amount for a baseline credit.</small>
        ${form.tiers.length ? html`<table class="tier-table">
          <thead><tr><th scope="col">Tier</th><th scope="col">Cycle import</th><th scope="col">Added per kWh</th><th><span class="vh">Remove</span></th></tr></thead>
          <tbody>${form.tiers.map((t, i) => {
            const from = i === 0 ? '0' : (form.tiers[i - 1].up || '…');
            return html`<tr>
              <td>${i + 1}</td>
              <td>${t.up == null ? html`<span class="muted">${`above ${from} kWh`}</span>` : html`<span class="input-unit">
                <input type="number" min="0" step="any" required aria-label=${`Tier ${i + 1} upper limit`} value=${t.up}
                  onInput=${(e) => setTier(i, { up: e.target.value })} /><span aria-hidden="true">kWh</span></span>`}</td>
              <td><span class="input-unit"><span aria-hidden="true">$</span><input type="number" min="-10" max="10" step="0.00001" required
                aria-label=${`Tier ${i + 1} amount per kWh`} value=${t.adder} onInput=${(e) => setTier(i, { adder: e.target.value })} /></span></td>
              <td><button type="button" class="link" onClick=${() => removeTier(i)}>Remove</button></td>
            </tr>`;
          })}</tbody>
        </table>` : null}
        ${form.tiers.length < 6 ? html`<button type="button" class="link" onClick=${addTier}>Add a tier</button>` : null}
      </fieldset>
      ${error ? html`<p class="error-text" role="alert">${error}</p>` : null}`}
      <div class="dialog-actions">
        <button type="button" class="btn" onClick=${onClose}>Cancel</button>
        <button type="submit" class="btn btn-primary" disabled=${!form || busy}>Save</button>
      </div>
    </form>
  </dialog>`;
}

// ---------- hero: state of charge across the tariff day ----------------------------------------
const PERIOD = { super_off_peak: 'Super off-peak', on_peak: 'On-peak', off_peak: 'Off-peak' };
const periodAt = (bands, t) => (bands.find((b) => t >= b.start && t < b.end) || { kind: 'off_peak' }).kind;

function decisionSentence(pl) {
  const p = pl.plan || {};
  const m = pl.manual;
  if (m) {
    const by = hhmm(m.until);
    if (m.mode === 'time') return `Manual charge from the grid until ${by}.`;
    return `Manual charge to ${m.target_soc} %${m.eta ? `, expected at ${hhmm(isoTs(m.eta))}` : ''}. It stops by ${by} at the latest.`;
  }
  if (!pl.enabled) return 'The planner is off, so the inverter follows its own settings.';
  if (p.action === 'stale') return 'No fresh inverter data, so the planner is not changing anything.';
  if (p.action === 'waiting') return 'The planner is waiting for the clock to synchronise.';
  if (p.action === 'starting' || !p.action) return 'The planner is starting.';
  if (p.action === 'on') return `Charging from the grid to ${p.target_soc} %.`;
  if (p.action === 'wait') {
    const hold = p.hold_start ? ` A standby hold runs first, from ${hhmm(isoTs(p.hold_start))}.` : '';
    return `A grid charge to ${p.target_soc} % is planned from ${hhmm(isoTs(p.start_at))}.${hold}`;
  }
  if (p.window_closing) {
    return `Super off-peak ends at ${hhmm(isoTs(p.window_end))}. Holds and charges stopped at ${hhmm(isoTs(p.actions_stop))} so the inverter is back to normal in time.`;
  }
  if (p.floor_hold) {
    return `The battery is at the ${p.floor_soc} % floor. Standby until ${hhmm(isoTs(p.hold_end))}: the grid carries the house, and nothing charges until super off-peak.`;
  }
  if (p.hold_start) {
    return p.hold
      ? `Standby hold until ${hhmm(isoTs(p.hold_end))}: the grid carries the house so the battery lasts.`
      : `A standby hold is planned from ${hhmm(isoTs(p.hold_start))} to ${hhmm(isoTs(p.hold_end))}.`;
  }
  if (!p.in_sop) {
    const next = (p.actions || []).find((a) => a.preview);
    let then = ' No hold or charge is expected in the next 24 hours.';
    if (next && next.kind === 'hold') then = ` The forecast points to a standby hold from ${dayHhmm(isoTs(next.start))} to ${hhmm(isoTs(next.end))}.`;
    if (next && next.kind === 'hold' && next.floor) then = ` The battery is expected to reach the ${p.floor_soc} % floor at ${dayHhmm(isoTs(next.start))}; the grid then carries the house until ${hhmm(isoTs(next.end))}.`;
    if (next && next.kind === 'charge') then = ` The forecast points to a grid charge to ${next.target} % from ${dayHhmm(isoTs(next.start))}.`;
    return `Nothing to do until super off-peak starts at ${dayHhmm(isoTs(p.next_window))}.${then}`;
  }
  if (p.forecast_min_soc != null && p.floor_soc != null && p.forecast_min_soc < p.reserve_soc) {
    return `No grid charge or standby hold needed. The battery dips to about ${p.forecast_min_soc} %, above the ${p.floor_soc} % floor.`;
  }
  return `No grid charge or standby hold needed. The battery stays above the ${p.reserve_soc} % reserve.`;
}

function DayStrip({ s }) {
  const pl = s.planner || {};
  const plan = pl.plan || {};
  const [hist] = usePoll('/api/history?hours=13', 60000);
  const [ref, width] = useWidth();
  const [hover, setHover] = useState(null);
  const [table, setTable] = useState(false);
  const now = s.ts;
  const socNow = s.jk?.data?.soc ?? s.inverter?.data?.soc;
  const reserve = plan.reserve_soc ?? 20;
  const floor = plan.floor_soc;
  const bands = pl.tariff || [];
  const measured = useMemo(() => {
    if (!hist?.t) return [];
    const out = [];
    hist.t.forEach((t, i) => {
      const v = hist.jk_soc[i] ?? hist.soc[i];
      if (v != null && t >= now - 12 * 3600) out.push([t, v]);
    });
    return out;
  }, [hist]);
  const projection = (pl.projection || []).filter(([t]) => t > now);
  const low = projection.length ? projection.reduce((a, b) => (b[1] < a[1] ? b : a)) : null;
  const nextHold = (plan.actions || []).find((a) => a.kind === 'hold');
  const nextCharge = (plan.actions || []).find((a) => a.kind === 'charge');
  return html`<section class="panel strip" aria-labelledby="strip-title">
    <div class="strip-head">
      <h2 id="strip-title">${width && width < 640 ? 'State of charge, last 6 hours and next 18' : 'State of charge, last 12 hours and next 24'}</h2>
      <p class="decision" aria-live="polite">${decisionSentence(pl)}</p>
    </div>
    <div class="strip-plot" ref=${ref}>
      ${width ? html`<${StripChart} width=${width} now=${now} bands=${bands} measured=${measured} projection=${projection}
        plan=${plan} reserve=${reserve} floor=${floor} socNow=${socNow} low=${low} hover=${hover} setHover=${setHover} />` : null}
    </div>
    <div class="strip-foot">
      <div class="strip-legend" aria-label="Legend">
        <span><svg class="key-line" viewBox="0 0 18 8" aria-hidden="true"><line class="soc-measured" x1="1" x2="17" y1="4" y2="4" /></svg>Measured</span>
        <span><svg class="key-line" viewBox="0 0 18 8" aria-hidden="true"><line class="soc-planned" x1="1" x2="17" y1="4" y2="4" /></svg>Planned</span>
        <span><svg class="key-block" viewBox="0 0 16 8" aria-hidden="true"><rect class="block-hold" width="16" height="8" rx="4" /></svg>Standby hold</span>
        <span><svg class="key-block" viewBox="0 0 16 8" aria-hidden="true"><rect class="block-charge" width="16" height="8" rx="4" /></svg>Grid charge</span>
        <span><svg class="key-block" viewBox="0 0 16 8" aria-hidden="true"><rect class="band-super_off_peak" width="16" height="8" stroke="var(--axis)" /></svg>Super off-peak</span>
        <span><svg class="key-block" viewBox="0 0 16 8" aria-hidden="true"><rect class="band-on_peak" width="16" height="8" /></svg>On-peak</span>
      </div>
      <button class="link" aria-pressed=${table} onClick=${() => setTable(!table)}>${table ? 'Hide table' : 'Show as table'}</button>
    </div>
    <dl class="strip-facts">
      <div><dt>Lowest in 24 hours</dt><dd>${plan.lowest_24h != null ? `${plan.lowest_24h} % at ${dayHhmm(isoTs(plan.lowest_24h_at))}` : '–'}</dd></div>
      <div><dt>At next super off-peak</dt><dd>${plan.projected_soc_next_window != null ? `${plan.projected_soc_next_window} %` : '–'}</dd></div>
      <div><dt>Lowest with no action</dt><dd>${plan.forecast_min_soc != null ? `${plan.forecast_min_soc} %` : '–'}</dd></div>
      <div><dt>Standby hold</dt><dd>${nextHold ? `${dayHhmm(isoTs(nextHold.start))} to ${hhmm(isoTs(nextHold.end))}` : 'None in 24 hours'}</dd></div>
      <div><dt>Grid charge</dt><dd>${nextCharge ? `To ${nextCharge.target} %, ${dayHhmm(isoTs(nextCharge.start))} to ${hhmm(isoTs(nextCharge.end))}` : 'None in 24 hours'}</dd></div>
      <div><dt>Tariff now</dt><dd>${PERIOD[periodAt(bands, now)]}</dd></div>
    </dl>
    ${table ? html`<${StripTable} now=${now} bands=${bands} measured=${measured} projection=${projection} />` : null}
  </section>`;
}

function interpolate(series, t) {
  for (let i = 0; i < series.length - 1; i++) {
    const [ta, va] = series[i];
    const [tb, vb] = series[i + 1];
    if (t >= ta && t <= tb) return va + ((vb - va) * (t - ta)) / (tb - ta || 1);
  }
  if (!series.length) return null;
  const near = series.reduce((a, b) => (Math.abs(b[0] - t) < Math.abs(a[0] - t) ? b : a));
  return Math.abs(near[0] - t) <= 1800 ? near[1] : null;
}

function StripChart({ width, now, bands, measured, projection, plan, reserve, floor, socNow, low, hover, setHover }) {
  const W = Math.max(width, 300);
  const H = 232;
  const L = 44;
  const R = 12;
  const TOP = 24;
  const PH = 150;
  const base = TOP + PH;
  const compact = W < 640;                     // phones: 6 h back and 18 h ahead
  const t0 = now - (compact ? 6 : 12) * 3600;
  const t1 = now + (compact ? 18 : 24) * 3600;
  const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
  const y = (v) => TOP + (1 - v / 100) * PH;
  const clamp = (t) => Math.min(t1, Math.max(t0, t));
  const path = (pts) => pts.map(([t, v], i) => `${i ? 'L' : 'M'}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join('');
  const planned = socNow != null ? [[now, socNow], ...projection.filter(([t]) => t <= t1)] : projection;
  const planEnd = planned.length > 1 ? planned[planned.length - 1] : null;
  const shown = measured.filter(([t]) => t >= t0);
  const ticks = [];
  const every = W < 560 ? 6 : 3;
  for (let t = Math.ceil(t0 / 3600) * 3600; t <= t1; t += 3600) if (localHour(t) % every === 0) ticks.push(t);
  const blocks = (plan.actions || []).map((a) => ({
    kind: a.kind, a: isoTs(a.start), b: isoTs(a.end), preview: a.preview,
    label: a.kind === 'hold' ? (W < 640 ? 'Hold' : 'Standby hold') : `${W < 640 ? 'Charge' : 'Grid charge'} to ${a.target} %`,
  })).sort((p, q) => p.a - q.a);
  // labels sit above their block, clamped inside the plot; one that would collide with the previous label is dropped
  // (the block itself stays, and the facts below the chart name every action)
  let labelRight = -Infinity;
  for (const b of blocks) {
    const w = b.label.length * 6.6;
    const a = x(Math.min(t1, Math.max(t0, b.a)));
    const lx = Math.max(L, Math.min(a, W - R - w));
    const drawn = x(Math.min(t1, Math.max(t0, b.b))) - a >= 2;
    b.labelX = drawn && lx >= labelRight + 10 ? lx : null;
    if (b.labelX != null) labelRight = lx + w;
  }

  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const t = t0 + ((px - L) / (W - L - R)) * (t1 - t0);
    setHover(px < L || px > W - R ? null : t);
  };
  const onKey = (e) => {
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      setHover((h) => clamp((h ?? now) + (e.key === 'ArrowRight' ? 1800 : -1800)));
    } else if (e.key === 'Escape') {
      setHover(null);
    }
  };
  const hoverValue = hover == null ? null : interpolate(hover <= now ? shown : planned, hover);
  const tipLeft = hover == null ? 0 : x(hover) + 190 > W ? x(hover) - 190 : x(hover) + 12;
  const lowShown = low && low[0] > now + 1800 && low[0] <= t1;
  const narrow = W < 640;
  const lowIsEnd = lowShown && planEnd && Math.abs(planEnd[0] - low[0]) <= 3600;
  let lowLabelY = 0;
  let lowX = 0;
  let lowText = '';
  if (lowShown) {
    lowText = `Lowest ${num(low[1])} % at ${hhmm(low[0])}${lowIsEnd && !narrow ? ', where the plan ends' : ''}`;
    lowX = Math.max(L + 70, Math.min(x(low[0]), W - R - 90));
    const below = Math.min(base - 6, y(low[1]) + 20);
    const halfWidth = (lowText.length * 6.8) / 2;
    // the reserve label sits right-aligned just above the reserve line
    const overlapsReserve = below - 13 < y(reserve) + 1 && below + 3 > y(reserve) - 19 && lowX + halfWidth > W - R - 96;
    lowLabelY = overlapsReserve ? y(low[1]) - 12 : below;
  }
  return html`<div class="strip-chart">
    <svg width=${W} height=${H} viewBox=${`0 0 ${W} ${H}`} role="img" tabindex="0"
      aria-label=${`State of charge from ${dayHhmm(t0)} to ${dayHhmm(t1)}, measured then planned. Use the arrow keys to read values.`}
      onPointerMove=${onMove} onPointerLeave=${() => setHover(null)} onKeyDown=${onKey} onBlur=${() => setHover(null)}>
      <rect class="plot-bg" x=${L} y=${TOP} width=${W - L - R} height=${PH} />
      ${bands.map((b) => {
        const a = x(clamp(b.start));
        const c = x(clamp(b.end));
        if (c - a < 1) return null;
        return html`<g>
          <rect class=${`band band-${b.kind}`} x=${a} y=${TOP} width=${c - a} height=${PH} />
          ${c - a > 96 ? html`<text class="band-label" x=${a + 6} y=${TOP - 8}>${PERIOD[b.kind]}</text>` : null}
        </g>`;
      })}
      ${[0, 50, 100].map((v) => html`<g>
        <line class=${v === 0 ? 'axis' : 'grid'} x1=${L} x2=${W - R} y1=${y(v)} y2=${y(v)} />
        <text class="tick" x=${L - 8} y=${y(v) + 4} text-anchor="end">${`${v} %`}</text>
      </g>`)}
      <line class="reserve" x1=${L} x2=${W - R} y1=${y(reserve)} y2=${y(reserve)} />
      <text class="tick" x=${W - R - 4} y=${y(reserve) - 5} text-anchor="end">${`Reserve ${reserve} %`}</text>
      ${floor != null && floor < reserve ? html`<g>
        <line class="floor" x1=${L} x2=${W - R} y1=${y(floor)} y2=${y(floor)} />
        <text class="tick" x=${W - R - 4} y=${y(floor) + 13} text-anchor="end">${`Floor ${floor} %`}</text>
      </g>` : null}
      ${shown.length > 1 ? html`<path class="soc-measured" d=${path(shown)} />` : null}
      ${planned.length > 1 ? html`<path class="soc-planned" d=${path(planned)} />` : null}
      ${planEnd && planEnd[0] < t1 - 3600 && !narrow && !lowIsEnd ? html`<text class="point-label" x=${Math.min(x(planEnd[0]) + 8, W - R - 150)} y=${Math.max(TOP + 14, y(planEnd[1]) - 10)}>Projection ends here</text>` : null}
      <line class="now" x1=${x(now)} x2=${x(now)} y1=${TOP} y2=${base + 30} />
      ${lowShown ? html`<g>
        <circle class="dot-low" cx=${x(low[0])} cy=${y(low[1])} r="4" />
        <text class="point-label" x=${lowX} y=${lowLabelY} text-anchor="middle">${lowText}</text>
      </g>` : null}
      ${socNow != null ? html`<g>
        <circle class="dot-now" cx=${x(now)} cy=${y(socNow)} r="5" />
        <text class="point-label strong" x=${x(now) - 10} y=${Math.max(TOP + 14, y(socNow) - 10)} text-anchor="end">${`${num(socNow)} % now`}</text>
      </g>` : null}
      ${blocks.map((b) => {
        const a = x(clamp(b.a));
        const c = x(clamp(b.b));
        if (c - a < 2) return null;
        return html`<g>
          <rect class=${`block block-${b.kind}${b.preview ? ' is-preview' : ''}`} x=${a} y=${base + 18} width=${c - a} height="8" rx="4" />
          ${b.labelX != null ? html`<text class="block-label" x=${b.labelX} y=${base + 13}>${b.label}</text>` : null}
        </g>`;
      })}
      ${ticks.map((t) => html`<text class="tick" x=${x(t)} y=${H - 6} text-anchor="middle">${localHour(t) === 0 ? dayLabel(t) : hhmm(t)}</text>`)}
      ${hover != null ? html`<line class="cross" x1=${x(hover)} x2=${x(hover)} y1=${TOP} y2=${base} />` : null}
    </svg>
    ${hover != null ? html`<div class="tip" style=${{ left: `${Math.max(0, tipLeft)}px`, top: `${TOP + 6}px` }}>
      <div class="tip-time">${`${dayHhmm(hover)}, ${PERIOD[periodAt(bands, hover)]}`}</div>
      <div>${hoverValue == null
        ? html`<span class="muted">${hover <= now ? 'No measurement' : 'Beyond the current plan'}</span>`
        : html`<strong>${num(hoverValue)} %</strong> <span class="muted">${hover <= now ? 'measured' : 'planned'}</span>`}</div>
    </div>` : null}
  </div>`;
}

function StripTable({ now, bands, measured, projection }) {
  const rows = [];
  for (let t = Math.ceil((now - 12 * 3600) / 3600) * 3600; t <= now + 24 * 3600; t += 3600) {
    const past = t <= now;
    rows.push([t, PERIOD[periodAt(bands, t)], interpolate(past ? measured : projection, t), past]);
  }
  return html`<div class="scroll"><table class="tbl">
    <caption class="vh">State of charge by hour</caption>
    <thead><tr><th scope="col">Time</th><th scope="col">Tariff</th><th scope="col">State of charge</th></tr></thead>
    <tbody>${rows.map(([t, period, v, past]) => html`<tr><td>${dayHhmm(t)}</td><td>${period}</td><td>${v == null ? '–' : `${num(v)} % ${past ? 'measured' : 'planned'}`}</td></tr>`)}</tbody>
  </table></div>`;
}

// ---------- power right now ---------------------------------------------------------------------
function arrowPoints(x, y, dir) {
  const s = 5;
  const pts = {
    right: [[x - s, y - s], [x + 1, y], [x - s, y + s]],
    left: [[x + s, y - s], [x - 1, y], [x + s, y + s]],
    up: [[x - s, y + s], [x, y - 1], [x + s, y + s]],
    down: [[x - s, y - s], [x, y + 1], [x + s, y - s]],
  }[dir];
  return pts.map((p) => p.join(',')).join(' ');
}

function PowerFlow({ s }) {
  const d = s.inverter.data || {};
  const stale = s.inverter.age_s == null || s.inverter.age_s > 20;
  const pv = d.pv_power ?? 0;
  const load = d.load_power ?? 0;
  const batt = d.battery_power ?? 0;
  const grid = (d.grid_import_power ?? 0) - (d.grid_export_power ?? 0);
  const on = (w) => Math.abs(w) >= 20;
  const wire = (key, active) => `wire ${active ? `c-${key}` : 'wire-off'}`;
  const node = (key, active) => (active ? `c-${key}` : `node-off c-${key}`);
  const battWord = batt >= 20 ? 'charging' : batt <= -20 ? 'discharging' : 'idle';
  const gridWord = grid >= 20 ? 'importing' : grid <= -20 ? 'exporting' : 'no flow';
  const label = `Solar ${watts(pv)}, home ${watts(load)}, battery ${battWord} ${watts(Math.abs(batt))}, grid ${gridWord} ${watts(Math.abs(grid))}`;
  return html`<${Panel} id="flow-title" title="Power right now" className="flow" stale=${stale}
      aside=${html`<span class="aside">${d.mode || 'No data'}</span>`}>
    <svg class="flow-svg" viewBox="0 0 340 222" role="img" aria-label=${label}>
      <path class=${wire('pv', on(pv))} d="M40 53V108H125" />
      <path class=${wire('batt', on(batt))} d="M40 183V122H125" />
      <path class=${wire('load', on(load))} d="M300 53V108H215" />
      <path class=${wire('grid', on(grid))} d="M300 183V122H215" />
      ${on(pv) ? html`<polygon class="arrow c-pv" points=${arrowPoints(120, 108, 'right')} />` : null}
      ${on(load) ? html`<polygon class="arrow c-load" points=${arrowPoints(300, 60, 'up')} />` : null}
      ${batt >= 20 ? html`<polygon class="arrow c-batt" points=${arrowPoints(40, 176, 'down')} />` : null}
      ${batt <= -20 ? html`<polygon class="arrow c-batt" points=${arrowPoints(120, 122, 'right')} />` : null}
      ${grid >= 20 ? html`<polygon class="arrow c-grid" points=${arrowPoints(220, 122, 'left')} />` : null}
      ${grid <= -20 ? html`<polygon class="arrow c-grid" points=${arrowPoints(300, 176, 'down')} />` : null}
      <rect class="inv-box" x="125" y="92" width="90" height="46" rx="6" />
      <text class="inv-label" x="170" y="120" text-anchor="middle">Inverter</text>
      <circle class=${node('pv', on(pv))} cx="40" cy="45" r="7" />
      <text class="node-label" x="56" y="41">Solar</text>
      <text class="node-value" x="56" y="64">${watts(pv)}</text>
      <circle class=${node('load', on(load))} cx="300" cy="45" r="7" />
      <text class="node-label" x="284" y="41" text-anchor="end">Home</text>
      <text class="node-value" x="284" y="64" text-anchor="end">${watts(load)}</text>
      <circle class=${node('batt', on(batt))} cx="40" cy="190" r="7" />
      <text class="node-label" x="56" y="186">${`Battery, ${battWord}`}</text>
      <text class="node-value" x="56" y="209">${watts(Math.abs(batt))}</text>
      <circle class=${node('grid', on(grid))} cx="300" cy="190" r="7" />
      <text class="node-label" x="284" y="186" text-anchor="end">${`Grid, ${gridWord}`}</text>
      <text class="node-value" x="284" y="209" text-anchor="end">${watts(Math.abs(grid))}</text>
    </svg>
    ${stale ? html`<p class="stale-note">${s.inverter.age_s == null ? 'No inverter data yet.' : `Inverter data is ${ago(s.inverter.age_s)} old.`}</p>` : null}
  <//>`;
}

// ---------- battery -----------------------------------------------------------------------------
// ---------- CT calibration ----------------------------------------------------------------------
function signedW(v, digits = 1) {
  if (v == null) return '–';
  if (Math.abs(v) < 0.5 * 10 ** -digits) return `${num(0, digits)} W`;
  return `${v > 0 ? '+' : '−'}${num(Math.abs(v), digits)} W`;
}

function CtPanel({ s }) {
  const ct = s.ct || {};
  const d = s.inverter.data || {};
  const [hist] = usePoll('/api/history?hours=24', 120000);
  let kind = 'ok';
  let sentence;
  if (!s.core_connected || ct.enabled == null) {
    kind = 'off';
    sentence = 'No calibration data from the core yet.';
  } else if (!ct.enabled) {
    kind = 'off';
    sentence = `Calibration is off, so the offset stays at ${signedW(ct.offset_w)}.`;
  } else if (ct.last_error) {
    kind = 'error';
    sentence = `The last offset write failed: ${ct.last_error}.`;
  } else {
    sentence = `Holding the offset at ${signedW(ct.offset_w)} for ${watts(ct.load_w)} of home load.`;
  }
  let target = '–';
  if (ct.enabled && ct.target_w != null) {
    if (!ct.pending) target = `${signedW(ct.target_w)}, within ${num(ct.hyst_w)} W of the offset, so no write is needed`;
    else if (ct.next_write_in_s > 0) target = `${signedW(ct.target_w)}, write due in ${Math.ceil(ct.next_write_in_s)} s`;
    else target = `${signedW(ct.target_w)}, writing on the next poll`;
  }
  const slope = ct.slope ?? 0;
  const intercept = ct.intercept_w ?? 0;
  return html`<${Panel} id="ct-title" title="CT calibration" className="ct"
      aside=${html`<span class="aside">Offset register H119</span>`}>
    <p class=${`bridge-status st-${kind}`}><${Icon} kind=${kind} /><span>${sentence}</span></p>
    <${Facts} rows=${[
      ['Offset now', ct.register != null ? `${signedW(ct.offset_w)} (register value ${ct.register})` : '–'],
      ['Target at this load', target],
      ['Calibration', ct.slope != null ? `offset = ${slope < 0 ? '−' : ''}${num(Math.abs(slope), 3)} × load ${intercept < 0 ? '−' : '+'} ${num(Math.abs(intercept))} W` : '–'],
      ['Limits', ct.min_w != null ? `${signedW(ct.min_w, 0)} to ${signedW(ct.max_w, 0)}, firmware allows ${signedW(ct.firmware_min_w, 0)} to ${signedW(ct.firmware_max_w, 0)}` : '–'],
      ['Write rule', ct.hyst_w != null ? `when the target moves ${num(ct.hyst_w)} W or more, at most once every ${num(ct.min_write_s)} s` : '–'],
      ['Last write', ct.last_write_ts ? `${dayHhmm(ct.last_write_ts)}, ${signedW(ct.last_write_w)}. ${num(ct.writes)} since the core started.` : 'none since the core started'],
      ['Grid at the CT', `importing ${watts(d.grid_import_power)}, exporting ${watts(d.grid_export_power)}, ${num(d.ct_current, 2)} A`],
    ]} />
    ${ct.enabled && ct.slope != null ? html`<${CalibrationCurve} ct=${ct} hist=${hist} />` : null}
  <//>`;
}

function CalibrationCurve({ ct, hist }) {
  const [ref, width] = useWidth();
  const [table, setTable] = useState(false);
  const pts = useMemo(() => {
    const out = [];
    if (hist?.t && hist.ct_offset_w) {
      hist.t.forEach((t, i) => {
        const load = hist.load_w[i];
        const offset = hist.ct_offset_w[i];
        if (load != null && offset != null) out.push([t, load, offset]);
      });
    }
    return out;
  }, [hist]);
  const H = 206;
  const L = 60;
  const R = 12;
  const TOP = 12;
  const BASE = 162;
  const W = Math.max(width, 260);
  const X = Math.ceil((Math.max(1500, ct.load_w || 0, ...pts.map((p) => p[1])) * 1.1) / 1000) * 1000;
  const f = (load) => Math.max(ct.min_w, Math.min(ct.max_w, ct.slope * load + ct.intercept_w));
  const offsets = pts.map((p) => p[2]);
  // fit the vertical range to the line over the loads shown and to the offsets actually used, plus the band
  const known = [f(0), f(X), ct.offset_w, ct.target_w, ...offsets].filter((v) => v != null);
  const lo = Math.min(...known) - ct.hyst_w;
  const hi = Math.max(...known) + ct.hyst_w;
  const step = niceStep(hi - lo, 6);
  const y0 = Math.floor(lo / step) * step;
  let y1 = Math.ceil(hi / step) * step;
  if (y1 <= y0) y1 = y0 + step;
  const x = (v) => L + (v / X) * (W - L - R);
  const y = (v) => TOP + ((y1 - v) / (y1 - y0)) * (BASE - TOP);
  const clampY = (py) => Math.max(TOP, Math.min(BASE, py));
  const line = [];
  const upper = [];
  const lower = [];
  for (let i = 0; i <= 80; i++) {
    const load = (X * i) / 80;
    const v = f(load);
    line.push(`${x(load).toFixed(1)},${y(v).toFixed(1)}`);
    upper.push(`${x(load).toFixed(1)},${clampY(y(v + ct.hyst_w)).toFixed(1)}`);
    lower.unshift(`${x(load).toFixed(1)},${clampY(y(v - ct.hyst_w)).toFixed(1)}`);
  }
  const xticks = [];
  for (let v = 0; v <= X; v += X > 4000 ? 2000 : 1000) xticks.push(v);
  const yticks = [];
  for (let v = y0; v <= y1 + 1e-9; v += step) yticks.push(v);
  const dots = useMemo(() => pts.map(([t, load, offset]) => html`<circle class="ct-dot" cx=${x(load)} cy=${y(offset)} r="2.5"><title>${`${dayHhmm(t)}: ${signedW(offset)} at ${watts(load)}`}</title></circle>`),
    [pts, W, X, y0, y1]);
  const now = ct.load_w != null && ct.offset_w != null;
  const nx = now ? x(ct.load_w) : 0;
  const leftLabel = nx > W - 180;
  return html`<figure class="ct-chart" ref=${ref}>
    <figcaption>
      <span class="tchart-title">Offset against home load</span>
      <span class="legend">
        <span class="key"><svg width="16" height="8" aria-hidden="true"><line class="ct-curve" x1="0" x2="16" y1="4" y2="4" /></svg>Calibration line</span>
        <span class="key"><svg width="14" height="10" aria-hidden="true"><rect class="ct-band" width="14" height="10" /></svg>${`No write within ${num(ct.hyst_w)} W`}</span>
        <span class="key"><svg width="10" height="10" aria-hidden="true"><circle class="ct-dot ct-dot-key" cx="5" cy="5" r="3" /></svg>Offsets used, last 24 h</span>
        <span class="key"><svg width="12" height="12" aria-hidden="true"><circle class="ct-now" cx="6" cy="6" r="4" /></svg>Now</span>
        <span class="key"><svg width="12" height="12" aria-hidden="true"><circle class="ct-target" cx="6" cy="6" r="4" /></svg>Target</span>
      </span>
    </figcaption>
    ${width ? html`<svg width=${W} height=${H} viewBox=${`0 0 ${W} ${H}`} role="img"
        aria-label=${`Calibration line from ${signedW(f(0))} with no load to ${signedW(f(X))} at ${num(X / 1000)} kW. Offset now ${signedW(ct.offset_w)} at ${watts(ct.load_w)}.`}>
      ${yticks.map((v) => html`<g>
        <line class=${Math.abs(v) < 1e-9 ? 'axis' : 'grid'} x1=${L} x2=${W - R} y1=${y(v)} y2=${y(v)} />
        <text class="tick" x=${L - 8} y=${y(v) + 4} text-anchor="end">${signedW(v, 0)}</text>
      </g>`)}
      <path class="ct-band" d=${`M${upper.join('L')}L${lower.join('L')}Z`} />
      ${dots}
      <path class="ct-curve" d=${`M${line.join('L')}`} />
      ${now ? html`<g>
        ${ct.target_w != null ? html`<circle class="ct-target" cx=${nx} cy=${y(ct.target_w)} r="4" />` : null}
        <circle class="ct-now" cx=${nx} cy=${y(ct.offset_w)} r="5"><title>${`Offset now ${signedW(ct.offset_w)} at ${watts(ct.load_w)}`}</title></circle>
        <text class="point-label strong" x=${leftLabel ? nx - 10 : nx + 10} y=${Math.max(TOP + 12, y(ct.offset_w) - 10)}
          text-anchor=${leftLabel ? 'end' : 'start'}>${`${signedW(ct.offset_w)} at ${watts(ct.load_w)}`}</text>
      </g>` : null}
      ${xticks.map((v) => html`<text class="tick" x=${x(v)} y=${BASE + 18} text-anchor="middle">${v === 0 ? '0 W' : `${num(v / 1000)} kW`}</text>`)}
      <text class="tick" x=${W - R} y=${H - 4} text-anchor="end">Home load</text>
    </svg>` : null}
    <button class="link" aria-pressed=${table} onClick=${() => setTable(!table)}>${table ? 'Hide table' : 'Show as table'}</button>
    ${table ? html`<${CtTable} pts=${pts} />` : null}
  </figure>`;
}

function CtTable({ pts }) {
  if (!pts.length) return html`<p class="empty">No offsets recorded in the last 24 hours yet.</p>`;
  const byHour = new Map();
  for (const [t, load, offset] of pts) {
    const hour = Math.floor(t / 3600) * 3600;
    const acc = byHour.get(hour) || [0, 0, 0];
    acc[0] += load;
    acc[1] += offset;
    acc[2] += 1;
    byHour.set(hour, acc);
  }
  const rows = [...byHour.entries()].sort((a, b) => b[0] - a[0]);
  return html`<div class="scroll"><table class="tbl">
    <caption class="vh">Average home load and CT offset by hour</caption>
    <thead><tr><th scope="col">Hour</th><th scope="col">Home load</th><th scope="col">CT offset</th></tr></thead>
    <tbody>${rows.map(([hour, [load, offset, n]]) => html`<tr><td>${dayHhmm(hour)}</td><td>${watts(load / n)}</td><td>${signedW(offset / n)}</td></tr>`)}</tbody>
  </table></div>`;
}

function BatteryPanel({ s }) {
  const jk = s.jk.data || {};
  const inv = s.inverter.data || {};
  const stale = s.jk.age_s == null || s.jk.age_s > 20;
  const reserve = s.planner?.plan?.reserve_soc ?? 20;
  const soc = jk.soc;
  const state = jk.current > 0.5 ? 'Charging' : jk.current < -0.5 ? 'Discharging' : 'Idle';
  const differs = inv.soc != null && soc != null && Math.abs(inv.soc - soc) >= 5;
  return html`<${Panel} id="batt-title" title="Battery" className="battery" stale=${stale}
      aside=${html`<span class="aside">${jk.soc == null ? 'No data' : state}</span>`}>
    <div class="soc">
      <p class="soc-figure">${num(soc)}<span class="unit"> %</span></p>
      <p class="soc-caption">State of charge from the BMS.${differs ? html` The inverter estimates <strong>${inv.soc} %</strong>.` : null}</p>
    </div>
    <div class="meter" role="meter" aria-label="State of charge" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${soc ?? 0}>
      <div class="meter-fill" style=${{ width: `${Math.max(0, Math.min(100, soc ?? 0))}%` }}></div>
      <div class="meter-mark" style=${{ left: `${reserve}%` }}></div>
    </div>
    <p class="meter-note">${`The mark is the planner reserve, ${reserve} %`}</p>
    <${Facts} rows=${[
      ['Voltage', `${num(jk.voltage, 2)} V`],
      ['Current', `${num(jk.current, 1)} A`],
      ['Power', watts(jk.power)],
      ['Remaining', `${num(jk.remaining_capacity)} of ${num(jk.capacity)} Ah`],
      ['Temperature', `${num(jk.temp_1)} °C and ${num(jk.temp_2)} °C, switches ${num(jk.temp_mos)} °C`],
      ['Cycles', num(jk.cycle_count)],
      ['BMS switches', `charge ${jk.charge_mos_on ? 'on' : 'off'}, discharge ${jk.discharge_mos_on ? 'on' : 'off'}${jk.balancing ? ', balancing' : ''}`],
    ]} />
    ${jk.cells ? html`<${Cells} cells=${Object.entries(jk.cells)} />` : null}
    ${stale ? html`<p class="stale-note">${s.jk.age_s == null ? 'No BMS data yet.' : `BMS data is ${ago(s.jk.age_s)} old.`}</p>` : null}
  <//>`;
}

function Cells({ cells }) {
  const [ref, width] = useWidth();
  const vals = cells.map(([, v]) => v);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const floor = Math.floor((lo - Math.max(8, (hi - lo) * 0.8)) / 5) * 5;
  const H = 104;
  const TOP = 20;
  const BASE = 80;
  const n = cells.length;
  return html`<figure class="cells" ref=${ref}>
    <figcaption>${`Cells ${num(lo)} to ${num(hi)} mV, spread ${num(hi - lo)} mV. Bars start at ${num(floor)} mV.`}</figcaption>
    ${width > n * 6 ? html`<svg width=${width} height=${H} viewBox=${`0 0 ${width} ${H}`} role="img" aria-label=${`${n} cell voltages from ${lo} to ${hi} mV`}>
      <line class="axis" x1="0" x2=${width} y1=${BASE} y2=${BASE} />
      ${cells.map(([, v], i) => {
        const slot = width / n;
        const w = Math.max(4, Math.min(18, slot - 4));
        const cx = slot * i + slot / 2;
        const top = BASE - Math.max(2, ((v - floor) / (hi + 2 - floor)) * (BASE - TOP));
        const mark = v === hi ? 'highest' : v === lo ? 'lowest' : null;
        return html`<g class="cell">
          <title>${`Cell ${i + 1}: ${num(v)} mV`}</title>
          <rect class="hit" x=${slot * i} y="0" width=${slot} height=${H} />
          <path class="cell-bar" d=${barPath(cx - w / 2, w, top, BASE)} />
          ${mark && (mark === 'highest' ? vals.indexOf(hi) === i : vals.indexOf(lo) === i) ? html`<text class="bar-label" x=${cx} y=${top - 5} text-anchor="middle">${num(v)}</text>` : null}
          ${i % 4 === 0 || i === n - 1 ? html`<text class="tick" x=${cx} y=${H - 6} text-anchor="middle">${i + 1}</text>` : null}
        </g>`;
      })}
    </svg>` : null}
  </figure>`;
}

// ---------- BMS bridge ----------------------------------------------------------------------------
const REGISTERS = [
  ['Charge voltage', (v) => `${num(v / 10, 1)} V`], ['Max charge current', (v) => `${num(v / 10, 1)} A`],
  ['Max discharge current', (v) => `${num(v / 10, 1)} A`], ['SOH and SOC', (v) => `${v >> 8} %, ${v & 0xff} %`],
  ['Capacity', (v) => `${v} Ah`], ['Pack voltage', (v) => `${num(v / 10, 1)} V`],
  ['Current', (v) => `${num((v > 32767 ? v - 65536 : v) / 100, 2)} A`],
  ['Status', (v) => `charge ${v & 1 ? 'allowed' : 'forbidden'}, discharge ${v & 2 ? 'allowed' : 'forbidden'}`],
  ['Fault code', (v) => `${v}`], ['Warning code', (v) => `${v}`], ['Highest cell', (v) => `${v} mV`],
  ['Lowest cell', (v) => `${v} mV`], ['Highest temperature', (v) => `${num(v / 10, 1)} °C`],
  ['Lowest temperature', (v) => `${num(v / 10, 1)} °C`], ['Unused', (v) => `${v}`], ['Cycles', (v) => `${v}`],
];

function BridgePanel({ s }) {
  const e = s.emulator || {};
  const dec = s.holding.decoded || {};
  let kind;
  let sentence;
  if (!s.core_connected) {
    kind = 'error';
    sentence = 'The core process is not connected, so the bridge state is unknown.';
  } else if (!e.answering) {
    kind = 'error';
    sentence = `Not answering the inverter: BMS data is ${e.limit_reason}.`;
  } else if (!e.polling) {
    kind = 'warn';
    sentence = `Ready, but the inverter is not polling it${dec.battery_type && dec.battery_type !== 'lithium' ? ` (battery type is set to ${dec.battery_type})` : ''}.`;
  } else {
    kind = 'ok';
    sentence = `Answering the inverter's battery polls, last one ${ago(e.poll_age_s)} ago.`;
  }
  const allowed = (ok) => (ok ? 'allowed' : 'forbidden');
  return html`<${Panel} id="bridge-title" title="BMS bridge" className="bridge"
      aside=${html`<span class="aside">JK BMS to inverter battery port</span>`}>
    <p class=${`bridge-status st-${kind}`}><${Icon} kind=${kind} /><span>${sentence}</span></p>
    <${Facts} rows=${[
      ['Charging', e.answering ? `${allowed(e.charge_ok)}, up to ${num(e.max_charge_a)} A at ${num(e.charge_voltage, 1)} V` : '–'],
      ['Discharging', e.answering ? `${allowed(e.discharge_ok)}, up to ${num(e.max_discharge_a)} A` : '–'],
      ['Limit reason', e.limit_reason],
      ['BMS data age', s.jk.age_s == null ? 'no data' : `${num(s.jk.age_s, 1)} s`],
      ['Polls answered', `${num(e.polls)}${e.unanswered ? `, ${num(e.unanswered)} unanswered` : ''}`],
      ['Link errors', `${num(e.crc_errors)} bad CRC${e.last_error ? `, last serial error: ${e.last_error}` : ''}`],
      ['Inverter battery type', `${dec.battery_type ?? '–'}${dec.battery_type === 'lithium' ? `, brand ${dec.lithium_brand}` : ''}`],
    ]} />
    ${e.regs ? html`<details>
      <summary>Registers sent to the inverter</summary>
      <table class="tbl">
        <thead><tr><th scope="col">Register</th><th scope="col">Meaning</th><th scope="col">Raw</th><th scope="col">Value</th></tr></thead>
        <tbody>${e.regs.map((v, i) => html`<tr><td>${i}</td><td style="text-align:left">${REGISTERS[i][0]}</td><td>${v}</td><td>${REGISTERS[i][1](v)}</td></tr>`)}</tbody>
      </table>
    </details>` : null}
  <//>`;
}

// ---------- history --------------------------------------------------------------------------------
const RANGES = [[6, '6 h'], [24, '24 h'], [72, '3 days'], [168, '7 days']];

function History() {
  const [hours, setHours] = useState(() => {
    try { return Number(localStorage.getItem('solar01.hours')) || 24; } catch { return 24; }
  });
  const [table, setTable] = useState(false);
  const [data, loading] = usePoll(`/api/history?hours=${hours}`, 60000);
  const choose = (h) => {
    setHours(h);
    try { localStorage.setItem('solar01.hours', String(h)); } catch { /* private mode */ }
  };
  return html`<section class="panel history" aria-labelledby="hist-title">
    <div class="panel-head">
      <h2 id="hist-title">History</h2>
      <div class="hist-controls">
        <div class="filters" role="radiogroup" aria-label="Time range">
          ${RANGES.map(([h, label]) => html`<button role="radio" aria-checked=${hours === h} onClick=${() => choose(h)}>${label}</button>`)}
        </div>
        <button class="link" aria-pressed=${table} onClick=${() => setTable(!table)}>${table ? 'Show charts' : 'Show as table'}</button>
      </div>
    </div>
    <div class=${loading && data ? 'charts refetch' : 'charts'}>
      ${table ? html`<${HistoryTable} data=${data} />` : html`
        <${TimeChart} title="Solar and home load" data=${data} unit="W" syncKey="history" span=${hours}
          series=${[{ key: 'pv_w', label: 'Solar', color: '--pv' }, { key: 'load_w', label: 'Home load', color: '--load' }]} />
        <${TimeChart} title="Battery and grid" note="Above zero the battery is charging and the grid is supplying power."
          data=${data} unit="W" zero syncKey="history" span=${hours}
          series=${[{ key: 'batt_w', label: 'Battery', color: '--batt' }, { key: 'grid_w', label: 'Grid', color: '--grid' }]} />
        <${TimeChart} title="State of charge (BMS)" data=${data} unit="%" fixed=${[0, 100]} syncKey="history" span=${hours}
          series=${[{ key: 'jk_soc', fallback: 'soc', label: 'State of charge', color: '--ink' }]} />`}
    </div>
    <${DailyEnergy} />
  </section>`;
}

function lastValue(arr) {
  for (let i = arr.length - 1; i >= 0; i--) if (arr[i] != null) return [i, arr[i]];
  return [null, null];
}

function showTip(u, el, series, colors, fmt) {
  if (!el) return;
  const i = u.cursor.idx;
  if (i == null || u.cursor.left < 0) {
    el.hidden = true;
    return;
  }
  el.replaceChildren();
  const head = document.createElement('div');
  head.className = 'tip-time';
  head.textContent = dayHhmm(u.data[0][i]);
  el.appendChild(head);
  series.forEach((sr, k) => {
    const row = document.createElement('div');
    row.className = 'tip-row';
    const key = document.createElement('span');
    key.className = 'tip-key';
    key.style.background = colors[k];
    const value = document.createElement('strong');
    value.textContent = fmt(u.data[k + 1][i]);
    const label = document.createElement('span');
    label.textContent = sr.label;
    row.append(key, value, label);
    el.appendChild(row);
  });
  el.hidden = false;
  const frame = el.parentElement.getBoundingClientRect();
  const over = u.over.getBoundingClientRect();
  const left = over.left - frame.left + u.cursor.left;
  const w = el.offsetWidth;
  el.style.left = `${left + 14 + w > frame.width ? left - w - 14 : left + 14}px`;
  el.style.top = `${over.top - frame.top + 6}px`;
}

function drawEndLabels(u, series, fmt, ink, surface) {
  const pr = uPlot.pxRatio;
  const pts = [];
  series.forEach((sr, k) => {
    const [i, v] = lastValue(u.data[k + 1]);
    if (i != null) pts.push({ x: u.valToPos(u.data[0][i], 'x', true), y: u.valToPos(v, 'y', true), text: `${sr.label} ${fmt(v)}` });
  });
  if (pts.length > 1 && Math.abs(pts[0].y - pts[1].y) < 18 * pr) return;   // converging: legend and tooltip carry it
  const c = u.ctx;
  c.save();
  c.font = `500 ${12.5 * pr}px ${FONT}`;
  c.textAlign = 'right';
  c.textBaseline = 'bottom';
  c.lineJoin = 'round';
  for (const p of pts) {
    const ty = Math.max(u.bbox.top + 15 * pr, p.y - 6 * pr);
    c.strokeStyle = surface;
    c.lineWidth = 4 * pr;
    c.strokeText(p.text, p.x - 6 * pr, ty);
    c.fillStyle = ink;
    c.fillText(p.text, p.x - 6 * pr, ty);
  }
  c.restore();
}

const TIME_INCRS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800];

function TimeChart({ title, note, data, series, unit, zero = false, fixed, syncKey, span, height = 150 }) {
  const [wrap, width] = useWidth();
  const host = useRef(null);
  const tip = useRef(null);
  const theme = useTheme();
  const cols = useMemo(() => {
    if (!data?.t || data.t.length < 2) return null;
    return [data.t, ...series.map((sr) => data[sr.key].map((v, i) => v ?? (sr.fallback ? data[sr.fallback][i] : null)))];
  }, [data]);
  const fmt = (v) => (v == null ? '–' : unit === 'W' ? watts(v) : `${num(v)} %`);
  useEffect(() => {
    if (!width || !host.current || !cols) return undefined;
    const colors = series.map((sr) => cssVar(sr.color));
    const muted = cssVar('--muted');
    const hair = cssVar('--hair');
    const axis = cssVar('--axis');
    const surface = cssVar('--surface');
    const ink = cssVar('--ink');
    const spanHours = span ?? (cols[0][cols[0].length - 1] - cols[0][0]) / 3600;
    const opts = {
      width,
      height,
      padding: [14, 14, 0, 0],
      tzDate: TZ ? (ts) => uPlot.tzDate(new Date(ts * 1e3), TZ) : undefined,
      legend: { show: false },
      cursor: {
        y: false,
        drag: { x: false, y: false, setScale: false },
        sync: syncKey ? { key: syncKey } : undefined,
        points: { size: 9, width: 2, stroke: () => surface, fill: (u, i) => colors[i - 1] },
      },
      scales: {
        x: span ? { time: true, range: () => { const end = Date.now() / 1000; return [end - span * 3600, end]; } } : { time: true },
        y: fixed ? { range: fixed } : {
          range: (u, lo, hi) => {
            if (lo == null) return [0, 1];
            return uPlot.rangeNum(Math.min(lo, 0), zero ? Math.max(hi, 0) : hi, 0.1, true);
          },
        },
      },
      axes: [
        { stroke: muted, font: `12px ${FONT}`, grid: { show: false }, ticks: { stroke: axis, width: 1, size: 4 },
          incrs: TIME_INCRS, space: 56,
          values: (u, splits) => splits.map((ts) => (spanHours <= 48 || localHour(ts) !== 0 ? hhmm(ts) : dayLabel(ts))) },
        { stroke: muted, font: `12px ${FONT}`, size: 58, gap: 6, grid: { stroke: hair, width: 1 }, ticks: { show: false },
          values: (u, splits) => splits.map((v) => (unit === 'W' ? (Math.abs(v) >= 1000 ? `${num(v / 1000, 1)} kW` : `${num(v)} W`) : `${num(v)} %`)) },
      ],
      series: [{}, ...series.map((sr, k) => ({ label: sr.label, stroke: colors[k], width: 2, points: { show: false } }))],
      hooks: {
        drawAxes: [(u) => {
          if (!zero) return;
          const c = u.ctx;
          const y0 = Math.round(u.valToPos(0, 'y', true)) + 0.5;
          c.save();
          c.strokeStyle = axis;
          c.lineWidth = uPlot.pxRatio;
          c.beginPath();
          c.moveTo(u.bbox.left, y0);
          c.lineTo(u.bbox.left + u.bbox.width, y0);
          c.stroke();
          c.restore();
        }],
        draw: [(u) => drawEndLabels(u, series, fmt, ink, surface)],
        setCursor: [(u) => showTip(u, tip.current, series, colors, fmt)],
      },
    };
    const chart = new uPlot(opts, cols, host.current);
    return () => chart.destroy();
  }, [width, cols, theme]);
  const latest = cols ? series.map((sr, k) => lastValue(cols[k + 1])[1]) : [];
  return html`<figure class="tchart" ref=${wrap}>
    <figcaption>
      <span class="tchart-title">${title}</span>
      ${series.length > 1 ? html`<span class="legend">${series.map((sr, k) => html`<span class="key">
        <svg width="16" height="8" aria-hidden="true"><line x1="0" x2="16" y1="4" y2="4" stroke-width="2" style=${{ stroke: `var(${sr.color})` }} /></svg>
        ${sr.label} <strong>${fmt(latest[k])}</strong></span>`)}</span>` : html`<span class="legend"><strong>${fmt(latest[0])}</strong></span>`}
      ${note ? html`<span class="tchart-note">${note}</span>` : null}
    </figcaption>
    <div class="tchart-plot" ref=${host}></div>
    ${cols ? null : html`<p class="empty">No history for this range yet. It fills in as the hub records data.</p>`}
    <div class="tip" ref=${tip} hidden></div>
  </figure>`;
}

function HistoryTable({ data }) {
  if (!data?.t?.length) return html`<p class="empty">No history for this range yet.</p>`;
  const stride = Math.max(1, Math.round((data.hours <= 6 ? 600 : 3600) / data.bucket_s));
  const keys = ['pv_w', 'load_w', 'batt_w', 'grid_w', 'jk_soc'];
  const rows = [];
  for (let i = 0; i < data.t.length; i += stride) {
    const means = keys.map((k) => {
      const vals = data[k].slice(i, i + stride).filter((v) => v != null);
      return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
    });
    rows.push([data.t[i], ...means]);
  }
  rows.reverse();
  return html`<div class="scroll"><table class="tbl">
    <caption class="vh">History averages</caption>
    <thead><tr><th scope="col">From</th><th scope="col">Solar</th><th scope="col">Home load</th><th scope="col">Battery</th><th scope="col">Grid</th><th scope="col">State of charge</th></tr></thead>
    <tbody>${rows.map(([t, pv, load, batt, grid, soc]) => html`<tr><td>${dayHhmm(t)}</td><td>${watts(pv)}</td><td>${watts(load)}</td><td>${watts(batt)}</td><td>${watts(grid)}</td><td>${soc == null ? '–' : `${num(soc)} %`}</td></tr>`)}</tbody>
  </table></div>`;
}

function DailyEnergy() {
  const [data] = usePoll('/api/daily?days=14', 600000);
  const [ref, width] = useWidth();
  const days = data?.days || [];
  const H = 176;
  const L = 44;
  const R = 8;
  const TOP = 22;
  const BASE = 144;
  const max = Math.max(1, ...days.flatMap((r) => [r.pv_kwh, r.load_kwh]));
  const step = niceStep(max);
  const top = Math.ceil(max / step) * step;
  const y = (v) => BASE - (v / top) * (BASE - TOP);
  const grid = [];
  for (let v = 0; v <= top + 1e-9; v += step) grid.push(v);
  const labelOf = (date) => {
    const [yy, mm, dd] = date.split('-').map(Number);
    return weekdayDay(new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', weekday: 'short', day: 'numeric' }).formatToParts(Date.UTC(yy, mm - 1, dd, 12)));
  };
  return html`<figure class="daily" ref=${ref}>
    <figcaption>
      <span class="tchart-title">Energy per day, last 14 days</span>
      <span class="legend">
        <span class="key"><svg width="12" height="10" aria-hidden="true"><rect class="bar-pv" width="12" height="10" rx="2" /></svg>Solar</span>
        <span class="key"><svg width="12" height="10" aria-hidden="true"><rect class="bar-load" width="12" height="10" rx="2" /></svg>Home load</span>
      </span>
    </figcaption>
    ${!days.length ? html`<p class="empty">No daily totals yet.</p>` : width > L + R + days.length * 8 ? html`<svg width=${width} height=${H} viewBox=${`0 0 ${width} ${H}`} role="img" aria-label="Solar and home load energy per day">
      ${grid.map((v) => html`<g><line class=${v === 0 ? 'axis' : 'grid'} x1=${L} x2=${width - R} y1=${y(v)} y2=${y(v)} /><text class="tick" x=${L - 8} y=${y(v) + 4} text-anchor="end">${num(v)}</text></g>`)}
      <text class="tick" x=${L - 8} y=${TOP - 10} text-anchor="end">kWh</text>
      ${days.map((r, i) => {
        const slot = (width - L - R) / days.length;
        const bw = Math.max(3, Math.min(14, (slot - 10) / 2));
        const cx = L + slot * i + slot / 2;
        const today = i === days.length - 1;
        const every = slot < 44 ? 2 : 1;
        return html`<g class="bar-group">
          <title>${`${labelOf(r.date)}${today ? ' (so far)' : ''}: solar ${num(r.pv_kwh, 1)} kWh, home load ${num(r.load_kwh, 1)} kWh`}</title>
          <rect class="hit" x=${cx - slot / 2} y=${TOP} width=${slot} height=${BASE - TOP} />
          <path class="bar bar-pv" d=${barPath(cx - bw - 1, bw, y(r.pv_kwh), BASE)} />
          <path class="bar bar-load" d=${barPath(cx + 1, bw, y(r.load_kwh), BASE)} />
          ${today ? html`<text class="bar-label" x=${cx} y=${Math.min(y(r.pv_kwh), y(r.load_kwh)) - 6} text-anchor="middle">${`${num(r.pv_kwh, 1)} / ${num(r.load_kwh, 1)}`}</text>` : null}
          ${(days.length - 1 - i) % every === 0 ? html`<text class="tick" x=${cx} y=${H - 8} text-anchor="middle">${today ? 'Today' : labelOf(r.date)}</text>` : null}
        </g>`;
      })}
    </svg>` : null}
  </figure>`;
}

// ---------- planner ---------------------------------------------------------------------------------
const SOC_PRESETS = [50, 80, 90, 100];
const TIME_PRESETS = [[15, '15 min'], [30, '30 min'], [60, '1 h'], [120, '2 h'], [180, '3 h']];

function ManualCharge({ s }) {
  const pl = s.planner;
  const m = pl.manual;
  const socNow = s.jk?.data?.soc ?? s.inverter?.data?.soc;
  const [mode, setMode] = useState('soc');
  const [target, setTarget] = useState(80);
  const [minutes, setMinutes] = useState(60);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const period = periodAt(pl.tariff || [], s.ts);
  async function send(method, body) {
    setBusy(true);
    setError('');
    try {
      const r = await fetch('/api/charge', { method, headers: { 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) });
      if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    } catch (err) {
      setError(`${method === 'DELETE' ? 'The charge could not be stopped' : 'The charge could not start'}: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }
  // while a manual charge runs, the controls show its settings (locked) and the button stops it
  const shownMode = m ? m.mode : mode;
  const shownTarget = m ? m.target_soc : target;
  const shownMinutes = m ? Math.round((m.until - m.started) / 60) : minutes;
  const low = !m && socNow != null && mode === 'soc' && target <= socNow;
  let note;
  if (m) {
    note = m.mode === 'time'
      ? `Charging from the grid until ${hhmm(m.until)}, started at ${hhmm(m.started)} from ${num(m.start_soc)} %.`
      : `Charging to ${m.target_soc} %${m.eta ? `, expected at ${hhmm(isoTs(m.eta))}` : ''}. Stops by ${hhmm(m.until)} at the latest.`;
  } else {
    note = `${socNow != null ? `The battery is at ${num(socNow)} % now. ` : ''}${low ? 'Pick a target above that. ' : ''}${
      period !== 'super_off_peak' ? `It is ${PERIOD[period].toLowerCase()} now, so this charge is billed at the ${PERIOD[period].toLowerCase()} rate. ` : ''}${
      pl.enabled ? 'The planner resumes when the charge ends.' : 'The planner is off; the charge still runs and stops by itself.'}`;
  }
  return html`<div class="manual">
    <h3>Manual charge</h3>
    <fieldset class="manual-controls" disabled=${!!m || busy}>
      <legend class="vh">Charge settings</legend>
      <div class="filters" role="radiogroup" aria-label="Charge until">
        <button role="radio" aria-checked=${shownMode === 'soc'} onClick=${() => setMode('soc')}>To a state of charge</button>
        <button role="radio" aria-checked=${shownMode === 'time'} onClick=${() => setMode('time')}>For a set time</button>
      </div>
      ${shownMode === 'soc' ? html`<div class="manual-row">
        <label class="field inline"><span>Target</span><span class="input-unit"><input type="number" min="5" max="100" step="1"
          value=${shownTarget} onInput=${(e) => setTarget(Number(e.target.value))} /><span aria-hidden="true">%</span></span></label>
        <div class="filters" role="radiogroup" aria-label="Target presets">
          ${SOC_PRESETS.map((v) => html`<button role="radio" aria-checked=${shownTarget === v} onClick=${() => setTarget(v)}>${`${v} %`}</button>`)}
        </div>
      </div>` : html`<div class="manual-row">
        <div class="filters" role="radiogroup" aria-label="Duration">
          ${TIME_PRESETS.map(([v, label]) => html`<button role="radio" aria-checked=${shownMinutes === v} onClick=${() => setMinutes(v)}>${label}</button>`)}
        </div>
      </div>`}
    </fieldset>
    ${m ? html`<p class="bridge-status st-ok" role="status"><${Icon} kind="ok" /><span>${note}</span></p>`
      : html`<p class="manual-note">${note}</p>`}
    ${m ? html`<button class="btn btn-stop" disabled=${busy} onClick=${() => send('DELETE')}>${busy ? 'Stopping…' : 'Stop charging'}</button>`
      : html`<button class="btn btn-primary" disabled=${busy || low || socNow == null}
          onClick=${() => send('POST', mode === 'soc' ? { mode, target_soc: target } : { mode, minutes })}>${busy ? 'Starting…' : 'Start charging'}</button>`}
    ${error ? html`<p class="error-text" role="alert">${error}</p>` : null}
  </div>`;
}

function PlannerPanel({ s }) {
  const pl = s.planner;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const forecast = useMemo(() => {
    const f = pl?.forecast || [];
    return f.length ? { t: f.map((r) => r.ts), pv_w: f.map((r) => r.pv_w), load_w: f.map((r) => r.load_w) } : null;
  }, [pl?.plan?.ts]);
  if (!pl) {
    return html`<${Panel} id="plan-title" title="Charge planner" className="planner"><p class="empty">The planner is not running.</p><//>`;
  }
  const p = pl.plan || {};
  async function toggle() {
    setBusy(true);
    setError('');
    try {
      const r = await fetch('/api/planner', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !pl.enabled }),
      });
      if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    } catch (err) {
      setError(`The planner could not be switched: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }
  const aside = html`<button class="switch" role="switch" aria-checked=${pl.enabled} disabled=${busy} onClick=${toggle}>
    <span class="track"><span class="knob"></span></span>${pl.enabled ? 'On' : 'Off'}${pl.dry_run ? ', dry run' : ''}</button>`;
  return html`<${Panel} id="plan-title" title="Charge planner" className="planner" aside=${aside}>
    ${error ? html`<p class="error-text" role="alert">${error}</p>` : null}
    <${ManualCharge} s=${s} />
    <h3>Plan</h3>
    <${Facts} rows=${[
      ['Decision', p.reason],
      ['Tariff window', p.in_sop ? `Super off-peak until ${hhmm(isoTs(p.window_end))}` : `Next super off-peak ${dayHhmm(isoTs(p.next_window))}`],
      ['Grid charge', p.target_soc != null ? `to ${p.target_soc} %, ${num(p.grid_kwh, 1)} kWh from ${dayHhmm(isoTs(p.start_at))}` : 'not planned'],
      ['Standby hold', p.hold_start ? `${hhmm(isoTs(p.hold_start))} to ${hhmm(isoTs(p.hold_end))}${p.hold ? ', active now' : ''}` : 'not planned'],
      ['Next 24 hours', (p.actions || []).length ? p.actions.map((a) => `${a.kind === 'hold' ? 'standby hold' : `grid charge to ${a.target} %`} ${dayHhmm(isoTs(a.start))} to ${hhmm(isoTs(a.end))}${a.preview ? ' (forecast)' : ''}`).join(', ') : 'no holds or charges expected'],
      ['Stop time', p.actions_stop ? `holds and charges end at ${hhmm(isoTs(p.actions_stop))}, 10 minutes before the window closes` : 'outside super off-peak'],
      ['Planned state of charge', p.projected_min_soc != null ? `lowest ${p.projected_min_soc} %, ${p.projected_soc_next_window} % at the next window` : '–'],
      ['Solar forecast', `${num(p.pv_forecast_today_kwh, 1)} kWh today, ${num(p.pv_forecast_tomorrow_kwh, 1)} kWh tomorrow`],
      ['Forecast correction', p.pv_live_scale != null ? `measured solar is ${num(p.pv_live_scale * 100)} % of forecast` : '–'],
      ['Charge rates', p.charge_kw != null ? `${num(p.charge_kw, 2)} kW from the grid, battery limit ${num(p.chg_cap_kw, 2)} kW` : '–'],
      ['Load profile', `${num(p.history_days, 1)} days of history`],
      ['State of charge from', p.soc_source === 'bms' ? 'the BMS' : p.soc_source === 'inverter' ? 'the inverter estimate' : '–'],
      ['Last inverter write', p.last_write],
    ]} />
    ${p.warning ? html`<p class="notice" role="status"><${Icon} kind="warn" /><span>${p.warning}</span></p>` : null}
    ${pl.last_error ? html`<p class="notice" role="status"><${Icon} kind="error" /><span>${`Planner error: ${pl.last_error}`}</span></p>` : null}
    <div class="forecast">
      <${TimeChart} title="Next 36 hours: solar forecast and expected load" data=${forecast} unit="W" height=${130}
        series=${[{ key: 'pv_w', label: 'Solar', color: '--pv' }, { key: 'load_w', label: 'Home load', color: '--load' }]} />
    </div>
  <//>`;
}

// ---------- inverter, system, events ------------------------------------------------------------------
function InverterPanel({ s }) {
  const d = s.inverter.data || {};
  const dec = s.holding.decoded || {};
  const stale = s.inverter.age_s == null || s.inverter.age_s > 20;
  const energy = [['Solar', d.pv_energy_today], ['Home load', d.load_energy_today], ['Battery charged', d.charge_energy_today],
    ['Battery discharged', d.discharge_energy_today], ['Grid import', d.import_energy_today], ['Grid export', d.export_energy_today]];
  return html`<${Panel} id="inv-title" title="Inverter" className="inverter" stale=${stale}
      aside=${html`<span class="aside">${s.inverter.age_s == null ? 'No data' : `Read ${ago(s.inverter.age_s)} ago`}</span>`}>
    <${Facts} rows=${[
      ['Mode', d.mode],
      ['Solar', `${watts(d.pv_power)} over 1 min, ${watts(d.pv_power_raw)} now, PV1 at ${num(d.pv1_voltage, 1)} V`],
      ['Grid', `${num(d.grid_voltage_l1, 1)} V and ${num(d.grid_voltage_l2, 1)} V, ${num(d.grid_frequency, 2)} Hz`],
      ['Temperature', `${num(d.temp_internal)} °C inside, ${num(d.temp_radiator1)} °C heatsink`],
      ['CT offset', `${num(d.ct_power_offset, 1)} W${s.safety?.ct_calibration ? ', adjusted automatically' : ''}`],
      ['Quick charge', dec.quick_charge ? `on, ${duration(d.quick_charge_remaining)} left` : 'off'],
      ['Standby', dec.standby ? 'yes, the grid feeds the house' : 'no'],
      s.inverter.last_error ? ['Last link error', s.inverter.last_error] : null,
    ]} />
    <h3>Energy today</h3>
    <table class="tbl compact">
      <thead><tr><th scope="col">Flow</th><th scope="col">kWh</th></tr></thead>
      <tbody>${energy.map(([k, v]) => html`<tr><td>${k}</td><td>${num(v, 1)}</td></tr>`)}</tbody>
    </table>
    <h3>Settings read from the inverter</h3>
    <${Facts} rows=${[
      ['Battery type', `${dec.battery_type ?? '–'}${dec.battery_type === 'lithium' ? `, brand ${dec.lithium_brand}` : ''} (H0 ${dec.h0 ?? '–'})`],
      ['Charge current limit', `${num(dec.charge_current_limit_a)} A`],
      ['Discharge current limit', `${num(dec.discharge_current_limit_a)} A`],
      ['End of discharge', `${num(dec.eod_soc)} %`],
      ['AC charge power', `${num(dec.ac_charge_power_kw, 1)} kW`],
      ['Output limit', `${num(dec.active_power_pct)} %`],
      ['AC charge function', dec.ac_charge_function ? 'on (the planner never uses it)' : 'off'],
    ]} />
  <//>`;
}

function SystemPanel({ s }) {
  const pi = s.pi || {};
  const m = s.mqtt || {};
  const wx = s.planner?.weather || {};
  const workers = Object.entries(s.workers || {});
  return html`<${Panel} id="sys-title" title="System" className="system">
    <ul class="health-list">
      ${HEALTH.map(([key, label]) => {
        const h = s.health[key] || { status: 'off', msg: '' };
        return html`<li class=${`st st-${h.status}`}><${Icon} kind=${h.status} /><span class="hl-name">${label}</span><span class="hl-msg">${h.msg || STATUS_WORD[h.status]}</span></li>`;
      })}
    </ul>
    <${Facts} rows=${[
      ['Core process', s.core_connected ? `version ${s.core_version}, running ${duration(s.core_uptime_s)}` : 'not connected'],
      ['Core threads', workers.length ? workers.map(([k, v]) => `${k} ${v.alive ? `${num(v.age_s, 1)} s` : 'stopped'}`).join(', ') : '–'],
      ['Hub process', `version ${s.version}, running ${duration(s.hub_uptime_s)}`],
      ['Home Assistant', m.enabled ? `${m.connected ? 'connected to' : 'not connected to'} ${m.host}` : 'bridge disabled'],
      ['Weather forecast', wx.fetched ? `fetched ${ago(s.ts - wx.fetched)} ago` : (wx.last_error || 'not fetched yet')],
      ['Pi', `CPU ${num(pi.cpu_temp, 1)} °C, load ${num(pi.load1, 2)}, ${num(pi.mem_available_mb)} MB memory free`],
      ['Disk', pi.disk_free_mb != null ? `${num(pi.disk_free_mb / 1024, 1)} GB free` : '–'],
      ['Power supply', pi.undervoltage_now ? 'undervoltage now' : pi.undervoltage_since_boot ? 'undervoltage seen since boot' : pi.throttled_raw ? 'no undervoltage since boot' : '–'],
      ['Pi running', duration(pi.uptime_s)],
    ]} />
  <//>`;
}

const LEVEL_ICON = { info: 'info', warning: 'warn', error: 'error' };

function Events({ events }) {
  const [all, setAll] = useState(false);
  const list = all ? events : events.slice(0, 12);
  return html`<${Panel} id="events-title" title="Events" className="events"
      aside=${html`<span class="aside">Writes to the inverter, warnings and restarts</span>`}>
    ${events.length ? html`<ol class="events-list">
      ${list.map((e) => html`<li key=${`${e.ts}|${e.msg}`} class=${`ev ev-${e.level}`}>
        <time>${dayHhmm(e.ts)}</time><${Icon} kind=${LEVEL_ICON[e.level] || 'info'} /><span class="ev-src">${e.source}</span><span class="ev-msg">${e.msg}</span>
      </li>`)}
    </ol>` : html`<p class="empty">No events recorded yet.</p>`}
    ${events.length > 12 ? html`<button class="link" onClick=${() => setAll(!all)}>${all ? 'Show the latest 12' : `Show all ${events.length}`}</button>` : null}
  <//>`;
}

// ---------- app ----------------------------------------------------------------------------------------
function App() {
  const { state: s, connected, events, lastRx } = useLive();
  const [settings, setSettings] = useState(false);
  const [summaryKey, setSummaryKey] = useState(0);
  const [summary] = usePoll(`/api/summary?k=${summaryKey}`, 60000);
  if (!s) return html`<p class="boot">Connecting to solar01</p>`;
  TZ = s.tz;
  return html`
    <${Header} s=${s} connected=${connected} lastRx=${lastRx} />
    <main class="page">
      ${s.simulate ? html`<p class="banner">Simulated devices: these readings do not come from the inverter or the battery.</p>` : null}
      <${Headline} s=${s} summary=${summary} onSettings=${() => setSettings(true)} />
      <${DayStrip} s=${s} />
      <div class="row-now">
        <div class="stack"><${PowerFlow} s=${s} /><${CtPanel} s=${s} /></div>
        <${BatteryPanel} s=${s} />
        <${BridgePanel} s=${s} />
      </div>
      <${History} />
      <div class="row-detail">
        <${PlannerPanel} s=${s} />
        <${InverterPanel} s=${s} />
        <${SystemPanel} s=${s} />
      </div>
      <${Events} events=${events} />
    </main>
    <${TariffDialog} open=${settings} onClose=${() => setSettings(false)} onSaved=${() => setSummaryKey((k) => k + 1)} />`;
}

const root = document.getElementById('app');
root.textContent = '';
render(html`<${App} />`, root);
