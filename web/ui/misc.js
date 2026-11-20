// Optimise, Livery and Data modes plus the persistent notices strip.
import { $, h, clear, api, tryApi, toast, pollJob, fmtLap, fmtRace, fmtDate, num, decimals } from './util.js';
import { S, emit, on, currentConfig, teamName, teamById, trackById, driverById, teamColor } from './store.js';
import { runButton, table, kv, swatch, card } from './widgets.js';
import { provChip, provLegend, setParam, renderParams } from './params.js';
import { sc, showStatic } from './stage.js';

// =============================================================================================
// Optimise
const optSel = new Set();
let optObjective = 'quali', optInit = false;
const OPT_DEFAULT = ['front_wing', 'rear_wing', 'ride_height_f', 'ride_height_r', 'spring_f', 'spring_r', 'arb_f', 'arb_r', 'brake_bias', 'diff_lock'];

export function buildOptimisePanel(host) {
  host.append(
    h('label', { class: 'field' }, h('span', null, 'Objective'),
      h('select', { onchange: (e) => { optObjective = e.target.value; } }, h('option', { value: 'quali' }, 'Qualifying lap time'), h('option', { value: 'race' }, 'Race pace'))),
    h('div', { class: 'sec-head tight' }, h('h2', { id: 'opt-count' }, 'Parameters to vary'),
      h('span', { class: 'row gap' },
        h('button', { class: 'btn small', onclick: () => { optSel.clear(); renderOptList(); } }, 'None'),
        h('button', { class: 'btn small', onclick: () => { optSel.clear(); OPT_DEFAULT.forEach((k) => optSel.add(k)); renderOptList(); } }, 'Typical'))),
    h('div', { id: 'opt-list', class: 'opt-list' }),
    h('p', { class: 'hint' }, 'Locked parameters (L in the setup list) are never varied. Fewer parameters converge faster.'),
    runButton('Run optimiser', runOptimise, { id: 'run-opt' }));
  renderOptList();
}

function optCandidates() {
  return (S.schema?.params || []).filter((p) => (p.kind === 'float' || !p.kind) && !S.locks.has(p.key));
}
function renderOptList() {
  const box = $('#opt-list');
  if (!box) return;
  clear(box);
  const cands = optCandidates();
  if (!cands.length) { box.append(h('p', { class: 'empty' }, 'Parameter schema unavailable.')); return; }
  if (!optInit) { optInit = true; OPT_DEFAULT.forEach((k) => optSel.add(k)); }
  const ok = new Set(cands.map((p) => p.key));
  for (const k of [...optSel]) if (!ok.has(k)) optSel.delete(k);
  for (const g of S.schema.groups) {
    const ps = cands.filter((p) => p.group === g);
    if (!ps.length) continue;
    const nSel = ps.filter((p) => optSel.has(p.key)).length;
    box.append(h('details', { class: 'group', open: nSel > 0 },
      h('summary', null, h('span', { class: 'g-name' }, g), h('span', { class: 'g-count' + (nSel ? ' changed' : '') }, `${nSel}/${ps.length}`)),
      ps.map((p) => h('label', { class: 'opt-item', title: p.tip }, h('input', { type: 'checkbox', checked: optSel.has(p.key), onchange: (e) => { if (e.target.checked) optSel.add(p.key); else optSel.delete(p.key); $('#opt-count').textContent = `Parameters to vary (${optSel.size})`; } }), h('span', { class: 'grow' }, p.label), provChip(p.prov)))));
  }
  $('#opt-count').textContent = `Parameters to vary (${optSel.size})`;
}
on('schema', renderOptList); on('locks', renderOptList);

async function runOptimise(progress) {
  if (!optSel.size) throw new Error('Select at least one parameter to vary.');
  const t0 = performance.now();
  progress(0, 'Submitting…');
  const { job } = await api('/api/optimise', { ...currentConfig(), params: [...optSel], objective: optObjective });
  if (job == null) throw new Error('/api/optimise did not return a job id');
  const res = await pollJob(job, progress);
  if (!res) throw new Error('Optimiser finished without a result');
  S.opt = { ...res, track: S.track, team: S.team, driver: S.driver, base: { ...S.overrides } };
  if (S.mode === 'optimise') renderOptRight();
  return `${res.evaluations ?? '?'} evaluations · ${((performance.now() - t0) / 1000).toFixed(1)} s`;
}

