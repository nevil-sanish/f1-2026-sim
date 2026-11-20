// Hand-drawn canvas charts. No dependencies.
//  - TraceStack: stacked telemetry rows over lap distance with one shared cursor, wheel zoom, drag scrub.
//  - drawLineChart / drawStrategy: static race charts (lap chart, gaps, tyre stints).

export const THEME = {
  grid: '#1f262d', frame: '#2a333c', axis: '#7d8996', label: '#93a0ad', text: '#d5dbe1',
  cursor: '#e8edf2', bg: '#11161a', mono: '11px ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace',
  font: '11px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
};

function fit(canvas) {
  const r = canvas.getBoundingClientRect();
  const d = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width * d)), hh = Math.max(1, Math.round(r.height * d));
  if (canvas.width !== w || canvas.height !== hh) { canvas.width = w; canvas.height = hh; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(d, 0, 0, d, 0, 0);
  return { ctx, w: r.width, h: r.height };
}

function lowerBound(arr, x) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] > x) hi = mid; else lo = mid + 1; }
  return lo;
}

export function valueAt(xs, ys, x, step) {
  const n = xs.length;
  if (!n) return NaN;
  const k = lowerBound(xs, x);
  if (k <= 0) return ys[0];
  if (k >= n) return ys[n - 1];
  if (step) return ys[k - 1];
  const f = (x - xs[k - 1]) / ((xs[k] - xs[k - 1]) || 1);
  return ys[k - 1] + (ys[k] - ys[k - 1]) * f;
}

function niceTicks(lo, hi, n) {
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const raw = span / Math.max(1, n);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return out;
}
const tickLabel = (v) => (Math.abs(v) >= 100 || Number.isInteger(v) ? String(Math.round(v)) : v.toFixed(Math.abs(v) < 1 ? 2 : 1));

// =============================================================================================
export class TraceStack {
  // rows: [{id, label, unit, weight, min, max, kind:'line'|'band', digits, zero:bool,
  //         series:[{x:[], y:[], color, label, dash:bool, step:bool, alpha}]}]
  constructor(host, { onCursor } = {}) {
    this.host = host;
    this.onCursor = onCursor;
    this.base = document.createElement('canvas');
    this.over = document.createElement('canvas');
    this.base.className = 'ts-base'; this.over.className = 'ts-over';
    host.append(this.base, this.over);
    this.pad = { l: 44, r: 8, t: 3, b: 17 };
    this.data = null; this.view = [0, 1]; this.cx = null; this.hover = false;
    this._drag = false;
    const pos = (e) => { const r = this.over.getBoundingClientRect(); return e.clientX - r.left; };
    this.over.addEventListener('pointerdown', (e) => {
      if (!this.data) return;
      this._drag = true; this.over.setPointerCapture(e.pointerId);
      this._emit(pos(e), true);
    });
    this.over.addEventListener('pointermove', (e) => { if (this.data && (this._drag || e.buttons === 0)) this._emit(pos(e), this._drag); });
    this.over.addEventListener('pointerup', () => { this._drag = false; });
    this.over.addEventListener('pointercancel', () => { this._drag = false; });
    this.over.addEventListener('wheel', (e) => {
      if (!this.data) return;
      e.preventDefault();
      const x = this._pxToX(pos(e));
      const k = e.deltaY > 0 ? 1.25 : 0.8;
      let [a, b] = this.view;
      a = x - (x - a) * k; b = x + (b - x) * k;
      const full = this.data.xMax;
      if (b - a >= full) { a = 0; b = full; }
      if (b - a < full / 200) return;
      if (a < 0) { b -= a; a = 0; }
      if (b > full) { a -= b - full; b = full; }
      this.view = [Math.max(0, a), Math.min(full, b)];
      this.draw();
    }, { passive: false });
    this.over.addEventListener('dblclick', () => { if (this.data) { this.view = [0, this.data.xMax]; this.draw(); } });
    this._ro = new ResizeObserver(() => this.draw());
    this._ro.observe(host);
  }

  _emit(px, commit) {
    const x = Math.min(this.view[1], Math.max(this.view[0], this._pxToX(px)));
    if (this.onCursor) this.onCursor(x, commit);
  }
  _plotW() { return Math.max(10, this.host.clientWidth - this.pad.l - this.pad.r); }
  _xToPx(x) { return this.pad.l + ((x - this.view[0]) / (this.view[1] - this.view[0])) * this._plotW(); }
  _pxToX(px) { return this.view[0] + ((px - this.pad.l) / this._plotW()) * (this.view[1] - this.view[0]); }

