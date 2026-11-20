// Car visuals: turns a GLB (if the user supplied one) or the procedural blueprint into animated scene objects.
// One CarVisual per car: a full-detail rig (wheels, flaps, brake glow, rain light), a single-mesh distant LOD,
// a blob shadow and a floating driver tag.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { DecalGeometry } from 'three/addons/geometries/DecalGeometry.js';
import { buildProceduralCar, DIM, DECAL_ANCHORS } from './procedural_car.js';
import {
  normaliseLivery, regionColor, isCarbon, compoundColor, makePaintMaterial, applyFinish, getCarbonMaterial,
  makeTextDecal, makeTagTexture, CARBON_HEX,
} from './livery.js';
import { ASSET_BASE, clamp, damp, lerp, canvasTexture, loadLocalIndex, loadTexture } from './util.js';

const GLB_URL = ASSET_BASE + 'cars/f1_2026.glb';
const DRACO_PATH = new URL('../vendor/three/addons/libs/draco/gltf/', import.meta.url).href;
const USER_DECALS = { 'nose.png': 'user_nose', 'rear_wing.png': 'user_rear_wing', 'sidepod.png': 'user_sidepod', 'engine_cover.png': 'user_engine_cover' };

const LOD_KIND_COLOR = { rubber: '#121212', dark: '#060606', metal: '#6f7378', glass: '#0c0d10', disc: '#2a2420', light: '#5a0000', carbon: CARBON_HEX };

const _c = new THREE.Color(), _c2 = new THREE.Color();
const _v = new THREE.Vector3();

function shared(obj) { obj.userData.shared = true; return obj; }

// Vertex colour for one vertex of a painted part. `ringT` is the position around a lofted ring
// (0 = +Z side, 0.25 = top, 0.75 = bottom), which lets stripes follow the body lines.
function paintColor(part, livery, ringT, out) {
  if (part.region === 'helmet') return out.set(livery.accent);
  regionColor(livery, part.region, out);
  if (part.style === 'sidepod' || part.style === 'tub') {
    const lo = part.style === 'sidepod' ? 0.57 : 0.60, hi = 1.5 - lo;
    if (ringT > lo && ringT < hi) out.set(CARBON_HEX);                           // dark lower flank / undercut
    else if (part.style === 'sidepod' && ringT > lo - 0.07 && ringT < hi + 0.07) out.set(livery.accent);   // pinstripe
  }
  return out;
}

function colorAttributeFor(part, livery) {
  const g = part.geometry, n = g.attributes.position.count;
  const col = new Float32Array(n * 3);
  const uv = g.attributes.uv;
  for (let i = 0; i < n; i++) {
    paintColor(part, livery, uv ? uv.getY(i) : 0, _c);
    col[i * 3] = _c.r; col[i * 3 + 1] = _c.g; col[i * 3 + 2] = _c.b;
  }
  return new THREE.BufferAttribute(col, 3);
}

function pivotMatrices(blueprint) {
  const byName = Object.fromEntries(blueprint.pivots.map((p) => [p.name, p]));
  const out = {};
  const resolve = (name) => {
    if (out[name]) return out[name];
    const p = byName[name];
    const m = new THREE.Matrix4().makeTranslation(p.position.x, p.position.y, p.position.z);
    if (p.type === 'hub' && p.side < 0) m.multiply(new THREE.Matrix4().makeRotationY(Math.PI));
    out[name] = p.parent ? resolve(p.parent).clone().multiply(m) : m;
    return out[name];
  };
  for (const p of blueprint.pivots) resolve(p.name);
  return out;
}

