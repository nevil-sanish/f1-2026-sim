// Left-panel setup editor: grouped parameter sliders with provenance chips, reset, lock, diff filter,
// preset loader, driver skill sliders, setup save/load/delete and the printable setup sheet.
import { $, h, clear, toast, api, tryApi, decimals, fmtDate } from './util.js';
import { S, emit, on, teamName, teamById, trackById, driverById } from './store.js';

const PROV = {
  regulation: { short: 'REG', title: 'Regulation: value comes from the 2026 FIA rules.' },
  'public-derived': { short: 'PUB', title: 'Public-derived: fitted from public timing / telemetry data.' },
  estimate: { short: 'EST', title: 'Estimate: engineering guess, not published by any team.' },
};
const openGroups = new Set(['Aerodynamics']);
let filterText = '';

export function provChip(prov) {
  const p = PROV[prov] || { short: '?', title: prov || 'unknown provenance' };
  return h('span', { class: `prov prov-${(prov || 'x').replace(/[^a-z]/g, '')}`, title: p.title }, p.short);
}
export function provLegend() {
  return h('div', { class: 'legend' }, Object.entries(PROV).map(([k, v]) =>
    h('span', { class: 'legend-item', title: v.title }, provChip(k), k)));
}

const base = (p) => (p.preset ?? p.default);
const cur = (p) => (p.key in S.overrides ? S.overrides[p.key] : base(p));
const same = (a, b) => (typeof a === 'number' && typeof b === 'number' ? Math.abs(a - b) < 1e-9 : String(a) === String(b));
function fmtVal(p, v) {
  if (p.kind === 'bool') return +v ? 'on' : 'off';
  if (p.kind === 'choice' || typeof v !== 'number') return String(v);
  return v.toFixed(decimals(p.step));
}

export function setParam(key, value, silent) {
  const p = S.schema?.params.find((q) => q.key === key);
  if (!p) return;
  if (p.kind === 'float' || p.kind === undefined) {
    value = Math.min(p.max, Math.max(p.min, +value));
    if (!isFinite(value)) return;
    value = +value.toFixed(Math.max(decimals(p.step), 6));
  } else if (p.kind === 'bool') value = +value ? 1 : 0;
  if (same(value, base(p))) delete S.overrides[key]; else S.overrides[key] = value;
  if (!silent) emit('params', key);
}

// ---------------------------------------------------------------- schema load
export async function loadSchema() {
  const team = S.team;
  const sch = await tryApi(`/api/params/schema?team=${encodeURIComponent(team)}`);
  if (S.team !== team) return;
  S.schema = sch;
  if (sch) {
    // drop overrides that no longer exist or are now equal to the preset
    for (const k of Object.keys(S.overrides)) {
      const p = sch.params.find((q) => q.key === k);
      if (!p || same(S.overrides[k], base(p))) delete S.overrides[k];
    }
  }
  renderParams(); renderDriver();
  emit('schema');
}

// ---------------------------------------------------------------- params panel
export function buildSetupPanel() {
  const host = clear($('#setup-panel'));
  host.append(
    h('div', { class: 'sec-head' },
      h('h2', null, 'Car setup'),
      h('span', { id: 'calib-tag', class: 'tag' })),
    h('div', { class: 'row gap' },
      h('label', { class: 'field grow', title: 'Copy another team’s calibrated preset onto this car as overrides' }, h('span', null, 'Load preset'),
        h('select', { id: 'preset-from', onchange: (e) => loadPresetFrom(e.target.value) })),
      h('button', { class: 'btn', title: 'Restore every parameter to this team’s preset', onclick: resetAll }, 'Reset all')),
    h('div', { class: 'row gap' },
      h('input', { id: 'param-filter', type: 'search', placeholder: 'Filter parameters', class: 'grow', 'aria-label': 'Filter parameters',
        oninput: (e) => { filterText = e.target.value.trim().toLowerCase(); renderParams(); } }),
      h('label', { class: 'tb-check', title: 'Show only parameters that differ from the team preset' },
        h('input', { id: 'diff-only', type: 'checkbox', onchange: (e) => { S.diffOnly = e.target.checked; renderParams(); } }), 'Diff vs preset')),
    provLegend(),
    h('div', { id: 'params' }),
    h('div', { class: 'sec-head' }, h('h2', null, 'Driver'), h('button', { class: 'btn small', onclick: () => { S.driverOverrides = {}; renderDriver(); emit('params'); } }, 'Reset')),
    h('div', { id: 'driver-params' }),
    h('div', { class: 'sec-head' }, h('h2', null, 'Setups')),
    h('div', { id: 'setups' }),
  );
  renderSetups();
}