  setData(data) {
    const keepView = this.data && data && this.data.xMax === data.xMax;
    this.data = data;
    if (data) {
      if (!keepView) this.view = [0, data.xMax];
      for (const row of data.rows) this._range(row);
    }
    this.draw();
  }

  _range(row) {
    if (row.kind === 'band') { row._lo = 0; row._hi = 1; return; }
    let lo = Infinity, hi = -Infinity;
    for (const s of row.series) for (const v of s.y) { if (v < lo) lo = v; if (v > hi) hi = v; }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (row.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    const padY = (hi - lo || 1) * 0.08;
    row._lo = row.min != null ? row.min : lo - padY;
    row._hi = row.max != null ? row.max : hi + padY;
    if (row._hi <= row._lo) row._hi = row._lo + 1;
  }

  _layout(h) {
    const rows = this.data.rows, gap = 3;
    const avail = h - this.pad.t - this.pad.b - gap * (rows.length - 1);
    const total = rows.reduce((a, r) => a + (r.weight || 1), 0);
    let y = this.pad.t;
    for (const r of rows) { r._y = y; r._h = Math.max(8, (avail * (r.weight || 1)) / total); y += r._h + gap; }
  }

  draw() {
    const { ctx, w, h } = fit(this.base);
    ctx.clearRect(0, 0, w, h);
    if (!this.data) {
      ctx.fillStyle = THEME.axis; ctx.font = THEME.font; ctx.textAlign = 'center';
      ctx.fillText('Run a lap to see telemetry traces.', w / 2, h / 2);
      this._drawOver(); return;
    }
    this._layout(h);
    const pw = this._plotW(), x0 = this.pad.l, [va, vb] = this.view;
    const xticks = niceTicks(va, vb, Math.max(3, Math.floor(pw / 90)));

    for (const row of this.data.rows) {
      const { _y: y, _h: rh, _lo: lo, _hi: hi } = row;
      const yPx = (v) => y + rh - ((v - lo) / (hi - lo)) * rh;
      ctx.fillStyle = THEME.bg; ctx.fillRect(x0, y, pw, rh);
      // grid
      ctx.strokeStyle = THEME.grid; ctx.lineWidth = 1; ctx.beginPath();
      for (const t of xticks) { const px = Math.round(this._xToPx(t)) + 0.5; ctx.moveTo(px, y); ctx.lineTo(px, y + rh); }
      ctx.stroke();
      ctx.font = THEME.mono; ctx.fillStyle = THEME.axis; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      if (row.kind !== 'band') {
        const yt = niceTicks(lo, hi, Math.max(1, Math.floor(rh / 26)));
        ctx.beginPath();
        for (const t of yt) {
          const py = Math.round(yPx(t)) + 0.5;
          if (py < y + 5 || py > y + rh - 3) continue;
          ctx.moveTo(x0, py); ctx.lineTo(x0 + pw, py);
          ctx.fillText(tickLabel(t), x0 - 5, py);
        }
        ctx.stroke();
        if (row.zero && lo < 0 && hi > 0) {
          ctx.strokeStyle = THEME.frame; ctx.beginPath();
          const py = Math.round(yPx(0)) + 0.5; ctx.moveTo(x0, py); ctx.lineTo(x0 + pw, py); ctx.stroke();
        }
      }
      // corner ticks
      if (this.data.corners) {
        ctx.strokeStyle = 'rgba(147,160,173,0.16)'; ctx.beginPath();
        for (const c of this.data.corners) {
          if (c.s < va || c.s > vb) continue;
          const px = Math.round(this._xToPx(c.s)) + 0.5; ctx.moveTo(px, y); ctx.lineTo(px, y + rh);
        }
        ctx.stroke();
      }
      // series
      ctx.save(); ctx.beginPath(); ctx.rect(x0, y, pw, rh); ctx.clip();
      if (row.kind === 'band') {
        const n = row.series.length, bh = rh / n;
        row.series.forEach((s, k) => {
          ctx.fillStyle = s.color; ctx.globalAlpha = 0.85;
          let start = null;
          for (let i = 0; i <= s.x.length; i++) {
            const on = i < s.x.length && s.y[i] >= 0.5;
            if (on && start === null) start = s.x[i];
            if (!on && start !== null) {
              const end = i < s.x.length ? s.x[i] : s.x[s.x.length - 1];
              const a = this._xToPx(start), b = this._xToPx(end);
              ctx.fillRect(a, y + k * bh + 1, Math.max(1, b - a), bh - 2);
              start = null;
            }
          }
          ctx.globalAlpha = 1;
        });
      } else {
        for (const s of row.series) {
          const n = s.x.length;
          if (!n) continue;
          let i0 = Math.max(0, lowerBound(s.x, va) - 1), i1 = Math.min(n - 1, lowerBound(s.x, vb));
          const stride = Math.max(1, Math.floor((i1 - i0) / (pw * 2)));
          ctx.strokeStyle = s.color; ctx.globalAlpha = s.alpha || 1; ctx.lineWidth = s.width || 1.4; ctx.lineJoin = 'round';
          ctx.setLineDash(s.dash ? [5, 3] : []);
          ctx.beginPath();
          let prevY = null;
          for (let i = i0; i <= i1; i += stride) {
            const px = this._xToPx(s.x[i]), py = yPx(s.y[i]);
            if (i === i0) ctx.moveTo(px, py);
            else if (s.step) { ctx.lineTo(px, prevY); ctx.lineTo(px, py); } else ctx.lineTo(px, py);
            prevY = py;
          }
          ctx.stroke();
        }
        ctx.setLineDash([]); ctx.globalAlpha = 1;
      }
      ctx.restore();
      ctx.strokeStyle = THEME.frame; ctx.lineWidth = 1; ctx.strokeRect(x0 + 0.5, y + 0.5, pw - 1, rh - 1);
    }
    // corner numbers along the top edge of the first row, x axis labels along the bottom
    const first = this.data.rows[0];
    if (this.data.corners && first) {
      ctx.font = '9px system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'; ctx.fillStyle = THEME.axis;
      let lastPx = -99;
      for (const c of this.data.corners) {
        if (c.s < va || c.s > vb) continue;
        const px = this._xToPx(c.s);
        if (px - lastPx < 13) continue;
        ctx.fillText(String(c.num), px, first._y + first._h - 2); lastPx = px;
      }
    }
    ctx.font = THEME.mono; ctx.fillStyle = THEME.axis; ctx.textBaseline = 'top'; ctx.textAlign = 'center';
    for (const t of xticks) ctx.fillText(tickLabel(t), this._xToPx(t), h - this.pad.b + 4);
    ctx.textAlign = 'right'; ctx.fillText('m', x0 - 5, h - this.pad.b + 4);
    this._drawOver();
  }

  setCursor(x) { this.cx = x; this._drawOver(); }

  _drawOver() {
    const { ctx, w, h } = fit(this.over);
    ctx.clearRect(0, 0, w, h);
    if (!this.data) return;
    const x0 = this.pad.l, cx = this.cx;
    const inView = cx != null && cx >= this.view[0] && cx <= this.view[1];
    if (inView) {
      const px = Math.round(this._xToPx(cx)) + 0.5;
      ctx.strokeStyle = THEME.cursor; ctx.globalAlpha = 0.75; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(px, this.pad.t); ctx.lineTo(px, h - this.pad.b); ctx.stroke(); ctx.globalAlpha = 1;
    }
    for (const row of this.data.rows) {
      if (row._y == null) continue;
      let tx = x0 + 6;
      const ty = row._y + 3;
      ctx.textBaseline = 'top'; ctx.textAlign = 'left';
      ctx.font = '600 10px system-ui, sans-serif'; ctx.fillStyle = THEME.label;
      const title = row.label.toUpperCase() + (row.unit ? `  ${row.unit}` : '');
      const tw = ctx.measureText(title).width;
      ctx.fillStyle = 'rgba(17,22,26,0.78)'; ctx.fillRect(tx - 3, ty - 1, tw + 6, 13);
      ctx.fillStyle = THEME.label; ctx.fillText(title, tx, ty);
      tx += tw + 12;
      if (cx == null) continue;
      ctx.font = THEME.mono;
      for (const s of row.series) {
        const v = valueAt(s.x, s.y, cx, s.step || row.kind === 'band');
        if (!isFinite(v)) continue;
        const txt = row.kind === 'band' ? (v >= 0.5 ? (row.on || 'on') : (row.off || 'off'))
          : (s.tag ? s.tag + ' ' : '') + v.toFixed(row.digits != null ? row.digits : 1);
        const sw = ctx.measureText(txt).width;
        if (tx + sw > w - this.pad.r - 4) break;
        ctx.fillStyle = 'rgba(17,22,26,0.78)'; ctx.fillRect(tx - 3, ty - 1, sw + 6, 13);
        ctx.fillStyle = s.color; ctx.fillText(txt, tx, ty);
        tx += sw + 10;
      }
    }
  }
}

// =============================================================================================
// Generic multi-series line chart over an integer x axis (laps).
// opts: {series:[{label, color, dash, y:[]}], invertY, yInt, yMin, yMax, xLabel, yLabel, cursorX, yClamp}
export function drawLineChart(canvas, opts) {
  const { ctx, w, h } = fit(canvas);
  ctx.clearRect(0, 0, w, h);
  const pad = { l: 40, r: 44, t: 8, b: 24 };
  const series = (opts.series || []).filter((s) => s.y && s.y.length);
  ctx.font = THEME.font;
  if (!series.length) { ctx.fillStyle = THEME.axis; ctx.textAlign = 'center'; ctx.fillText(opts.empty || 'No data.', w / 2, h / 2); return; }
  const nx = Math.max(...series.map((s) => s.y.length)) - 1;
  let lo = Infinity, hi = -Infinity;
  for (const s of series) for (const v of s.y) { if (v == null || !isFinite(v)) continue; if (v < lo) lo = v; if (v > hi) hi = v; }
  if (opts.yMin != null) lo = opts.yMin;
  if (opts.yMax != null) hi = opts.yMax;
  if (opts.yClamp != null) hi = Math.min(hi, opts.yClamp);
  if (!(hi > lo)) hi = lo + 1;
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  const x0 = opts.x0 || 0;
  const xPx = (i) => pad.l + (nx ? (i / nx) * pw : 0);
  const yPx = (v) => { const f = (v - lo) / (hi - lo); return pad.t + (opts.invertY ? f : 1 - f) * ph; };

  ctx.fillStyle = THEME.bg; ctx.fillRect(pad.l, pad.t, pw, ph);
  ctx.strokeStyle = THEME.grid; ctx.lineWidth = 1; ctx.fillStyle = THEME.axis; ctx.font = THEME.mono;
  const yt = opts.yInt ? Array.from({ length: Math.round(hi - lo) + 1 }, (_, i) => lo + i).filter((v) => (hi - lo) <= 12 || (v - lo) % 2 === 0 || v === hi)
    : niceTicks(lo, hi, Math.max(2, Math.floor(ph / 32)));
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.beginPath();
  for (const t of yt) { const py = Math.round(yPx(t)) + 0.5; ctx.moveTo(pad.l, py); ctx.lineTo(pad.l + pw, py); ctx.fillText(tickLabel(t), pad.l - 5, py); }
  const xt = niceTicks(x0, x0 + nx, Math.max(3, Math.floor(pw / 60))).filter((v) => Number.isInteger(v));
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const t of xt) { const px = Math.round(xPx(t - x0)) + 0.5; ctx.moveTo(px, pad.t); ctx.lineTo(px, pad.t + ph); ctx.fillText(String(t), px, pad.t + ph + 4); }
  ctx.stroke();
  ctx.strokeStyle = THEME.frame; ctx.strokeRect(pad.l + 0.5, pad.t + 0.5, pw - 1, ph - 1);
  ctx.fillStyle = THEME.label; ctx.font = '10px system-ui, sans-serif';
  if (opts.xLabel) { ctx.textAlign = 'right'; ctx.fillText(opts.xLabel, pad.l + pw, pad.t + ph + 13); }
  if (opts.yLabel) { ctx.textAlign = 'left'; ctx.textBaseline = 'top'; ctx.fillText(opts.yLabel, pad.l + 5, pad.t + 4); }

