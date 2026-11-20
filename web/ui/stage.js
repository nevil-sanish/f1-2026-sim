// Centre stage: owns the 3D scene (or the 2D fallback), the scene toolbar, the replay clock and
// the per-frame car poses. The app owns time; the scene only renders (docs/API.md section 3).
import { $, h, clear, toast, tryApi, fmtLap, fmtRace, lowerBound, lerp, lerpAngle, clamp, store } from './util.js';
import { S, emit, on, trackById, driverById, compoundLabel } from './store.js';
import { createFallbackScene } from './fallback2d.js';

let scene = null;
let lastSceneFrame = 0;
const tickFns = [];

export const R = {
  kind: null,            // null | 'static' | 'lap' | 'race'
  t: 0, dur: 0, playing: false, speed: 1,
  cars: [],              // lap: [{id, T, L, lapTime, label, color}]   race: [{id, d, pit, speed, ...}]
  poses: new Map(),      // id -> last pose (with .hud extras)
  follow: null,          // car id | 'leader' | 'battle'
  targetId: null,
  race: null,            // race replay context
  order: [],             // race: car ids ordered by distance (live)
};
const UI = { cam: store.get('cam', 'orbit'), quality: store.get('quality', 'medium'), tod: 'day', line: 'speed', corners: true, brakeMarkers: true, tags: true };

export function sc(name, ...args) {
  if (!scene || typeof scene[name] !== 'function') return undefined;
  try { return scene[name](...args); } catch (e) { console.error(`scene.${name} failed`, e); return undefined; }
}
export const onTick = (fn) => tickFns.push(fn);
export const stageUI = UI;

// ---------------------------------------------------------------- init
export async function initStage() {
  buildBars();
  const host = $('#scene');
  const quality = UI.quality;
  try {
    const mod = await import('../scene/scene.js');
    if (typeof mod.createScene !== 'function') throw new Error('scene.js has no createScene export');
    scene = await Promise.race([
      mod.createScene(host, { quality }),
      new Promise((_, rej) => setTimeout(() => rej(new Error('scene start timed out')), 45000)),
    ]);
    if (!scene) throw new Error('createScene returned nothing');
    S.sceneKind = '3d';
  } catch (e) {
    console.warn('3D scene unavailable, using 2D fallback:', e);
    clear(host);
    const reason = String(e && e.message || e).split('\n')[0].slice(0, 90);
    scene = createFallbackScene(host, reason);
    S.sceneKind = '2d';
    toast(`3D scene unavailable (${reason}). Showing the 2D top-down view instead.`, 'warn', 9000);
  }
  sc('onFrame', (dt) => { lastSceneFrame = performance.now(); frame(dt); });
  sc('setOverlay', overlayState());
  // Safety net: keep the replay clock running even if the scene stops calling onFrame.
  let last = performance.now();
  const own = (now) => {
    const dt = Math.min(0.1, (now - last) / 1000); last = now;
    if (now - lastSceneFrame > 400) frame(dt);
    requestAnimationFrame(own);
  };
  requestAnimationFrame(own);
  new ResizeObserver(() => sc('resize')).observe(host);
  setInterval(updateStats, 1000);
  emit('scene');
}

function overlayState() { return { line: UI.line, corners: UI.corners, brakeMarkers: UI.brakeMarkers, tags: UI.tags }; }

function updateStats() {
  const el = $('#scene-stats');
  if (!el) return;
  const st = sc('getStats');
  if (!st) { el.textContent = ''; return; }
  el.textContent = S.sceneKind === '2d' ? `2D · ${st.fps || 0} fps` : `${Math.round(st.fps || 0)} fps · ${st.drawCalls ?? '–'} calls`;
  const notes = (st.notes || []).join('\n');
  el.title = notes || 'Renderer statistics';
  const key = notes;
  if (key !== S._notesKey) { S._notesKey = key; S.sceneNotes = st.notes || []; emit('notices'); }
}

