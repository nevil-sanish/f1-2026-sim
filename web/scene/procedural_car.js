// Procedural 2026-style F1 car, used when no GLB is supplied.
//
// Car frame: +X forward, +Y up, +Z right, origin on the ground at the wheelbase midpoint. Metres.
// The car is described as a "blueprint": named pivots (wheel steer/spin, aero flaps) and a flat list of
// parts, each with a geometry in its pivot's local frame, a material kind and a livery region.
// car_model.js turns a blueprint into scene objects (full detail) or one merged mesh (distant LOD).
import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

export const DIM = {
  wheelbase: 3.4, width: 1.9,
  frontAxleX: 1.7, rearAxleX: -1.7,
  front: { radius: 0.355, halfWidth: 0.14 },   // 2026: 25 mm narrower fronts
  rear: { radius: 0.36, halfWidth: 0.1875 },   // 30 mm narrower rears
  rimRadius: 0.232,
};
DIM.front.z = DIM.width / 2 - DIM.front.halfWidth;
DIM.rear.z = DIM.width / 2 - DIM.rear.halfWidth;

const V3 = (x, y, z) => new THREE.Vector3(x, y, z);

// ---------------------------------------------------------------- generic surface builders

// Skin a list of rings (equal-length arrays of Vector3) into a smooth closed tube.
// uv = (ring index fraction, position around the ring) so the bake step can paint stripes along the body.
function skin(rings, { capStart = false, capEnd = false } = {}) {
  const n = rings[0].length, m = rings.length;
  const pos = [], uv = [], idx = [];
  for (let i = 0; i < m; i++) {
    for (let k = 0; k < n; k++) {
      const p = rings[i][k];
      pos.push(p.x, p.y, p.z);
      uv.push(i / (m - 1), k / n);
    }
  }
  for (let i = 0; i < m - 1; i++) {
    for (let k = 0; k < n; k++) {
      const a = i * n + k, b = i * n + (k + 1) % n, c = a + n, d = b + n;
      idx.push(a, c, b, b, c, d);
    }
  }
  // Caps get their own vertices so the rim stays a crisp edge.
  const cap = (ring, flip, u) => {
    const base = pos.length / 3;
    const c = V3(0, 0, 0);
    for (const p of ring) { pos.push(p.x, p.y, p.z); uv.push(u, 0); c.add(p); }
    c.multiplyScalar(1 / n);
    pos.push(c.x, c.y, c.z); uv.push(u, 0);
    for (let k = 0; k < n; k++) {
      const a = base + k, b = base + (k + 1) % n;
      if (flip) idx.push(base + n, b, a); else idx.push(base + n, a, b);
    }
  };
  if (capStart) cap(rings[0], false, 0);
  if (capEnd) cap(rings[m - 1], true, 1);

  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
  g.setIndex(idx);
  orientOutward(g);
  g.computeVertexNormals();
  return g;
}

// Flip the winding if the signed volume says the surface is inside out (lets mirrored parts reuse code).
function orientOutward(g) {
  const p = g.attributes.position, idx = g.index.array;
  const c = new THREE.Box3().setFromBufferAttribute(p).getCenter(V3(0, 0, 0));
  let vol = 0;
  const a = V3(0, 0, 0), b = V3(0, 0, 0), d = V3(0, 0, 0);
  for (let i = 0; i < idx.length; i += 3) {
    a.fromBufferAttribute(p, idx[i]).sub(c);
    b.fromBufferAttribute(p, idx[i + 1]).sub(c);
    d.fromBufferAttribute(p, idx[i + 2]).sub(c);
    vol += a.dot(b.cross(d));
  }
  if (vol < 0) for (let i = 0; i < idx.length; i += 3) { const t = idx[i + 1]; idx[i + 1] = idx[i + 2]; idx[i + 2] = t; }
}

// Superellipse cross-section in the YZ plane: half-width w, separate top/bottom heights and squareness.
function sectionRing(s, n) {
  const ring = [];
  for (let k = 0; k < n; k++) {
    const t = (k / n) * Math.PI * 2;
    const c = Math.cos(t), sn = Math.sin(t);
    const e = sn >= 0 ? s.nt : s.nb, h = sn >= 0 ? s.ht : s.hb;
    ring.push(V3(s.x,
      s.y + h * Math.sign(sn) * Math.abs(sn) ** (2 / e),
      (s.z || 0) + s.w * Math.sign(c) * Math.abs(c) ** (2 / e)));
  }
  return ring;
}