// Whole car flattened to one vertex-coloured mesh for distant cars.
function buildLodGeometry(blueprint, livery, compound) {
  const mats = pivotMatrices(blueprint);
  const pos = [], nrm = [], col = [];
  const nm = new THREE.Matrix3(), p = new THREE.Vector3(), n = new THREE.Vector3();
  for (const part of blueprint.parts) {
    const g = part.geometry, P = g.attributes.position, N = g.attributes.normal, uv = g.attributes.uv;
    const m = mats[part.pivot];
    nm.getNormalMatrix(m);
    const count = g.index ? g.index.count : P.count;
    for (let i = 0; i < count; i++) {
      const v = g.index ? g.index.array[i] : i;
      p.fromBufferAttribute(P, v).applyMatrix4(m);
      n.fromBufferAttribute(N, v).applyMatrix3(nm);
      if (part.kind === 'paint') paintColor(part, livery, uv ? uv.getY(v) : 0, _c);
      else if (part.kind === 'band') _c.set(compoundColor(compound));
      else _c.set(LOD_KIND_COLOR[part.kind] || '#222222');
      pos.push(p.x, p.y, p.z); nrm.push(n.x, n.y, n.z); col.push(_c.r, _c.g, _c.b);
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute('normal', new THREE.Float32BufferAttribute(nrm, 3));
  g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  g.computeBoundingSphere();
  return g;
}

function decalQuad(anchor, texture) {
  const mat = new THREE.MeshStandardMaterial({
    map: texture, transparent: true, alphaTest: 0.03, roughness: 0.4, metalness: 0.1,
    polygonOffset: true, polygonOffsetFactor: -4, depthWrite: false,
  });
  const mesh = new THREE.Mesh(shared(new THREE.PlaneGeometry(1, 1)), mat);
  const z = _v.fromArray(anchor.normal).normalize().clone();
  const y = new THREE.Vector3().fromArray(anchor.up).addScaledVector(z, -z.dot(new THREE.Vector3().fromArray(anchor.up))).normalize();
  const x = new THREE.Vector3().crossVectors(y, z);
  mesh.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(x, y, z));
  mesh.position.fromArray(anchor.pos);
  mesh.scale.set(anchor.size[0], anchor.size[1], 1);
  return mesh;
}

// Brake disc glow: black body below ~450 C, dull red, orange, then yellow-white near 1100 C.
function discGlow(tempC, material) {
  const t = clamp((tempC - 420) / 680, 0, 1);
  _c.setRGB(1.0, 0.08 + 0.55 * t * t, 0.02 + 0.25 * t * t * t);
  material.emissive.copy(_c);
  material.emissiveIntensity = t * t * 7;
}

export class CarFactory {
  constructor(notes) {
    this.notes = notes;
    this.blueprints = {};
    this.index = null;
    this.glb = null;
    this.cache = new Map();          // baked per-livery geometry / textures
    this.materials = {
      rubber: shared(new THREE.MeshStandardMaterial({ color: 0x101010, roughness: 0.92, metalness: 0.0 })),
      dark: shared(new THREE.MeshStandardMaterial({ color: 0x050506, roughness: 0.85, metalness: 0.1 })),
      metal: shared(new THREE.MeshStandardMaterial({ color: 0x8a8d92, roughness: 0.28, metalness: 0.95 })),
      glass: shared(new THREE.MeshPhysicalMaterial({ color: 0x08090c, roughness: 0.05, metalness: 0.9, clearcoat: 1 })),
      carbon: getCarbonMaterial(),
      lod: shared(new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.42, metalness: 0.25 })),
      blob: shared(new THREE.MeshBasicMaterial({
        map: canvasTexture(64, 128, (ctx, w, h) => {
          const g = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, w / 2);
          g.addColorStop(0, 'rgba(0,0,0,0.9)'); g.addColorStop(0.55, 'rgba(0,0,0,0.55)'); g.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.setTransform(1, 0, 0, h / w, 0, 0);
          ctx.fillStyle = g; ctx.fillRect(0, 0, w, w);
        }),
        transparent: true, depthWrite: false, opacity: 0.6, polygonOffset: true, polygonOffsetFactor: -2, toneMapped: false,
      })),
    };
    this.blobGeometry = shared(new THREE.PlaneGeometry(6.2, 2.5).rotateX(-Math.PI / 2));
  }

  async init() {
    this.index = await loadLocalIndex();
    if (this.index?.car_glb) {
      try {
        const loader = new GLTFLoader();
        const draco = new DRACOLoader().setDecoderPath(DRACO_PATH);
        loader.setDRACOLoader(draco);
        const gltf = await loader.loadAsync(GLB_URL);
        this.glb = normaliseGlb(gltf.scene);
        draco.dispose();
      } catch (e) {
        this.notes.add(`Car GLB failed to load (${e.message || e}); using the procedural car.`);
      }
    } else {
      this.notes.add('No car GLB at assets/cars/f1_2026.glb: using the procedural 2026 car.');
    }
  }

  blueprint(level) {
    if (!this.blueprints[level]) this.blueprints[level] = buildProceduralCar(level === 'hi' ? 1 : 0.42);
    return this.blueprints[level];
  }

  cached(key, make) {
    if (!this.cache.has(key)) this.cache.set(key, make());
    return this.cache.get(key);
  }

  userDecals(team) {
    return this.index?.liveries?.[team] || [];
  }

  create(spec) { return new CarVisual(this, spec); }

  // Drop baked data for a team so the next build picks up an edited livery.
  invalidate(team) {
    for (const [k, v] of this.cache) {
      if (k.startsWith(`${team}|`)) { v.dispose?.(); this.cache.delete(k); }
    }
  }

  dispose() {
    for (const v of this.cache.values()) v.dispose?.();
    this.cache.clear();
    for (const bp of Object.values(this.blueprints)) for (const p of bp.parts) p.geometry.dispose();
    for (const m of Object.values(this.materials)) { m.map?.dispose(); m.dispose(); }
    this.blobGeometry.dispose();
  }
}