function resetAll() { S.overrides = {}; renderParams(); emit('params'); }

async function loadPresetFrom(team) {
  $('#preset-from').value = '';
  if (!team || !S.schema) return;
  const pre = await tryApi(`/api/presets/${encodeURIComponent(team)}`);
  if (!pre || !pre.values) return;
  S.overrides = {};
  for (const [k, v] of Object.entries(pre.values)) setParam(k, v, true);
  renderParams(); emit('params');
  toast(`Applied ${teamName(team)} preset values to the ${teamName(S.team)} car (${Object.keys(S.overrides).length} differences).`, 'info', 4000);
}

export function renderParams() {
  const box = $('#params');
  if (!box) return;
  clear(box);
  const pf = $('#preset-from');
  if (pf) { clear(pf).append(h('option', { value: '' }, 'from team…'), S.teams.map((t) => h('option', { value: t.id }, t.name))); }
  const tag = $('#calib-tag');
  if (tag) {
    const cal = S.schema?.calibrated;
    tag.textContent = S.schema ? (cal ? 'calibrated preset' : 'baseline (not calibrated)') : '';
    tag.className = 'tag ' + (cal ? 'ok' : 'warn');
    tag.title = cal ? 'Team preset fitted from public timing data. Still an estimate.' : 'No calibration available for this team: baseline car values are used.';
  }
  if (!S.schema) { box.append(h('div', { class: 'empty' }, 'Parameter schema unavailable (GET /api/params/schema failed). Runs use the team preset.')); return; }
  let shown = 0;
  for (const g of S.schema.groups) {
    const all = S.schema.params.filter((p) => p.group === g);
    const changed = all.filter((p) => p.key in S.overrides).length;
    const rows = all.filter((p) => (!S.diffOnly || p.key in S.overrides)
      && (!filterText || p.label.toLowerCase().includes(filterText) || p.key.toLowerCase().includes(filterText)));
    if (!rows.length) continue;
    shown += rows.length;
    const det = h('details', { class: 'group', open: openGroups.has(g) || S.diffOnly || !!filterText },
      h('summary', null, h('span', { class: 'g-name' }, g),
        h('span', { class: 'g-count' + (changed ? ' changed' : ''), 'data-group': g }, changed ? `${changed} changed` : `${all.length}`)),
      rows.map(paramRow));
    det.addEventListener('toggle', () => { if (det.open) openGroups.add(g); else openGroups.delete(g); });
    box.append(det);
  }
  if (!shown) box.append(h('div', { class: 'empty' }, S.diffOnly ? 'No differences: every parameter equals the team preset.' : 'No parameter matches the filter.'));
}

