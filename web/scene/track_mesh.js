// Track geometry: road ribbon, kerbs, run-off, walls, verge, start line, pit lane, and trackside dressing.
// Input is the GET /api/track/<id> JSON (docs/API.md). World frame x east, y north, z up; scene is y-up.
import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { ASSET_BASE, loadTexture, canvasTexture, mulberry32, smoothstep, clamp } from './util.js';

let texPromise = null;
// Shared CC0 textures; any that fail to load stay null and the materials fall back to flat colour.
export function loadTrackTextures(notes) {
  if (!texPromise) {
    const t = (n, srgb) => loadTexture(`${ASSET_BASE}textures/${n}`, { srgb });
    texPromise = Promise.all([
      t('asphalt_diff.jpg', true), t('asphalt_nor.jpg'), t('gravel_diff.jpg', true), t('grass_diff.jpg', true),
      t('concrete_diff.jpg', true),
    ]).then(([asphalt, asphaltN, gravel, grass, concrete]) => {
      const miss = Object.entries({ asphalt, gravel, grass, concrete }).filter(([, v]) => !v).map(([k]) => k);
      if (miss.length) notes?.add(`Missing textures (${miss.join(', ')}); using flat colours. Run scripts/fetch_assets.py.`);
      return { asphalt, asphaltN, gravel, grass, concrete };
    });
  }
  return texPromise;
}

function kerbTexture() {
  const t = canvasTexture(8, 64, (c, w, h) => {
    c.fillStyle = '#d3211f'; c.fillRect(0, 0, w, h / 2);
    c.fillStyle = '#f2f2f2'; c.fillRect(0, h / 2, w, h / 2);
  }, { repeat: true });
  return t;
}

function checkerTexture() {
  const t = canvasTexture(16, 2, (c, w, h) => {
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      c.fillStyle = (x + y) % 2 ? '#f4f4f4' : '#101010';
      c.fillRect(x, y, 1, 1);
    }
  });
  t.magFilter = THREE.NearestFilter; t.minFilter = THREE.NearestFilter; t.generateMipmaps = false;
  return t;
}

export const TILE = 8;   // metres per texture repeat

/**
 * @returns {{group, n, ds, half, bounds, groundY, pose, nearest, tvPoints, lampPositions, dispose}}
 */
