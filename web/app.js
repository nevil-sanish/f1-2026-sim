// App shell: loads catalogues, owns selections and modes, mounts the UI modules.
import { $, h, clear, tryApi, toast } from './ui/util.js';
import { S, emit, on, trackById, teamById } from './ui/store.js';
import { initStage, loadTrack, showStatic, R } from './ui/stage.js';
import { buildSetupPanel, loadSchema, applyValues } from './ui/params.js';
import { buildLapPanel, buildComparePanel, enterLap, enterCompare } from './ui/lap.js';
import { buildRacePanel, enterRace } from './ui/race.js';
import { buildOptimisePanel, renderOptRight, buildLiveryPanel, renderLivery, renderLiveryRight, buildDataPanel, renderDataPage, renderNotices } from './ui/misc.js';
import { store } from './ui/util.js';

const MODES = [
  { id: 'lap', label: 'Lap', build: buildLapPanel, enter: enterLap },
  { id: 'compare', label: 'Compare', build: buildComparePanel, enter: enterCompare },
  { id: 'optimise', label: 'Optimise', build: buildOptimisePanel, enter: () => { showStatic(); renderOptRight(); } },
  { id: 'race', label: 'Race', build: buildRacePanel, enter: enterRace },
  { id: 'livery', label: 'Livery', build: buildLiveryPanel, enter: () => { showStatic(); renderLivery(); renderLiveryRight(); } },
  { id: 'data', label: 'Data', build: buildDataPanel, enter: renderDataPage },
];
const panels = {};

function setMode(id, force) {
  const m = MODES.find((x) => x.id === id) || MODES[0];
  if (S.mode === m.id && !force && panels[m.id]) return;
  S.mode = m.id; store.set('mode', m.id);
  document.body.dataset.mode = m.id;
  for (const x of MODES) {
    const b = $(`#mode-${x.id}`);
    if (b) { b.classList.toggle('on', x.id === m.id); b.setAttribute('aria-selected', x.id === m.id); }
    if (panels[x.id]) panels[x.id].hidden = x.id !== m.id;
  }
  if (!panels[m.id]) {
    panels[m.id] = h('div', { class: 'mode-panel', id: `panel-${m.id}` });
    $('#mode-panels').append(panels[m.id]);
    m.build(panels[m.id]);
  }
  panels[m.id].hidden = false;
  $('#page').hidden = m.id !== 'data';
  document.body.classList.toggle('page-mode', m.id === 'data');
  clear($('#right'));
  guarded(m.enter);
}
function guarded(fn) { try { fn(); } catch (e) { console.error(e); toast(`UI error: ${e.message}`); } }

// ---------------------------------------------------------------- selectors
const opt = (v, t, sel, dis) => h('option', { value: v, selected: sel, disabled: dis }, t);

function fillTracks() {
  const sel = clear($('#sel-track'));
  for (const t of S.tracks) sel.append(opt(t.id, `${t.round ? 'R' + t.round + ' ' : ''}${t.name}${t.available === false ? ' (no data)' : ''}`, t.id === S.track, t.available === false));
}
function fillTeams() {
  const sel = clear($('#sel-team'));
  for (const t of S.teams) sel.append(opt(t.id, t.name, t.id === S.team));
}
function fillDrivers() {
  const sel = clear($('#sel-driver'));
  const team = teamById(S.team);
  const ids = team ? [...(team.drivers || []), ...(team.other_drivers || [])] : [];
  const list = ids.length ? ids.map((id) => S.drivers.find((d) => d.id === id)).filter(Boolean) : S.drivers;
  for (const d of list) sel.append(opt(d.id, `${d.code} ${d.name}`, d.id === S.driver));
  if (!list.some((d) => d.id === S.driver) && list[0]) S.driver = list[0].id;
  sel.value = S.driver || '';
}

async function selectTrack(id) {
  if (!id || id === S.track) return;
  S.track = id; store.set('track', id);
  $('#sel-track').value = id;
  emit('selection', 'track');
  await loadTrack(id);
  setMode(S.mode, true);
}
async function selectTeam(id, driver) {
  S.team = id; store.set('team', id);
  $('#sel-team').value = id;
  const team = teamById(id);
  S.driver = driver || team?.drivers?.[0] || S.driver;
  fillDrivers();
  S.overrides = {}; S.driverOverrides = {};
  await loadSchema();
  emit('selection', 'team'); emit('selection', 'driver');
  emit('params');
  if (R.kind === 'static') showStatic();
}
function selectDriver(id) {
  S.driver = id; store.set('driver', id);
  S.driverOverrides = {};
  emit('selection', 'driver'); emit('params');
  if (R.kind === 'static') showStatic();
}

on('apply-setup', async (s) => {
  if (s.team && s.team !== S.team) await selectTeam(s.team, s.driver);
  else if (s.driver && s.driver !== S.driver) { S.driver = s.driver; $('#sel-driver').value = s.driver; emit('selection', 'driver'); }
  if (s.track && !s.keepTrack && s.track !== S.track && trackById(s.track)) await selectTrack(s.track);
  applyValues(s.values, s.driver_values);
});
on('goto', (id) => setMode(id));
on('recalibrated', async () => { fillTeams(); if (S.team) await loadSchema(); });

// ---------------------------------------------------------------- boot
async function boot() {
  const nav = $('#modes');
  for (const m of MODES) nav.append(h('button', { id: `mode-${m.id}`, class: 'tab mode-tab', role: 'tab', onclick: () => setMode(m.id) }, m.label));
  $('#sel-track').addEventListener('change', (e) => selectTrack(e.target.value));
  $('#sel-team').addEventListener('change', (e) => selectTeam(e.target.value));
  $('#sel-driver').addEventListener('change', (e) => selectDriver(e.target.value));

  const [tracks, teams, drivers, liveries, ds] = await Promise.all([
    tryApi('/api/tracks', undefined, []), tryApi('/api/teams', undefined, []), tryApi('/api/drivers', undefined, []),
    tryApi('/api/liveries', undefined, {}), tryApi('/api/data_status', undefined, null, true),
  ]);
  S.tracks = tracks; S.teams = teams; S.drivers = drivers; S.liveries = liveries || {}; S.dataStatus = ds;

  const okTrack = (id) => S.tracks.find((t) => t.id === id && t.available !== false);
  S.track = [store.get('track'), 'monza', S.tracks.find((t) => t.available !== false)?.id].find(okTrack) || null;
  S.team = [store.get('team'), 'ferrari'].find((id) => teamById(id)) || S.teams[0]?.id || null;
  const team = teamById(S.team);
  const sd = store.get('driver');
  S.driver = (team?.drivers || []).includes(sd) || (team?.other_drivers || []).includes(sd) ? sd : team?.drivers?.[0] || S.drivers[0]?.id || null;
  fillTracks(); fillTeams(); fillDrivers();
  if (!S.tracks.length) toast('Could not load the track list. Is the server running and is track data fetched?', 'error', 0);

  buildSetupPanel();
  await initStage();
  renderNotices();
  const first = MODES.find((m) => m.id === store.get('mode', 'lap'))?.id || 'lap';
  const jobs = [];
  if (S.team) jobs.push(loadSchema());
  if (S.track) jobs.push(loadTrack(S.track));
  await Promise.all(jobs);
  setMode(first === 'data' ? 'lap' : first, true);
  emit('liveries');
}

boot().catch((e) => { console.error(e); toast(`Startup failed: ${e.message}`, 'error', 0); });
window.addEventListener('unhandledrejection', (e) => { console.error(e.reason); toast(String(e.reason?.message || e.reason), 'error', 8000); });
