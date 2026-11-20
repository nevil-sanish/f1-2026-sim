// Camera rig: orbit, chase, onboard, heli, top-down, TV/trackside and free modes.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { clamp, damp } from './util.js';

export const CAMERA_MODES = ['orbit', 'chase', 'onboard', 'heli', 'top', 'tv', 'free'];

const _f = new THREE.Vector3(), _p = new THREE.Vector3(), _t = new THREE.Vector3(), _d = new THREE.Vector3();
const UP = new THREE.Vector3(0, 1, 0);

export class CameraRig {
  constructor(camera, domElement) {
    this.camera = camera;
    this.controls = new OrbitControls(camera, domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;
    this.controls.maxPolarAngle = Math.PI * 0.495;
    this.controls.minDistance = 4;
    this.controls.maxDistance = 3000;
    this.mode = 'orbit';
    this.tv = [];
    this.tvIndex = -1;
    this.smooth = new THREE.Vector3();
    this.look = new THREE.Vector3();
    this.lastTarget = new THREE.Vector3();
    this.hasLast = false;
    this.centre = new THREE.Vector3();
    this.extent = 1000;
    this.fresh = true;
    this.setMode('orbit');
  }

  setTrack(bounds, tvPoints) {
    this.tv = tvPoints || [];
    this.tvIndex = -1;
    this.centre.set(bounds.cx, (bounds.minZ + bounds.maxZ) / 2, -bounds.cy);
    this.extent = Math.max(bounds.maxX - bounds.minX, bounds.maxY - bounds.minY);
    this.frameTrack();
  }

  frameTrack() {
    this.controls.target.copy(this.centre);
    this.camera.position.set(this.centre.x + this.extent * 0.35, this.extent * 0.55, this.centre.z + this.extent * 0.6);
    this.camera.fov = 50;
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.hasLast = false;
  }

  setMode(mode) {
    if (!CAMERA_MODES.includes(mode)) mode = 'orbit';
    const prev = this.mode;
    this.mode = mode;
    const orbitLike = mode === 'orbit' || mode === 'free';
    this.controls.enabled = orbitLike;
    this.fresh = true;
    this.hasLast = false;
    if (mode !== 'tv') this.tvIndex = -1;
    this.camera.near = mode === 'onboard' ? 0.05 : 0.3;
    if (mode !== 'tv') { this.camera.fov = mode === 'onboard' ? 78 : mode === 'top' ? 40 : 55; }
    this.camera.updateProjectionMatrix();
    if (orbitLike && prev !== mode) {
      // keep the view where it is; orbit re-targets on the next update
      this.camera.up.copy(UP);
    }
    if (!orbitLike) this.camera.up.copy(UP);
  }

  /**
   * @param {number} dt seconds
   * @param {{pos:THREE.Vector3, psi:number, speed:number}|null} target pose of the followed car in scene space
   */
  update(dt, target) {
    const cam = this.camera;
    const a = (rate) => damp(rate, dt);
    const m = this.mode;

    if (!target) {
      if (m === 'orbit' || m === 'free') this.controls.update();
      return;
    }
    _f.set(Math.cos(target.psi), 0, -Math.sin(target.psi));
    const tp = target.pos;
    const fresh = this.fresh;
    this.fresh = false;

    if (m === 'orbit' || m === 'free') {
      if (m === 'orbit') {
        if (this.hasLast) {
          _d.copy(tp).sub(this.lastTarget);
          if (_d.lengthSq() < 80 * 80) { cam.position.add(_d); this.controls.target.add(_d); }
          else { cam.position.add(_d); this.controls.target.copy(tp); }
        } else {
          this.controls.target.copy(tp);
          cam.position.copy(tp).addScaledVector(_f, -16).add(_t.set(0, 7, 0));
        }
        this.lastTarget.copy(tp); this.hasLast = true;
      }
      this.controls.update();
      return;
    }

    if (m === 'chase') {
      const dist = 8.5 + clamp(target.speed / 90, 0, 1) * 2.5;
      _p.copy(tp).addScaledVector(_f, -dist).add(_t.set(0, 2.9, 0));
      if (fresh) this.smooth.copy(_p); else this.smooth.lerp(_p, a(7));
      cam.position.copy(this.smooth);
      this.look.copy(tp).addScaledVector(_f, 9).add(_t.set(0, 0.8, 0));
      cam.lookAt(this.look);
    } else if (m === 'onboard') {
      cam.position.copy(tp).addScaledVector(_f, 0.15).add(_t.set(0, 0.92, 0));
      // look slightly ahead, easing into corners rather than snapping
      this.look.copy(tp).addScaledVector(_f, 40).add(_t.set(0, 0.9, 0));
      cam.lookAt(this.look);
    } else if (m === 'heli') {
      _p.copy(tp).addScaledVector(_f, -22).add(_t.set(0, 55, 0));
      if (fresh) this.smooth.copy(_p); else this.smooth.lerp(_p, a(2.5));
      cam.position.copy(this.smooth);
      this.look.copy(tp).addScaledVector(_f, 12);
      cam.lookAt(this.look);
    } else if (m === 'top') {
      _p.copy(tp).add(_t.set(0, 140, 0));
      if (fresh) this.smooth.copy(_p); else this.smooth.lerp(_p, a(5));
      cam.position.copy(this.smooth);
      cam.up.copy(_f);
      this.look.copy(this.smooth).setY(tp.y);
      cam.lookAt(this.look);
      cam.up.copy(UP);
    } else if (m === 'tv') {
      this.updateTv(dt, tp, fresh);
    }
  }

  updateTv(dt, tp, fresh) {
    const cam = this.camera;
    if (!this.tv.length) {
      cam.position.set(tp.x + 18, tp.y + 8, tp.z + 18);
      cam.lookAt(tp);
      return;
    }
    const dist = (i) => this.tv[i].distanceTo(tp);
    let best = 0, bd = Infinity;
    for (let i = 0; i < this.tv.length; i++) { const d = dist(i); if (d < bd) { bd = d; best = i; } }
    // hysteresis: only cut when the current camera is clearly worse
    if (this.tvIndex < 0 || fresh || dist(this.tvIndex) > Math.max(220, bd * 1.8)) {
      if (best !== this.tvIndex) { this.tvIndex = best; this.smooth.copy(tp); }
    }
    cam.position.copy(this.tv[this.tvIndex]);
    this.smooth.lerp(tp, damp(9, dt));
    cam.lookAt(this.smooth);
    const d = cam.position.distanceTo(this.smooth);
    const fov = clamp(2 * Math.atan(9 / Math.max(d, 8)) * 180 / Math.PI, 6, 50);
    cam.fov += (fov - cam.fov) * damp(4, dt);
    cam.updateProjectionMatrix();
  }

  dispose() { this.controls.dispose(); }
}
