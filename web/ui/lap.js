// Single-lap and Compare modes: run, telemetry stack, results column, corner table, 3D replay.
import { $, h, clear, api, toast, fmtLap, fmtGap, num } from './util.js';
import { S, emit, on, currentConfig, teamColor, seriesColors, teamName, driverCode, trackById, paramValue } from './store.js';
import { TraceStack } from '../charts/charts.js';
import { runButton, setDock, table, kv, swatch, card } from './widgets.js';
import { R, onTick, playLap, setTime, timeAtS, showStatic } from './stage.js';

let stack = null;
const teleEl = h('div', { class: 'tstack' });
const cornersEl = h('div', { class: 'scroll pad' });
let lapStale = false, cmpStale = false;
const lapOpts = { mode: 'quali', tyre_age: 0 };

export function getStack() {
  if (!stack) {
    stack = new TraceStack(teleEl, {
      onCursor: (s, commit) => {
        stack.setCursor(s);
        if (R.kind === 'lap' && (commit || !R.playing)) { const t = timeAtS(s); if (t != null) setTime(t); }
      },
    });
    onTick((r) => { if (r.kind === 'lap' && !teleEl.hidden) { const p = r.poses.get('car0'); if (p) stack.setCursor(p.hud.s); } });
  }
  return stack;
}

// ---------------------------------------------------------------- telemetry rows
function rowsFor(items, delta) {
  const multi = items.length > 1;
  const mk = (key, o = {}) => items.map((it) => ({ x: it.result.telemetry.s, y: it.result.telemetry[key] || [], color: o.color || it.color, dash: o.dash, step: o.step, alpha: o.alpha, tag: o.tag })).filter((s) => s.y.length);
  const rows = [{ id: 'v', label: 'Speed', unit: 'km/h', weight: 2.2, digits: 0, series: mk('v') }];
  if (delta && delta.series) {
    rows.push({ id: 'dt', label: 'Δ time vs ' + (items[0].label || 'ref'), unit: 's', weight: 1.3, digits: 3, zero: true,
      series: delta.series.filter((d) => d.index !== 0).map((d) => ({ x: delta.s, y: d.dt, color: items[d.index]?.color || '#8a96a3' })) });
  }
  rows.push(
    { id: 'tb', label: multi ? 'Throttle / brake (dashed)' : 'Throttle / brake', unit: '', weight: 1.1, min: -0.04, max: 1.04, digits: 2,
      series: multi ? [...mk('throttle'), ...mk('brake', { dash: true })] : [...mk('throttle', { color: '#4cb782', tag: 'T' }), ...mk('brake', { color: '#e5484d', tag: 'B' })] },
    { id: 'gear', label: 'Gear', unit: '', weight: 0.8, min: 0.5, max: 8.5, digits: 0, series: mk('gear', { step: true }) },
    { id: 'ay', label: 'Lateral g', unit: 'g', weight: 1, digits: 2, zero: true, series: mk('ay') },
    { id: 'soc', label: 'Battery SoC', unit: 'MJ', weight: 0.9, digits: 2, series: mk('soc') },
    { id: 'mode', label: 'Aero mode', unit: '', kind: 'band', weight: multi ? 0.25 + 0.14 * items.length : 0.38, on: 'X (straight)', off: 'Z (corner)', series: mk('mode', { step: true }) },
    { id: 'tyre', label: 'Tyre temp front / rear (dashed)', unit: '°C', weight: 1, digits: 0, series: [...mk('tyre_temp_f', { tag: multi ? '' : 'F' }), ...mk('tyre_temp_r', { dash: true, tag: multi ? '' : 'R' })] },
  );
  return rows;
}

function showTelemetry(items, delta) {
  const ref = items[0].result;
  const T = ref.telemetry;
  const xMax = S.trackGeom?.length || T.s[T.s.length - 1];
  getStack().setData({ rows: rowsFor(items, delta), xMax, corners: (ref.corners || []).map((c) => ({ s: c.s_apex, num: c.num })) });
}

