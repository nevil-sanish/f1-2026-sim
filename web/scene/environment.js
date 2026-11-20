// Sky / image-based lighting, sun (or moon) with a shadow box that follows the focus, ground plane, fog,
// and the night-race lighting option.
import * as THREE from 'three';
import { HDRLoader } from 'three/addons/loaders/HDRLoader.js';
import { ASSET_BASE, loadTexture } from './util.js';

export const TIME_PRESETS = {
  day: {
    hdr: 'day', exposure: 0.9, envIntensity: 1.0, bgIntensity: 1.0,
    sunColor: 0xfff2dc, sunIntensity: 3.2, sunDir: [0.55, 0.75, 0.35], hemi: 0.35, fog: 0xbcd0e4, fogFar: 5500, lamps: false,
    bloom: 0.18,
  },
  sunset: {
    hdr: 'sunset', exposure: 0.85, envIntensity: 0.9, bgIntensity: 1.0,
    sunColor: 0xffa860, sunIntensity: 2.6, sunDir: [0.9, 0.22, 0.25], hemi: 0.25, fog: 0xd69a78, fogFar: 4500, lamps: false,
    bloom: 0.3,
  },
  night: {
    hdr: 'night', exposure: 1.5, envIntensity: 0.6, bgIntensity: 0.9,
    sunColor: 0x8fa8ff, sunIntensity: 1.4, sunDir: [-0.4, 0.8, 0.3], hemi: 0.55, fog: 0x070b16, fogFar: 3000, lamps: true,
    bloom: 0.35,
  },
};

export class Environment {
  constructor(renderer, scene, notes) {
    this.renderer = renderer;
    this.scene = scene;
    this.notes = notes;
    this.pmrem = new THREE.PMREMGenerator(renderer);
    this.envCache = {};
    this.tod = null;
    this.quality = 'medium';
    this.focus = new THREE.Vector3();
    this.lamps = [];

    this.hemi = new THREE.HemisphereLight(0xcfe0ff, 0x4a5a3a, 0.3);
    this.sun = new THREE.DirectionalLight(0xffffff, 3);
    this.sun.castShadow = true;
    this.sun.shadow.bias = -0.0004;
    this.sun.shadow.normalBias = 0.05;
    const cam = this.sun.shadow.camera;
    cam.near = 1; cam.far = 600;
    this.setShadowExtent(90);
    this.scene.add(this.hemi, this.sun, this.sun.target);

    // A light that rides with the focus car at night so the track near the action is visible.
    this.headlight = new THREE.PointLight(0xfff0d0, 0, 140, 1.4);
    this.scene.add(this.headlight);

    this.groundMat = new THREE.MeshStandardMaterial({ color: 0x5f8a49, roughness: 1 });
    this.ground = new THREE.Mesh(new THREE.PlaneGeometry(1, 1).rotateX(-Math.PI / 2), this.groundMat);
    this.ground.receiveShadow = true;
    this.ground.scale.set(14000, 1, 14000);
    this.scene.add(this.ground);
    loadTexture(ASSET_BASE + 'textures/grass_diff.jpg', { srgb: true }).then((t) => {
      if (!t) return;
      t.repeat.set(14000 / 12, 14000 / 12);
      this.groundMat.map = t; this.groundMat.color.set(0x7ea866); this.groundMat.needsUpdate = true;
    });

    this.scene.fog = new THREE.Fog(0xbcd0e4, 600, 5500);
  }

  setShadowExtent(r) {
    const c = this.sun.shadow.camera;
    c.left = -r; c.right = r; c.top = r; c.bottom = -r;
    c.updateProjectionMatrix();
    this.shadowExtent = r;
  }

  setGroundY(y) {
    this.ground.position.set(0, y, 0);
  }

