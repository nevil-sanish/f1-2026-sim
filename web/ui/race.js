// Race mode: entry list builder, per-car strategy, options, job polling, results and live replay views.
import { $, h, clear, api, toast, pollJob, fmtLap, fmtRace, fmtGap, num, lowerBound, COMPOUND_COLORS } from './util.js';
import { S, on, teamColor, teamName, teamById, driverById, driverCode, trackById, compoundLabel } from './store.js';
import { drawLineChart, drawStrategy } from '../charts/charts.js';
import { runButton, setDock, activeDock, table, swatch, card, kv } from './widgets.js';
import { R, onTick, playRace, setTime, setFollow, showStatic } from './stage.js';

const MAX_ENTRIES = 22;
const COMPOUNDS = ['C1', 'C2', 'C3', 'C4', 'C5'];
const opts = { laps: '', grid: 'quali', safety_car: true, dnf: true, seed: 1, monte_carlo: 0 };

const el = {
  cls: h('div', { class: 'scroll pad' }), lapc: h('div', { class: 'chart-pane' }, h('canvas')), gap: h('div', { class: 'chart-pane' }, h('canvas')),
  strat: h('div', { class: 'chart-pane' }, h('canvas')), mc: h('div', { class: 'scroll pad' }), ev: h('div', { class: 'scroll pad' }),
};
let lapChartGeom = null, lastDraw = 0, lastBoard = 0;

// ---------------------------------------------------------------- panel
export function buildRacePanel(host) {
  host.append(
    h('div', { class: 'sec-head tight' }, h('h2', { id: 'entry-count' }, 'Entries'),
      h('span', { class: 'row gap' },
        h('button', { class: 'btn small', title: 'Add the selected team + driver with the current setup overrides', onclick: addCurrent }, 'Add current'),
        h('button', { class: 'btn small', title: 'Replace the list with every team’s two current drivers (team presets)', onclick: fullGrid }, 'Full grid'),
        h('button', { class: 'btn small', onclick: () => { S.raceEntries = []; renderEntries(); } }, 'Clear'))),
    h('div', { id: 'entries' }),
    h('div', { class: 'opt-grid' },
      h('label', { class: 'field', title: 'Leave empty for the official race distance' }, h('span', null, 'Laps'),
        h('input', { id: 'race-laps', type: 'number', min: 1, max: 100, step: 1, placeholder: 'auto', onchange: (e) => { opts.laps = e.target.value; } })),
      h('label', { class: 'field' }, h('span', null, 'Grid'),
        h('select', { onchange: (e) => { opts.grid = e.target.value; } }, h('option', { value: 'quali' }, 'Simulated quali'), h('option', { value: 'list' }, 'List order'))),
      h('label', { class: 'field', title: 'Number of Monte Carlo races (0 = one race with replay only)' }, h('span', null, 'Monte Carlo'),
        h('input', { type: 'number', min: 0, max: 2000, step: 10, value: 0, onchange: (e) => { opts.monte_carlo = Math.max(0, Math.round(+e.target.value || 0)); } })),
      h('label', { class: 'field' }, h('span', null, 'Seed'),
        h('input', { type: 'number', min: 0, step: 1, value: 1, onchange: (e) => { opts.seed = Math.round(+e.target.value || 0); } })),
      h('label', { class: 'tb-check' }, h('input', { type: 'checkbox', checked: true, onchange: (e) => { opts.safety_car = e.target.checked; } }), 'Safety car'),
      h('label', { class: 'tb-check' }, h('input', { type: 'checkbox', checked: true, onchange: (e) => { opts.dnf = e.target.checked; } }), 'DNFs')),
    runButton('Run race', runRace, { id: 'run-race' }));
  renderEntries();
}

function addCurrent() {
  if (!S.team || !S.driver) { toast('Select a team and driver first.', 'warn'); return; }
  if (S.raceEntries.length >= MAX_ENTRIES) { toast(`The grid is limited to ${MAX_ENTRIES} cars.`, 'warn'); return; }
  if (S.raceEntries.some((e) => e.driver === S.driver)) { toast(`${driverById(S.driver)?.name || S.driver} is already entered.`, 'warn', 3500); return; }
  S.raceEntries.push({ team: S.team, driver: S.driver, overrides: { ...S.overrides }, driver_overrides: { ...S.driverOverrides }, strategy: 'auto' });
  renderEntries();
}
function fullGrid() {
  S.raceEntries = [];
  for (const t of S.teams) for (const d of (t.drivers || []).slice(0, 2)) {
    if (S.raceEntries.length < MAX_ENTRIES) S.raceEntries.push({ team: t.id, driver: d, overrides: {}, driver_overrides: {}, strategy: 'auto' });
  }
  if (!S.raceEntries.length) toast('Team list unavailable, cannot build the full grid.', 'warn');
  renderEntries();
}