// Loft: Catmull-Rom interpolate the section parameters along the car, then skin.
function loft(rows, keys, { ringN, step, capStart, capEnd, mirror = 1 }) {
  const secs = rows.map((r) => Object.fromEntries(keys.map((k, i) => [k, r[i]])));
  const cr = (p0, p1, p2, p3, t) => 0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
    + (-p0 + 3 * p1 - 3 * p2 + p3) * t * t * t);
  const rings = [];
  for (let i = 0; i < secs.length - 1; i++) {
    const s0 = secs[Math.max(0, i - 1)], s1 = secs[i], s2 = secs[i + 1], s3 = secs[Math.min(secs.length - 1, i + 2)];
    const sub = Math.max(1, Math.round(Math.abs(s2.x - s1.x) / step));
    for (let j = 0; j < sub + (i === secs.length - 2 ? 1 : 0); j++) {
      const t = j / sub, s = {};
      for (const k of keys) {
        s[k] = k === 'x' ? s1.x + (s2.x - s1.x) * t : cr(s0[k], s1[k], s2[k], s3[k], t);
        // Catmull-Rom may overshoot between a section pair; clamp to the pair for sizes.
        if (k !== 'x' && k !== 'y' && k !== 'z') s[k] = Math.max(Math.min(s1[k], s2[k]), Math.min(Math.max(s1[k], s2[k]), s[k]));
      }
      if (s.z !== undefined) s.z *= mirror;
      rings.push(sectionRing(s, ringN));
    }
  }
  return skin(rings, { capStart, capEnd });
}

// Closed aerofoil outline (u aft, v up), inverted camber for downforce, rotated so the trailing edge rises.
function airfoilRing(leX, leY, z, chord, angle, thickness, camber, n) {
  const pts = [];
  const ca = Math.cos(angle), sa = Math.sin(angle);
  const half = n / 2;
  for (let k = 0; k < n; k++) {
    const upper = k < half;
    const f = upper ? k / half : 1 - (k - half) / half;
    const x = 0.5 * (1 - Math.cos(f * Math.PI));
    const yt = 5 * thickness * (0.2969 * Math.sqrt(x) - 0.126 * x - 0.3516 * x * x + 0.2843 * x ** 3 - 0.1036 * x ** 4);
    const yc = -camber * 4 * x * (1 - x);
    const u = x * chord, v = (yc + (upper ? yt : -yt)) * chord;
    pts.push(V3(leX - (u * ca - v * sa), leY + (u * sa + v * ca), z));
  }
  return pts;
}

// Wing element from span stations {z, leX, leY, chord, angle}.
function wingElement(stations, { thickness = 0.1, camber = 0.05, n = 20 }) {
  return skin(stations.map((s) => airfoilRing(s.leX, s.leY, s.z, s.chord, s.angle, thickness, camber, n)),
    { capStart: true, capEnd: true });
}

// Flat plate from a side-profile polygon (x, y), extruded across the car and centred on z.
function plate(points, thickness, z = 0, bevel = 0.003) {
  const shape = new THREE.Shape(points.map(([x, y]) => new THREE.Vector2(x, y)));
  const g = new THREE.ExtrudeGeometry(shape, {
    depth: thickness - 2 * bevel, bevelEnabled: bevel > 0, bevelThickness: bevel, bevelSize: bevel, bevelSegments: 2,
  });
  g.translate(0, 0, z - thickness / 2 + bevel);
  return g;
}

// Aero-section strut between two points, wide axis kept roughly horizontal.
function strut(a, b, radius, flat = 2.4, sides = 8) {
  const dir = b.clone().sub(a);
  const len = dir.length();
  dir.normalize();
  const wide = V3(0, 1, 0).cross(dir);
  if (wide.lengthSq() < 1e-6) wide.set(1, 0, 0);
  wide.normalize();
  const thin = dir.clone().cross(wide).normalize();
  const g = new THREE.CylinderGeometry(radius, radius, len, sides, 1);
  g.scale(flat, 1, 1);
  g.applyMatrix4(new THREE.Matrix4().makeBasis(wide, dir, thin).setPosition(a.clone().add(b).multiplyScalar(0.5)));
  return g;
}

function tube(points, radius, tubular, radial, closed = false) {
  return new THREE.TubeGeometry(new THREE.CatmullRomCurve3(points, closed, 'centripetal'), tubular, radius, radial, closed);
}

const mirrorZ = (pts) => pts.map((p) => V3(p.x, p.y, -p.z));