// ---------------------------------------------------------------- toolbars
function sel(id, label, opts, value, onchange) {
  return h('label', { class: 'tb-field' }, h('span', null, label),
    h('select', { id, onchange: (e) => onchange(e.target.value) },
      opts.map(([v, t]) => h('option', { value: v, selected: v === value }, t))));
}
function chk(label, key, title) {
  return h('label', { class: 'tb-check', title }, h('input', { type: 'checkbox', checked: UI[key], onchange: (e) => { UI[key] = e.target.checked; sc('setOverlay', overlayState()); } }), label);
}

function buildBars() {
  const bar = clear($('#scene-bar'));
  bar.append(
    sel('cam', 'Camera', [['orbit', 'Orbit'], ['chase', 'Chase'], ['onboard', 'Onboard'], ['heli', 'Helicopter'], ['tv', 'Trackside TV'], ['free', 'Free']], UI.cam,
      (v) => { UI.cam = v; store.set('cam', v); sc('setCamera', v, R.targetId); }),
    h('label', { class: 'tb-field', id: 'follow-wrap', hidden: true }, h('span', null, 'Follow'),
      h('select', { id: 'follow', onchange: (e) => setFollow(e.target.value) })),
    sel('quality', 'Quality', [['low', 'Low'], ['medium', 'Medium'], ['high', 'High']], UI.quality,
      (v) => { UI.quality = v; store.set('quality', v); sc('setQuality', v); }),
    sel('tod', 'Light', [['day', 'Day'], ['sunset', 'Sunset'], ['night', 'Night']], UI.tod,
      (v) => { UI.tod = v; sc('setTimeOfDay', v); }),
    h('span', { class: 'tb-sep' }),
    sel('linecol', 'Line', [['speed', 'Speed'], ['throttle', 'Throttle / brake'], ['off', 'Off']], UI.line,
      (v) => { UI.line = v; sc('setOverlay', overlayState()); }),
    chk('Corners', 'corners', 'Corner numbers'),
    chk('Brake pts', 'brakeMarkers', 'Braking-point markers'),
    chk('Tags', 'tags', 'Driver tags above cars'),
    h('span', { class: 'tb-fill' }),
    h('span', { id: 'scene-stats', class: 'mono dim' }),
  );

  const rb = clear($('#replay'));
  rb.append(
    h('button', { id: 'play', class: 'btn icon', title: 'Play / pause (space)', onclick: togglePlay }, playIcon(false)),
    h('select', { id: 'speed', title: 'Replay speed', onchange: (e) => { R.speed = +e.target.value; } },
      [0.25, 0.5, 1, 2, 4, 8].map((v) => h('option', { value: v, selected: v === 1 }, `${v}×`))),
    h('input', { id: 'scrub', type: 'range', min: 0, max: 1000, step: 1, value: 0, 'aria-label': 'Replay position',
      oninput: (e) => { if (R.dur) setTime((+e.target.value / 1000) * R.dur); } }),
    h('span', { id: 'clock', class: 'mono' }, '–'),
  );
  document.addEventListener('keydown', (e) => {
    if (e.code !== 'Space' || /INPUT|SELECT|TEXTAREA|BUTTON/.test(e.target.tagName)) return;
    e.preventDefault(); togglePlay();
  });
  setReplayEnabled(false);
}

function playIcon(playing) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 12 12'); svg.setAttribute('width', '12'); svg.setAttribute('height', '12');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', playing ? 'M2 1h3v10H2zM7 1h3v10H7z' : 'M2.5 1l8 5-8 5z');
  p.setAttribute('fill', 'currentColor');
  svg.append(p);
  return svg;
}
function setReplayEnabled(onOff) {
  for (const id of ['play', 'speed', 'scrub']) { const el = $('#' + id); if (el) el.disabled = !onOff; }
  if (!onOff) { $('#clock').textContent = '–'; $('#hud').textContent = ''; }
}
function refreshPlayBtn() { const b = $('#play'); if (b) { clear(b).append(playIcon(R.playing)); b.setAttribute('aria-pressed', R.playing); } }