function cornerTable(res) {
  clear(cornersEl).append(table([
    { label: 'Corner', get: (c) => `T${c.num}` },
    { label: 'Dir', get: (c) => c.dir },
    { label: 'Apex s (m)', cls: 'r', get: (c) => num(c.s_apex, 0) },
    { label: 'Min speed (km/h)', cls: 'r', get: (c) => num(c.v_min, 1) },
    { label: 'Brake point s (m)', cls: 'r', get: (c) => (c.brake_s == null ? 'no braking' : num(c.brake_s, 0)) },
    { label: 'Brake to apex (m)', cls: 'r', get: (c) => { if (c.brake_s == null) return ''; const len = S.trackGeom?.length || 0; let dlt = c.s_apex - c.brake_s; if (dlt < 0 && len) dlt += len; return num(dlt, 0); } },
  ], res.corners || [], { empty: 'No corner data in this result.', cls: 'clickable', rowProps: (c) => ({ onclick: () => { const t = timeAtS(c.brake_s ?? c.s_apex); if (t != null) setTime(t); }, title: 'Jump the replay to this corner' }) }));
}

function legendFor(items) {
  return h('div', { class: 'legend' }, items.map((it) => h('span', { class: 'legend-item' }, swatch(it.color), it.label)));
}

// ---------------------------------------------------------------- flags / summary
export function flagsCard(res) {
  const flags = res.flags || [];
  return card('Suitability', flags.length
    ? h('ul', { class: 'flags' }, flags.map((f) => h('li', { class: `flag ${f.level}` }, h('i', { class: 'dot' }), h('div', null, h('b', null, f.title), h('span', null, f.text)))))
    : h('p', { class: 'empty' }, 'No suitability flags returned.'));
}

function summaryCard(res) {
  const s = res.summary || {};
  const sec = s.sectors || [];
  return card('Lap summary', kv([
    ['Top speed', `${num(s.vmax, 1)} km/h`], ['Min speed', `${num(s.vmin, 1)} km/h`], ['Average', `${num(s.v_avg, 1)} km/h`],
    ['Sectors', sec.length ? sec.map((x) => num(x, 3)).join(' / ') : '–'],
    ['Deployed', `${num(s.e_deploy_mj, 2)} MJ`], ['Harvested', `${num(s.e_harvest_mj, 2)} MJ`], ['Energy budget', `${num(s.e_budget_mj, 2)} MJ`],
    ['X-mode share', `${num(s.x_mode_pct, 1)} %`],
    ['Mass (incl. fuel)', `${num(s.mass, 1)} kg`], ['Fuel', `${num(s.fuel, 1)} kg`],
    ['Tyre wear / lap', `F ${num((s.wear_per_lap_f ?? NaN) * 100, 2)} %  R ${num((s.wear_per_lap_r ?? NaN) * 100, 2)} %`, 'Share of tyre life used per lap (model estimate)'],
    ['Max lateral', `${num(s.max_lat_g, 2)} g`], ['Max braking', `${num(s.max_brake_g, 2)} g`], ['Upshifts', num(s.upshifts, 0)],
  ]));
}

function warningsCard(res) {
  const w = res.warnings || [];
  if (!w.length) return null;
  return card('Warnings', h('ul', { class: 'warns' }, w.map((x) => h('li', null, x))));
}

// ---------------------------------------------------------------- single lap
export function buildLapPanel(el) {
  el.append(
    h('div', { class: 'row gap' },
      h('label', { class: 'field grow' }, h('span', null, 'Session'),
        h('select', { onchange: (e) => { lapOpts.mode = e.target.value; } }, h('option', { value: 'quali' }, 'Qualifying lap'), h('option', { value: 'race_pace' }, 'Race pace lap'))),
      h('label', { class: 'field', title: 'Laps already done on this tyre set' }, h('span', null, 'Tyre age'),
        h('input', { type: 'number', min: 0, max: 60, step: 1, value: 0, class: 'narrow', onchange: (e) => { lapOpts.tyre_age = Math.max(0, Math.round(+e.target.value || 0)); } }))),
    runButton('Run lap', runLap, { id: 'run-lap' }));
}

async function runLap() {
  const t0 = performance.now();
  const res = await api('/api/simulate', currentConfig({ mode: lapOpts.mode, tyre_age: lapOpts.tyre_age }));
  if (!res || !res.telemetry || !res.line) throw new Error('/api/simulate returned no telemetry');
  S.lap = res; lapStale = false;
  if (S.mode === 'lap') enterLap();
  emit('result:lap');
  return `solver ${num(res.run_time_s, 2)} s · total ${((performance.now() - t0) / 1000).toFixed(2)} s`;
}

function lapItem() {
  const res = S.lap;
  return { result: res, label: `${driverCode(res.driver)} ${teamName(res.team)}`, color: teamColor(res.team), team: res.team, driver: res.driver, compound: S.lapCompound };
}