// Bring an arbitrary GLB into the car frame (+X forward, origin on the ground at mid-wheelbase, metres).
function normaliseGlb(root) {
  const holder = new THREE.Group();
  holder.add(root);
  root.updateMatrixWorld(true);
  let box = new THREE.Box3().setFromObject(root);
  const size = box.getSize(new THREE.Vector3());
  const centre = (prefix) => {
    const b = new THREE.Box3();
    root.traverse((o) => { if (o.name.toLowerCase().startsWith(prefix)) b.expandByObject(o); });
    return b.isEmpty() ? null : b.getCenter(new THREE.Vector3());
  };
  const f = centre('wheel_f'), r = centre('wheel_r');
  let fwd = size.x >= size.z ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 0, 1);   // glTF convention: +Z front
  if (f && r) fwd = f.clone().sub(r).setY(0).normalize();
  root.rotation.y = Math.atan2(fwd.z, fwd.x);
  const length = Math.max(size.x, size.z);
  if (length < 4 || length > 7) root.scale.multiplyScalar(5.45 / length);
  holder.updateMatrixWorld(true);
  box = new THREE.Box3().setFromObject(holder);
  const f2 = f && r ? null : box.getCenter(new THREE.Vector3());
  holder.updateMatrixWorld(true);
  const mid = f2 || (() => {
    const a = new THREE.Box3(), b = new THREE.Box3();
    root.traverse((o) => {
      const n = o.name.toLowerCase();
      if (n.startsWith('wheel_f')) a.expandByObject(o); else if (n.startsWith('wheel_r')) b.expandByObject(o);
    });
    return a.getCenter(new THREE.Vector3()).add(b.getCenter(new THREE.Vector3())).multiplyScalar(0.5);
  })();
  root.position.x -= mid.x; root.position.z -= mid.z; root.position.y -= box.min.y;
  return holder;
}