  ctx.save(); ctx.beginPath(); ctx.rect(pad.l, pad.t, pw, ph); ctx.clip();
  for (const s of series) {
    ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 1.5; ctx.lineJoin = 'round'; ctx.setLineDash(s.dash ? [5, 3] : []);
    ctx.globalAlpha = s.alpha || 1;
    ctx.beginPath(); let pen = false;
    s.y.forEach((v, i) => {
      if (v == null || !isFinite(v)) { pen = false; return; }
      const px = xPx(i), py = yPx(v);
      if (pen) ctx.lineTo(px, py); else { ctx.moveTo(px, py); pen = true; }
    });
    ctx.stroke();
  }
  ctx.setLineDash([]); ctx.globalAlpha = 1;
  if (opts.cursorX != null) {
    const px = Math.round(xPx(Math.min(nx, Math.max(0, opts.cursorX - x0)))) + 0.5;
    ctx.strokeStyle = THEME.cursor; ctx.globalAlpha = 0.7; ctx.beginPath(); ctx.moveTo(px, pad.t); ctx.lineTo(px, pad.t + ph); ctx.stroke(); ctx.globalAlpha = 1;
  }
  ctx.restore();

  // end labels, de-overlapped vertically
  ctx.font = '10px system-ui, sans-serif'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  const labels = series.map((s) => {
    let k = s.y.length - 1;
    while (k > 0 && (s.y[k] == null || !isFinite(s.y[k]))) k--;
    return { s, y: Math.min(pad.t + ph, Math.max(pad.t, yPx(s.y[k]))), x: xPx(k) };
  }).sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) if (labels[i].y - labels[i - 1].y < 10) labels[i].y = labels[i - 1].y + 10;
  for (const l of labels) { if (l.y > h - 4) continue; ctx.fillStyle = l.s.color; ctx.fillText(l.s.label, pad.l + pw + 5, l.y); }
  return { xPx, pad, nx, x0, pw };
}

