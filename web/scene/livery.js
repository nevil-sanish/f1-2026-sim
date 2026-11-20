// Livery system: colour resolution, paint/carbon materials and generated decals (number, driver code).
// Only colours, numbers and three-letter codes are ever drawn here: no logos, no badges.
import * as THREE from 'three';
import { canvasTexture } from './util.js';

export const REGIONS = ['nose', 'engine_cover', 'sidepods', 'front_wing', 'rear_wing', 'endplates', 'halo',
  'floor', 'wheel_covers'];

export const DEFAULT_LIVERY = {
  base: '#8a9099', secondary: '#16181c', accent: '#e8e8e8', finish: 'gloss',
  regions: {
    nose: 'base', engine_cover: 'base', sidepods: 'base', front_wing: 'secondary', rear_wing: 'secondary',
    endplates: 'base', halo: 'carbon', floor: 'carbon', wheel_covers: 'carbon',
  },
  numbers: {}, number_color: '#ffffff',
};

export const COMPOUND_COLORS = {
  soft: '#e0232b', medium: '#f2c81e', hard: '#f0f0f0', inter: '#3fae49', wet: '#2a6fd6',
  C5: '#e0232b', C4: '#e0232b', C3: '#f2c81e', C2: '#f0f0f0', C1: '#f0f0f0',
};

export const CARBON_HEX = '#0d0e10';

export function normaliseLivery(livery) {
  const l = livery || {};
  return {
    ...DEFAULT_LIVERY, ...l,
    regions: { ...DEFAULT_LIVERY.regions, ...(l.regions || {}) },
    numbers: { ...(l.numbers || {}) },
  };
}

// A region value is "base" | "secondary" | "accent" | "carbon" | "#rrggbb".
export function regionValue(livery, region) {
  if (region === 'body') return 'base';
  return livery.regions[region] ?? 'base';
}

export function isCarbon(livery, region) {
  return regionValue(livery, region) === 'carbon';
}

const tmp = new THREE.Color();
export function regionColor(livery, region, out = new THREE.Color()) {
  const v = regionValue(livery, region);
  if (v === 'carbon') return out.set(CARBON_HEX);
  if (v === 'base' || v === 'secondary' || v === 'accent') return out.set(livery[v]);
  try { return out.set(v); } catch { return out.set(livery.base); }
}

export function compoundColor(compound) {
  return COMPOUND_COLORS[compound] || COMPOUND_COLORS[String(compound || '').toLowerCase()] || '#f2c81e';
}

const FINISH = {
  gloss: { roughness: 0.32, metalness: 0.35, clearcoat: 1.0, clearcoatRoughness: 0.04 },
  satin: { roughness: 0.45, metalness: 0.3, clearcoat: 0.55, clearcoatRoughness: 0.22 },
  matte: { roughness: 0.62, metalness: 0.2, clearcoat: 0.12, clearcoatRoughness: 0.5 },
};

// Paint reads its colour from vertex colours so one material covers every painted region of a car.
export function makePaintMaterial(finish = 'gloss') {
  const m = new THREE.MeshPhysicalMaterial({ color: 0xffffff, vertexColors: true, envMapIntensity: 1.0 });
  applyFinish(m, finish);
  return m;
}

export function applyFinish(material, finish) {
  Object.assign(material, FINISH[finish] || FINISH.gloss);
  material.needsUpdate = true;
}