export class CarVisual {
  constructor(factory, spec) {
    this.factory = factory;
    this.spec = { ...spec };
    this.livery = normaliseLivery(spec.livery);
    this.group = new THREE.Group();
    this.group.name = `car_${spec.id}`;
    this.hi = null;
    this.lo = null;
    this.lod = -1;
    this.rig = null;
    this.aero = 0;                  // 0 = Z-mode (flaps closed), 1 = X-mode (open)
    this.spin = 0;
    this.brakeTemp = 300;
    this.pitch = 0;
    this.last = null;
    this.state = { visible: true };
    this.owned = [];                // per-car materials / textures to dispose

    this.blob = new THREE.Mesh(factory.blobGeometry, factory.materials.blob);
    this.blob.position.y = 0.02;
    this.blob.renderOrder = 1;
    this.group.add(this.blob);

    this.tag = new THREE.Sprite(new THREE.SpriteMaterial({
      map: makeTagTexture(spec.code || spec.id, this.livery.base), depthTest: false, sizeAttenuation: false, toneMapped: false, transparent: true,
    }));
    this.tag.scale.set(0.085, 0.032, 1);
    this.tag.position.y = 1.75;
    this.tag.renderOrder = 10;
    this.group.add(this.tag);
  }

  get key() { return `${this.spec.team}|${JSON.stringify(this.livery)}`; }

  setLod(level) {
    if (level === this.lod) return;
    this.lod = level;
    if (level === 0 && !this.hi) this.buildHi();
    if (level === 1 && !this.lo) this.buildLo();
    if (this.hi) this.hi.visible = level === 0;
    if (this.lo) this.lo.visible = level === 1;
  }

  buildLo() {
    const f = this.factory;
    if (f.glb) {
      this.lo = new THREE.Group();   // GLB cars have no simplified mesh; the full model is reused
      if (!this.hi) this.buildHi();
      return;
    }
    const geo = f.cached(`${this.key}|lod|${this.spec.compound}`, () => shared(buildLodGeometry(f.blueprint('lo'), this.livery, this.spec.compound)));
    this.lo = new THREE.Mesh(geo, f.materials.lod);
    this.lo.frustumCulled = true;
    this.group.add(this.lo);
  }

  buildHi() {
    const f = this.factory;
    this.paint = makePaintMaterial(this.livery.finish);
    this.discMat = new THREE.MeshStandardMaterial({ color: 0x2b2622, roughness: 0.55, metalness: 0.6, emissive: 0x000000 });
    this.lightMat = new THREE.MeshStandardMaterial({ color: 0x300000, roughness: 0.4, emissive: 0xff1010, emissiveIntensity: 0 });
    this.bandMat = new THREE.MeshStandardMaterial({ color: compoundColor(this.spec.compound), roughness: 0.7 });
    this.owned.push(this.paint, this.discMat, this.lightMat, this.bandMat);
    this.hi = f.glb ? this.buildGlb() : this.buildProcedural();
    this.group.add(this.hi);
  }