function renderEntries() {
  const box = $('#entries');
  if (!box) return;
  clear(box);
  $('#entry-count').textContent = `Entries ${S.raceEntries.length}/${MAX_ENTRIES}`;
  if (!S.raceEntries.length) { box.append(h('p', { class: 'empty' }, 'No cars entered. Use Full grid, or pick a team + driver above and press Add current.')); return; }
  S.raceEntries.forEach((e, i) => {
    const n = Object.keys(e.overrides || {}).length;
    const manual = e.strategy !== 'auto';
    const row = h('div', { class: 'entry' },
      h('div', { class: 'list-row' },
        swatch(teamColor(e.team)),
        h('b', { class: 'mono' }, driverCode(e.driver)),
        h('span', { class: 'dim grow ellip', title: n ? Object.entries(e.overrides).map(([k, v]) => `${k} = ${v}`).join('\n') : 'Team preset' }, teamName(e.team), n ? ` · ${n} chg` : ''),
        h('select', { class: 'small', 'aria-label': 'Strategy', onchange: (ev) => { e.strategy = ev.target.value === 'auto' ? 'auto' : { start_compound: 'C3', stops: [{ lap: Math.max(2, Math.round((raceLaps() || 50) / 2)), compound: 'C2' }] }; renderEntries(); } },
          h('option', { value: 'auto', selected: !manual }, 'Auto'), h('option', { value: 'manual', selected: manual }, 'Manual')),
        h('button', { class: 'ibtn', title: 'Move up (matters for list-order grid)', disabled: i === 0, onclick: () => { S.raceEntries.splice(i - 1, 0, S.raceEntries.splice(i, 1)[0]); renderEntries(); } }, '↑'),
        h('button', { class: 'ibtn', title: 'Remove', onclick: () => { S.raceEntries.splice(i, 1); renderEntries(); } }, '×')));
    if (manual) {
      const st = e.strategy;
      const csel = (val, set) => h('select', { class: 'small', 'aria-label': 'Compound', onchange: (ev) => set(ev.target.value) }, COMPOUNDS.map((c) => h('option', { value: c, selected: c === val }, c)));
      row.append(h('div', { class: 'strat' },
        h('span', { class: 'dim' }, 'Start'), csel(st.start_compound, (v) => { st.start_compound = v; }),
        st.stops.map((sp, k) => h('span', { class: 'stop' }, h('span', { class: 'dim' }, 'L'),
          h('input', { type: 'number', min: 1, max: 100, step: 1, value: sp.lap, class: 'narrow', 'aria-label': 'Pit lap', onchange: (ev) => { sp.lap = Math.max(1, Math.round(+ev.target.value || 1)); } }),
          csel(sp.compound, (v) => { sp.compound = v; }),
          h('button', { class: 'ibtn', title: 'Remove stop', onclick: () => { st.stops.splice(k, 1); renderEntries(); } }, '×'))),
        h('button', { class: 'btn small', disabled: st.stops.length >= 4, onclick: () => { const last = st.stops[st.stops.length - 1]; st.stops.push({ lap: (last ? last.lap : 10) + 12, compound: 'C3' }); renderEntries(); } }, '+ stop')));
    }
    box.append(row);
  });
}
const raceLaps = () => +opts.laps || trackById(S.track)?.laps || 0;

// ---------------------------------------------------------------- run
async function runRace(progress) {
  if (S.raceEntries.length < 1) throw new Error('Add at least one car to the entry list.');
  const t0 = performance.now();
  const body = {
    track: S.track, laps: opts.laps ? Math.round(+opts.laps) : null,
    entries: S.raceEntries.map((e) => ({ team: e.team, driver: e.driver, overrides: e.overrides, driver_overrides: e.driver_overrides,
      strategy: e.strategy === 'auto' ? 'auto' : { start_compound: e.strategy.start_compound, stops: [...e.strategy.stops].sort((a, b) => a.lap - b.lap) } })),
    grid: opts.grid === 'list' ? S.raceEntries.map((e) => e.driver) : 'quali',
    options: { safety_car: opts.safety_car, dnf: opts.dnf, seed: opts.seed }, monte_carlo: opts.monte_carlo,
  };
  progress(0, 'Submitting…');
  const { job } = await api('/api/race', body);
  if (job == null) throw new Error('/api/race did not return a job id');
  const res = await pollJob(job, progress);
  if (!res || !res.classification) throw new Error('Race job finished without a classification');
  S.race = res;
  if (S.mode === 'race') enterRace();
  return `${res.laps} laps, ${(res.entries || []).length} cars · ${((performance.now() - t0) / 1000).toFixed(1)} s`;
}