export function enterLap() {
  const res = S.lap;
  const tabs = [{ id: 'tele', label: 'Telemetry', el: teleEl, onShow: () => getStack().draw() }, { id: 'corners', label: 'Corners', el: cornersEl }];
  getStack();
  if (res && res.track === S.track) {
    S.lapCompound = S.lapCompound || paramValue('compound');
    const it = lapItem();
    setDock(tabs, 'tele', legendFor([it]));
    showTelemetry([it]); cornerTable(res);
    playLap([it]);
  } else {
    setDock(tabs, 'tele');
    stack.setData(null); clear(cornersEl).append(h('p', { class: 'empty' }, 'Run a lap to list its corners.'));
    showStatic();
  }
  renderLapRight();
}

function renderLapRight() {
  const box = clear($('#right'));
  const res = S.lap;
  if (!res || res.track !== S.track) {
    box.append(card('Single lap', h('p', { class: 'empty' }, 'Pick track, team and driver, adjust the setup, then press Run lap. The lap replays in 3D and the traces below share one cursor with the car.')));
    return;
  }
  box.append(
    h('section', { class: 'card hero' },
      h('div', { class: 'hero-label' }, `${trackById(res.track)?.name || res.track} · ${res.mode === 'quali' ? 'qualifying' : 'race pace'}`),
      h('div', { class: 'hero-time mono' }, res.lap_time_str || fmtLap(res.lap_time)),
      h('div', { class: 'hero-sub' }, swatch(teamColor(res.team)), `${teamName(res.team)} · ${driverCode(res.driver)}`,
        lapStale ? h('span', { class: 'tag warn', title: 'Selections or parameters changed after this lap was simulated' }, 'setup changed since run') : null),
      h('div', { class: 'hero-note' }, 'Model lap for trends and comparisons, not a real lap-time prediction.')),
    flagsCard(res), warningsCard(res), summaryCard(res));
}

// ---------------------------------------------------------------- compare
export function buildComparePanel(el) {
  const label = h('input', { type: 'text', placeholder: 'Label (optional)', class: 'grow', 'aria-label': 'Config label', maxlength: 40 });
  el.append(
    h('p', { class: 'hint' }, 'Snapshot the current team, driver and setup as a config. Change something, snapshot again, then run. All configs run on the selected track; the first one is the reference.'),
    h('div', { class: 'row gap' }, label,
      h('button', { class: 'btn', onclick: () => { addConfig(label.value); label.value = ''; } }, 'Add current')),
    h('div', { id: 'cmp-list' }),
    runButton('Run compare', runCompare, { id: 'run-compare' }));
  renderCmpList();
}

function addConfig(label) {
  if (!S.team || !S.driver) { toast('Select a team and driver first.', 'warn'); return; }
  const n = Object.keys(S.overrides).length;
  const auto = `${driverCode(S.driver)} ${teamName(S.team)}${n ? ` +${n}` : ''}`;
  S.compareConfigs.push({ ...currentConfig({ mode: lapOpts.mode, tyre_age: lapOpts.tyre_age }), label: (label || '').trim() || auto, compound: paramValue('compound') });
  cmpStale = true; renderCmpList();
}

function renderCmpList() {
  const box = $('#cmp-list');
  if (!box) return;
  clear(box);
  if (!S.compareConfigs.length) { box.append(h('p', { class: 'empty' }, 'No configs yet. Add at least two.')); return; }
  const colors = seriesColors(S.compareConfigs.map((c) => c.team));
  S.compareConfigs.forEach((c, i) => {
    const n = Object.keys(c.overrides || {}).length;
    box.append(h('div', { class: 'list-row' },
      swatch(colors[i]),
      h('input', { type: 'text', class: 'grow bare', value: c.label, 'aria-label': 'Config label', onchange: (e) => { c.label = e.target.value.trim() || c.label; e.target.value = c.label; } }),
      h('span', { class: 'dim small', title: Object.entries(c.overrides || {}).map(([k, v]) => `${k} = ${v}`).join('\n') || 'Team preset, no overrides' }, i === 0 ? 'ref' : '', n ? ` ${n} chg` : ''),
      h('button', { class: 'ibtn', title: 'Load this config into the editor', onclick: () => emit('apply-setup', { team: c.team, driver: c.driver, values: c.overrides, driver_values: c.driver_overrides, keepTrack: true }) }, '↑'),
      h('button', { class: 'ibtn', title: 'Remove', onclick: () => { S.compareConfigs.splice(i, 1); cmpStale = true; renderCmpList(); } }, '×')));
  });
}