function paramRow(p) {
  const v = cur(p), b = base(p), dec = decimals(p.step);
  const row = h('div', { class: 'param' + (p.key in S.overrides ? ' changed' : '') + (S.locks.has(p.key) ? ' locked' : ''), 'data-key': p.key });
  const tip = [p.tip, `Preset ${fmtVal(p, b)}${p.unit ? ' ' + p.unit : ''} · baseline ${fmtVal(p, p.default)}` + (p.min != null ? ` · range ${p.min}–${p.max}` : '')].filter(Boolean).join('\n');
  const refresh = () => {
    const val = cur(p);
    row.classList.toggle('changed', p.key in S.overrides);
    if (input.type === 'checkbox') input.checked = !!+val; else if (document.activeElement !== input) input.value = p.kind === 'choice' ? val : (+val).toFixed(dec);
    if (slider && document.activeElement !== slider) slider.value = val;
    const gc = document.querySelector(`.g-count[data-group="${CSS.escape(p.group)}"]`);
    if (gc) {
      const all = S.schema.params.filter((q) => q.group === p.group), ch = all.filter((q) => q.key in S.overrides).length;
      gc.textContent = ch ? `${ch} changed` : `${all.length}`; gc.classList.toggle('changed', ch > 0);
    }
  };
  const commit = (val) => { setParam(p.key, val); refresh(); };
  let input, slider = null;
  if (p.kind === 'choice') {
    input = h('select', { class: 'p-val', onchange: (e) => commit(e.target.value) }, (p.options || []).map((o) => h('option', { value: o, selected: String(o) === String(v) }, o)));
  } else if (p.kind === 'bool') {
    input = h('input', { type: 'checkbox', class: 'p-bool', checked: !!+v, onchange: (e) => commit(e.target.checked ? 1 : 0) });
  } else {
    input = h('input', { type: 'number', class: 'p-val', min: p.min, max: p.max, step: p.step, value: (+v).toFixed(dec), 'aria-label': p.label,
      onchange: (e) => commit(e.target.value) });
    slider = h('input', { type: 'range', class: 'p-slider', min: p.min, max: p.max, step: p.step, value: v, 'aria-label': `${p.label} slider`, tabindex: -1,
      oninput: (e) => commit(e.target.value) });
  }
  const lock = h('button', { class: 'ibtn lock', title: 'Lock: exclude from the optimiser', 'aria-pressed': S.locks.has(p.key),
    onclick: () => { if (S.locks.has(p.key)) S.locks.delete(p.key); else S.locks.add(p.key); const onL = S.locks.has(p.key); row.classList.toggle('locked', onL); lock.setAttribute('aria-pressed', onL); emit('locks'); } }, 'L');
  const reset = h('button', { class: 'ibtn reset', title: `Reset to preset (${fmtVal(p, b)})`, onclick: () => { delete S.overrides[p.key]; emit('params', p.key); refresh(); if (S.diffOnly) renderParams(); } }, '↺');
  row.append(
    h('div', { class: 'p-head' },
      h('span', { class: 'p-label', title: tip }, p.label), provChip(p.prov),
      h('span', { class: 'p-fill' }), input, h('span', { class: 'p-unit' }, p.unit || ''), lock, reset));
  if (slider) {
    const frac = (b - p.min) / ((p.max - p.min) || 1);
    row.append(h('div', { class: 'p-track' }, slider, h('i', { class: 'p-preset', title: `Preset ${fmtVal(p, b)}`, style: { left: `calc(${(frac * 100).toFixed(2)}% + ${(6 - frac * 12).toFixed(1)}px)` } })));
  }
  return row;
}

// ---------------------------------------------------------------- driver sliders
export function renderDriver() {
  const box = $('#driver-params');
  if (!box) return;
  clear(box);
  const dps = S.schema?.driver_params;
  if (!dps || !dps.length) { box.append(h('div', { class: 'empty' }, 'Driver parameters unavailable.')); return; }
  const d = driverById(S.driver);
  for (const dp of dps) {
    const b = d?.ratings?.[dp.key] ?? dp.default, dec = decimals(dp.step);
    const v = dp.key in S.driverOverrides ? S.driverOverrides[dp.key] : b;
    const row = h('div', { class: 'param' + (dp.key in S.driverOverrides ? ' changed' : '') });
    const set = (val) => {
      val = Math.min(dp.max, Math.max(dp.min, +val));
      if (!isFinite(val)) return;
      if (Math.abs(val - b) < 1e-9) delete S.driverOverrides[dp.key]; else S.driverOverrides[dp.key] = val;
      row.classList.toggle('changed', dp.key in S.driverOverrides);
      if (document.activeElement !== inp) inp.value = val.toFixed(dec);
      if (document.activeElement !== sl) sl.value = val;
      emit('params');
    };
    const inp = h('input', { type: 'number', class: 'p-val', min: dp.min, max: dp.max, step: dp.step, value: (+v).toFixed(dec), 'aria-label': dp.label, onchange: (e) => set(e.target.value) });
    const sl = h('input', { type: 'range', class: 'p-slider', min: dp.min, max: dp.max, step: dp.step, value: v, tabindex: -1, 'aria-label': `${dp.label} slider`, oninput: (e) => set(e.target.value) });
    const frac = (b - dp.min) / ((dp.max - dp.min) || 1);
    row.append(
      h('div', { class: 'p-head' }, h('span', { class: 'p-label', title: `${dp.tip || ''}\n${d ? d.name : 'Driver'} rating ${(+b).toFixed(dec)}` }, dp.label), provChip('estimate'), h('span', { class: 'p-fill' }), inp,
        h('button', { class: 'ibtn reset', title: 'Reset to driver rating', onclick: () => set(b) }, '↺')),
      h('div', { class: 'p-track' }, sl, h('i', { class: 'p-preset', style: { left: `calc(${(frac * 100).toFixed(2)}% + ${(6 - frac * 12).toFixed(1)}px)` } })));
    box.append(row);
  }
}