  buildProcedural() {
    const f = this.factory, bp = f.blueprint('hi');
    const root = new THREE.Group();
    const nodes = {};
    const rig = { wheels: [], flaps: [], body: null };
    for (const p of bp.pivots) {
      const o = new THREE.Object3D();
      o.name = p.name;
      o.position.copy(p.position);
      nodes[p.name] = o;
      (p.parent ? nodes[p.parent] : root).add(o);
      if (p.type === 'hub') {
        const wheel = { hub: o, spin: null, radius: p.radius, front: p.front, side: p.side, base: p.side < 0 ? Math.PI : 0 };
        o.rotation.y = wheel.base;
        rig.wheels.push(wheel);
      } else if (p.type === 'spin') {
        rig.wheels[rig.wheels.length - 1].spin = o;
      } else if (p.type === 'flap') {
        rig.flaps.push({ node: o, open: p.open });
      }
    }
    rig.body = nodes.body;

    const kindMat = { rubber: f.materials.rubber, dark: f.materials.dark, metal: f.materials.metal, glass: f.materials.glass,
      carbon: f.materials.carbon, disc: this.discMat, light: this.lightMat, band: this.bandMat };
    this.paintMeshes = [];
    const meshes = {};
    for (const part of bp.parts) {
      let geometry = part.geometry, material = kindMat[part.kind];
      if (part.kind === 'paint') {
        geometry = this.paintGeometry(part);
        material = isCarbon(this.livery, part.region) ? f.materials.carbon : this.paint;
      }
      geometry.userData.shared = true;
      const mesh = new THREE.Mesh(geometry, material);
      mesh.name = part.name;
      mesh.castShadow = part.kind !== 'band' && part.kind !== 'light';
      mesh.receiveShadow = part.kind === 'paint' || part.kind === 'carbon';
      nodes[part.pivot].add(mesh);
      meshes[part.name] = mesh;
      if (part.kind === 'paint') this.paintMeshes.push({ mesh, part });
    }

    // Generated decals: car number and driver code. No logos.
    const numTex = (color) => f.cached(`${this.spec.team}|num|${this.spec.number}|${color}`, () => makeTextDecal(String(this.spec.number ?? ''), color));
    const codeTex = f.cached(`${this.spec.team}|code|${this.spec.code}|${this.livery.number_color}`,
      () => makeTextDecal(String(this.spec.code || '').toUpperCase().slice(0, 3), this.livery.number_color, { w: 320, h: 112 }));
    const decals = new THREE.Group();
    decals.name = 'decals';
    if (this.spec.number != null) {
      for (const k of ['number_fin_R', 'number_fin_L', 'number_nose']) decals.add(decalQuad(DECAL_ANCHORS[k], numTex(this.livery.number_color)));
    }
    if (this.spec.code) for (const k of ['code_endplate_R', 'code_endplate_L']) decals.add(decalQuad(DECAL_ANCHORS[k], codeTex));
    nodes.body.add(decals);
    this.decals = decals;
    this.addUserDecals(nodes, meshes);

    this.rig = rig;
    return root;
  }

  paintGeometry(part) {
    const src = part.geometry;
    return this.factory.cached(`${this.key}|paint|${part.name}`, () => {
      const g = new THREE.BufferGeometry();
      for (const k of ['position', 'normal', 'uv']) if (src.attributes[k]) g.setAttribute(k, src.attributes[k]);
      if (src.index) g.setIndex(src.index);
      g.setAttribute('color', colorAttributeFor(part, this.livery));
      g.boundingSphere = src.boundingSphere || (src.computeBoundingSphere(), src.boundingSphere);
      return shared(g);
    });
  }

  // Optional PNGs dropped into assets/liveries/<team>/ (listed in assets/local_index.json).
  addUserDecals(nodes, meshes) {
    const files = this.factory.userDecals(this.spec.team);
    for (const file of files) {
      const anchor = DECAL_ANCHORS[USER_DECALS[file]];
      if (!anchor) continue;
      loadTexture(`${ASSET_BASE}liveries/${this.spec.team}/${file}`, { srgb: true }).then((tex) => {
        if (!tex || !this.hi) return;
        tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping;
        this.owned.push(tex);
        if (!anchor.project) {
          (anchor.pivot ? nodes[anchor.pivot] : nodes.body).add(decalQuad(anchor, tex));
          return;
        }
        for (const side of [1, -1]) {
          const target = meshes[anchor.project === 'sidepod' ? `sidepod_${side > 0 ? 'R' : 'L'}` : anchor.project];
          if (!target) continue;
          const probe = new THREE.Mesh(target.geometry);
          const pos = new THREE.Vector3(anchor.pos[0], anchor.pos[1], anchor.pos[2] * side);
          const g = new DecalGeometry(probe, pos, new THREE.Euler(0, side > 0 ? 0 : Math.PI, 0), new THREE.Vector3(...anchor.size));
          const mat = new THREE.MeshStandardMaterial({ map: tex, transparent: true, roughness: 0.4, polygonOffset: true, polygonOffsetFactor: -4, depthWrite: false });
          this.owned.push(mat);
          nodes.body.add(new THREE.Mesh(g, mat));
        }
      });
    }
  }