async function runCompare() {
  if (S.compareConfigs.length < 2) throw new Error('Add at least two configs to compare.');
  const t0 = performance.now();
  const configs = S.compareConfigs.map(({ compound, ...c }) => ({ ...c, track: S.track }));
  const res = await api('/api/compare', { configs });
  if (!res || !Array.isArray(res.results) || res.results.length < 2) throw new Error('/api/compare returned no results');
  S.compare = { ...res, track: S.track, labels: configs.map((c) => c.label), teams: configs.map((c) => c.team), drivers: configs.map((c) => c.driver), compounds: S.compareConfigs.map((c) => c.compound) };
  cmpStale = false;
  if (S.mode === 'compare') enterCompare();
  return `${res.results.length} laps · ${((performance.now() - t0) / 1000).toFixed(2)} s`;
}

function cmpItems() {
  const c = S.compare, colors = seriesColors(c.teams);
  return c.results.map((r, i) => ({ result: r, label: c.labels[i] || r.label || `Config ${i + 1}`, color: colors[i], team: c.teams[i], driver: c.drivers[i], compound: c.compounds[i] }));
}

export function enterCompare() {
  const c = S.compare;
  const tabs = [{ id: 'tele', label: 'Telemetry + Δ time', el: teleEl, onShow: () => getStack().draw() }, { id: 'corners', label: 'Corners (reference)', el: cornersEl }];
  getStack();
  if (c && c.track === S.track) {
    const items = cmpItems();
    setDock(tabs, 'tele', legendFor(items));
    showTelemetry(items, c.delta); cornerTable(c.results[0]);
    playLap(items);
  } else {
    setDock(tabs, 'tele');
    stack.setData(null); clear(cornersEl).append(h('p', { class: 'empty' }, 'Run a comparison first.'));
    showStatic();
  }
  renderCmpRight();
}

function renderCmpRight() {
  const box = clear($('#right'));
  const c = S.compare;
  if (!c || c.track !== S.track) { box.append(card('Compare', h('p', { class: 'empty' }, 'Add two or more configs and press Run compare. Traces are overlaid, the Δ-time row shows where time is gained or lost against the reference, and each config gets a ghost car.'))); return; }
  const items = cmpItems();
  const ranking = c.ranking || c.results.map((r, i) => ({ index: i, label: items[i].label, lap_time: r.lap_time, gap: 0 }));
  box.append(
    card('Ranking',
      cmpStale ? h('p', { class: 'tag warn' }, 'config list changed since run') : null,
      table([
        { label: '#', cls: 'r', get: (r, i) => i + 1 },
        { label: 'Config', get: (r) => h('span', { class: 'nowrap' }, swatch(items[r.index]?.color), items[r.index]?.label || r.label) },
        { label: 'Lap', cls: 'r mono', get: (r) => fmtLap(r.lap_time) },
        { label: 'Gap', cls: 'r mono', get: (r, i) => (i === 0 ? '' : fmtGap(r.gap)) },
      ], ranking)),
    card('Per config', table([
      { label: 'Config', get: (it) => h('span', { class: 'nowrap' }, swatch(it.color), it.label) },
      { label: 'Vmax', cls: 'r', get: (it) => num(it.result.summary?.vmax, 0) },
      { label: 'Dep MJ', cls: 'r', get: (it) => num(it.result.summary?.e_deploy_mj, 2) },
      { label: 'Flags', cls: 'r', get: (it) => { const f = it.result.flags || []; const b = f.filter((x) => x.level === 'bad').length, w = f.filter((x) => x.level === 'warn').length; return h('span', { title: f.filter((x) => x.level !== 'ok').map((x) => `${x.title}: ${x.text}`).join('\n') }, b ? h('span', { class: 'c-bad' }, `${b} bad `) : null, w ? h('span', { class: 'c-warn' }, `${w} warn`) : (b ? null : 'ok')); } },
    ], items)),
    flagsCard(c.results[0]),
    card(null, h('p', { class: 'hint' }, 'Ghost cars share one clock: all start together at the line. Use Follow in the scene bar to switch car.')));
}

on('params', () => { if (S.lap && !lapStale) { lapStale = true; if (S.mode === 'lap') renderLapRight(); } });
on('params', (key) => { if (key === 'compound' || key === undefined) S.lapCompound = null; });