function merge(list) {
  const flat = list.map((g) => {
    const n = g.index ? g.toNonIndexed() : g;
    for (const k of Object.keys(n.attributes)) if (!['position', 'normal', 'uv'].includes(k)) n.deleteAttribute(k);
    if (!n.attributes.uv) n.setAttribute('uv', new THREE.Float32BufferAttribute(new Float32Array(n.attributes.position.count * 2), 2));
    return n;
  });
  return mergeGeometries(flat, false);
}

// Planar UVs chosen per vertex from the dominant normal axis: enough for the tiling carbon weave.
function boxUV(g, scale = 14) {
  const p = g.attributes.position, nrm = g.attributes.normal;
  const uv = new Float32Array(p.count * 2);
  for (let i = 0; i < p.count; i++) {
    const ax = Math.abs(nrm.getX(i)), ay = Math.abs(nrm.getY(i)), az = Math.abs(nrm.getZ(i));
    let u, v;
    if (ay >= ax && ay >= az) { u = p.getX(i); v = p.getZ(i); } else if (ax >= az) { u = p.getZ(i); v = p.getY(i); } else { u = p.getX(i); v = p.getY(i); }
    uv[i * 2] = u * scale; uv[i * 2 + 1] = v * scale;
  }
  g.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
  return g;
}

// ---------------------------------------------------------------- the car

const BODY_KEYS = ['x', 'y', 'w', 'ht', 'hb', 'nt', 'nb'];
const POD_KEYS = ['x', 'y', 'z', 'w', 'ht', 'hb', 'nt', 'nb'];

// Nose tip to gearbox: one continuous spine. The nose droops onto the front wing mainplane.
const SPINE = [
  [2.80, 0.160, 0.030, 0.020, 0.020, 2.0, 2.0],
  [2.72, 0.170, 0.080, 0.036, 0.034, 2.2, 2.2],
  [2.45, 0.212, 0.118, 0.056, 0.052, 2.4, 2.4],
  [2.10, 0.282, 0.150, 0.076, 0.074, 2.5, 2.6],
  [1.70, 0.352, 0.185, 0.098, 0.110, 2.6, 2.8],
  [1.30, 0.392, 0.228, 0.124, 0.200, 2.8, 3.0],
  [0.90, 0.404, 0.270, 0.148, 0.310, 3.0, 3.2],
  [0.55, 0.405, 0.296, 0.160, 0.325, 3.0, 3.4],
  [0.20, 0.400, 0.312, 0.150, 0.320, 3.0, 3.5],
  [-0.15, 0.400, 0.318, 0.156, 0.320, 3.0, 3.5],
  [-0.50, 0.380, 0.316, 0.176, 0.300, 3.0, 3.5],
  [-1.00, 0.340, 0.270, 0.160, 0.260, 2.8, 3.2],
  [-1.50, 0.300, 0.190, 0.130, 0.220, 2.5, 3.0],
  [-1.95, 0.290, 0.115, 0.090, 0.130, 2.3, 2.5],
  [-2.28, 0.290, 0.055, 0.048, 0.055, 2.0, 2.0],
  [-2.37, 0.290, 0.018, 0.018, 0.018, 2.0, 2.0],
];
const NOSE_SPLIT_X = 1.30;

// Airbox and engine cover: tall narrow spine tapering down to the rear crash structure.
const ENGINE_COVER = [
  [-0.235, 0.705, 0.128, 0.205, 0.150, 2.4, 2.4],
  [-0.40, 0.690, 0.150, 0.245, 0.200, 2.4, 2.4],
  [-0.80, 0.600, 0.196, 0.300, 0.220, 2.3, 2.6],
  [-1.20, 0.500, 0.186, 0.300, 0.180, 2.2, 2.6],
  [-1.60, 0.420, 0.136, 0.236, 0.140, 2.1, 2.4],
  [-1.95, 0.360, 0.078, 0.150, 0.090, 2.0, 2.2],
  [-2.22, 0.320, 0.030, 0.066, 0.045, 2.0, 2.0],
];

// Sidepod: high inlet, deep undercut, downwash ramp into the coke-bottle waist.
const SIDEPOD = [
  [0.64, 0.470, 0.600, 0.170, 0.072, 0.072, 2.6, 2.6],
  [0.42, 0.456, 0.610, 0.212, 0.112, 0.128, 2.8, 2.8],
  [0.02, 0.430, 0.615, 0.243, 0.148, 0.200, 3.0, 3.0],
  [-0.45, 0.400, 0.600, 0.252, 0.154, 0.232, 3.0, 3.0],
  [-0.90, 0.352, 0.530, 0.246, 0.134, 0.218, 2.8, 3.0],
  [-1.30, 0.300, 0.420, 0.206, 0.100, 0.178, 2.6, 2.8],
  [-1.65, 0.260, 0.300, 0.148, 0.070, 0.130, 2.4, 2.4],
  [-1.96, 0.240, 0.200, 0.078, 0.040, 0.080, 2.0, 2.0],
];

