// 2D top-down stand-in for the 3D scene. Implements the same interface as web/scene/scene.js
// (docs/API.md section 3) so the rest of the app does not care which one is active.
import { COMPOUND_COLORS } from './util.js';

function speedColor(f) {           // slow = red, fast = cyan-blue
  const hue = 4 + 196 * Math.min(1, Math.max(0, f));
  return `hsl(${hue.toFixed(0)} 78% 56%)`;
}

export function createFallbackScene(container, reason) {
  const canvas = document.createElement('canvas');
  canvas.className = 'fallback2d';
  container.append(canvas);
  const st = {
    track: null, line: null, tel: null, cars: [], poses: new Map(), markers: [],
    overlay: { line: 'speed', corners: true, brakeMarkers: true, tags: true },
    cam: 'orbit', target: null, cb: null, raf: 0, last: 0, fps: 0, dead: false, tod: 'day',
    bounds: null, zoom: 1,
  };

  function bounds() {
    const t = st.track;
    if (!t) return null;
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (let i = 0; i < t.x.length; i++) {
      if (t.x[i] < x0) x0 = t.x[i]; if (t.x[i] > x1) x1 = t.x[i];
      if (t.y[i] < y0) y0 = t.y[i]; if (t.y[i] > y1) y1 = t.y[i];
    }
    return { x0, x1, y0, y1 };
  }

  function draw() {
    const r = canvas.getBoundingClientRect(), d = window.devicePixelRatio || 1;
    const W = Math.max(1, Math.round(r.width * d)), H = Math.max(1, Math.round(r.height * d));
    if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(d, 0, 0, d, 0, 0);
    const w = r.width, h = r.height;
    ctx.fillStyle = st.tod === 'night' ? '#090c0f' : '#10151a';
    ctx.fillRect(0, 0, w, h);
    ctx.font = '11px system-ui, sans-serif'; ctx.textBaseline = 'top'; ctx.textAlign = 'left';
    ctx.fillStyle = '#7d8996';
    ctx.fillText(`2D top-down fallback – ${reason}`, 10, 8);
    const t = st.track, b = st.bounds;
    if (!t || !b) { ctx.fillText('No track geometry loaded.', 10, 24); return; }

    // view transform
    const m = 34;
    let scale = Math.min((w - 2 * m) / Math.max(1, b.x1 - b.x0), (h - 2 * m) / Math.max(1, b.y1 - b.y0));
    let cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
    const tp = st.target && st.poses.get(st.target);
    if (tp && (st.cam === 'chase' || st.cam === 'onboard')) { scale *= st.cam === 'onboard' ? 7 : 4; cx = tp.x; cy = tp.y; }
    else if (tp && st.cam === 'tv') { scale *= 2; cx = tp.x; cy = tp.y; }
    const X = (x) => w / 2 + (x - cx) * scale, Y = (y) => h / 2 - (y - cy) * scale;

    // road
    const path = (xs, ys) => { ctx.beginPath(); for (let i = 0; i < xs.length; i++) { const px = X(xs[i]), py = Y(ys[i]); if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py); } ctx.closePath(); };
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    path(t.x, t.y);
    ctx.strokeStyle = '#3b444e'; ctx.lineWidth = Math.max(5, (t.width || 12) * scale + 2); ctx.stroke();
    ctx.strokeStyle = '#262d35'; ctx.lineWidth = Math.max(3, (t.width || 12) * scale); ctx.stroke();
    // start line
    const nx = -Math.sin(t.psi[0]), ny = Math.cos(t.psi[0]), hw = Math.max(8, t.width || 12) * 0.7;
    ctx.strokeStyle = '#e8edf2'; ctx.lineWidth = 2; ctx.beginPath();
    ctx.moveTo(X(t.x[0] + nx * hw), Y(t.y[0] + ny * hw)); ctx.lineTo(X(t.x[0] - nx * hw), Y(t.y[0] - ny * hw)); ctx.stroke();

    // racing line
    const L = st.line;
    if (L && st.overlay.line !== 'off') {
      const tel = st.tel, n = L.x.length;
      let vmin = Infinity, vmax = -Infinity;
      if (tel && tel.v) for (const v of tel.v) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; }
      ctx.lineWidth = 2;
      const stride = Math.max(1, Math.floor(n / 900));
      for (let i = 0; i < n; i += stride) {
        const j = (i + stride) % n;
        if (tel && st.overlay.line === 'throttle') ctx.strokeStyle = tel.brake[i] > 0.05 ? '#e5484d' : tel.throttle[i] > 0.95 ? '#4cb782' : '#e2a93b';
        else if (tel && tel.v) ctx.strokeStyle = speedColor((tel.v[i] - vmin) / Math.max(1, vmax - vmin));
        else ctx.strokeStyle = '#7fb2e5';
        ctx.beginPath(); ctx.moveTo(X(L.x[i]), Y(L.y[i])); ctx.lineTo(X(L.x[j]), Y(L.y[j])); ctx.stroke();
      }
    }
    if (st.overlay.brakeMarkers) {
      ctx.fillStyle = '#e5484d';
      for (const mk of st.markers) { ctx.beginPath(); ctx.arc(X(mk.x), Y(mk.y), 3, 0, 7); ctx.fill(); }
    }
    if (st.overlay.corners && t.corners) {
      ctx.fillStyle = '#93a0ad'; ctx.font = '10px system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      for (const c of t.corners) {
        const i = c.apex, side = c.dir === 'L' ? -1 : 1;
        const ox = -Math.sin(t.psi[i]) * side, oy = Math.cos(t.psi[i]) * side;
        const off = (t.width || 12) / 2 + 14 / scale;
        ctx.fillText(String(c.num), X(t.x[i] + ox * off), Y(t.y[i] + oy * off));
      }
    }
    // cars
    for (const c of st.cars) {
      const p = st.poses.get(c.id);
      if (!p || p.visible === false) continue;
      const px = X(p.x), py = Y(p.y);
      const col = (c.livery && c.livery.base) || '#d5dbe1';
      ctx.save(); ctx.translate(px, py); ctx.rotate(-p.psi);
      const len = Math.max(9, 5.4 * scale), wid = Math.max(4.5, 1.9 * scale);
      ctx.fillStyle = col; ctx.strokeStyle = '#0d1013'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(len / 2, 0); ctx.lineTo(-len / 2, wid / 2); ctx.lineTo(-len / 2, -wid / 2); ctx.closePath(); ctx.fill(); ctx.stroke();
      ctx.restore();
      if (c.compound) { ctx.fillStyle = COMPOUND_COLORS[c.compound] || '#8a96a3'; ctx.beginPath(); ctx.arc(px, py, 1.6, 0, 7); ctx.fill(); }
      if (st.overlay.tags && c.code) {
        ctx.font = '600 10px system-ui, sans-serif'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
        const tw = ctx.measureText(c.code).width;
        ctx.fillStyle = 'rgba(13,16,19,0.8)'; ctx.fillRect(px + 7, py - 14, tw + 6, 12);
        ctx.fillStyle = c.id === st.target ? '#ffffff' : '#c3ccd5'; ctx.fillText(c.code, px + 10, py - 8);
      }
    }
  }

  function loop(now) {
    if (st.dead) return;
    const dt = st.last ? Math.min(0.1, (now - st.last) / 1000) : 0;
    st.last = now;
    if (dt > 0) st.fps = st.fps * 0.9 + (1 / dt) * 0.1;
    if (st.cb) { try { st.cb(dt); } catch (e) { console.error(e); } }
    draw();
    st.raf = requestAnimationFrame(loop);
  }
  st.raf = requestAnimationFrame(loop);

  return {
    setTrack(g, o) { st.track = g; st.bounds = bounds(); st.line = null; st.tel = null; st.markers = []; if (o && o.timeOfDay) st.tod = o.timeOfDay; },
    setLine(line, o) { st.line = line; st.tel = (o && o.telemetry) || null; if (o && o.colorBy) st.overlay.line = o.colorBy; },
    setCars(cars) { st.cars = cars || []; st.poses.clear(); },
    updateCars(list) { for (const p of list) st.poses.set(p.id, p); },
    setCamera(mode, target) { st.cam = mode; st.target = target ?? st.target; },
    setOverlay(o) { Object.assign(st.overlay, o); },
    setBrakeMarkers(m) { st.markers = m || []; },
    setQuality() {}, setTimeOfDay(t) { st.tod = t; },
    setLivery(team, livery) { for (const c of st.cars) if (c.team === team) c.livery = livery; },
    onFrame(cb) { st.cb = cb; },
    getStats() { return { fps: Math.round(st.fps), drawCalls: 0, triangles: 0, notes: [`2D fallback: ${reason}`] }; },
    resize() {}, dispose() { st.dead = true; cancelAnimationFrame(st.raf); canvas.remove(); },
  };
}
