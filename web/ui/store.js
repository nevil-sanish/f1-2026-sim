// Shared application state and a tiny event bus.
import { luminance, CATEGORICAL } from './util.js';

export const S = {
  tracks: [], teams: [], drivers: [], liveries: {}, dataStatus: null,
  track: null, team: null, driver: null, trackGeom: null,
  schema: null, overrides: {}, driverOverrides: {}, locks: new Set(),
  mode: 'lap', diffOnly: false,
  lap: null, compare: null, compareConfigs: [], race: null, raceEntries: [], opt: null,
  sceneKind: 'none', sceneNotes: [],
};

const listeners = {};
export function on(evt, fn) { (listeners[evt] ??= []).push(fn); }
export function emit(evt, ...args) {
  for (const fn of listeners[evt] || []) { try { fn(...args); } catch (e) { console.error(`[${evt}]`, e); } }
}

export const trackById = (id) => S.tracks.find((t) => t.id === id) || null;
export const teamById = (id) => S.teams.find((t) => t.id === id) || null;
export const driverById = (id) => S.drivers.find((d) => d.id === id) || null;
export const teamName = (id) => teamById(id)?.name || id || '';
export const driverCode = (id) => driverById(id)?.code || (id || '').slice(0, 3).toUpperCase();

// A team colour that is readable on the dark chart background.
export function teamColor(teamId) {
  const L = S.liveries[teamId];
  if (!L) return CATEGORICAL[Math.abs(hash(teamId || '')) % CATEGORICAL.length];
  const cands = [L.base, L.accent, L.secondary].filter((c) => /^#[0-9a-f]{6}$/i.test(c || ''));
  return cands.find((c) => luminance(c) > 0.12) || cands.sort((a, b) => luminance(b) - luminance(a))[0] || '#8a96a3';
}
function hash(s) { let x = 0; for (const c of s) x = (x * 31 + c.charCodeAt(0)) | 0; return x; }

// Colours for a list of configs: team colour first, categorical when a team repeats.
export function seriesColors(teamIds) {
  const used = new Set(), out = [];
  let k = 0;
  for (const t of teamIds) {
    let c = teamColor(t);
    while (used.has(c)) c = CATEGORICAL[k++ % CATEGORICAL.length] + (k > CATEGORICAL.length ? '' : '');
    used.add(c); out.push(c);
  }
  return out;
}

export function paramValue(key) {
  if (key in S.overrides) return S.overrides[key];
  const p = S.schema?.params.find((q) => q.key === key);
  return p ? (p.preset ?? p.default) : undefined;
}

// Body for /api/simulate from the current selections.
export function currentConfig(extra = {}) {
  return {
    track: S.track, team: S.team, driver: S.driver,
    overrides: { ...S.overrides }, driver_overrides: { ...S.driverOverrides },
    ...extra,
  };
}

// Pirelli names C1 (hardest) .. C5 (softest); which three are hard/medium/soft depends on the event.
// Without the per-event nomination this is only a display approximation.
export function compoundLabel(c) {
  if (!c) return 'medium';
  const s = String(c).toLowerCase();
  if (['soft', 'medium', 'hard', 'intermediate', 'wet'].includes(s)) return s;
  const n = parseInt(s.replace(/\D/g, ''), 10);
  if (!n) return 'medium';
  return n <= 2 ? 'hard' : n === 3 ? 'medium' : 'soft';
}