export function togglePlay() {
  if (R.kind !== 'lap' && R.kind !== 'race') return;
  if (!R.playing && R.kind === 'race' && R.t >= R.dur - 1e-3) R.t = 0;
  R.playing = !R.playing; refreshPlayBtn();
}
export function setTime(t) { if (R.dur) { R.t = clamp(t, 0, R.dur); R._dirty = true; } }

function setFollowOptions(opts, value) {
  const wrap = $('#follow-wrap'), s = $('#follow');
  clear(s);
  wrap.hidden = opts.length < 2;
  for (const [v, t] of opts) s.append(h('option', { value: v, selected: v === value }, t));
}
export function setFollow(v) {
  R.follow = v;
  const s = $('#follow'); if (s && s.value !== v) s.value = v;
  resolveTarget(true);
}
function resolveTarget(force) {
  let id = R.follow;
  if (R.kind === 'race') {
    if (R.follow === 'leader') id = R.order[0] || null;
    else if (R.follow === 'battle') id = R._battle || R.order[0] || null;
  }
  if (force || id !== R.targetId) { R.targetId = id; sc('setCamera', UI.cam, id); emit('follow', id); }
}

// ---------------------------------------------------------------- track + static car
export async function loadTrack(id) {
  const meta = trackById(id);
  const geom = await tryApi(`/api/track/${encodeURIComponent(id)}`);
  if (S.track !== id) return;                       // selection changed while loading
  S.trackGeom = geom;
  UI.tod = meta && meta.night ? 'night' : 'day';
  const todSel = $('#tod'); if (todSel) todSel.value = UI.tod;
  if (geom) sc('setTrack', geom, { timeOfDay: UI.tod });
  showStatic();
  emit('track');
}

export function carMeta(id, team, driverId, compound, codeOverride) {
  const d = driverById(driverId), L = S.liveries[team] || null;
  return {
    id, team, code: codeOverride || d?.code || String(driverId || '').slice(0, 3).toUpperCase(),
    number: (L && L.numbers && L.numbers[driverId]) ?? d?.number ?? 0,
    livery: L, compound: compoundLabel(compound),
  };
}

// One parked car on the start line so the scene is never empty.
export function showStatic() {
  const g = S.trackGeom;
  R.kind = 'static'; R.playing = false; R.t = 0; R.dur = 0; R.cars = []; R.race = null; R.order = [];
  R.poses.clear();
  setReplayEnabled(false); refreshPlayBtn();
  setFollowOptions([], null);
  if (!g || !S.team) { sc('setCars', []); return; }
  sc('setLine', null, { colorBy: 'off' });
  sc('setBrakeMarkers', []);
  sc('setCars', [carMeta('car0', S.team, S.driver, 'C4')]);
  R.poses.set('car0', { id: 'car0', x: g.x[0], y: g.y[0], z: (g.z && g.z[0]) || 0, psi: g.psi[0], speed: 0, kappa: 0, throttle: 0, brake: 0, brakeTemp: 100, xmode: false, harvesting: false, visible: true });
  R.follow = 'car0'; resolveTarget(true);
}

// ---------------------------------------------------------------- lap replay (1 car or N ghosts)
// items: [{result, label, color, team, driver}]
export function playLap(items) {
  const cars = items.map((it, i) => {
    const res = it.result, T = res.telemetry, L = res.line;
    const len = S.trackGeom?.length || (L.s[L.s.length - 1] + (L.s[1] - L.s[0]));
    return { id: `car${i}`, T, L, n: T.t.length, lapTime: res.lap_time, len, label: it.label, color: it.color, team: it.team || res.team, driver: it.driver || res.driver, compound: it.compound };
  });
  R.kind = 'lap'; R.cars = cars; R.race = null; R.order = [];
  R.t = 0; R.dur = Math.max(...cars.map((c) => c.lapTime)); R.playing = true;
  R.poses.clear();
  sc('setCars', cars.map((c) => carMeta(c.id, c.team, c.driver, c.compound)));
  const ref = items[0].result;
  sc('setLine', ref.line, { colorBy: UI.line, telemetry: ref.telemetry });
  sc('setOverlay', overlayState());
  // braking markers from the reference lap
  const marks = [];
  for (const c of ref.corners || []) {
    if (c.brake_s == null) continue;
    const p = posOnLine(ref.line, c.brake_s, cars[0].len);
    if (p) marks.push({ x: p.x, y: p.y, z: p.z });
  }
  sc('setBrakeMarkers', marks);
  setReplayEnabled(true); refreshPlayBtn();
  setFollowOptions(cars.map((c) => [c.id, c.label || c.id]), 'car0');
  R.follow = 'car0'; resolveTarget(true);
  R._dirty = true;
}