// Split a lofted geometry at a station so two livery regions share one smooth surface (normals stay continuous).
function splitByX(g, x) {
  const p = g.attributes.position, idx = g.index.array;
  const fore = [], aft = [];
  for (let i = 0; i < idx.length; i += 3) {
    const cx = (p.getX(idx[i]) + p.getX(idx[i + 1]) + p.getX(idx[i + 2])) / 3;
    (cx > x ? fore : aft).push(idx[i], idx[i + 1], idx[i + 2]);
  }
  return [fore, aft].map((list) => {
    const part = new THREE.BufferGeometry();
    for (const k of ['position', 'normal', 'uv']) part.setAttribute(k, g.attributes[k]);
    part.setIndex(list);
    return part.toNonIndexed();
  });
}

function frontWingStations(q) {
  const N = Math.round(18 * q) + 4, half = 0.925;
  const st = [];
  for (let i = 0; i <= N; i++) {
    const z = -half + (2 * half * i) / N, a = Math.abs(z);
    st.push({
      z,
      leX: 2.87 - 0.36 * a ** 1.7,
      leY: 0.082 + 0.048 * Math.exp(-((z / 0.26) ** 2)),
      chord: 0.30 - 0.07 * a,
      angle: 0.05,
    });
  }
  return st;
}

function wheelParts(prefix, cfg, q, add) {
  const { radius: R, halfWidth: hw } = cfg;
  const rim = DIM.rimRadius;
  const seg = Math.round(44 * q) + 8;
  // Tyre: lathe of a rounded sidewall/shoulder profile; the lathe axis (Y) becomes the axle (Z).
  const prof = new THREE.SplineCurve([
    new THREE.Vector2(rim, -hw * 0.86), new THREE.Vector2(rim + 0.03, -hw * 0.98),
    new THREE.Vector2(R - 0.07, -hw * 1.0), new THREE.Vector2(R - 0.022, -hw * 0.95),
    new THREE.Vector2(R - 0.003, -hw * 0.80), new THREE.Vector2(R, -hw * 0.55),
    new THREE.Vector2(R, hw * 0.55), new THREE.Vector2(R - 0.003, hw * 0.80),
    new THREE.Vector2(R - 0.022, hw * 0.95), new THREE.Vector2(R - 0.07, hw * 1.0),
    new THREE.Vector2(rim + 0.03, hw * 0.98), new THREE.Vector2(rim, hw * 0.86),
  ]).getPoints(Math.round(26 * q) + 8);
  const tyre = new THREE.LatheGeometry(prof, seg).rotateX(Math.PI / 2);
  add({ name: `tyre_${prefix}`, kind: 'rubber', pivot: `wheel_${prefix}`, geometry: tyre });

  // Rim barrel with rolled lips.
  const barrel = new THREE.LatheGeometry([
    new THREE.Vector2(rim + 0.004, -hw * 0.86), new THREE.Vector2(rim - 0.012, -hw * 0.84),
    new THREE.Vector2(rim - 0.016, -hw * 0.6), new THREE.Vector2(rim - 0.016, hw * 0.6),
    new THREE.Vector2(rim - 0.012, hw * 0.84), new THREE.Vector2(rim + 0.004, hw * 0.86),
  ], seg).rotateX(Math.PI / 2);
  add({ name: `rim_${prefix}`, kind: 'metal', pivot: `wheel_${prefix}`, geometry: barrel });

  // Outboard wheel cover: dished disc with vent slots that show the brake disc behind.
  const cover = new THREE.Shape();
  cover.absarc(0, 0, rim - 0.014, 0, Math.PI * 2, false);
  const slots = 9;
  for (let i = 0; i < slots; i++) {
    const a0 = (i / slots) * Math.PI * 2, da = (Math.PI * 2 / slots) * 0.56;
    const hole = new THREE.Path();
    hole.absarc(0, 0, 0.185, a0, a0 + da, false);
    hole.absarc(0, 0, 0.105, a0 + da * 0.85, a0 + da * 0.15, true);
    hole.closePath();
    cover.holes.push(hole);
  }
  const coverG = new THREE.ExtrudeGeometry(cover, {
    depth: 0.006, bevelEnabled: true, bevelThickness: 0.004, bevelSize: 0.004, bevelSegments: 2, curveSegments: Math.round(10 * q) + 4,
  }).translate(0, 0, hw * 0.60);
  add({ name: `wheel_cover_${prefix}`, kind: 'paint', region: 'wheel_covers', pivot: `wheel_${prefix}`, geometry: coverG });
  const nut = new THREE.CylinderGeometry(0.03, 0.036, 0.05, 12).rotateX(Math.PI / 2).translate(0, 0, hw * 0.60 + 0.025);
  add({ name: `wheel_nut_${prefix}`, kind: 'metal', pivot: `wheel_${prefix}`, geometry: nut });

  const disc = new THREE.CylinderGeometry(0.19, 0.19, 0.03, Math.round(28 * q) + 8).rotateX(Math.PI / 2).translate(0, 0, hw * 0.34);
  add({ name: `brake_disc_${prefix}`, kind: 'disc', pivot: `wheel_${prefix}`, geometry: disc });
  // Brake drum / upright filling the inboard side of the wheel (does not spin).
  const drum = new THREE.CylinderGeometry(rim - 0.03, rim - 0.045, hw * 1.25, Math.round(20 * q) + 8).rotateX(Math.PI / 2).translate(0, 0, -hw * 0.34);
  add({ name: `brake_drum_${prefix}`, kind: 'carbon', pivot: `hub_${prefix}`, geometry: boxUV(drum) });

  // Compound marking: two arcs per sidewall so wheel rotation reads clearly.
  const arcs = [];
  for (const side of [1, -1]) {
    for (const start of [0.25, Math.PI + 0.25]) {
      const ring = new THREE.RingGeometry(R - 0.088, R - 0.058, Math.round(20 * q) + 6, 1, start, Math.PI - 0.9);
      if (side < 0) ring.rotateY(Math.PI);
      ring.translate(0, 0, side * (hw * 1.0 + 0.0015));
      arcs.push(ring);
    }
  }
  add({ name: `tyre_band_${prefix}`, kind: 'band', pivot: `wheel_${prefix}`, geometry: merge(arcs) });
}

