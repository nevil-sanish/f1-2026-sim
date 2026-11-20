// 3D scene entry point (docs/API.md section 3). The app owns replay time and car poses; this module owns
// rendering, cameras, track/environment, materials and per-car part animation.
import * as THREE from 'three';
import { CarFactory } from './car_model.js';
import { buildTrack, loadTrackTextures } from './track_mesh.js';
import { Environment, TIME_PRESETS } from './environment.js';
import { CameraRig } from './cameras.js';
import { PostFX } from './postfx.js';
import { clamp, canvasTexture } from './util.js';

const PIXEL_RATIO = { low: 1, medium: 1.5, high: 2 };
const LOD_DIST = { low: 35, medium: 70, high: 130 };

const _c = new THREE.Color();

function speedColor(t, out) {   // slow -> fast: red, orange, yellow, green
  const stops = [[0.86, 0.12, 0.12], [0.98, 0.55, 0.1], [0.97, 0.88, 0.2], [0.2, 0.85, 0.35]];
  const x = clamp(t, 0, 1) * 3, i = Math.min(2, Math.floor(x)), f = x - i;
  return out.setRGB(
    stops[i][0] + (stops[i + 1][0] - stops[i][0]) * f,
    stops[i][1] + (stops[i + 1][1] - stops[i][1]) * f,
    stops[i][2] + (stops[i + 1][2] - stops[i][2]) * f,
  );
}

function numberSprite(num) {
  const tex = canvasTexture(128, 128, (c, w, h) => {
    c.fillStyle = 'rgba(12,14,18,0.78)';
    c.beginPath(); c.arc(w / 2, h / 2, 56, 0, Math.PI * 2); c.fill();
    c.lineWidth = 6; c.strokeStyle = '#f2f2f2'; c.stroke();
    c.fillStyle = '#ffffff'; c.font = 'bold 64px sans-serif'; c.textAlign = 'center'; c.textBaseline = 'middle';
    c.fillText(String(num), w / 2, h / 2 + 3);
  });
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthWrite: false, transparent: true, fog: false }));
  sp.scale.set(14, 14, 1);
  return sp;
}

/**
 * @param {HTMLElement|HTMLCanvasElement} target container (a canvas is appended) or an existing canvas
 * @param {{quality?:'low'|'medium'|'high', timeOfDay?:string, liveries?:object}} opts
 */
