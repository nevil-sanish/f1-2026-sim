// Small DOM / fetch / formatting helpers shared by the UI modules.

export const $ = (sel, root = document) => root.querySelector(sel);

export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'selected' || k === 'open' || k === 'hidden') el[k] = v;
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export function clear(el) { el.textContent = ''; return el; }

// ---------------------------------------------------------------- toasts
export function toast(msg, level = 'error', ms = 6000) {
  const box = $('#toasts');
  if (!box) return;
  const t = h('div', { class: `toast ${level}`, role: 'status' },
    h('span', null, msg), h('button', { class: 'x', title: 'Dismiss', onclick: () => t.remove() }, '×'));
  box.append(t);
  while (box.children.length > 5) box.firstChild.remove();
  if (ms) setTimeout(() => t.remove(), ms);
}

// ---------------------------------------------------------------- API
export async function api(path, body) {
  const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  let res;
  try { res = await fetch(path, opt); } catch (e) { throw new Error(`${path}: server unreachable`); }
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
  if (!res.ok) throw new Error(`${path}: ${(data && data.error) || `HTTP ${res.status}`}`);
  if (data === null) throw new Error(`${path}: response was not JSON`);
  return data;
}

// GET/POST that never throws: shows a toast and returns the fallback.
export async function tryApi(path, body, fallback = null, quiet = false) {
  try { return await api(path, body); } catch (e) {
    if (!quiet) toast(e.message);
    console.warn(e.message);
    return fallback;
  }
}

export async function pollJob(id, onProgress, interval = 350) {
  let misses = 0;
  for (;;) {
    let j;
    try { j = await api(`/api/jobs/${id}`); misses = 0; } catch (e) {
      if (++misses > 5) throw e;
      await sleep(interval * 2); continue;
    }
    if (onProgress) onProgress(j.progress || 0, j.message || '');
    if (j.status === 'done') return j.result;
    if (j.status === 'error') throw new Error(j.error || j.message || 'job failed');
    await sleep(interval);
  }
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- formatting
export function fmtLap(t) {
  if (t == null || !isFinite(t)) return '–';
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${s.toFixed(3).padStart(6, '0')}`;
}
export function fmtRace(t) {
  if (t == null || !isFinite(t)) return '–';
  const hh = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return hh ? `${hh}:${String(m).padStart(2, '0')}:${s.toFixed(3).padStart(6, '0')}` : `${m}:${s.toFixed(3).padStart(6, '0')}`;
}
export function fmtGap(g, digits = 3) {
  if (g == null || g === '') return '';
  if (typeof g === 'string') return g;
  if (!isFinite(g)) return '–';
  return (g >= 0 ? '+' : '−') + Math.abs(g).toFixed(digits);
}
export function num(v, d = 1) {
  if (v == null || (typeof v === 'number' && !isFinite(v))) return '–';
  return typeof v === 'number' ? v.toFixed(d) : String(v);
}
export function decimals(step) {
  if (!step || step >= 1) return 0;
  const s = String(step);
  if (s.includes('e-')) return +s.split('e-')[1];
  return (s.split('.')[1] || '').length;
}
export function fmtDate(iso) {
  if (!iso) return 'unknown';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return d.toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

// ---------------------------------------------------------------- colour
export function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
  if (!m) return [136, 150, 163];
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
export function luminance(hex) {
  const [r, g, b] = hexToRgb(hex).map((c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
export function rgba(hex, a) { const [r, g, b] = hexToRgb(hex); return `rgba(${r},${g},${b},${a})`; }

export const CATEGORICAL = ['#7fb2e5', '#e2a93b', '#4cb782', '#d47fd0', '#e5786d', '#9d8cf0', '#58c4c9', '#c2c96a'];
export const COMPOUND_COLORS = { soft: '#e5484d', medium: '#f2c94c', hard: '#f1f3f5', intermediate: '#4cb782', wet: '#4a90d9' };

// ---------------------------------------------------------------- numeric
export function lowerBound(arr, x) {       // first index with arr[i] > x
  let lo = 0, hi = arr.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] > x) hi = mid; else lo = mid + 1; }
  return lo;
}
export function lerp(a, b, f) { return a + (b - a) * f; }
export function lerpAngle(a, b, f) {
  let d = b - a;
  while (d > Math.PI) d -= 2 * Math.PI;
  while (d < -Math.PI) d += 2 * Math.PI;
  return a + d * f;
}
export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export const store = {
  get(k, d) { try { const v = localStorage.getItem('f1sim.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('f1sim.' + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};