/**
 * @param {number} q detail factor: 1 = close-up car, ~0.45 = distant LOD
 * @returns {{pivots: object[], parts: object[]}}
 */
export function buildProceduralCar(q = 1) {
  const ringN = Math.max(12, Math.round(30 * q) & ~1);
  const step = 0.05 / q;
  const parts = [];
  const add = (p) => { parts.push({ pivot: 'body', region: null, ...p }); };
  const pivots = [{ name: 'body', parent: null, position: V3(0, 0, 0) }];

  // ---- bodywork lofts
  const [noseG, tubG] = splitByX(loft(SPINE, BODY_KEYS, { ringN, step }), NOSE_SPLIT_X);
  add({ name: 'nose', kind: 'paint', region: 'nose', geometry: noseG, style: 'nose' });
  add({ name: 'body', kind: 'paint', region: 'body', geometry: tubG, style: 'tub' });
  add({ name: 'engine_cover', kind: 'paint', region: 'engine_cover', geometry: loft(ENGINE_COVER, BODY_KEYS, { ringN, step, capStart: true }), style: 'cover' });
  for (const side of [1, -1]) {
    add({
      name: `sidepod_${side > 0 ? 'R' : 'L'}`, kind: 'paint', region: 'sidepods', style: 'sidepod',
      geometry: loft(SIDEPOD, POD_KEYS, { ringN, step, capStart: true, mirror: side }),
    });
  }
  // Shark fin
  add({
    name: 'engine_fin', kind: 'paint', region: 'engine_cover',
    geometry: plate([[-0.70, 0.928], [-1.10, 0.928], [-1.88, 0.865], [-2.02, 0.75], [-2.01, 0.50], [-1.9, 0.44], [-1.2, 0.6], [-0.75, 0.80]], 0.014),
  });

  // Intakes and cockpit opening (matte black insets)
  const dark = [];
  dark.push(new THREE.SphereGeometry(1, 20, 12).scale(0.012, 0.135, 0.095).translate(-0.233, 0.725, 0));
  for (const side of [1, -1]) dark.push(new THREE.SphereGeometry(1, 20, 12).scale(0.014, 0.056, 0.14).translate(0.642, 0.47, 0.6 * side));
  dark.push(new THREE.SphereGeometry(1, 24, 12).scale(0.37, 0.045, 0.2).translate(0.10, 0.545, 0));
  dark.push(new THREE.BoxGeometry(0.17, 0.034, 0.05).translate(-0.44, 0.948, 0));   // T-camera pod
  add({ name: 'intakes', kind: 'dark', geometry: merge(dark) });

  // Cockpit rim / headrest collar
  const rimPts = [V3(-0.24, 0.615, 0.205), V3(-0.05, 0.592, 0.245), V3(0.24, 0.57, 0.225), V3(0.44, 0.558, 0.13), V3(0.50, 0.553, 0)];
  const collar = tube([...rimPts, ...mirrorZ(rimPts.slice(0, -1)).reverse()], 0.036, Math.round(40 * q) + 8, 8);
  add({ name: 'cockpit_rim', kind: 'paint', region: 'body', geometry: collar });

  // ---- floor, diffuser, plank
  const fl = [[1.18, 0.20], [1.08, 0.30], [0.86, 0.56], [0.62, 0.79], [0.2, 0.83], [-1.06, 0.83], [-1.26, 0.79], [-1.37, 0.545], [-2.0, 0.52], [-2.05, 0.44]];
  const floorShape = new THREE.Shape([...fl.map(([x, z]) => new THREE.Vector2(x, z)), ...fl.slice().reverse().map(([x, z]) => new THREE.Vector2(x, -z))]);
  const floorG = new THREE.ExtrudeGeometry(floorShape, { depth: 0.022, bevelEnabled: true, bevelThickness: 0.006, bevelSize: 0.008, bevelSegments: 2 })
    .rotateX(Math.PI / 2).translate(0, 0.082, 0);
  const floorBits = [floorG];
  // Raised floor-edge fence along each side
  for (const side of [1, -1]) {
    floorBits.push(plate([[0.5, 0.086], [0.42, 0.135], [-0.95, 0.15], [-1.1, 0.086]], 0.008, side * 0.812));
  }
  // Diffuser ramp with strakes
  const ramp = new THREE.BoxGeometry(0.62, 0.012, 1.0).rotateZ(-0.27).translate(-1.80, 0.135, 0);
  floorBits.push(ramp);
  for (const z of [-0.5, -0.25, 0.25, 0.5]) floorBits.push(plate([[-1.5, 0.055], [-2.1, 0.055], [-2.1, 0.215], [-1.52, 0.06]], 0.008, z, 0));
  add({ name: 'floor', kind: 'paint', region: 'floor', geometry: boxUV(merge(floorBits)) });

  // ---- front wing
  const fw = frontWingStations(q);
  const afN = Math.max(10, Math.round(22 * q) & ~1);
  add({ name: 'front_wing', kind: 'paint', region: 'front_wing', geometry: boxUV(wingElement(fw, { thickness: 0.11, camber: 0.04, n: afN })) });
  for (const side of [1, -1]) {
    const tag = side > 0 ? 'R' : 'L';
    const outer = fw.filter((s) => s.z * side >= 0.165 && Math.abs(s.z) <= 0.915);
    const defs = [
      { n: 1, dx: 0.255, dy: 0.034, chord: [0.175, 0.125], angle: [0.30, 0.22], open: 0.16 },
      { n: 2, dx: 0.395, dy: 0.088, chord: [0.150, 0.095], angle: [0.50, 0.36], open: 0.27 },
    ];
    for (const d of defs) {
      const st = outer.map((s) => {
        const t = (Math.abs(s.z) - 0.165) / 0.75;
        return {
          z: s.z, leX: s.leX - d.dx * (s.chord / 0.30), leY: s.leY + d.dy * (1 - 0.25 * t),
          chord: d.chord[0] + (d.chord[1] - d.chord[0]) * t, angle: d.angle[0] + (d.angle[1] - d.angle[0]) * t,
        };
      });
      const mid = st[Math.floor(st.length / 2)];
      const pivot = V3(mid.leX, mid.leY, 0);
      const g = wingElement(st, { thickness: 0.09, camber: 0.07, n: afN }).translate(-pivot.x, -pivot.y, 0);
      const pname = `flap_front_${d.n}${tag}`;
      pivots.push({ name: pname, parent: 'body', position: pivot, type: 'flap', open: d.open });
      add({ name: `front_wing_flap_${d.n}${tag}`, kind: 'paint', region: 'front_wing', pivot: pname, geometry: boxUV(g) });
    }
    // Endplate with a swept leading edge, canted slightly outward at the top
    const ep = plate([[2.60, 0.045], [2.585, 0.10], [2.40, 0.275], [2.12, 0.305], [2.085, 0.26], [2.09, 0.045]], 0.012, 0);
    ep.rotateX(-side * 0.07).translate(0, 0, side * 0.935);
    add({ name: `front_endplate_${tag}`, kind: 'paint', region: 'endplates', geometry: boxUV(ep) });
  }

  // ---- rear wing
  const rwHalf = 0.50;
  const span = (leX, leY, chord, angle) => [-rwHalf, -rwHalf / 2, 0, rwHalf / 2, rwHalf].map((z) => ({
    z, leX, leY: leY - 0.022 * (1 - (z / rwHalf) ** 2), chord: chord * (1 + 0.08 * (1 - (z / rwHalf) ** 2)), angle,
  }));
  add({ name: 'rear_wing_main', kind: 'paint', region: 'rear_wing', geometry: boxUV(wingElement(span(-2.02, 0.75, 0.30, 0.20), { thickness: 0.12, camber: 0.09, n: afN })) });
  const flapPivot = V3(-2.27, 0.822, 0);
  pivots.push({ name: 'flap_rear', parent: 'body', position: flapPivot, type: 'flap', open: 0.46 });
  add({
    name: 'rear_wing_flap', kind: 'paint', region: 'rear_wing', pivot: 'flap_rear',
    geometry: boxUV(wingElement(span(0, 0, 0.19, 0.58).map((s) => ({ ...s, leY: 0 })), { thickness: 0.10, camber: 0.08, n: afN })),
  });
  add({ name: 'beam_wing', kind: 'carbon', geometry: boxUV(wingElement(span(-2.13, 0.345, 0.14, 0.22), { thickness: 0.11, camber: 0.07, n: afN })) });
  for (const side of [1, -1]) {
    const ep = plate([[-1.955, 0.30], [-1.90, 0.60], [-1.965, 0.935], [-2.47, 0.935], [-2.485, 0.62], [-2.40, 0.30]], 0.014, side * (rwHalf + 0.007));
    add({ name: `rear_endplate_${side > 0 ? 'R' : 'L'}`, kind: 'paint', region: 'endplates', geometry: boxUV(ep) });
  }
  add({ name: 'rear_wing_pylon', kind: 'carbon', geometry: boxUV(plate([[-1.88, 0.40], [-2.02, 0.40], [-2.16, 0.745], [-2.05, 0.745]], 0.022)) });

  // ---- halo
  const hoopHalf = [V3(-0.38, 0.632, 0.27), V3(-0.14, 0.705, 0.288), V3(0.18, 0.728, 0.225), V3(0.40, 0.722, 0.105), V3(0.47, 0.716, 0)];
  const hoop = tube([...hoopHalf, ...mirrorZ(hoopHalf.slice(0, -1)).reverse()], 0.027, Math.round(48 * q) + 8, Math.round(10 * q) + 4);
  const pillar = tube([V3(0.455, 0.716, 0), V3(0.56, 0.66, 0), V3(0.68, 0.545, 0)], 0.024, 10, Math.round(10 * q) + 4);
  add({ name: 'halo', kind: 'paint', region: 'halo', geometry: boxUV(merge([hoop, pillar]), 30) });

  // ---- driver helmet
  const helmet = new THREE.SphereGeometry(0.128, Math.round(24 * q) + 6, Math.round(16 * q) + 4).scale(1.12, 1.0, 0.98).translate(-0.06, 0.665, 0);
  add({ name: 'driver_helmet', kind: 'paint', region: 'helmet', geometry: helmet });
  const visor = new THREE.SphereGeometry(0.1305, 20, 6, Math.PI - 1.15, 2.3, 1.18, 0.42).scale(1.12, 1.0, 0.98).translate(-0.06, 0.665, 0);
  const glass = [visor];

  // ---- mirrors
  const mirrorShells = [], mirrorStalks = [];
  for (const side of [1, -1]) {
    mirrorShells.push(new THREE.SphereGeometry(1, 14, 10).scale(0.06, 0.042, 0.095).translate(0.50, 0.635, side * 0.50));
    glass.push(new THREE.SphereGeometry(1, 12, 8).scale(0.012, 0.034, 0.082).translate(0.452, 0.635, side * 0.50));
    mirrorStalks.push(strut(V3(0.50, 0.54, side * 0.30), V3(0.50, 0.615, side * 0.47), 0.008, 3));
  }
  add({ name: 'mirror_shells', kind: 'paint', region: 'body', geometry: merge(mirrorShells) });
  add({ name: 'mirror_glass', kind: 'glass', geometry: merge(glass) });

  // ---- suspension (static, carbon)
  const arms = [...mirrorStalks];
  for (const side of [1, -1]) {
    const P = (x, y, z) => V3(x, y, z * side);
    const fz = DIM.front.z - 0.15, rz = DIM.rear.z - 0.2;
    arms.push(
      strut(P(2.04, 0.425, 0.155), P(1.72, 0.50, fz), 0.011), strut(P(1.38, 0.455, 0.215), P(1.69, 0.50, fz), 0.011),
      strut(P(2.05, 0.215, 0.135), P(1.72, 0.20, fz), 0.011), strut(P(1.36, 0.19, 0.2), P(1.69, 0.20, fz), 0.011),
      strut(P(1.74, 0.44, 0.18), P(1.72, 0.23, fz - 0.02), 0.012, 1.6),
      strut(P(1.88, 0.40, 0.175), P(1.84, 0.42, fz), 0.009),
      strut(P(-1.32, 0.42, 0.21), P(-1.69, 0.50, rz), 0.012), strut(P(-1.98, 0.40, 0.11), P(-1.71, 0.50, rz), 0.012),
      strut(P(-1.28, 0.175, 0.24), P(-1.69, 0.19, rz), 0.012), strut(P(-2.02, 0.17, 0.13), P(-1.71, 0.19, rz), 0.012),
      strut(P(-1.62, 0.21, 0.2), P(-1.70, 0.47, rz - 0.02), 0.012, 1.6),
      strut(P(-1.70, 0.33, 0.1), P(-1.70, 0.36, rz), 0.024, 1, 10),
    );
  }
  add({ name: 'suspension', kind: 'carbon', geometry: boxUV(merge(arms), 30) });

  // ---- rain light + endplate lights
  const lights = [new THREE.BoxGeometry(0.03, 0.10, 0.055).translate(-2.375, 0.29, 0)];
  for (const side of [1, -1]) lights.push(new THREE.BoxGeometry(0.012, 0.16, 0.016).translate(-2.482, 0.72, side * (rwHalf + 0.007)));
  add({ name: 'rain_light', kind: 'light', geometry: merge(lights) });

  // ---- wheels
  for (const [tag, cfg, x, side] of [['FL', DIM.front, DIM.frontAxleX, -1], ['FR', DIM.front, DIM.frontAxleX, 1],
    ['RL', DIM.rear, DIM.rearAxleX, -1], ['RR', DIM.rear, DIM.rearAxleX, 1]]) {
    // hub: steer pivot (also flips left wheels so "outboard" is always local +Z); wheel: spin pivot
    pivots.push({ name: `hub_${tag}`, parent: null, position: V3(x, cfg.radius, side * cfg.z), type: 'hub', side, front: x > 0, radius: cfg.radius });
    pivots.push({ name: `wheel_${tag}`, parent: `hub_${tag}`, position: V3(0, 0, 0), type: 'spin' });
    wheelParts(tag, cfg, q, add);
  }

  for (const p of parts) if (!p.geometry.attributes.normal) p.geometry.computeVertexNormals();
  return { pivots, parts, decalAnchors: DECAL_ANCHORS };
}