  // GLB path: find the named parts from the brief and wrap them in pivots the animation code understands.
  buildGlb() {
    const root = this.factory.glb.clone(true);
    const rig = { wheels: [], flaps: [], body: root };
    root.updateMatrixWorld(true);
    const find = (re) => { const out = []; root.traverse((o) => { if (re.test(o.name)) out.push(o); }); return out; };
    const base = regionColor(this.livery, 'body', new THREE.Color());
    const applyFinishTo = (o, color) => o.traverse((m) => {
      if (!m.isMesh) return;
      m.castShadow = true;
      m.material = m.material.clone();
      this.owned.push(m.material);
      if (color) { m.material.color.copy(color); if ('clearcoat' in m.material) applyFinish(m.material, this.livery.finish); }
    });
    for (const o of find(/^body/i)) applyFinishTo(o, base);
    for (const [re, region] of [[/^front_wing/i, 'front_wing'], [/^rear_wing/i, 'rear_wing'], [/^halo/i, 'halo'], [/^floor/i, 'floor']]) {
      for (const o of find(re)) applyFinishTo(o, regionColor(this.livery, region, new THREE.Color()));
    }
    for (const o of find(/^driver_helmet/i)) applyFinishTo(o, new THREE.Color(this.livery.accent));
    for (const o of find(/^brake_disc/i)) o.traverse((m) => { if (m.isMesh) m.material = this.discMat; });

    const wrap = (obj, at) => {
      const pivot = new THREE.Object3D();
      pivot.position.copy(at);
      root.add(pivot);
      pivot.updateMatrixWorld(true);
      pivot.attach(obj);
      return pivot;
    };
    for (const tag of ['FL', 'FR', 'RL', 'RR']) {
      const members = find(new RegExp(`^(wheel|tyre|brake_disc)_${tag}`, 'i')).filter((o) => !/^(wheel|tyre|brake_disc)_/i.test(o.parent?.name || ''));
      if (!members.length) continue;
      const box = new THREE.Box3();
      for (const o of members) box.expandByObject(o);
      const c = root.worldToLocal(box.getCenter(new THREE.Vector3()));
      const hub = new THREE.Object3D();
      hub.position.copy(c);
      root.add(hub);
      const spin = new THREE.Object3D();
      hub.add(spin);
      hub.updateMatrixWorld(true);
      for (const o of members) spin.attach(o);
      rig.wheels.push({ hub, spin, radius: Math.max(0.2, (box.max.y - box.min.y) / 2), front: tag[0] === 'F', side: 1, base: 0 });
    }
    for (const [re, open] of [[/^rear_wing_flap/i, 0.45], [/^front_wing_flap/i, 0.2]]) {
      for (const o of find(re)) {
        const box = new THREE.Box3().setFromObject(o);
        const le = root.worldToLocal(new THREE.Vector3(box.max.x, box.min.y, (box.min.z + box.max.z) / 2));
        rig.flaps.push({ node: wrap(o, le), open });
      }
    }
    this.rig = rig;
    return root;
  }

  applyLivery(livery) {
    this.factory.invalidate(this.spec.team);
    this.livery = normaliseLivery(livery);
    this.spec.livery = livery;
    this.rebuild();
  }

  setCompound(compound) {
    if (compound === this.spec.compound) return;
    this.spec.compound = compound;
    if (this.bandMat) this.bandMat.color.set(compoundColor(compound));
    if (this.lo?.isMesh) { this.lo.removeFromParent(); this.lo = null; if (this.lod === 1) { this.lod = -1; this.setLod(1); } }
  }

  rebuild() {
    const lod = this.lod;
    this.disposeBuilt();
    this.tag.material.map.dispose();
    this.tag.material.map = makeTagTexture(this.spec.code || this.spec.id, this.livery.base);
    this.lod = -1;
    if (lod >= 0) this.setLod(lod);
  }