function posOnLine(L, s, len) {
  const n = L.s.length;
  if (!n) return null;
  s = ((s % len) + len) % len;
  let k = lowerBound(L.s, s);
  const i = k <= 0 ? n - 1 : k - 1, j = k >= n ? 0 : k;
  let s0 = L.s[i], s1 = L.s[j];
  if (k <= 0) s0 -= len;
  if (k >= n) s1 += len;
  const f = clamp((s - s0) / ((s1 - s0) || 1), 0, 1);
  return { x: lerp(L.x[i], L.x[j], f), y: lerp(L.y[i], L.y[j], f), z: lerp(L.z ? L.z[i] : 0, L.z ? L.z[j] : 0, f), psi: lerpAngle(L.psi[i], L.psi[j], f), i, j, f };
}

function lapPose(c, tGlobal) {
  const T = c.T, L = c.L, n = c.n, lt = c.lapTime;
  const t = ((tGlobal % lt) + lt) % lt;
  const k = lowerBound(T.t, t);
  const i = k <= 0 ? n - 1 : k - 1, j = k >= n ? 0 : k;
  let t0 = T.t[i], t1 = T.t[j];
  if (k <= 0) t0 -= lt;
  if (k >= n) t1 += lt;
  const f = clamp((t - t0) / ((t1 - t0) || 1), 0, 1);
  let s0 = T.s[i], s1 = T.s[j];
  if (k <= 0) s0 -= c.len;
  if (k >= n) s1 += c.len;
  const a = f < 0.5 ? i : j;                        // nearest sample for discrete channels
  const thr = lerp(T.throttle[i], T.throttle[j], f), brk = lerp(T.brake[i], T.brake[j], f);
  const harv = (T.p_harv ? T.p_harv[a] > 0 : false) && (thr < 0.05 || (T.lift ? T.lift[a] > 0 : false));
  return {
    id: c.id,
    x: lerp(L.x[i], L.x[j], f), y: lerp(L.y[i], L.y[j], f), z: lerp(L.z ? L.z[i] : 0, L.z ? L.z[j] : 0, f),
    psi: lerpAngle(L.psi[i], L.psi[j], f),
    speed: lerp(T.v[i], T.v[j], f) / 3.6,
    kappa: T.kappa ? lerp(T.kappa[i], T.kappa[j], f) : 0,
    throttle: thr, brake: brk,
    brakeTemp: Math.max(T.brake_temp_f ? T.brake_temp_f[a] : 300, T.brake_temp_r ? T.brake_temp_r[a] : 300),
    xmode: T.mode ? T.mode[a] === 1 : false, harvesting: harv, visible: true,
    hud: { s: ((lerp(s0, s1, f) % c.len) + c.len) % c.len, t, gear: T.gear ? T.gear[a] : null, soc: T.soc ? T.soc[a] : null, label: c.label },
  };
}

// lap distance -> replay time for the reference car (chart cursor -> clock)
export function timeAtS(s) {
  const c = R.cars[0];
  if (R.kind !== 'lap' || !c) return null;
  const T = c.T, n = c.n;
  const k = lowerBound(T.s, s);
  if (k <= 0) return T.t[0];
  if (k >= n) return lerp(T.t[n - 1], c.lapTime, clamp((s - T.s[n - 1]) / ((c.len - T.s[n - 1]) || 1), 0, 1));
  const f = (s - T.s[k - 1]) / ((T.s[k] - T.s[k - 1]) || 1);
  return lerp(T.t[k - 1], T.t[k], f);
}