// ---------------------------------------------------------------- results
const teamOf = (code) => (S.race.entries || []).find((e) => e.code === code)?.team;
function seriesStyle() {             // team colour, second car of a team dashed
  const seen = {}, out = {};
  for (const e of S.race.entries || []) { out[e.code] = { color: teamColor(e.team), dash: !!seen[e.team] }; seen[e.team] = true; }
  return out;
}
function finishOrder() { return (S.race.classification || []).map((c) => c.code); }

export function enterRace() {
  const res = S.race;
  if (!res || res.track !== S.track) {
    setDock([{ id: 'cls', label: 'Classification', el: clear(el.cls) }], 'cls');
    el.cls.append(h('p', { class: 'empty' }, 'Build an entry list and press Run race. The classification, lap chart, gaps, tyre strategy and Monte Carlo table appear here; the race replays in 3D.'));
    showStatic(); renderBoard(true);
    return;
  }
  setDock([
    { id: 'cls', label: 'Classification', el: el.cls },
    { id: 'lapc', label: 'Lap chart', el: el.lapc, onShow: drawCharts },
    { id: 'gap', label: 'Gap to leader', el: el.gap, onShow: drawCharts },
    { id: 'strat', label: 'Tyre strategy', el: el.strat, onShow: drawCharts },
    { id: 'mc', label: 'Monte Carlo', el: el.mc },
    { id: 'ev', label: 'Overtakes & events', el: el.ev },
  ], 'cls');
  renderClassification(); renderMC(); renderEvents();
  playRace(res);
  renderBoard(true);
}

function renderClassification() {
  const res = S.race, fl = res.fastest_lap;
  clear(el.cls).append(
    fl ? h('p', { class: 'hint' }, `Fastest lap: ${fl.code} ${fmtLap(fl.time)} on lap ${fl.lap}. Simulated result for comparison, not a prediction.`) : null,
    table([
      { label: 'Pos', cls: 'r', get: (c) => c.pos },
      { label: 'Driver', get: (c) => h('span', { class: 'nowrap' }, swatch(teamColor(c.team)), h('b', null, c.code), ' ', driverById(c.driver)?.name || c.driver) },
      { label: 'Team', get: (c) => teamName(c.team) },
      { label: 'Grid', cls: 'r', get: (c) => c.grid },
      { label: '+/−', cls: 'r', get: (c) => { const d = c.grid - c.pos; return c.status === 'DNF' ? '' : d > 0 ? h('span', { class: 'c-ok' }, `+${d}`) : d < 0 ? h('span', { class: 'c-bad' }, `−${-d}`) : '0'; } },
      { label: 'Laps', cls: 'r', get: (c) => c.laps },
      { label: 'Time / gap', cls: 'r mono', get: (c) => (c.status === 'DNF' ? 'DNF' : c.pos === 1 ? fmtRace(c.time) : (c.status && c.status !== 'FIN' ? c.status : fmtGap(c.gap))) },
      { label: 'Stops', cls: 'r', get: (c) => c.stops },
      { label: 'Best lap', cls: 'r mono', get: (c) => (c.best_lap ? fmtLap(c.best_lap) : '') },
      { label: 'on', cls: 'r', get: (c) => c.best_lap_n ?? '' },
    ], res.classification || [], { cls: 'clickable', rowProps: (c) => ({ onclick: () => setFollow(c.code), title: 'Follow this car in the replay' }) }));
}

function renderMC() {
  const mc = S.race.monte_carlo;
  clear(el.mc);
  if (!mc || !mc.table) { el.mc.append(h('p', { class: 'empty' }, 'Monte Carlo was not run. Set Monte Carlo to e.g. 200 in the race options and run again to get win / podium probabilities.')); return; }
  const pct = (v) => (v == null ? '' : `${(v <= 1.0001 ? v * 100 : v).toFixed(1)} %`);
  el.mc.append(h('p', { class: 'hint' }, `${mc.runs} simulated races with randomised events. Probabilities describe the model, not the real championship.`),
    table([
      { label: 'Driver', get: (r) => h('span', { class: 'nowrap' }, swatch(teamColor(r.team)), h('b', null, r.code)) },
      { label: 'Team', get: (r) => teamName(r.team) },
      { label: 'Win', cls: 'r mono', get: (r) => pct(r.win) },
      { label: 'Podium', cls: 'r mono', get: (r) => pct(r.podium) },
      { label: 'Avg points', cls: 'r mono', get: (r) => num(r.points, 2) },
      { label: 'Avg finish', cls: 'r mono', get: (r) => num(r.avg_finish, 2) },
      { label: 'Finish distribution (P1 → last)', get: (r) => { const d = r.dist || [], mx = Math.max(1e-9, ...d); return h('span', { class: 'dist', title: d.map((v, i) => `P${i + 1}: ${(v <= 1.0001 ? v * 100 : v).toFixed(1)}%`).join('  ') }, d.map((v) => h('i', { style: { height: `${Math.max(1, (v / mx) * 16)}px`, background: teamColor(r.team) } }))); } },
    ], mc.table));
}