const fmtObj = (v) => (v == null ? '–' : v < 600 ? fmtLap(v) : fmtRace(v));
export function renderOptRight() {
  const box = clear($('#right'));
  const o = S.opt;
  if (!o) { box.append(card('Optimise', h('p', { class: 'empty' }, 'Choose the parameters the optimiser may change and an objective, then run. You get the before / after time, the best setup with an Apply button and a sensitivity table.'))); return; }
  const byKey = Object.fromEntries((S.schema?.params || []).map((p) => [p.key, p]));
  const best = Object.entries(o.best || {});
  const gain = o.gain ?? (o.before - o.after);
  const sens = [...(o.sensitivities || [])].sort((a, b) => Math.abs(b.delta_per_step) - Math.abs(a.delta_per_step));
  const smax = Math.max(1e-9, ...sens.map((s) => Math.abs(s.delta_per_step)));
  box.append(
    h('section', { class: 'card hero' },
      h('div', { class: 'hero-label' }, `${trackById(o.track)?.name || o.track} · ${teamName(o.team)} · ${o.objective === 'race' ? 'race pace' : 'qualifying'}`),
      h('div', { class: 'ba' }, h('div', null, h('span', null, 'Before'), h('b', { class: 'mono' }, fmtObj(o.before))), h('div', null, h('span', null, 'After'), h('b', { class: 'mono' }, fmtObj(o.after))),
        h('div', null, h('span', null, 'Gain'), h('b', { class: 'mono ' + (gain > 0 ? 'c-ok' : '') }, `${gain >= 0 ? '−' : '+'}${Math.abs(gain).toFixed(3)} s`))),
      h('div', { class: 'hero-note' }, `${o.evaluations ?? '?'} solver evaluations. The optimum belongs to this model, not to the real car.`)),
    card('Best setup',
      table([
        { label: 'Parameter', get: ([k]) => byKey[k]?.label || k },
        { label: 'Was', cls: 'r mono', get: ([k]) => { const p = byKey[k]; const v = k in o.base ? o.base[k] : p ? (p.preset ?? p.default) : null; return typeof v === 'number' ? v.toFixed(decimals(p?.step)) : String(v ?? ''); } },
        { label: 'Best', cls: 'r mono', get: ([k, v]) => (typeof v === 'number' ? v.toFixed(decimals(byKey[k]?.step)) : String(v)) },
        { label: 'Unit', get: ([k]) => byKey[k]?.unit || '' },
      ], best, { empty: 'Optimiser returned no parameter changes.' }),
      h('button', { class: 'btn primary wide', disabled: !best.length, onclick: () => {
        if (S.team !== o.team) { toast(`This result is for ${teamName(o.team)}; select that team to apply it.`, 'warn'); return; }
        for (const [k, v] of best) setParam(k, v, true);
        renderParams(); emit('params');
        toast(`Applied ${best.length} optimised values to the setup.`, 'info', 3500);
      } }, 'Apply best setup')),
    card('Sensitivity', h('p', { class: 'hint' }, 'Lap-time change for one slider step up, at the optimum. Negative = faster.'),
      table([
        { label: 'Parameter', get: (s) => s.label || s.key },
        { label: 'Step', cls: 'r mono', get: (s) => `${s.step ?? ''} ${s.unit || ''}` },
        { label: 'Δ s / step', cls: 'r mono', get: (s) => h('span', { class: s.delta_per_step < 0 ? 'c-ok' : s.delta_per_step > 0 ? 'c-bad' : '' }, `${s.delta_per_step >= 0 ? '+' : '−'}${Math.abs(s.delta_per_step).toFixed(4)}`) },
        { label: '', get: (s) => h('span', { class: 'sbar' }, h('i', { class: s.delta_per_step < 0 ? 'neg' : 'pos', style: { width: `${(Math.abs(s.delta_per_step) / smax) * 100}%` } })) },
      ], sens, { empty: 'No sensitivities returned.' })));
}