// ---------------------------------------------------------------- setups
let setupList = [];
export async function renderSetups() {
  const box = $('#setups');
  if (!box) return;
  setupList = await tryApi('/api/setups', undefined, [], true) || [];
  clear(box);
  const name = h('input', { id: 'setup-name', type: 'text', placeholder: 'Setup name', class: 'grow', 'aria-label': 'Setup name', maxlength: 60 });
  const notes = h('input', { id: 'setup-notes', type: 'text', placeholder: 'Notes (optional)', 'aria-label': 'Setup notes', maxlength: 200 });
  const list = h('select', { id: 'setup-list', class: 'grow', 'aria-label': 'Saved setups' },
    h('option', { value: '' }, setupList.length ? `${setupList.length} saved setup${setupList.length > 1 ? 's' : ''}…` : 'No saved setups'),
    setupList.map((s) => h('option', { value: s.name, title: s.notes || '' }, `${s.name} – ${teamName(s.team)} @ ${trackById(s.track)?.name || s.track}`)));
  const del = h('button', { class: 'btn', onclick: async () => {
    const n = list.value; if (!n) return;
    if (del.dataset.arm !== n) { del.dataset.arm = n; del.textContent = 'Confirm delete'; del.classList.add('danger'); setTimeout(() => { del.dataset.arm = ''; del.textContent = 'Delete'; del.classList.remove('danger'); }, 3500); return; }
    try { await api(`/api/setups/${encodeURIComponent(n)}/delete`, {}); toast(`Deleted setup “${n}”.`, 'info', 3000); renderSetups(); } catch (e) { toast(e.message); }
  } }, 'Delete');
  box.append(
    h('div', { class: 'row gap' }, name, h('button', { class: 'btn', onclick: () => saveSetup(name.value, notes.value) }, 'Save')),
    notes,
    h('div', { class: 'row gap' }, list, h('button', { class: 'btn', onclick: () => loadSetup(list.value) }, 'Load'), del),
    h('button', { class: 'btn wide', onclick: () => openSheet(name.value, notes.value) }, 'Setup sheet (print view)'));
}

async function saveSetup(name, notes) {
  name = (name || '').trim();
  if (!name) { toast('Give the setup a name first.', 'warn', 3000); return; }
  try {
    await api('/api/setups', { name, track: S.track, team: S.team, driver: S.driver, notes: notes || '', values: { ...S.overrides }, driver_values: { ...S.driverOverrides } });
    toast(`Saved setup “${name}”.`, 'info', 3000);
    await renderSetups();
    $('#setup-name').value = name; $('#setup-notes').value = notes || '';
  } catch (e) { toast(e.message); }
}

async function loadSetup(name) {
  if (!name) return;
  const s = await tryApi(`/api/setups/${encodeURIComponent(name)}`);
  if (!s) return;
  emit('apply-setup', s);
  $('#setup-name').value = s.name || name; $('#setup-notes').value = s.notes || '';
}

// Apply stored values onto the current schema (called after selection has switched to the setup's team).
export function applyValues(values, driverValues) {
  S.overrides = {};
  for (const [k, v] of Object.entries(values || {})) setParam(k, v, true);
  S.driverOverrides = { ...(driverValues || {}) };
  renderParams(); renderDriver(); emit('params');
}