// Where generated decals (number, driver code) and optional user PNGs sit: flat quads just above the surface,
// given by centre, outward normal, text-up direction and size. `pivot` attaches a quad to a moving part;
// `project` instead projects the image onto the named curved part with DecalGeometry (mirrored on both sides).
const NOSE_N = [0.22, 0.975, 0], NOSE_UP = [-0.975, 0.22, 0];
export const DECAL_ANCHORS = {
  number_fin_R: { pos: [-1.50, 0.795, 0.0085], normal: [0, 0, 1], up: [0.08, 1, 0], size: [0.27, 0.165] },
  number_fin_L: { pos: [-1.50, 0.795, -0.0085], normal: [0, 0, -1], up: [0.08, 1, 0], size: [0.27, 0.165] },
  number_nose: { pos: [2.02, 0.386, 0], normal: NOSE_N, up: NOSE_UP, size: [0.17, 0.115] },
  code_endplate_R: { pos: [-2.20, 0.80, 0.5155], normal: [0, 0, 1], up: [0, 1, 0], size: [0.40, 0.14] },
  code_endplate_L: { pos: [-2.20, 0.80, -0.5155], normal: [0, 0, -1], up: [0, 1, 0], size: [0.40, 0.14] },
  user_nose: { pos: [1.62, 0.470, 0], normal: [0.17, 0.985, 0], up: [-0.985, 0.17, 0], size: [0.24, 0.16] },
  user_rear_wing: { pivot: 'flap_rear', pos: [-0.096, 0.027, 0], normal: [-0.548, -0.836, 0], up: [-0.836, 0.548, 0], size: [0.9, 0.15] },
  user_sidepod: { project: 'sidepod', pos: [-0.30, 0.42, 0.86], size: [0.95, 0.26, 0.5] },
  user_engine_cover: { project: 'engine_cover', pos: [-0.85, 0.66, 0.2], size: [0.62, 0.22, 0.4] },
};