export async function createScene(target, opts = {}) {
  const notes = new Set();
  const isCanvas = target instanceof HTMLCanvasElement;
  const canvas = isCanvas ? target : document.createElement('canvas');
  const container = isCanvas ? (target.parentElement || document.body) : target;
  if (!isCanvas) { canvas.style.cssText = 'display:block;width:100%;height:100%'; container.appendChild(canvas); }

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.shadowMap.enabled = true;
  renderer.info.autoReset = false;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(55, 1, 0.3, 9000);
  const rig = new CameraRig(camera, canvas);
  const env = new Environment(renderer, scene, notes);
  const post = new PostFX(renderer, scene, camera);
  const factory = new CarFactory(notes);
  const texPromise = loadTrackTextures(notes);
  await factory.init();

  const S = {
    quality: opts.quality || 'medium',
    tod: opts.timeOfDay || 'day',
    track: null, trackGeom: null,
    cars: new Map(),
    liveries: { ...(opts.liveries || {}) },
    camMode: 'orbit', targetId: null,
    overlay: { line: 'speed', corners: false, brakeMarkers: false, tags: true },
    lineData: null, lineMesh: null,
    cornerGroup: null, brakeGroup: null, brakeMarks: [],
    frameCbs: [],
    time: 0, lastDt: 1 / 60,
    fps: 60, fpsAcc: 0, fpsN: 0,
    disposed: false, raf: 0,
    focus: new THREE.Vector3(),
  };

  function size() {
    const w = Math.max(1, container.clientWidth || canvas.clientWidth || 800);
    const h = Math.max(1, container.clientHeight || canvas.clientHeight || 600);
    return [w, h];
  }

  function resize() {
    const [w, h] = size();
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, PIXEL_RATIO[S.quality]));
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    post.resize(w, h);
  }

  function applyQuality(q) {
    if (!PIXEL_RATIO[q]) q = 'medium';
    const changed = q !== S.quality;
    S.quality = q;
    const [w, h] = size();
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, PIXEL_RATIO[q]));
    renderer.setSize(w, h, false);
    env.setQuality(q);
    post.setQuality(q, w, h);
    post.setBloom(env.preset ? env.preset.bloom : 0.2);
    for (const car of S.cars.values()) car.setTagVisible(S.overlay.tags);
    if (changed && S.trackGeom) rebuildTrack();   // dressing density depends on quality
  }

  async function rebuildTrack() {
    const geom = S.trackGeom;
    if (!geom) return;
    const tex = await texPromise;
    if (S.track) S.track.dispose();
    S.track = buildTrack(geom, tex, { quality: S.quality });
    scene.add(S.track.group);
    env.setGroundY(S.track.groundY);
    rig.setTrack(S.track.bounds, S.track.tvPoints);
    S.track.group.getObjectByName('lamp_heads').visible = TIME_PRESETS[S.tod]?.lamps ?? false;
    S.track.group.getObjectByName('lamp_poles').visible = true;
    rebuildCorners();
    applyLine();
  }

  function setLampVisibility() {
    const heads = S.track?.group.getObjectByName('lamp_heads');
    if (heads) heads.visible = !!TIME_PRESETS[S.tod]?.lamps;
  }

  async function setTimeOfDay(tod) {
    S.tod = tod;
    await env.setTimeOfDay(tod);
    post.setBloom(env.preset.bloom);
    setLampVisibility();
  }

  function setTrack(geom, o = {}) {
    S.trackGeom = geom;
    const tod = o.timeOfDay || (geom.night ? 'night' : S.tod);
    setTimeOfDay(tod);
    return rebuildTrack();
  }

  // ---- racing line
  function applyLine() {
    if (S.lineMesh) { S.lineMesh.geometry.dispose(); S.lineMesh.material.dispose(); S.lineMesh.removeFromParent(); S.lineMesh = null; }
    const d = S.lineData;
    const mode = S.overlay.line;
    if (!d || !d.line || !d.line.x || mode === 'off') return;
    const { x, y, z, psi } = d.line;
    const n = x.length;
    const half = 0.45;
    const pos = new Float32Array((n + 1) * 6), col = new Float32Array((n + 1) * 6);
    const tel = d.telemetry;
    const series = mode === 'throttle' ? tel?.throttle : tel?.v;
    let vmin = Infinity, vmax = -Infinity;
    if (series && mode === 'speed') for (const v of series) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; }
    for (let k = 0; k <= n; k++) {
      const i = k % n;
      const nx = -Math.sin(psi[i]), ny = Math.cos(psi[i]);
      const yy = (z?.[i] || 0) + 0.07;
      pos.set([x[i] + nx * half, yy, -(y[i] + ny * half), x[i] - nx * half, yy, -(y[i] - ny * half)], k * 6);
      let c = _c.setRGB(0.95, 0.95, 0.95);
      if (series) {
        const sv = series[Math.min(series.length - 1, Math.floor(i / n * series.length))];
        if (mode === 'throttle') {
          const br = tel?.brake ? tel.brake[Math.min(tel.brake.length - 1, Math.floor(i / n * tel.brake.length))] : 0;
          c = br > 0.1 ? _c.setRGB(0.9, 0.15, 0.15) : _c.setRGB(0.25 + 0.0 * sv, 0.35 + 0.6 * sv, 0.3);
        } else c = speedColor((sv - vmin) / Math.max(1e-6, vmax - vmin), _c);
      }
      col.set([c.r, c.g, c.b, c.r, c.g, c.b], k * 6);
    }
    const ind = [];
    for (let k = 0; k < n; k++) { const a = k * 2; ind.push(a, a + 1, a + 2, a + 1, a + 3, a + 2); }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('color', new THREE.BufferAttribute(col, 3));
    g.setIndex(ind);
    const m = new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide, transparent: true, opacity: 0.85, polygonOffset: true, polygonOffsetFactor: -6, polygonOffsetUnits: -6, toneMapped: false });
    S.lineMesh = new THREE.Mesh(g, m);
    S.lineMesh.frustumCulled = false;
    S.lineMesh.renderOrder = 2;
    scene.add(S.lineMesh);
  }

  function setLine(line, o = {}) {
    S.lineData = line ? { line, telemetry: o.telemetry || null } : null;
    if (o.colorBy) S.overlay.line = o.colorBy;
    applyLine();
  }

  // ---- overlays
  function rebuildCorners() {
    if (S.cornerGroup) { S.cornerGroup.traverse((o) => { o.material?.map?.dispose(); o.material?.dispose(); }); S.cornerGroup.removeFromParent(); S.cornerGroup = null; }
    const g = S.trackGeom, t = S.track;
    if (!g || !t || !S.overlay.corners) return;
    S.cornerGroup = new THREE.Group();
    for (const c of g.corners || []) {
      const i = c.apex ?? c.i0;
      const side = c.dir === 'L' ? -1 : 1;   // label on the outside of the corner
      const p = t.pose(i, side * (t.half + 14));
      const sp = numberSprite(c.num);
      sp.position.set(p.x, p.z + 8, -p.y);
      S.cornerGroup.add(sp);
    }
    scene.add(S.cornerGroup);
  }

  function rebuildBrakeMarkers() {
    if (S.brakeGroup) { S.brakeGroup.geometry.dispose(); S.brakeGroup.material.dispose(); S.brakeGroup.removeFromParent(); S.brakeGroup = null; }
    if (!S.overlay.brakeMarkers || !S.brakeMarks.length) return;
    const geo = new THREE.CylinderGeometry(1.1, 1.1, 5, 10).translate(0, 2.5, 0);
    const mat = new THREE.MeshBasicMaterial({ color: 0xff3030, toneMapped: false });
    const mesh = new THREE.InstancedMesh(geo, mat, S.brakeMarks.length);
    const d = new THREE.Object3D();
    S.brakeMarks.forEach((m, k) => { d.position.set(m.x, m.z || 0, -m.y); d.updateMatrix(); mesh.setMatrixAt(k, d.matrix); });
    S.brakeGroup = mesh;
    scene.add(mesh);
  }

  function setOverlay(o = {}) {
    const prev = { ...S.overlay };
    Object.assign(S.overlay, o);
    if (prev.line !== S.overlay.line) applyLine();
    if (prev.corners !== S.overlay.corners) rebuildCorners();
    if (prev.brakeMarkers !== S.overlay.brakeMarkers) rebuildBrakeMarkers();
    if (prev.tags !== S.overlay.tags) for (const c of S.cars.values()) c.setTagVisible(S.overlay.tags && c.spec.id !== S.targetId);
  }

  function setBrakeMarkers(list) { S.brakeMarks = list || []; rebuildBrakeMarkers(); }

  // ---- cars
  function liveryFor(item) { return item.livery || S.liveries[item.team] || null; }

  function setCars(list = []) {
    for (const c of S.cars.values()) c.dispose();
    S.cars.clear();
    for (const item of list) {
      const car = factory.create({ id: item.id, team: item.team, code: item.code, number: item.number, livery: liveryFor(item), compound: item.compound || 'C3' });
      car.setLod(1);
      car.group.visible = false;
      car.setTagVisible(S.overlay.tags);
      scene.add(car.group);
      S.cars.set(item.id, car);
    }
    if (S.targetId && !S.cars.has(S.targetId)) S.targetId = null;
    if (!S.targetId && list.length) S.targetId = list[0].id;
  }

  function updateCars(poses = []) {
    const camPos = camera.position;
    for (const p of poses) {
      const car = S.cars.get(p.id);
      if (!car) continue;
      car.spec._speed = p.speed || 0;
      car.update(p, S.lastDt, S.time);
      if (!car.group.visible) continue;
      const d = camPos.distanceTo(car.group.position);
      car.setLod(d < LOD_DIST[S.quality] || (p.id === S.targetId && S.camMode !== 'top') ? 0 : 1);
      if (p.compound) car.setCompound(p.compound);
    }
  }

  function targetPose() {
    const car = S.cars.get(S.targetId);
    if (!car || !car.group.visible) return null;
    const v = car.last ? Math.hypot(car.group.position.x - car.last.x, car.group.position.z - car.last.z) : 0;
    return { pos: car.group.position, psi: car.group.rotation.y, speed: car.spec._speed || v / Math.max(S.lastDt, 1e-3) };
  }

  function setCamera(mode, targetId) {
    if (targetId !== undefined && targetId !== null) S.targetId = targetId;
    S.camMode = mode;
    rig.setMode(mode);
    for (const c of S.cars.values()) c.setTagVisible(S.overlay.tags && !(mode === 'onboard' && c.spec.id === S.targetId));
  }

  function setLivery(teamId, livery) {
    S.liveries[teamId] = livery;
    for (const c of S.cars.values()) if (c.spec.team === teamId) c.applyLivery(livery);
  }

  // ---- loop
  function frame(now) {
    if (S.disposed) return;
    S.raf = requestAnimationFrame(frame);
    const dt = Math.min(0.1, S.last ? (now - S.last) / 1000 : 1 / 60);
    S.last = now;
    S.lastDt = Math.max(dt, 1e-4);
    S.time += dt;
    renderer.info.reset();

    for (const cb of S.frameCbs) { try { cb(dt); } catch (e) { console.error('onFrame callback failed', e); } }

    const tp = targetPose();
    rig.update(dt, tp);
    if (tp) S.focus.copy(tp.pos);
    else S.focus.copy(rig.controls.target);
    env.update(S.focus);
    for (const c of S.cars.values()) if (c.tag?.visible) c.tag.visible = S.overlay.tags && !(S.camMode === 'onboard' && c.spec.id === S.targetId);
    post.render(dt);

    S.fpsAcc += dt; S.fpsN++;
    if (S.fpsAcc >= 0.5) { S.fps = S.fpsN / S.fpsAcc; S.fpsAcc = 0; S.fpsN = 0; }
  }

  function onContextLost(e) { e.preventDefault(); notes.add('WebGL context lost.'); }
  canvas.addEventListener('webglcontextlost', onContextLost);
  const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => resize()) : null;
  ro?.observe(container);

  applyQuality(S.quality);
  resize();
  await setTimeOfDay(S.tod);
  S.raf = requestAnimationFrame(frame);

  return {
    setTrack, setLine, setCars, updateCars, setCamera, setOverlay, setBrakeMarkers, setLivery, resize,
    setQuality: applyQuality,
    setTimeOfDay,
    onFrame(cb) { S.frameCbs.push(cb); return () => { S.frameCbs = S.frameCbs.filter((f) => f !== cb); }; },
    getStats() {
      return { fps: S.fps, drawCalls: renderer.info.render.calls, triangles: renderer.info.render.triangles, notes: [...notes] };
    },
    cameraModes: ['orbit', 'chase', 'onboard', 'heli', 'top', 'tv', 'free'],
    renderer, scene, camera,
    dispose() {
      S.disposed = true;
      cancelAnimationFrame(S.raf);
      ro?.disconnect();
      canvas.removeEventListener('webglcontextlost', onContextLost);
      for (const c of S.cars.values()) c.dispose();
      S.cars.clear();
      S.track?.dispose();
      if (S.lineMesh) { S.lineMesh.geometry.dispose(); S.lineMesh.material.dispose(); }
      post.dispose(); rig.dispose(); env.dispose(); factory.dispose();
      renderer.dispose();
      if (!isCanvas) canvas.remove();
    },
  };
}