// ---------------------------------------------------------------- race replay
export function playRace(res) {
  const rp = res.replay, line = res.line;
  if (!rp || !line || !rp.t || !rp.t.length) { toast('Race result has no replay data.', 'warn'); return; }
  const n = line.x.length, len = res.lap_length || S.trackGeom?.length || line.s[n - 1];
  const kappa = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n;
    let dpsi = line.psi[j] - line.psi[i];
    while (dpsi > Math.PI) dpsi -= 2 * Math.PI; while (dpsi < -Math.PI) dpsi += 2 * Math.PI;
    let ds = line.s[j] - line.s[i]; if (ds <= 0) ds += len;
    kappa[i] = dpsi / (ds || 1);
  }
  const status = Object.fromEntries((res.classification || []).map((c) => [c.code, c.status]));
  const cars = [];
  for (const e of res.entries || []) {
    const rc = rp.cars[e.code];
    if (!rc) continue;
    let lastMove = rc.d.length - 1;
    while (lastMove > 0 && rc.d[lastMove] - rc.d[lastMove - 1] < 1e-6) lastMove--;
    const stints = (res.strategy && res.strategy[e.code]) || [];
    cars.push({ id: e.code, code: e.code, team: e.team, driver: e.driver, number: e.number, d: rc.d, pit: rc.pit || [], speed: rc.speed || [], cidx: rc.compound_idx || [], lastMove, dnf: status[e.code] === 'DNF', stints });
  }
  const side = (S.trackGeom?.pit?.side || 'R') === 'L' ? 1 : -1;
  R.kind = 'race'; R.cars = cars; R.t = 0; R.dur = rp.t[rp.t.length - 1]; R.playing = true;
  R.race = { res, T: rp.t, line, len, kappa, pitOff: side * ((S.trackGeom?.width || 12) / 2 + 9), laps: res.laps };
  R.poses.clear(); R.order = cars.map((c) => c.id); R._battle = null; R._battleAt = -99;
  sc('setCars', cars.map((c) => {
    const m = carMeta(c.id, c.team, c.driver, c.stints[0]?.label || 'medium', c.code);
    if (c.number != null) m.number = c.number;
    return m;
  }));
  sc('setLine', line, { colorBy: 'off' });
  sc('setBrakeMarkers', []);
  setReplayEnabled(true); refreshPlayBtn();
  setFollowOptions([['leader', 'Leader'], ['battle', 'Closest battle'], ...cars.map((c) => [c.id, c.code])], 'leader');
  R.follow = 'leader'; resolveTarget(true);
  R._dirty = true;
}

export function raceCompound(c, idx) {       // compound label of car c at replay sample idx
  const ci = c.cidx.length ? c.cidx[Math.min(idx, c.cidx.length - 1)] : 0;
  const st = c.stints;
  if (st.length && ci >= 0 && ci < st.length && R.race.cidxIsStint) return st[ci].label;
  return R.race.compoundNames ? (R.race.compoundNames[ci] || 'medium') : (st[Math.min(ci, st.length - 1)]?.label || 'medium');
}