function renderEvents() {
  const res = S.race;
  const rows = [
    ...(res.overtakes || []).map((o) => ({ t: o.t, lap: o.lap, kind: 'PASS', text: `${o.by} passes ${o.on}`, code: o.by })),
    ...(res.events || []).map((e) => ({ t: e.t, lap: e.lap, kind: e.kind, text: e.text, code: e.code })),
  ].sort((a, b) => a.t - b.t);
  const n = (res.overtakes || []).length;
  clear(el.ev).append(h('p', { class: 'hint' }, `${n} on-track overtake${n === 1 ? '' : 's'}. Click a row to jump the replay there.`),
    table([
      { label: 'Lap', cls: 'r', get: (r) => r.lap },
      { label: 'Time', cls: 'r mono', get: (r) => fmtRace(r.t) },
      { label: 'Type', get: (r) => h('span', { class: `ev ev-${String(r.kind).toLowerCase()}` }, r.kind) },
      { label: 'Event', get: (r) => r.text },
    ], rows, { cls: 'clickable', empty: 'No overtakes or events.', rowProps: (r) => ({ onclick: () => { setTime(Math.max(0, r.t - 3)); if (r.code) setFollow(r.code); } }) }));
}

function leaderLap() {
  if (R.kind !== 'race' || !R.order.length) return null;
  const p = R.poses.get(R.order[0]);
  return p ? Math.min(S.race.laps, p.hud.d / R.race.len) : null;
}

function drawCharts() {
  const res = S.race;
  if (!res) return;
  const tab = activeDock(), style = seriesStyle(), order = finishOrder(), cur = leaderLap();
  if (tab === 'lapc') {
    lapChartGeom = drawLineChart(el.lapc.firstChild, {
      series: order.map((code) => ({ label: code, y: res.lap_chart?.[code] || [], ...style[code] })),
      invertY: true, yInt: true, yMin: 1, yMax: Math.max(2, (res.entries || []).length), xLabel: 'lap', yLabel: 'Position', cursorX: cur,
    });
  } else if (tab === 'gap') {
    const all = order.flatMap((c) => res.gaps?.[c] || []).filter((v) => isFinite(v)).sort((a, b) => a - b);
    const p90 = all.length ? all[Math.floor(all.length * 0.97)] : 60;
    drawLineChart(el.gap.firstChild, {
      series: order.map((code) => ({ label: code, y: res.gaps?.[code] || [], ...style[code] })),
      invertY: true, yMin: 0, yClamp: Math.max(10, p90 * 1.1), x0: (res.gaps?.[order[0]]?.length === res.laps ? 1 : 0), xLabel: 'lap', yLabel: 'Gap to leader (s)', cursorX: cur,
    });
  } else if (tab === 'strat') {
    drawStrategy(el.strat.firstChild, {
      rows: order.map((code) => ({ label: code, color: style[code]?.color || '#8a96a3', stints: (res.strategy?.[code] || []).map((s) => ({ ...s, label: s.label || compoundLabel(s.compound) })) })),
      laps: res.laps, compoundColors: COMPOUND_COLORS, cursorLap: cur,
    });
  }
}
el.lapc.firstChild.addEventListener('click', (e) => {
  if (!lapChartGeom || R.kind !== 'race') return;
  const r = e.target.getBoundingClientRect();
  const lap = ((e.clientX - r.left - lapChartGeom.pad.l) / lapChartGeom.pw) * lapChartGeom.nx;
  const t = timeAtLap(Math.max(0, lap));
  if (t != null) setTime(t);
});
for (const k of ['lapc', 'gap', 'strat']) new ResizeObserver(() => { if (S.mode === 'race' && activeDock() === k) drawCharts(); }).observe(el[k]);