let carbonTextures = null;
// 2x2 twill weave: height drives a normal map, and tow direction drives a subtle roughness change.
function getCarbonTextures() {
  if (carbonTextures) return carbonTextures;
  const N = 128, cells = 8, cell = N / cells;
  const height = new Float32Array(N * N);
  const rough = new Uint8ClampedArray(N * N * 4);
  for (let y = 0; y < N; y++) {
    for (let x = 0; x < N; x++) {
      const cx = Math.floor(x / cell), cy = Math.floor(y / cell);
      const warp = ((cx + cy) % 4) < 2;
      const u = ((warp ? x : y) % cell) / cell;
      const along = ((warp ? y : x) % (cell * 2)) / (cell * 2);
      height[y * N + x] = Math.sin(u * Math.PI) * 0.7 + Math.sin(along * Math.PI) * 0.3;
      const r = warp ? 96 : 132;
      rough.set([r, r, r, 255], (y * N + x) * 4);
    }
  }
  const nrm = new Uint8ClampedArray(N * N * 4);
  const h = (x, y) => height[((y + N) % N) * N + ((x + N) % N)];
  for (let y = 0; y < N; y++) {
    for (let x = 0; x < N; x++) {
      const dx = (h(x + 1, y) - h(x - 1, y)) * 1.6, dy = (h(x, y + 1) - h(x, y - 1)) * 1.6;
      const inv = 1 / Math.hypot(dx, dy, 1);
      nrm.set([(-dx * inv * 0.5 + 0.5) * 255, (-dy * inv * 0.5 + 0.5) * 255, (inv * 0.5 + 0.5) * 255, 255],
        (y * N + x) * 4);
    }
  }
  const mk = (data) => {
    const t = new THREE.DataTexture(data, N, N, THREE.RGBAFormat);
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    t.magFilter = THREE.LinearFilter;
    t.minFilter = THREE.LinearMipmapLinearFilter;
    t.generateMipmaps = true;
    t.anisotropy = 8;
    t.userData.shared = true;
    t.needsUpdate = true;
    return t;
  };
  carbonTextures = { normal: mk(nrm), rough: mk(rough) };
  return carbonTextures;
}

let carbonMaterial = null;
export function getCarbonMaterial() {
  if (!carbonMaterial) {
    const t = getCarbonTextures();
    carbonMaterial = new THREE.MeshPhysicalMaterial({
      color: 0x17181b, roughness: 0.9, metalness: 0.25, clearcoat: 0.7, clearcoatRoughness: 0.18,
      normalMap: t.normal, normalScale: new THREE.Vector2(0.35, 0.35), roughnessMap: t.rough,
    });
    carbonMaterial.userData.shared = true;
  }
  return carbonMaterial;
}

// Transparent decal with the text in `color` and a thin contrasting outline.
export function makeTextDecal(text, color = '#ffffff', { w = 256, h = 160 } = {}) {
  return canvasTexture(w, h, (ctx) => {
    ctx.clearRect(0, 0, w, h);
    let size = h * 0.86;
    ctx.font = `900 italic ${size}px "Arial Black", Arial, sans-serif`;
    const tw = ctx.measureText(text).width;
    if (tw > w * 0.9) size *= (w * 0.9) / tw;
    ctx.font = `900 italic ${size}px "Arial Black", Arial, sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const c = tmp.set(color);
    const lum = 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b;
    ctx.lineJoin = 'round';
    ctx.lineWidth = size * 0.07;
    ctx.strokeStyle = lum > 0.5 ? 'rgba(0,0,0,0.75)' : 'rgba(255,255,255,0.75)';
    ctx.strokeText(text, w / 2, h / 2 + size * 0.04);
    ctx.fillStyle = color;
    ctx.fillText(text, w / 2, h / 2 + size * 0.04);
  });
}

// Floating tag above a car: driver code on a dark pill with a team-colour bar.
export function makeTagTexture(code, teamColor = '#ffffff') {
  const w = 192, h = 72;
  return canvasTexture(w, h, (ctx) => {
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = 'rgba(12,14,18,0.86)';
    ctx.beginPath();
    ctx.roundRect(4, 6, w - 8, h - 12, 12);
    ctx.fill();
    ctx.fillStyle = teamColor;
    ctx.beginPath();
    ctx.roundRect(12, 16, 10, h - 32, 4);
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.font = '700 40px "Helvetica Neue", Arial, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(String(code || '').toUpperCase().slice(0, 4), w / 2 + 10, h / 2 + 3);
  });
}