function racePoses(t) {
  const rc = R.race, T = rc.T, m = T.length;
  const k = lowerBound(T, t);
  const i = clamp(k - 1, 0, Math.max(0, m - 2)), j = Math.min(m - 1, i + 1);
  const f = clamp((t - T[i]) / ((T[j] - T[i]) || 1), 0, 1);
  const dtS = (T[j] - T[i]) || 1;
  const out = [];
  for (const c of R.cars) {
    const d = lerp(c.d[i], c.d[j], f);
    const p = posOnLine(rc.line, d, rc.len);
    const pit = c.pit.length ? lerp(c.pit[i] || 0, c.pit[j] || 0, f) : 0;
    const v = c.speed.length ? lerp(c.speed[i], c.speed[j], f) / 3.6 : (c.d[j] - c.d[i]) / dtS;
    const acc = c.speed.length ? (c.speed[j] - c.speed[i]) / 3.6 / dtS : 0;
    const off = pit * rc.pitOff;
    const kap = rc.kappa[p.i];
    const stopped = i >= c.lastMove && c.lastMove < m - 2;
    out.push({
      id: c.id, x: p.x - Math.sin(p.psi) * off, y: p.y + Math.cos(p.psi) * off, z: p.z, psi: p.psi,
      speed: v, kappa: kap, throttle: acc > -1.5 ? (pit > 0.5 ? 0.3 : 1) : 0, brake: clamp(-acc / 35, 0, 1),
      brakeTemp: 350 + clamp(-acc / 35, 0, 1) * 500, xmode: Math.abs(kap) < 1 / 1100 && v > 55 && pit < 0.5,
      harvesting: false, visible: !(c.dnf && stopped && t > T[c.lastMove] + 4),
      hud: { d, lap: Math.max(0, Math.floor(d / rc.len)) + 1, pit: pit > 0.5, idx: f < 0.5 ? i : j, label: c.code },
    });
  }
  return out;
}

// ---------------------------------------------------------------- frame
function frame(dt) {
  if (!scene) return;
  const live = R.kind === 'lap' || R.kind === 'race';
  if (live && R.playing) {
    R.t += dt * R.speed;
    if (R.t >= R.dur) {
      if (R.kind === 'race') { R.t = R.dur; R.playing = false; refreshPlayBtn(); } else R.t %= R.dur;
    }
  }
  let poses;
  if (R.kind === 'lap') poses = R.cars.map((c) => lapPose(c, R.t));
  else if (R.kind === 'race') poses = racePoses(R.t);
  else poses = [...R.poses.values()];
  if (live) {
    R.poses.clear();
    for (const p of poses) R.poses.set(p.id, p);
    if (R.kind === 'race') {
      R.order = poses.filter((p) => p.visible).sort((a, b) => b.hud.d - a.hud.d).map((p) => p.id);
      if (R.follow === 'battle' && Math.abs(R.t - R._battleAt) > 4) {
        R._battleAt = R.t;
        let best = Infinity, who = null;
        for (let q = 1; q < R.order.length; q++) {
          const a = R.poses.get(R.order[q - 1]), b = R.poses.get(R.order[q]);
          if (a.hud.pit || b.hud.pit) continue;
          const gap = a.hud.d - b.hud.d;
          if (gap < best) { best = gap; who = b.id; }
        }
        R._battle = who;
      }
      resolveTarget(false);
    }
  }
  sc('updateCars', poses);
  if (live) {
    const scrub = $('#scrub');
    if (scrub && document.activeElement !== scrub) scrub.value = Math.round((R.t / R.dur) * 1000);
    updateHud();
    for (const fn of tickFns) { try { fn(R); } catch (e) { console.error(e); } }
  }
}

function updateHud() {
  const clock = $('#clock'), hud = $('#hud');
  const p = R.poses.get(R.targetId) || R.poses.values().next().value;
  if (R.kind === 'lap') {
    clock.textContent = `${fmtLap(R.t)} / ${fmtLap(R.dur)}`;
    if (p) hud.textContent = `${p.hud.label ? p.hud.label + '   ' : ''}${Math.round(p.speed * 3.6)} km/h   G${p.hud.gear ?? '–'}   ${p.xmode ? 'X' : 'Z'}-mode${p.harvesting ? '   harvest' : ''}`;
  } else if (R.kind === 'race') {
    const lead = R.poses.get(R.order[0]);
    const lap = lead ? Math.min(R.race.laps, lead.hud.lap) : 0;
    clock.textContent = `Lap ${lap}/${R.race.laps}  ${fmtRace(R.t)}`;
    if (p) hud.textContent = `${p.hud.label}   P${R.order.indexOf(p.id) + 1}   ${Math.round(p.speed * 3.6)} km/h${p.hud.pit ? '   PIT' : ''}`;
  }
}

// Re-apply liveries (after the livery editor saved or the catalogue loaded).
on('liveries', () => { for (const [team, L] of Object.entries(S.liveries)) sc('setLivery', team, L); });