function timeAtLap(lap) {
  const rc = R.race;
  if (!rc) return null;
  if (!rc.maxD) {
    const m = rc.T.length; rc.maxD = new Float64Array(m);
    for (const c of R.cars) for (let i = 0; i < m; i++) if (c.d[i] > rc.maxD[i]) rc.maxD[i] = c.d[i];
    for (let i = 1; i < m; i++) if (rc.maxD[i] < rc.maxD[i - 1]) rc.maxD[i] = rc.maxD[i - 1];
  }
  const k = lowerBound(rc.maxD, lap * rc.len);
  return rc.T[Math.min(rc.T.length - 1, k)];
}

// ---------------------------------------------------------------- live leaderboard (right column)
function stintLabel(car, lapIdx) {
  const st = car.stints;
  for (const s of st) if (lapIdx >= s.start_lap && lapIdx < s.end_lap) return s.label || compoundLabel(s.compound);
  return st.length ? (st[st.length - 1].label || compoundLabel(st[st.length - 1].compound)) : null;
}

function renderBoard(force) {
  if (S.mode !== 'race') return;
  const box = $('#right');
  const res = S.race;
  if (!res || res.track !== S.track || R.kind !== 'race') {
    if (force) clear(box).append(card('Race', h('p', { class: 'empty' }, 'The live leaderboard appears here during the race replay.')));
    return;
  }
  const rc = R.race, lead = R.poses.get(R.order[0]);
  const vref = rc.len / Math.max(40, (res.fastest_lap?.time || 90));
  const byId = Object.fromEntries(R.cars.map((c) => [c.id, c]));
  const running = new Set(R.order);
  const retired = R.cars.filter((c) => !running.has(c.id));
  const rows = [...R.order.map((id) => byId[id]), ...retired];
  const tNow = R.t;
  const recent = [...(res.events || []), ...(res.overtakes || []).map((o) => ({ t: o.t, lap: o.lap, kind: 'PASS', text: `${o.by} passes ${o.on}` }))]
    .filter((e) => e.t <= tNow && e.t > tNow - 45).sort((a, b) => b.t - a.t).slice(0, 5);
  const lapNow = lead ? Math.min(res.laps, lead.hud.lap) : 0;
  clear(box).append(
    h('section', { class: 'card' },
      h('h3', null, 'Leaderboard ', h('span', { class: 'dim' }, `lap ${lapNow}/${res.laps}`)),
      h('table', { class: 'data board clickable' }, h('tbody', null, rows.map((c, i) => {
        const p = R.poses.get(c.id), out = !running.has(c.id);
        const gapM = lead && p ? lead.hud.d - p.hud.d : 0;
        const lapsDown = Math.floor(gapM / rc.len);
        const gap = out ? 'OUT' : i === 0 ? 'leader' : lapsDown >= 1 ? `+${lapsDown} lap${lapsDown > 1 ? 's' : ''}` : `+${(gapM / vref).toFixed(1)}`;
        const comp = p ? stintLabel(c, p.hud.lap - 1) : null;
        return h('tr', { class: (c.id === R.targetId ? 'sel ' : '') + (out ? 'out' : ''), onclick: () => setFollow(c.id), title: 'Follow this car' },
          h('td', { class: 'r dim' }, out ? '' : i + 1), h('td', null, swatch(teamColor(c.team)), h('b', null, c.code)),
          h('td', { class: 'r mono' }, gap),
          h('td', null, comp ? h('i', { class: 'tyre', style: { borderColor: COMPOUND_COLORS[comp] || '#8a96a3' }, title: comp }) : null),
          h('td', { class: 'dim' }, p && p.hud.pit && !out ? 'PIT' : ''));
      }))),
      h('p', { class: 'hint' }, 'Gaps are estimated from track distance at average race speed.')),
    recent.length ? card('Recent', h('ul', { class: 'ticker' }, recent.map((e) => h('li', null, h('span', { class: `ev ev-${String(e.kind).toLowerCase()}` }, e.kind), ` L${e.lap} ${e.text || ''}`)))) : null,
    card('Race', kv([['Track', trackById(res.track)?.name || res.track], ['Distance', `${res.laps} laps`], ['Cars', (res.entries || []).length],
      ['Fastest lap', res.fastest_lap ? `${res.fastest_lap.code} ${fmtLap(res.fastest_lap.time)}` : '–'], ['Winner', res.classification?.[0]?.code || '–']])));
}

onTick((r) => {
  if (r.kind !== 'race' || S.mode !== 'race') return;
  const now = performance.now();
  if (now - lastBoard > 300) { lastBoard = now; renderBoard(false); }
  if (now - lastDraw > 250) { lastDraw = now; drawCharts(); }
});
on('follow', () => { if (S.mode === 'race') { lastBoard = 0; } });
