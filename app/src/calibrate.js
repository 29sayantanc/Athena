'use strict';
let targets = [];
let origin = { x: 0, y: 0 };
const msg = document.getElementById('msg');

function setMsg(title, text, cls = '') {
  msg.className = cls;
  msg.replaceChildren();
  const h = document.createElement('h1'); h.textContent = title;
  const p = document.createElement('p'); p.textContent = text;
  msg.append(h, p);
}

window.athena.onCalibration((d) => {
  targets = d.targets; origin = d.origin;
  const box = document.getElementById('targets');
  box.replaceChildren(...targets.map((t) => {
    const el = document.createElement('div');
    el.className = 't'; el.style.left = `${t.x}px`; el.style.top = `${t.y}px`; el.textContent = t.label;
    return el;
  }));
});

window.athena.onEvent(({ event, data }) => {
  if (event === 'calibration.status') setMsg('Calibrating', 'Keep the mouse still.');
  if (event !== 'calibration.result') return;
  if (!data.ok) { setMsg('Calibration failed', data.error, 'err'); return; }
  const r = data.result;
  setMsg(`Average error ${r.error_px} px`, 'The pointer now visits each target.');
  document.body.style.cursor = 'default';
  const svg = document.getElementById('lines');
  for (const m of data.mapped) {
    const x = m.x - origin.x, y = m.y - origin.y;
    const p = document.createElement('div');
    p.className = 'p'; p.style.left = `${x}px`; p.style.top = `${y}px`;
    document.body.append(p);
    const t = targets.find((q) => q.label === m.label);
    if (t && Math.hypot(t.x - x, t.y - y) > 4) {
      const ln = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      ln.setAttribute('x1', t.x); ln.setAttribute('y1', t.y); ln.setAttribute('x2', x); ln.setAttribute('y2', y);
      ln.setAttribute('stroke', '#A6B85A'); ln.setAttribute('stroke-width', '1.5'); ln.setAttribute('stroke-dasharray', '3 4');
      svg.append(ln);
    }
  }
});