  /**
   * @param {object} s pose/state from scene.updateCars
   * @param {number} dt seconds since the last frame
   * @param {number} time scene clock, seconds
   */
  update(s, dt, time) {
    const g = this.group;
    g.visible = s.visible !== false;
    if (!g.visible) { this.last = null; return; }
    const x = s.x, y = s.z || 0, z = -s.y;

    // Distance actually travelled drives wheel rotation and slope pitch, so pausing or scrubbing the replay
    // never leaves wheels spinning under a stationary car.
    let ds = 0;
    if (this.last) {
      const dx = x - this.last.x, dz = z - this.last.z, dy = y - this.last.y;
      const d = Math.hypot(dx, dz);
      if (d < 60) {
        ds = d * Math.sign(dx * Math.cos(s.psi) - dz * Math.sin(s.psi) || 1);
        if (d > 0.02) this.pitch = lerp(this.pitch, Math.atan2(dy, d) * Math.sign(ds), damp(8, dt));
      }
    } else {
      this.last = { x, y, z };
    }
    this.last.x = x; this.last.y = y; this.last.z = z;

    g.position.set(x, y, z);
    g.rotation.set(0, s.psi || 0, this.pitch, 'YZX');

    const speed = s.speed || 0, kappa = s.kappa || 0;
    if (!this.rig || this.lod !== 0) return;
    const rig = this.rig;

    // Body attitude: a little roll from lateral g, dive/squat, and aero squat with speed.
    const latG = clamp(speed * speed * kappa / 9.81, -6, 6);
    const longG = (s.throttle || 0) * 0.6 - (s.brake || 0) * 4.5;
    const a = damp(10, dt);
    rig.body.rotation.x = lerp(rig.body.rotation.x, latG * 0.0035, a);
    rig.body.rotation.z = lerp(rig.body.rotation.z, clamp(-longG * 0.0016, -0.004, 0.009), a);
    rig.body.position.y = lerp(rig.body.position.y, -0.012 * clamp(speed / 90, 0, 1) ** 2, a);

    const steer = clamp(Math.atan(DIM.wheelbase * kappa) * 1.15, -0.42, 0.42);
    this.spinAngle = (this.spinAngle || 0) + ds;
    for (const w of rig.wheels) {
      if (w.front) w.hub.rotation.y = w.base + steer;
      w.spin.rotation.z = -(this.spinAngle / w.radius) * (w.base ? -1 : 1);
    }

    // Active aero: flaps drop flat in X-mode.
    this.aero = lerp(this.aero, s.xmode ? 1 : 0, damp(7, dt));
    for (const f of rig.flaps) f.node.rotation.z = f.open * this.aero;

    // Brake glow from the supplied temperature, or a small thermal model when none is given.
    if (s.brakeTemp != null) this.brakeTemp = lerp(this.brakeTemp, s.brakeTemp, damp(12, dt));
    else this.brakeTemp = clamp(this.brakeTemp + ((s.brake || 0) * speed * 22 - (this.brakeTemp - 250) * (0.25 + speed * 0.006)) * dt, 200, 1150);
    discGlow(this.brakeTemp, this.discMat);

    // Rain light flashes while harvesting.
    const on = s.harvesting && (time * 4) % 1 < 0.5;
    this.lightMat.emissiveIntensity = on ? 9 : 0.15;
  }

  setTagVisible(v) { this.tag.visible = v; }

  disposeBuilt() {
    for (const o of [this.hi, this.lo]) {
      if (!o) continue;
      o.traverse((m) => {
        if (m.isMesh && m.geometry && !m.geometry.userData.shared) m.geometry.dispose();
        if (m.isMesh && m.parent?.name === 'decals') m.material.dispose();
      });
      o.removeFromParent();
    }
    for (const r of this.owned) r.dispose();
    this.owned = [];
    this.hi = this.lo = this.rig = null;
  }

  dispose() {
    this.disposeBuilt();
    this.tag.material.map.dispose();
    this.tag.material.dispose();
    this.group.removeFromParent();
  }
}
