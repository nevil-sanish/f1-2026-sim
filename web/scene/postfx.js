// Optional post-processing: bloom (medium, high) with multisampled render target (high). Off on 'low'.
// The vendored addons have no SMAA pass, so anti-aliasing on 'high' comes from a 4x MSAA composer target.
import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

export class PostFX {
  constructor(renderer, scene, camera) {
    this.renderer = renderer;
    this.scene = scene;
    this.camera = camera;
    this.composer = null;
    this.bloom = null;
    this.quality = 'low';
    this.strength = 0.2;
    this.size = new THREE.Vector2(1, 1);
  }

  get enabled() { return !!this.composer; }

  setQuality(q, w, h) {
    this.quality = q;
    this.size.set(w, h);
    this.disposeComposer();
    if (q === 'low') return;
    const samples = q === 'high' ? 4 : 0;
    const dpr = this.renderer.getPixelRatio();
    const target = new THREE.WebGLRenderTarget(w * dpr, h * dpr, { type: THREE.HalfFloatType, samples });
    this.composer = new EffectComposer(this.renderer, target);
    this.composer.setPixelRatio(dpr);
    this.composer.setSize(w, h);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new THREE.Vector2(w, h), this.strength, 0.6, 0.85);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());
  }

  setBloom(strength) {
    this.strength = strength;
    if (this.bloom) this.bloom.strength = strength;
  }

  resize(w, h) {
    this.size.set(w, h);
    this.composer?.setPixelRatio(this.renderer.getPixelRatio());
    this.composer?.setSize(w, h);
  }

  setCamera(camera) {
    this.camera = camera;
    if (this.composer) this.composer.passes[0].camera = camera;
  }

  render(dt) {
    if (this.composer) this.composer.render(dt);
    else this.renderer.render(this.scene, this.camera);
  }

  disposeComposer() {
    if (!this.composer) return;
    for (const p of this.composer.passes) p.dispose?.();
    this.composer.renderTarget1.dispose();
    this.composer.renderTarget2.dispose();
    this.composer = null; this.bloom = null;
  }

  dispose() { this.disposeComposer(); }
}
