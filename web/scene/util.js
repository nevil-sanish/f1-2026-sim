// Shared helpers: world<->scene mapping, small maths, asset probing.
import * as THREE from 'three';

// World frame is x east, y north, z up; the scene is y-up with -Z = north.
export function w2s(x, y, z, out = new THREE.Vector3()) {
  return out.set(x, z || 0, -y);
}

export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const lerp = (a, b, t) => a + (b - a) * t;
export const smoothstep = (a, b, x) => {
  const t = clamp((x - a) / (b - a), 0, 1);
  return t * t * (3 - 2 * t);
};
// Frame-rate independent exponential approach factor.
export const damp = (rate, dt) => 1 - Math.exp(-rate * dt);

export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const ASSET_BASE = new URL('../assets/', import.meta.url).href;

// Resolves to a texture or null; never rejects.
export function loadTexture(url, { srgb = false, repeat = null, anisotropy = 8 } = {}) {
  return new Promise((resolve) => {
    new THREE.TextureLoader().load(url, (tex) => {
      if (srgb) tex.colorSpace = THREE.SRGBColorSpace;
      tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
      if (repeat) tex.repeat.set(repeat[0], repeat[1]);
      tex.anisotropy = anisotropy;
      resolve(tex);
    }, undefined, () => resolve(null));
  });
}

export function canvasTexture(w, h, draw, { srgb = true, repeat = false } = {}) {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  draw(c.getContext('2d'), w, h);
  const tex = new THREE.CanvasTexture(c);
  if (srgb) tex.colorSpace = THREE.SRGBColorSpace;
  if (repeat) tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.anisotropy = 8;
  return tex;
}

let localIndexPromise = null;
// local_index.json (written by scripts/fetch_assets.py) lists the optional user-supplied files, so the
// scene never has to probe for them and litter the console with 404s.
export function loadLocalIndex() {
  if (!localIndexPromise) {
    localIndexPromise = fetch(ASSET_BASE + 'local_index.json')
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
  }
  return localIndexPromise;
}

export function disposeObject(obj) {
  obj.traverse((o) => {
    if (o.geometry && !o.geometry.userData.shared) o.geometry.dispose();
    const mats = Array.isArray(o.material) ? o.material : o.material ? [o.material] : [];
    for (const m of mats) {
      if (m.userData.shared) continue;
      for (const k of ['map', 'alphaMap', 'emissiveMap']) {
        if (m[k] && !m[k].userData.shared) m[k].dispose();
      }
      m.dispose();
    }
  });
  obj.removeFromParent();
}
