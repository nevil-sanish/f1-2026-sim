// Reusable UI pieces: run button with spinner/timer/progress, dock tabs, tables, key-value grids.
import { $, h, clear, toast } from './util.js';

// fn(progress) -> optional status string. progress(p 0..1, message) drives the bar.
export function runButton(label, fn, opts = {}) {
  const status = h('span', { class: 'run-status mono' });
  const bar = h('i'); const prog = h('div', { class: 'progress', hidden: true }, bar);
  const msg = h('div', { class: 'run-msg dim', hidden: true });
  const btn = h('button', { class: 'btn primary run', id: opts.id }, h('span', { class: 'spin', 'aria-hidden': 'true' }), h('span', null, label));
  btn.addEventListener('click', async () => {
    if (btn.disabled) return;
    btn.disabled = true; btn.classList.add('busy'); status.classList.remove('err'); status.title = '';
    const t0 = performance.now();
    const el = () => (performance.now() - t0) / 1000;
    const iv = setInterval(() => { status.textContent = `${el().toFixed(1)} s`; }, 100);
    const progress = (p, m) => { prog.hidden = false; bar.style.width = `${Math.round(Math.min(1, Math.max(0, p)) * 100)}%`; if (m) { msg.hidden = false; msg.textContent = m; } };
    try {
      const info = await fn(progress);
      status.textContent = info || `done in ${el().toFixed(2)} s`;
    } catch (e) {
      console.error(e);
      toast(e.message || String(e));
      status.textContent = 'failed'; status.classList.add('err'); status.title = e.message || '';
    } finally {
      clearInterval(iv); btn.disabled = false; btn.classList.remove('busy'); prog.hidden = true; msg.hidden = true; bar.style.width = '0';
    }
  });
  return h('div', { class: 'run-box' }, h('div', { class: 'run-row' }, btn, status), prog, msg);
}

// Dock (bottom area) tabs. tabs: [{id, label, el, onShow}]
let dockTabs = [], dockActive = null;
export function setDock(tabs, active, extra) {
  dockTabs = tabs;
  const bar = clear($('#dock-tabs')), body = $('#dock-body');
  for (const kid of [...body.children]) kid.remove();
  for (const t of tabs) {
    t.btn = h('button', { class: 'tab', role: 'tab', onclick: () => showDock(t.id) }, t.label);
    bar.append(t.btn);
    t.el.classList.add('dock-pane');
    body.append(t.el);
  }
  bar.append(h('span', { class: 'tb-fill' }), h('div', { id: 'dock-extra', class: 'dock-extra' }, extra || null));
  showDock(tabs.find((t) => t.id === active) ? active : tabs[0]?.id);
}
export function showDock(id) {
  dockActive = id;
  for (const t of dockTabs) {
    const onT = t.id === id;
    t.el.hidden = !onT; t.btn.classList.toggle('on', onT); t.btn.setAttribute('aria-selected', onT);
    if (onT && t.onShow) requestAnimationFrame(() => t.onShow());
  }
}
export const activeDock = () => dockActive;
export function setDockExtra(node) { const e = $('#dock-extra'); if (e) { clear(e); if (node) e.append(node); } }

// cols: [{label, cls, title, get:(row, i) => node|string}]
export function table(cols, rows, opts = {}) {
  return h('table', { class: 'data ' + (opts.cls || '') },
    h('thead', null, h('tr', null, cols.map((c) => h('th', { class: c.cls || '', title: c.title }, c.label)))),
    h('tbody', null, rows.length ? rows.map((r, i) => {
      const tr = h('tr', opts.rowProps ? opts.rowProps(r, i) : null, cols.map((c) => h('td', { class: c.cls || '' }, c.get(r, i))));
      return tr;
    }) : h('tr', null, h('td', { colspan: cols.length, class: 'empty' }, opts.empty || 'No rows.'))));
}

export function kv(pairs) {
  return h('dl', { class: 'kv' }, pairs.filter(Boolean).map(([k, v, title]) => h('div', { title }, h('dt', null, k), h('dd', null, v))));
}

export function swatch(color) { return h('i', { class: 'sw', style: { background: color } }); }

export function card(title, ...kids) {
  return h('section', { class: 'card' }, title ? h('h3', null, title) : null, ...kids);
}