// Horizontal tyre-stint bars. rows:[{label, color, stints:[{label, compound, start_lap, end_lap}]}]
export function drawStrategy(canvas, { rows, laps, compoundColors, cursorLap }) {
  const { ctx, w, h } = fit(canvas);
  ctx.clearRect(0, 0, w, h);
  ctx.font = THEME.font;
  if (!rows || !rows.length) { ctx.fillStyle = THEME.axis; ctx.textAlign = 'center'; ctx.fillText('No strategy data.', w / 2, h / 2); return; }
  const pad = { l: 44, r: 10, t: 6, b: 22 };
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  const rh = Math.min(22, ph / rows.length), bar = Math.max(3, rh - 4);
  const xPx = (lap) => pad.l + (lap / Math.max(1, laps)) * pw;
  ctx.textBaseline = 'middle';
  rows.forEach((r, i) => {
    const y = pad.t + i * rh;
    ctx.fillStyle = r.color; ctx.fillRect(pad.l - 40, y + 2, 3, bar);
    ctx.fillStyle = THEME.text; ctx.textAlign = 'left'; ctx.font = '10px system-ui, sans-serif';
    if (rh >= 9) ctx.fillText(r.label, pad.l - 33, y + rh / 2);
    for (const st of r.stints || []) {
      const a = xPx(Math.max(0, st.start_lap)), b = xPx(Math.min(laps, st.end_lap));
      ctx.fillStyle = compoundColors[st.label] || '#8a96a3';
      ctx.fillRect(a + 1, y + 2, Math.max(1, b - a - 2), bar);
      if (b - a > 34 && bar >= 9) {
        ctx.fillStyle = '#0d1013'; ctx.font = '600 9px system-ui, sans-serif'; ctx.textAlign = 'center';
        ctx.fillText(`${st.compound || ''} ${Math.round(st.end_lap - st.start_lap)}L`.trim(), (a + b) / 2, y + 2 + bar / 2 + 0.5);
      }
    }
  });
  ctx.font = THEME.mono; ctx.fillStyle = THEME.axis; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  ctx.strokeStyle = THEME.grid; ctx.beginPath();
  for (const t of niceTicks(0, laps, Math.max(3, Math.floor(pw / 60)))) {
    if (!Number.isInteger(t)) continue;
    const px = Math.round(xPx(t)) + 0.5;
    ctx.moveTo(px, pad.t + rows.length * rh); ctx.lineTo(px, pad.t + rows.length * rh + 4);
    ctx.fillText(String(t), px, pad.t + rows.length * rh + 6);
  }
  ctx.stroke();
  if (cursorLap != null) {
    const px = Math.round(xPx(Math.min(laps, cursorLap))) + 0.5;
    ctx.strokeStyle = THEME.cursor; ctx.globalAlpha = 0.7; ctx.beginPath(); ctx.moveTo(px, pad.t); ctx.lineTo(px, pad.t + rows.length * rh); ctx.stroke(); ctx.globalAlpha = 1;
  }
}