export function buildTrack(geom, tex, { quality = 'medium', seed = 7 } = {}) {
  const n = geom.x.length;
  const ds = geom.ds;
  const half = (geom.width || 12) / 2;
  const kerbW = geom.kerb_width || 1.2;
  const X = geom.x, Y = geom.y, Z = geom.z || new Array(n).fill(0), PSI = geom.psi, KAP = geom.kappa || new Array(n).fill(0);
  const street = geom.type === 'street';
  const owned = [];
  const own = (o) => { owned.push(o); return o; };

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, minZ = Infinity, maxZ = -Infinity;
  for (let i = 0; i < n; i++) {
    if (X[i] < minX) minX = X[i]; if (X[i] > maxX) maxX = X[i];
    if (Y[i] < minY) minY = Y[i]; if (Y[i] > maxY) maxY = Y[i];
    if (Z[i] < minZ) minZ = Z[i]; if (Z[i] > maxZ) maxZ = Z[i];
  }
  const groundY = minZ - 0.35;

  // Smoothed |curvature| (max over a window) so wide bands never fold over the inside of a tight bend.
  const kmax = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let m = 0;
    for (let d = -6; d <= 6; d++) m = Math.max(m, Math.abs(KAP[(i + d + n) % n]));
    kmax[i] = m;
  }
  // Lateral offset (positive = left) for a band edge at distance `v` from the centreline on `side` (+1 left, -1 right).
  const lat = (i, side, v) => {
    const k = KAP[i];
    if (k * side > 0 && kmax[i] > 1e-4) v = Math.min(v, Math.max(half + 0.3, 0.8 / kmax[i]));
    return side * v;
  };

  const idxRange = (i0, i1) => {
    const cnt = ((i1 - i0 + n) % n) + 1;
    const out = new Array(cnt);
    for (let k = 0; k < cnt; k++) out[k] = (i0 + k) % n;
    return out;
  };

  // ribbon over sample indices. offs(i, k) -> [offLeft, offRight] lateral offsets (positive left); yOff added to elevation.
  function ribbon(idx, offs, yOff, tile = TILE, vStart = 0) {
    const m = idx.length;
    const pos = new Float32Array(m * 6), uv = new Float32Array(m * 4);
    for (let k = 0; k < m; k++) {
      const i = idx[k] % n;
      const [a, b] = offs(i, k);
      const lo = Math.max(a, b), hi = Math.min(a, b);   // lo = left-most
      const nx = -Math.sin(PSI[i]), ny = Math.cos(PSI[i]);
      const y = Z[i] + yOff;
      pos.set([X[i] + nx * lo, y, -(Y[i] + ny * lo), X[i] + nx * hi, y, -(Y[i] + ny * hi)], k * 6);
      const v = (vStart + k * ds) / tile;
      uv.set([lo / tile, v, hi / tile, v], k * 4);
    }
    const ind = [];
    for (let k = 0; k < m - 1; k++) {
      const a = k * 2, b = a + 1, c = a + 2, d = a + 3;
      ind.push(a, b, c, b, d, c);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
    g.setIndex(ind);
    g.computeVertexNormals();
    return g;
  }

  // vertical wall at lateral offset off(i), height h, double sided
  function wall(idx, off, h, baseY = 0) {
    const m = idx.length;
    const pos = new Float32Array(m * 6), uv = new Float32Array(m * 4);
    for (let k = 0; k < m; k++) {
      const i = idx[k] % n;
      const o = off(i);
      const x = X[i] - Math.sin(PSI[i]) * o, yy = Y[i] + Math.cos(PSI[i]) * o;
      pos.set([x, Z[i] + baseY, -yy, x, Z[i] + baseY + h, -yy], k * 6);
      const u = k * ds / 6;
      uv.set([u, 0, u, h / 6], k * 4);
    }
    const ind = [];
    for (let k = 0; k < m - 1; k++) { const a = k * 2; ind.push(a, a + 1, a + 2, a + 1, a + 3, a + 2); }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
    g.setIndex(ind);
    g.computeVertexNormals();
    return g;
  }

  const buckets = {};
  const add = (key, g) => { (buckets[key] ||= []).push(g); };

  // ---- road
  const full = idxRange(0, n - 1).concat([0]);
  add('road', ribbon(full, (i) => [half, -half], 0));

  // ---- coverage arrays per side
  const cov = (v) => ({ L: new Float32Array(n).fill(v), R: new Float32Array(n).fill(v) });
  const kerbC = { L: new Uint8Array(n), R: new Uint8Array(n) };
  const gravelC = cov(0), asphC = cov(0), wallC = cov(0);
  for (const k of geom.kerbs || []) for (const i of idxRange(k.i0, k.i1)) kerbC[k.side][i] = 1;
  for (const r of geom.runoff || []) {
    for (const i of idxRange(r.i0, r.i1)) {
      if (r.kind === 'gravel') gravelC[r.side][i] = Math.max(gravelC[r.side][i], r.depth);
      else if (r.kind === 'asphalt') asphC[r.side][i] = Math.max(asphC[r.side][i], r.depth);
      else if (r.kind === 'wall') wallC[r.side][i] = wallC[r.side][i] ? Math.min(wallC[r.side][i], r.depth) : r.depth;
    }
  }
  const runs = (flag) => {
    const out = [];
    let start = -1;
    // start scanning at an index that is 0 so wrapped runs stay contiguous
    let first = 0;
    while (first < n && flag(first)) first++;
    if (first === n) return [Array.from({ length: n }, (_, i) => i).concat([0])];
    for (let t = 1; t <= n; t++) {
      const i = (first + t) % n;
      const on = flag(i);
      if (on && start < 0) start = t;
      if (!on && start >= 0) { out.push(idxRange((first + start) % n, (first + t - 1) % n)); start = -1; }
    }
    return out;
  };
  const sgn = { L: 1, R: -1 };

  for (const side of ['L', 'R']) {
    const s = sgn[side];
    // verge + skirt (grass)
    const vergeW = 70;
    add('grass', ribbon(full, (i) => [lat(i, s, half), lat(i, s, half + vergeW)], -0.07, 10));
    const skirt = ribbon(full, (i) => {
      const drop = Math.max(0.5, Z[i] - groundY);
      return [lat(i, s, half + vergeW), lat(i, s, half + vergeW + drop * 3)];
    }, -0.07, 10);
    // pull the outer edge down to the ground plane
    {
      const p = skirt.attributes.position;
      for (let k = 0; k < p.count; k++) if (k % 2 === (s > 0 ? 0 : 1)) p.setY(k, groundY);
      skirt.computeVertexNormals();
    }
    add('grass', skirt);

    for (const run of runs((i) => gravelC[side][i] > 0)) {
      add('gravel', ribbon(run, (i) => [lat(i, s, half + 0.5), lat(i, s, half + gravelC[side][i])], -0.05, 6));
    }
    for (const run of runs((i) => asphC[side][i] > 0)) {
      add('runoff', ribbon(run, (i) => [lat(i, s, half + 0.5), lat(i, s, half + asphC[side][i])], -0.035, 8));
    }
    for (const run of runs((i) => kerbC[side][i] > 0)) {
      add('kerb', ribbon(run, (i) => [lat(i, s, half - 0.05), lat(i, s, half + kerbW)], 0.025, 2));
    }
    for (const run of runs((i) => wallC[side][i] > 0)) {
      if (street) add('runoff', ribbon(run, (i) => [lat(i, s, half + 0.5), lat(i, s, wallC[side][i] + half)], -0.035, 8));
      add('wall', wall(run, (i) => lat(i, s, half + wallC[side][i]), 1.1));
    }
  }

  // ---- pit lane
  let pitInfo = null;
  if (geom.pit && geom.pit.entry_s != null) {
    const ie = Math.round(geom.pit.entry_s / ds) % n, ix = Math.round(geom.pit.exit_s / ds) % n;
    const run = idxRange(ie, ix);
    const s = geom.pit.side === 'L' ? 1 : -1;
    const m = run.length;
    if (m > 20 && m < n / 2) {
      const ramp = 14;
      const gapAt = (i, k) => 1.5 + 5 * Math.min(smoothstep(0, ramp, k), smoothstep(0, ramp, m - 1 - k));
      add('pit', ribbon(run, (i, k) => [s * (half + gapAt(i, k) - 1.5 * 0), s * (half + gapAt(i, k) + 12)], -0.01, 8));
      add('wall', wall(run.slice(ramp, m - ramp), (i) => s * (half + 6.5 + 12.5), 3.2));
      add('line', ribbon(run, (i, k) => [s * (half + gapAt(i, k) + 4.4), s * (half + gapAt(i, k) + 4.65)], 0.005, 4));
      pitInfo = { ie, ix };
    }
  }

  // ---- materials
  const mat = (o) => own(new THREE.MeshStandardMaterial(o));
  const asphaltN = tex.asphaltN;
  const materials = {
    road: mat({ color: 0x8c8c90, map: tex.asphalt, normalMap: quality === 'low' ? null : asphaltN, roughness: 0.9, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 }),
    runoff: mat({ color: 0x6e6e72, map: tex.asphalt, roughness: 0.95, polygonOffset: true, polygonOffsetFactor: -0.5, polygonOffsetUnits: -0.5 }),
    pit: mat({ color: 0x77777b, map: tex.asphalt, roughness: 0.9, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 }),
    gravel: mat({ color: 0xb8a58a, map: tex.gravel, roughness: 1 }),
    grass: mat({ color: 0x6f9a52, map: tex.grass, roughness: 1 }),
    kerb: mat({ color: 0xffffff, map: own(kerbTexture()), roughness: 0.6, polygonOffset: true, polygonOffsetFactor: -3, polygonOffsetUnits: -3 }),
    line: mat({ color: 0xf2f2f2, roughness: 0.7, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4 }),
    wall: mat({ color: 0xcfcfd2, map: tex.concrete, roughness: 0.85, side: THREE.DoubleSide }),
  };
  const group = new THREE.Group();
  group.name = 'track';
  for (const [key, list] of Object.entries(buckets)) {
    if (!list.length) continue;
    const g = own(list.length === 1 ? list[0] : mergeGeometries(list, false));
    const mesh = new THREE.Mesh(g, materials[key]);
    mesh.receiveShadow = true;
    mesh.castShadow = key === 'wall';
    mesh.name = `track_${key}`;
    group.add(mesh);
    if (list.length > 1) list.forEach((l) => l.dispose());
  }

  // ---- start / finish line + gantry
  {
    const g = ribbon([0, 1], () => [half, -half], 0.012, 1);
    // checker: u across the road (16 columns), v along 3 m (2 rows)
    const uv = g.attributes.uv;
    uv.setXY(0, 0, 0); uv.setXY(1, 1, 0); uv.setXY(2, 0, 1); uv.setXY(3, 1, 1);
    const m = new THREE.Mesh(own(g), mat({ color: 0xffffff, map: own(checkerTexture()), roughness: 0.6, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4 }));
    m.receiveShadow = true;
    group.add(m);

    const gm = mat({ color: 0x2a2c31, roughness: 0.5, metalness: 0.6 });
    const gantry = new THREE.Group();
    const w = half * 2 + 8;
    const beam = new THREE.Mesh(new THREE.BoxGeometry(1.2, 0.9, w), gm);
    beam.position.y = 7;
    const lightsMat = own(new THREE.MeshStandardMaterial({ color: 0x220000, emissive: 0xff1a10, emissiveIntensity: 0.4 }));
    const lights = new THREE.Mesh(new THREE.BoxGeometry(0.4, 0.5, w * 0.5), lightsMat);
    lights.position.set(-0.7, 7, 0);
    for (const sgn2 of [-1, 1]) {
      const post = new THREE.Mesh(new THREE.BoxGeometry(0.9, 7, 0.9), gm);
      post.position.set(0, 3.5, sgn2 * (w / 2 - 0.5));
      gantry.add(post);
    }
    gantry.add(beam, lights);
    gantry.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.geometry.userData.owned = true; owned.push(o.geometry); } });
    gantry.position.set(X[0], Z[0], -Y[0]);
    gantry.rotation.y = PSI[0];
    group.add(gantry);
  }

  // ---- spatial hash for nearest-sample queries and dressing placement
  const CELL = 50;
  const grid = new Map();
  for (let i = 0; i < n; i++) {
    const key = `${Math.floor(X[i] / CELL)},${Math.floor(Y[i] / CELL)}`;
    let a = grid.get(key); if (!a) grid.set(key, a = []);
    a.push(i);
  }
  function nearest(x, y, maxR = 150) {
    const cx = Math.floor(x / CELL), cy = Math.floor(y / CELL), r = Math.ceil(maxR / CELL);
    let best = -1, bd = maxR * maxR;
    for (let a = -r; a <= r; a++) for (let b = -r; b <= r; b++) {
      const arr = grid.get(`${cx + a},${cy + b}`);
      if (!arr) continue;
      for (const i of arr) {
        const d = (X[i] - x) ** 2 + (Y[i] - y) ** 2;
        if (d < bd) { bd = d; best = i; }
      }
    }
    return best < 0 ? null : { i: best, dist: Math.sqrt(bd) };
  }

  // ---- trackside dressing: trees (permanent) or buildings (street), instanced
  const rng = mulberry32(seed);
  const count = quality === 'low' ? 150 : quality === 'medium' ? 500 : 1000;
  const dress = [];
  const minDist = street ? 26 : 80, maxDist = street ? 70 : 260;
  let tries = 0;
  while (dress.length < count && tries++ < count * 25) {
    const i = Math.floor(rng() * n);
    const side = rng() < 0.5 ? 1 : -1;
    const d = minDist + rng() * (maxDist - minDist);
    const o = half + d;
    const px = X[i] - Math.sin(PSI[i]) * o * side, py = Y[i] + Math.cos(PSI[i]) * o * side;
    const nb = nearest(px, py, minDist - 2);
    if (nb) continue;
    const near = nearest(px, py, maxDist + 40);
    dress.push({ x: px, y: py, base: street ? (near ? Z[near.i] : Z[i]) - 0.2 : groundY, s: rng() });
  }
  if (dress.length) {
    let geo, dm;
    const dummy = new THREE.Object3D();
    if (street) {
      geo = new THREE.BoxGeometry(1, 1, 1).translate(0, 0.5, 0);
      dm = mat({ color: 0xd9cdb8, map: tex.concrete, roughness: 0.9 });
    } else {
      const trunk = new THREE.CylinderGeometry(0.4, 0.55, 3, 5).translate(0, 1.5, 0);
      const crown = new THREE.ConeGeometry(3.2, 11, 6).translate(0, 8, 0);
      const colour = (g, r, gg, b) => { const c = new Float32Array(g.attributes.position.count * 3); for (let k = 0; k < c.length; k += 3) { c[k] = r; c[k + 1] = gg; c[k + 2] = b; } g.setAttribute('color', new THREE.BufferAttribute(c, 3)); return g; };
      geo = mergeGeometries([colour(trunk.toNonIndexed(), 0.25, 0.16, 0.1), colour(crown.toNonIndexed(), 0.12, 0.3, 0.12)]);
      geo.deleteAttribute('uv');
      dm = mat({ color: 0xffffff, vertexColors: true, roughness: 1 });
    }
    own(geo);
    const inst = new THREE.InstancedMesh(geo, dm, dress.length);
    dress.forEach((d, k) => {
      dummy.position.set(d.x, d.base, -d.y);
      if (street) { dummy.scale.set(14 + d.s * 22, 14 + d.s * 30, 14 + ((d.s * 7) % 1) * 22); dummy.rotation.y = d.s * 6.28; }
      else { const sc = 0.8 + d.s * 0.9; dummy.scale.set(sc, sc, sc); dummy.rotation.y = d.s * 20; }
      dummy.updateMatrix();
      inst.setMatrixAt(k, dummy.matrix);
    });
    inst.castShadow = quality !== 'low';
    inst.receiveShadow = true;
    inst.name = 'dressing';
    group.add(inst);
  }

  // ---- lamp posts (emissive heads; the environment decides whether they matter)
  const lampPositions = [];
  const step = Math.max(1, Math.round(70 / ds));
  const lampGeo = own(new THREE.SphereGeometry(0.7, 8, 6));
  const lampMat = own(new THREE.MeshBasicMaterial({ color: 0xfff1c9, toneMapped: false }));
  const poleGeo = own(new THREE.CylinderGeometry(0.12, 0.16, 9, 5).translate(0, 4.5, 0));
  const poleMat = mat({ color: 0x555a60, roughness: 0.6, metalness: 0.5 });
  const lampIdx = [];
  for (let i = 0; i < n; i += step) lampIdx.push(i);
  const lampI = new THREE.InstancedMesh(lampGeo, lampMat, lampIdx.length);
  const poleI = new THREE.InstancedMesh(poleGeo, poleMat, lampIdx.length);
  const d3 = new THREE.Object3D();
  lampIdx.forEach((i, k) => {
    const side = k % 2 ? 1 : -1, o = (half + 7) * side;
    const x = X[i] - Math.sin(PSI[i]) * o, y = Y[i] + Math.cos(PSI[i]) * o;
    d3.rotation.set(0, 0, 0); d3.scale.set(1, 1, 1);
    d3.position.set(x, Z[i], -y); d3.updateMatrix(); poleI.setMatrixAt(k, d3.matrix);
    d3.position.set(x, Z[i] + 9.2, -y); d3.updateMatrix(); lampI.setMatrixAt(k, d3.matrix);
    lampPositions.push(new THREE.Vector3(x, Z[i] + 9.2, -y));
  });
  lampI.name = 'lamp_heads'; poleI.name = 'lamp_poles';
  group.add(poleI, lampI);

  // ---- TV camera positions beside the track (every ~350 m, alternating sides)
  const tvPoints = [];
  const tvStep = Math.max(1, Math.round(350 / ds));
  for (let i = 0, k = 0; i < n; i += tvStep, k++) {
    const side = k % 2 ? 1 : -1, o = (half + 22 + (k % 3) * 8) * side;
    tvPoints.push(new THREE.Vector3(X[i] - Math.sin(PSI[i]) * o, Z[i] + 5 + (k % 3) * 2, -(Y[i] + Math.cos(PSI[i]) * o)));
  }

  const pose = (i, latOff = 0) => {
    i = ((Math.round(i) % n) + n) % n;
    return {
      x: X[i] - Math.sin(PSI[i]) * latOff, y: Y[i] + Math.cos(PSI[i]) * latOff, z: Z[i], psi: PSI[i],
    };
  };

  return {
    group, n, ds, half, groundY, pose, nearest, tvPoints, lampPositions, pitInfo,
    lampHeads: lampI,
    gantryLights: null,
    bounds: { minX, maxX, minY, maxY, minZ, maxZ, cx: (minX + maxX) / 2, cy: (minY + maxY) / 2 },
    dispose() {
      for (const o of owned) o.dispose?.();
      group.removeFromParent();
    },
  };
}