// ---------------------------------------------------------------- setup sheet
export function openSheet(name, notes) {
  const el = $('#sheet');
  clear(el);
  const sch = S.schema;
  const d = driverById(S.driver), t = trackById(S.track), team = teamById(S.team);
  const head = (k, v) => h('div', { class: 'sh-kv' }, h('span', null, k), h('b', null, v || '–'));
  const groups = !sch ? [h('p', null, 'Parameter schema unavailable.')] : sch.groups.map((g) => {
    const rows = sch.params.filter((p) => p.group === g);
    return h('section', { class: 'sh-group' }, h('h3', null, g),
      h('table', null,
        h('thead', null, h('tr', null, h('th', null, 'Parameter'), h('th', { class: 'r' }, 'Value'), h('th', null, 'Unit'), h('th', { class: 'r' }, 'Preset'), h('th', { class: 'r' }, 'Δ'), h('th', null, 'Src'))),
        h('tbody', null, rows.map((p) => {
          const v = cur(p), b = base(p), ch = p.key in S.overrides;
          const delta = ch && typeof v === 'number' && typeof b === 'number' ? ((v - b) >= 0 ? '+' : '−') + Math.abs(v - b).toFixed(decimals(p.step)) : (ch ? '≠' : '');
          return h('tr', { class: ch ? 'ch' : '' }, h('td', null, p.label), h('td', { class: 'r' }, fmtVal(p, v)), h('td', null, p.unit || ''), h('td', { class: 'r' }, fmtVal(p, b)), h('td', { class: 'r' }, delta), h('td', null, (PROV[p.prov] || {}).short || ''));
        }))));
  });
  const drv = (sch?.driver_params || []).map((dp) => {
    const b = d?.ratings?.[dp.key] ?? dp.default, v = dp.key in S.driverOverrides ? S.driverOverrides[dp.key] : b;
    return h('tr', { class: dp.key in S.driverOverrides ? 'ch' : '' }, h('td', null, dp.label), h('td', { class: 'r' }, (+v).toFixed(decimals(dp.step))), h('td', { class: 'r' }, (+b).toFixed(decimals(dp.step))));
  });
  el.append(h('div', { class: 'sheet-page' },
    h('div', { class: 'sheet-actions no-print' },
      h('button', { class: 'btn primary', onclick: () => window.print() }, 'Print'),
      h('button', { class: 'btn', onclick: () => { el.hidden = true; document.body.classList.remove('sheet-open'); } }, 'Close')),
    h('header', { class: 'sh-head' },
      h('h1', null, 'Setup sheet'),
      h('div', { class: 'sh-meta' },
        head('Setup', (name || '').trim() || 'unsaved'), head('Track', t?.name || S.track), head('Team', team?.name || S.team),
        head('Driver', d ? `${d.name} (${d.code})` : S.driver), head('Power unit', team?.pu), head('Printed', fmtDate(new Date().toISOString())),
        head('Changes vs preset', String(Object.keys(S.overrides).length)), head('Data as of', fmtDate(S.dataStatus?.fetched_at)))),
    notes ? h('p', { class: 'sh-notes' }, h('b', null, 'Notes: '), notes) : null,
    S.lap && S.lap.team === S.team && S.lap.track === S.track ? h('p', { class: 'sh-notes' }, h('b', null, 'Last simulated lap: '), `${S.lap.lap_time_str || ''} (${S.lap.mode}) – may predate later edits.`) : null,
    h('div', { class: 'sh-cols' }, groups,
      drv.length ? h('section', { class: 'sh-group' }, h('h3', null, 'Driver model'), h('table', null,
        h('thead', null, h('tr', null, h('th', null, 'Parameter'), h('th', { class: 'r' }, 'Value'), h('th', { class: 'r' }, 'Rating'))), h('tbody', null, drv))) : null),
    h('footer', { class: 'sh-foot' },
      'REG = regulation, PUB = fitted from public timing data, EST = engineering estimate. Car data are estimates reverse-calibrated from public timing; ',
      'results are for trends and comparisons, not exact real lap times. Rows in bold differ from the team preset.')));
  el.hidden = false;
  document.body.classList.add('sheet-open');
}

on('selection', (what) => { if (what === 'driver') renderDriver(); });