// =============================================================================================
// Livery editor
const REGIONS = [['nose', 'Nose'], ['engine_cover', 'Engine cover'], ['sidepods', 'Sidepods'], ['front_wing', 'Front wing'], ['rear_wing', 'Rear wing'], ['endplates', 'Endplates'], ['halo', 'Halo'], ['floor', 'Floor'], ['wheel_covers', 'Wheel covers']];
const DEFAULT_LIVERY = { base: '#8a96a3', secondary: '#20262c', accent: '#d5dbe1', finish: 'gloss', regions: { nose: 'base', engine_cover: 'base', sidepods: 'base', front_wing: 'secondary', rear_wing: 'secondary', endplates: 'accent', halo: 'carbon', floor: 'carbon', wheel_covers: 'secondary' }, numbers: {}, number_color: '#ffffff' };
let draft = null, draftTeam = null, liveryDirty = false;

export function buildLiveryPanel(host) { host.append(h('div', { id: 'livery-editor' })); }

export function renderLivery() {
  const box = $('#livery-editor');
  if (!box) return;
  if (draftTeam !== S.team || !draft) {
    draftTeam = S.team; liveryDirty = false;
    draft = JSON.parse(JSON.stringify({ ...DEFAULT_LIVERY, ...(S.liveries[S.team] || {}) }));
    draft.regions = { ...DEFAULT_LIVERY.regions, ...(draft.regions || {}) };
    draft.numbers = { ...(draft.numbers || {}) };
  }
  clear(box);
  const team = teamById(S.team);
  const push = () => { liveryDirty = true; S.liveries[S.team] = draft; sc('setLivery', S.team, draft); const st = $('#livery-state'); if (st) st.textContent = 'unsaved changes'; };
  const hex = (v) => (/^#[0-9a-f]{6}$/i.test(v || '') ? v : '#888888');
  const color = (label, key) => h('label', { class: 'field color' }, h('span', null, label),
    h('input', { type: 'color', value: hex(draft[key]), oninput: (e) => { draft[key] = e.target.value; e.target.nextSibling.textContent = e.target.value; push(); } }), h('code', null, hex(draft[key])));
  const region = ([key, label]) => {
    const v = draft.regions[key] || 'base', custom = /^#/.test(v);
    const pick = h('input', { type: 'color', value: custom ? hex(v) : '#888888', hidden: !custom, 'aria-label': `${label} custom colour`, oninput: (e) => { draft.regions[key] = e.target.value; push(); } });
    return h('label', { class: 'field region' }, h('span', null, label),
      h('select', { onchange: (e) => { const c = e.target.value === 'custom'; pick.hidden = !c; draft.regions[key] = c ? pick.value : e.target.value; push(); } },
        ['base', 'secondary', 'accent', 'carbon', 'custom'].map((o) => h('option', { value: o, selected: custom ? o === 'custom' : o === v }, o))), pick);
  };
  const drivers = [...(team?.drivers || []), ...(team?.other_drivers || [])];
  box.append(
    h('div', { class: 'sec-head' }, h('h2', null, `Livery – ${team?.name || S.team || 'no team'}`), h('span', { id: 'livery-state', class: 'dim small' }, liveryDirty ? 'unsaved changes' : '')),
    h('div', { class: 'color-grid' }, color('Base', 'base'), color('Secondary', 'secondary'), color('Accent', 'accent'), color('Number', 'number_color')),
    h('label', { class: 'field' }, h('span', null, 'Paint finish'),
      h('select', { onchange: (e) => { draft.finish = e.target.value; push(); } }, ['gloss', 'satin', 'matte'].map((f) => h('option', { value: f, selected: draft.finish === f }, f)))),
    h('div', { class: 'sec-head tight' }, h('h2', null, 'Colour regions')),
    h('div', { class: 'region-grid' }, REGIONS.map(region)),
    h('div', { class: 'sec-head tight' }, h('h2', null, 'Car numbers')),
    h('div', { class: 'region-grid' }, drivers.length ? drivers.map((d) => h('label', { class: 'field' }, h('span', null, driverById(d)?.name || d),
      h('input', { type: 'number', min: 0, max: 99, step: 1, class: 'narrow', value: draft.numbers[d] ?? driverById(d)?.number ?? '', onchange: (e) => { draft.numbers[d] = Math.max(0, Math.min(99, Math.round(+e.target.value || 0))); push(); } }))) : h('p', { class: 'empty' }, 'No drivers listed for this team.')),
    h('div', { class: 'row gap' },
      h('button', { class: 'btn primary grow', onclick: saveLivery }, 'Save livery'),
      h('button', { class: 'btn', title: 'Reload the saved livery from disk', onclick: async () => { const all = await tryApi('/api/liveries'); if (all) { S.liveries = all; draft = null; renderLivery(); emit('liveries'); } } }, 'Revert')));
}

async function saveLivery() {
  if (!S.team || !draft) return;
  try {
    await api(`/api/liveries/${encodeURIComponent(S.team)}`, draft);
    liveryDirty = false; $('#livery-state').textContent = 'saved';
    toast(`Saved livery for ${teamName(S.team)} (data/liveries/${S.team}.json).`, 'info', 3500);
    emit('liveries');
  } catch (e) { toast(e.message); }
}

export function renderLiveryRight() {
  const t = S.team || '<team>';
  clear($('#right')).append(
    card('Livery preview', h('p', null, 'Edits are applied live to this team’s car in the scene. Use the Orbit camera to look around the car. Save writes the colour scheme to ', h('code', null, `data/liveries/${t}.json`), '.')),
    card('No real logos', h('p', null, 'Team liveries, sponsor logos and badges are trademarks. This simulator ships approximate public colour schemes only: no real sponsor logos or team badges are included, and none are downloaded.')),
    card('Decal slots', h('p', null, 'To add your own artwork, drop PNG files into the team’s decal folder. They are projected onto the matching body region the next time the page loads:'),
      h('ul', { class: 'files' }, ['sidepod.png', 'engine_cover.png', 'rear_wing.png', 'nose.png'].map((f) => h('li', null, h('code', null, `web/assets/liveries/${t}/${f}`)))),
      h('p', { class: 'hint' }, 'Only placeholder decals (number, driver code, generic stripes) ship with the project. Anything you add stays on your machine.')));
}
on('selection', (what) => { if (what === 'team' && S.mode === 'livery') { renderLivery(); renderLiveryRight(); showStatic(); } });

// =============================================================================================
// Data page
export function buildDataPanel(host) {
  host.append(
    h('p', { class: 'hint' }, 'Where the numbers come from: the public data snapshot, the calibration that turns it into team presets, and how well the simulator reproduces real pole laps.'),
    runButton('Re-run calibration', runCalibrate, { id: 'run-calib' }),
    h('p', { class: 'hint' }, 'Calibration refits the team presets from the cached public timing data. It does not download anything.'));
}

async function runCalibrate(progress) {
  const t0 = performance.now();
  progress(0, 'Submitting…');
  const { job } = await api('/api/calibrate', {});
  if (job == null) throw new Error('/api/calibrate did not return a job id');
  await pollJob(job, progress, 600);
  const ds = await tryApi('/api/data_status');
  if (ds) S.dataStatus = ds;
  const tm = await tryApi('/api/teams', undefined, null, true);
  if (tm) S.teams = tm;
  emit('data'); emit('recalibrated');
  return `calibrated in ${((performance.now() - t0) / 1000).toFixed(1)} s`;
}

const pctFmt = (v, d = 3) => (v == null || !isFinite(v) ? '–' : `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(d)} %`);

export function renderDataPage() {
  const page = clear($('#page'));
  const ds = S.dataStatus;
  if (!ds) { page.append(h('div', { class: 'page-inner' }, card('Data status unavailable', h('p', null, 'GET /api/data_status failed, so the snapshot date, sources and calibration report cannot be shown. The simulator still runs with whatever presets the server has.'), h('button', { class: 'btn', onclick: async () => { const d = await tryApi('/api/data_status'); if (d) { S.dataStatus = d; emit('data'); } } }, 'Retry')))); return; }
  const cal = ds.calibration;
  const status = ds.status && typeof ds.status === 'object' ? Object.entries(ds.status) : [];
  const paramLabel = (k) => S.schema?.params.find((p) => p.key === k)?.label || k;
  page.append(h('div', { class: 'page-inner' },
    h('div', { class: 'page-head' },
      h('h1', null, 'Data & calibration'),
      h('div', { class: 'asof' }, 'Data as of ', h('b', null, fmtDate(ds.fetched_at)), ds.stale_snapshot ? h('span', { class: 'tag warn', title: 'Live fetching failed; the bundled snapshot is used.' }, 'stale snapshot') : null,
        h('span', { class: 'dim' }, ` · ${ds.rounds_cached ?? 0} rounds cached`))),
    (ds.notices || []).length ? h('ul', { class: 'warns' }, ds.notices.map((n) => h('li', null, n))) : null,
    h('div', { class: 'page-grid' },
      card('Sources', (ds.sources || []).length ? h('ul', { class: 'files' }, ds.sources.map((s) => h('li', null, s))) : h('p', { class: 'empty' }, 'No sources listed.'),
        status.length ? kv(status.map(([k, v]) => [k, typeof v === 'object' ? JSON.stringify(v) : String(v)])) : null),
      card('Provenance of every parameter', provLegend(),
        h('p', null, h('b', null, 'REG'), ' regulation: fixed by the 2026 FIA rules (mass minimum, MGU-K power, energy limits).'),
        h('p', null, h('b', null, 'PUB'), ' public-derived: fitted so the simulator reproduces public qualifying gaps and speed-trap speeds.'),
        h('p', null, h('b', null, 'EST'), ' estimate: an engineering guess. No team publishes downforce, drag, spring rates or PU maps.'),
        h('p', { class: 'hint' }, 'Real 2026 car data is secret. Presets are reverse-calibrated from timing data; results are for trends and comparisons, not exact real lap times or race results.')),
      card('Constructor standings (sanity check only)', table([
        { label: 'Pos', cls: 'r', get: (r) => r.pos }, { label: 'Team', get: (r) => h('span', { class: 'nowrap' }, swatch(teamColor(r.team)), teamName(r.team)) }, { label: 'Points', cls: 'r mono', get: (r) => r.points },
      ], ds.standings || [], { empty: 'No standings cached.' }),
        h('p', { class: 'hint' }, 'Points mix pace, reliability, strategy and driver. They are not used to set car pace.'))),
    card('Calibration fit report',
      cal ? [
        h('p', { class: 'hint' }, `Generated ${fmtDate(cal.generated_at)}. Target = real average qualifying gap to the fastest team; simulated = the same gap from the fitted preset.`),
        table([
          { label: 'Team', get: (r) => h('span', { class: 'nowrap' }, swatch(teamColor(r.team)), teamName(r.team)) },
          { label: 'Target gap', cls: 'r mono', get: (r) => pctFmt(r.target_gap_pct) },
          { label: 'Simulated gap', cls: 'r mono', get: (r) => pctFmt(r.sim_gap_pct) },
          { label: 'Residual', cls: 'r mono', get: (r) => h('span', { class: Math.abs(r.residual_pct) > 0.15 ? 'c-warn' : '' }, pctFmt(r.residual_pct)) },
          { label: 'Standings pos', cls: 'r', get: (r) => r.standing_pos ?? '–' },
          { label: 'Pace rank', cls: 'r', get: (r) => h('span', { class: r.standing_pos != null && Math.abs(r.standing_pos - r.pace_rank) >= 3 ? 'c-warn' : '', title: 'Rank by fitted pace. A large difference to the standings usually means reliability, strategy or drivers, not car pace.' }, r.pace_rank ?? '–') },
          { label: 'Parameters moved', get: (r) => h('span', { class: 'small' }, Object.entries(r.params || {}).map(([k, v]) => `${paramLabel(k)} ${typeof v === 'number' ? +v.toFixed(4) : v}`).join(' · ') || 'none') },
        ], cal.teams || [], { empty: 'No per-team fit rows.' }),
        cal.global && Object.keys(cal.global).length ? h('div', null, h('h4', null, 'Global factors'), kv(Object.entries(cal.global).map(([k, v]) => [paramLabel(k), typeof v === 'number' ? String(+v.toFixed(4)) : JSON.stringify(v)]))) : null,
        (cal.notes || []).length ? h('ul', { class: 'warns' }, cal.notes.map((n) => h('li', null, n))) : null,
      ] : h('p', { class: 'empty' }, 'No calibration report yet. Presets are baseline values until calibration has run (left panel: Re-run calibration).')),
    card('Validation against real pole laps',
      ds.validation && ds.validation.length ? table([
        { label: 'Track', get: (r) => trackById(r.track)?.name || r.track },
        { label: 'Real pole', cls: 'r mono', get: (r) => fmtLap(r.real_pole) },
        { label: 'Simulated pole', cls: 'r mono', get: (r) => fmtLap(r.sim_pole) },
        { label: 'Error', cls: 'r mono', get: (r) => h('span', { class: Math.abs(r.err_pct) > 2 ? 'c-bad' : Math.abs(r.err_pct) > 1 ? 'c-warn' : 'c-ok' }, pctFmt(r.err_pct, 2)) },
      ], ds.validation) : h('p', { class: 'empty' }, 'No validation rows available.'),
      h('p', { class: 'hint' }, 'One global grip / drag calibration is used for all tracks: there is no per-track correction, so errors here are honest.'))));
}
on('data', () => { if (S.mode === 'data') renderDataPage(); });

// =============================================================================================
// Persistent notices
export function renderNotices() {
  const foot = $('#notices');
  if (!foot) return;
  clear(foot);
  const add = (text, level, title) => foot.append(h('span', { class: `notice ${level || ''}`, title: title || text }, text));
  const t = trackById(S.track), ds = S.dataStatus;
  add('Results are for trends and comparisons, not exact real lap times or race results.');
  add('Car data are estimates reverse-calibrated from public timing.');
  if (ds?.stale_snapshot) add('Stale data snapshot in use.', 'warn', 'Live fetching failed or was skipped: the bundled snapshot of public data is used.');
  if (t && t.elevation_source === 'flat') add(`Flat track: no elevation data for ${t.name}.`, 'warn', 'Gradients are ignored for this circuit, which changes braking and traction slightly.');
  for (const n of (t?.notes || [])) {
    const sparse = /sparse|centre ?line|center ?line|coarse|low[- ]res/i.test(n);
    add(sparse ? `Track: ${n}` : n, sparse ? 'warn' : '', n);
  }
  if (t && t.align_rms_m != null && t.align_rms_m > 15) add(`Track alignment RMS ${t.align_rms_m.toFixed(0)} m.`, 'warn', 'Fit quality of the processed centreline against the reference geometry.');
  add('Liveries are approximate colour schemes: no real sponsor logos or badges.');
  if (S.sceneKind === '2d') add('3D scene unavailable: 2D fallback view.', 'warn');
  else if (S.sceneNotes.length) add(`${S.sceneNotes.length} scene asset note${S.sceneNotes.length > 1 ? 's' : ''}`, 'warn', S.sceneNotes.join('\n'));
  for (const n of (ds?.notices || [])) add(n, 'warn');

  const hd = $('#hdr-data');
  if (hd) {
    clear(hd).append(h('button', { class: 'asof-btn', title: 'Open the Data tab', onclick: () => emit('goto', 'data') },
      'Data as of ', h('b', null, ds ? fmtDate(ds.fetched_at) : 'unknown'), ds?.stale_snapshot ? h('span', { class: 'tag warn' }, 'stale snapshot') : null));
  }
}
for (const e of ['selection', 'data', 'notices', 'scene', 'track']) on(e, renderNotices);