  setQuality(q) {
    this.quality = q;
    const on = q !== 'low';
    this.renderer.shadowMap.enabled = on;
    this.sun.castShadow = on;
    const size = q === 'high' ? 4096 : 2048;
    if (on && this.sun.shadow.mapSize.x !== size) {
      this.sun.shadow.mapSize.set(size, size);
      this.sun.shadow.map?.dispose();
      this.sun.shadow.map = null;
    }
    this.renderer.shadowMap.type = q === 'high' ? THREE.PCFSoftShadowMap : THREE.PCFShadowMap;
    this.scene.traverse((o) => { if (o.material) { const m = Array.isArray(o.material) ? o.material : [o.material]; m.forEach((x) => { x.needsUpdate = true; }); } });
  }

  // lampPositions: scene-space Vector3[] for emissive heads (handled by the track mesh); used for bloom only.
  setLamps(list) { this.lamps = list || []; }

  async loadEnv(name) {
    if (this.envCache[name]) return this.envCache[name];
    const p = new Promise((resolve) => {
      new HDRLoader().load(`${ASSET_BASE}hdri/${name}.hdr`, (tex) => {
        tex.mapping = THREE.EquirectangularReflectionMapping;
        const rt = this.pmrem.fromEquirectangular(tex);
        resolve({ env: rt.texture, sky: tex });
      }, undefined, () => {
        this.notes.add(`HDRI '${name}' missing; using a flat sky colour. Run scripts/fetch_assets.py.`);
        resolve(null);
      });
    });
    this.envCache[name] = p;
    return p;
  }

  async setTimeOfDay(tod) {
    const P = TIME_PRESETS[tod] || TIME_PRESETS.day;
    this.tod = tod in TIME_PRESETS ? tod : 'day';
    this.preset = P;
    const r = this.renderer;
    r.toneMappingExposure = P.exposure;
    this.sun.color.set(P.sunColor);
    this.sun.intensity = P.sunIntensity;
    this.sunDir = new THREE.Vector3(...P.sunDir).normalize();
    this.hemi.intensity = P.hemi;
    this.scene.fog.color.set(P.fog);
    this.scene.fog.near = 800; this.scene.fog.far = P.fogFar;
    this.headlight.intensity = P.lamps ? 220 : 0;
    this.groundMat.color.set(P.lamps ? 0x2a3a24 : this.tod === 'sunset' ? 0x9a8d5e : 0x7ea866);
    const res = await this.loadEnv(P.hdr);
    if (this.tod !== (tod in TIME_PRESETS ? tod : 'day')) return;   // superseded
    if (res) {
      this.scene.environment = res.env;
      this.scene.environmentIntensity = P.envIntensity;
      this.scene.background = res.sky;
      this.scene.backgroundIntensity = P.bgIntensity;
      this.scene.backgroundBlurriness = 0;
    } else {
      this.scene.environment = null;
      this.scene.background = new THREE.Color(P.fog);
    }
    return P;
  }

  // Called every frame with the point the shadow box should surround.
  update(focus) {
    if (focus) this.focus.copy(focus);
    const f = this.focus;
    // snap to the texel grid so shadows do not shimmer as the box follows
    const texel = (this.shadowExtent * 2) / this.sun.shadow.mapSize.x;
    const sx = Math.round(f.x / texel) * texel, sz = Math.round(f.z / texel) * texel;
    this.sun.target.position.set(sx, f.y, sz);
    const d = this.sunDir || new THREE.Vector3(0.5, 0.8, 0.3);
    this.sun.position.set(sx + d.x * 300, f.y + d.y * 300, sz + d.z * 300);
    this.sun.target.updateMatrixWorld();
    if (this.preset?.lamps) this.headlight.position.set(f.x, f.y + 20, f.z);
  }

  dispose() {
    this.pmrem.dispose();
    for (const p of Object.values(this.envCache)) p.then?.((r) => { r?.env.dispose(); r?.sky.dispose(); });
    this.ground.geometry.dispose(); this.groundMat.dispose();
    this.sun.shadow.map?.dispose();
  }
}
