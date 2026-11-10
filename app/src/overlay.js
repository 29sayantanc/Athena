'use strict';
// Athena cursor overlay: the instrument ring, captions, highlights and click marks.
// Every stroke gets a dark under-stroke so it reads on light and dark apps without glow.

const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
const capEl = document.getElementById('caption');

const DEFAULT_COLORS = {
  idle: '#A7ABA3', listening: '#A6B85A', thinking: '#9C8CD9', speaking: '#6FA8DC', acting: '#C8894A',
  confirm: '#E0B54A', user: '#A6B85A', error: '#D7654F', highlight: '#E8BE45',
};
const STATE_COLOR = {
  idle: 'idle', starting: 'idle', paused: 'idle', listening: 'listening', transcribing: 'thinking', thinking: 'thinking',
  speaking: 'speaking', acting: 'acting', waiting: 'listening', confirm: 'confirm', user: 'user', error: 'error',
};
const FONT = '"Instrument Sans", "Segoe UI", system-ui, sans-serif';
const UNDER = 'rgba(14, 15, 17, .55)';
const TAG_BG = 'rgba(23, 25, 28, .94)';
const TAU = Math.PI * 2;
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

const S = {
  state: 'starting', stateAt: performance.now(),
  colors: { ...DEFAULT_COLORS }, ringSize: 34, showIdle: true, name: 'Athena',
  cursor: { x: -999, y: -999, inside: false }, pos: { x: -999, y: -999 }, lastMove: 0,
  mic: 0, tts: 0, color: [167, 171, 163],
  highlights: [], marks: [], trail: [], speakRipples: [], lastSpeakRipple: 0, caption: null, hoverId: null, hlSeq: 0,
  origin: { x: window.screenX || 0, y: window.screenY || 0 },
};

let dpr = 1;
function resize() {
  dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(innerWidth * dpr);
  canvas.height = Math.round(innerHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  S.origin = { x: window.screenX || 0, y: window.screenY || 0 };
}
addEventListener('resize', resize);
resize();

// ---------------------------------------------------------------- helpers
const hexRgb = (h) => {
  const m = /^#?([0-9a-f]{6})$/i.exec(h || '');
  if (!m) return [236, 234, 228];
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};
const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;
const lerp = (a, b, t) => a + (b - a) * t;
const ease = (t) => 1 - Math.pow(1 - Math.min(1, Math.max(0, t)), 3);
const stateColor = (st) => hexRgb(S.colors[STATE_COLOR[st] || 'idle']);

/** Stroke the current path twice: dark under-stroke, then colour. */
function stroke2(col, w, a = 1) {
  ctx.lineWidth = w + 2; ctx.strokeStyle = `rgba(14,15,17,${0.55 * a})`; ctx.stroke();
  ctx.lineWidth = w; ctx.strokeStyle = rgba(col, a); ctx.stroke();
}
function arc(x, y, r, a0, a1) { ctx.beginPath(); ctx.arc(x, y, Math.max(0.5, r), a0, a1); }
function line(x1, y1, x2, y2) { ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); }
function rrect(x, y, w, h, r) {
  r = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

/** A label tag: graphite plate, 2px colour edge, paper text. Returns its box. */
function tag(text, x, y, col, alpha = 1, opts = {}) {
  ctx.save();
  ctx.font = `500 12.5px ${FONT}`;
  const kw = opts.key ? ctx.measureText(opts.key).width + 12 : 0;
  const w = Math.ceil(ctx.measureText(text).width + 20 + (kw ? kw + 8 : 0)), h = 24;
  const px = Math.max(6, Math.min(innerWidth - w - (opts.reserve || 0) - 6, x));
  const py = Math.max(6, Math.min(innerHeight - h - 6, y));
  ctx.globalAlpha = alpha;
  ctx.shadowColor = 'rgba(0,0,0,.25)'; ctx.shadowBlur = 10; ctx.shadowOffsetY = 2;
  rrect(px, py, w, h, 5); ctx.fillStyle = TAG_BG; ctx.fill();
  ctx.shadowColor = 'transparent';
  ctx.fillStyle = rgba(col, 1); ctx.fillRect(px, py + 5, 2, h - 10);
  ctx.fillStyle = '#ECEAE4'; ctx.textBaseline = 'middle';
  ctx.fillText(text, px + 11, py + h / 2 + 0.5);
  if (opts.key) {
    const kx = px + w - kw - 8;
    rrect(kx, py + 5, kw, h - 10, 3); ctx.strokeStyle = 'rgba(236,234,228,.3)'; ctx.lineWidth = 1; ctx.stroke();
    ctx.fillStyle = '#A3A59E'; ctx.font = `500 11px ${FONT}`; ctx.fillText(opts.key, kx + 6, py + h / 2 + 0.5);
  }
  ctx.restore();
  return { x: px, y: py, w, h };
}

// ---------------------------------------------------------------- the ring (original animation set)
const HOT = { x: 4, y: 7 };  // centre the halo slightly into the arrow body

function drawRing(now) {
  if (!S.cursor.inside) return;
  const st = S.state;
  if (st === 'idle' && !S.showIdle) return;
  const t = (now - S.stateAt) / 1000;
  const col = S.color;
  const cx = S.pos.x + HOT.x, cy = S.pos.y + HOT.y;
  const r0 = S.ringSize / 2;
  ctx.save();
  ctx.lineCap = 'round';
  const glow = (blur) => { ctx.shadowColor = rgba(col, 0.9); ctx.shadowBlur = blur; };
  const ring = (r, w, a) => { ctx.beginPath(); ctx.arc(cx, cy, Math.max(1, r), 0, TAU); ctx.lineWidth = w; ctx.strokeStyle = rgba(col, a); ctx.stroke(); };

  switch (st) {
    case 'idle': case 'starting': {
      ring(r0 * 0.78, 1.6, 0.55);
      ctx.beginPath(); ctx.arc(cx, cy, 1.8, 0, TAU); ctx.fillStyle = rgba(col, 0.6); ctx.fill();
      break;
    }
    case 'paused': {
      ctx.setLineDash([3, 5]); ring(r0 * 0.7, 1.4, 0.45); ctx.setLineDash([]);
      break;
    }
    case 'listening': case 'waiting': {
      const lvl = st === 'listening' ? S.mic : 0;
      const pulse = (t % 1.4) / 1.4;
      ring(r0 + pulse * 18, 1.5, 0.5 * (1 - pulse));
      glow(14 + lvl * 26);
      ring(r0 + lvl * 12, 2.6 + lvl * 2, 0.95);
      ctx.shadowBlur = 0;
      if (st === 'listening') {
        for (let i = 0; i < 5; i++) {   // level bars inside the ring
          const ph = Math.sin(t * 9 + i * 1.3) * 0.5 + 0.5;
          const h = 3 + (lvl * 14 + 2) * (0.4 + ph * 0.6) * (i === 2 ? 1 : i % 2 ? 0.8 : 0.55);
          ctx.fillStyle = rgba(col, 0.9);
          rrect(cx - 9 + i * 4, cy - h / 2, 2.2, h, 1.1); ctx.fill();
        }
      } else {
        ctx.setLineDash([4, 6]); ctx.lineDashOffset = -t * 16; ring(r0 + 7, 1.4, 0.7); ctx.setLineDash([]);
        tag('Your turn', cx + r0 + 12, cy + r0 - 2, col, 0.96);
      }
      break;
    }
    case 'transcribing': case 'thinking': {
      ring(r0, 1.2, 0.22);
      glow(16);
      for (let k = 0; k < 2; k++) {   // two counter-rotating comets
        const a = (k ? -1 : 1) * t * (k ? 3.1 : 4.4) + k * Math.PI;
        const r = k ? r0 + 5 : r0;
        const segs = 18;
        for (let i = 0; i < segs; i++) {
          const a0 = a - i * 0.09;
          ctx.beginPath(); ctx.arc(cx, cy, r, a0 - 0.1, a0);
          ctx.lineWidth = (k ? 2 : 3) * (1 - i / segs) + 0.4;
          ctx.strokeStyle = rgba(col, (1 - i / segs) * (k ? 0.7 : 1));
          ctx.stroke();
        }
      }
      ctx.shadowBlur = 0;
      break;
    }
    case 'speaking': {
      if (S.tts > 0.18 && now - S.lastSpeakRipple > 170) { S.speakRipples.push(now); S.lastSpeakRipple = now; }
      S.speakRipples = S.speakRipples.filter((b0) => now - b0 < 900);
      for (const b0 of S.speakRipples) { const p = (now - b0) / 900; ring(r0 + p * 22, 1.6 * (1 - p) + 0.3, 0.7 * (1 - p)); }
      glow(14 + S.tts * 22);
      ring(r0 + S.tts * 6, 2.6, 0.95);
      ctx.shadowBlur = 0;
      break;
    }
    case 'acting': case 'confirm': {
      for (const p of S.trail) {   // fading trail of where Athena moved the pointer
        const age = (now - p.t) / 450;
        if (age > 1) continue;
        ctx.beginPath(); ctx.arc(p.x + HOT.x, p.y + HOT.y, 3.2 * (1 - age) + 0.5, 0, TAU);
        ctx.fillStyle = rgba(col, 0.45 * (1 - age)); ctx.fill();
      }
      const breathe = st === 'confirm' ? (Math.sin(t * 5) * 0.5 + 0.5) : 0;
      glow(18 + breathe * 14);
      ring(r0 + breathe * 4, 3.2, 1);
      ctx.shadowBlur = 0;
      ctx.setLineDash([6, 7]); ctx.lineDashOffset = -t * 30; ring(r0 + 7, 1.6, 0.75); ctx.setLineDash([]);
      if (st === 'acting') tag(`${S.name} has control`, cx + r0 + 12, cy + r0 - 2, col, 0.97, { key: 'Esc' });
      else tag('Waiting for your approval', cx + r0 + 12, cy + r0 - 2, col, 0.97);
      break;
    }
    case 'user': {
      const p = ease(t / 0.7);
      ring(r0 + p * 26, 3 * (1 - p) + 0.5, 1 - p);
      ring(r0 * 0.8, 1.6, 0.6);
      break;
    }
    case 'error': {
      const shake = Math.sin(t * 40) * Math.max(0, 1 - t) * 3;
      ctx.translate(shake, 0);
      glow(10); ring(r0 * 0.85, 2.4, 0.95); ctx.shadowBlur = 0;
      ctx.beginPath(); ctx.moveTo(cx - 4, cy - 4); ctx.lineTo(cx + 4, cy + 4); ctx.moveTo(cx + 4, cy - 4); ctx.lineTo(cx - 4, cy + 4);
      ctx.lineWidth = 2; ctx.strokeStyle = rgba(col, 0.95); ctx.stroke();
      break;
    }
  }
  ctx.restore();
}

// ---------------------------------------------------------------- highlights
function geom(h) {
  if (h.rect) {
    const l = h.rect[0] - S.origin.x, t = h.rect[1] - S.origin.y, r = h.rect[2] - S.origin.x, b = h.rect[3] - S.origin.y;
    return { l, t, r, b, cx: (l + r) / 2, cy: (t + b) / 2, w: r - l, h: b - t };
  }
  const x = h.point[0] - S.origin.x, y = h.point[1] - S.origin.y;
  return { l: x - 18, t: y - 18, r: x + 18, b: y + 18, cx: x, cy: y, w: 36, h: 36, pointOnly: true };
}

function brackets(g, pad, col, a) {
  const l = g.l - pad, t = g.t - pad, r = g.r + pad, b = g.b + pad;
  const k = Math.min(14, (r - l) / 3, (b - t) / 3);
  ctx.beginPath();
  ctx.moveTo(l, t + k); ctx.lineTo(l, t); ctx.lineTo(l + k, t);
  ctx.moveTo(r - k, t); ctx.lineTo(r, t); ctx.lineTo(r, t + k);
  ctx.moveTo(r, b - k); ctx.lineTo(r, b); ctx.lineTo(r - k, b);
  ctx.moveTo(l + k, b); ctx.lineTo(l, b); ctx.lineTo(l, b - k);
  stroke2(col, 2, a);
}

function drawHighlights(now) {
  const col = hexRgb(S.colors.highlight);
  const spots = S.highlights.filter((h) => h.shape === 'spotlight');
  if (spots.length) {
    const a = Math.min(...spots.map((h) => h.alpha(now)));
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, innerWidth, innerHeight);
    for (const h of spots) { const g = geom(h); rrect(g.l - 8, g.t - 8, g.w + 16, g.h + 16, 6); }
    ctx.fillStyle = `rgba(10,11,13,${0.5 * a})`; ctx.fill('evenodd');
    ctx.restore();
  }
  for (const h of S.highlights) {
    const g = geom(h);
    if (g.r < -60 || g.b < -60 || g.l > innerWidth + 60 || g.t > innerHeight + 60) continue;
    const a = h.alpha(now);
    const age = (now - h.born) / 1000;
    const pop = reduced ? 1 : ease(age / 0.28);
    const hot = S.hoverId === h.id;
    ctx.save();
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    switch (h.shape) {
      case 'box': case 'spotlight': {
        rrect(g.l - 4, g.t - 4, g.w + 8, g.h + 8, 4);
        ctx.fillStyle = rgba(col, hot ? 0.12 : 0.06 * a); ctx.fill();
        ctx.setLineDash([6, 6]); ctx.lineDashOffset = reduced ? 0 : -age * 24;
        ctx.lineWidth = 1.25; ctx.strokeStyle = rgba(col, 0.55 * a); ctx.stroke(); ctx.setLineDash([]);
        brackets(g, 4 + (1 - pop) * 10, col, a);
        break;
      }
      case 'circle': {
        const base = g.pointOnly ? 20 : Math.max(g.w, g.h) / 2 + 8;
        arc(g.cx, g.cy, base + (1 - pop) * 14, 0, TAU); stroke2(col, 2, a);
        if (!reduced) {
          const p = (age % 1.8) / 1.8;
          arc(g.cx, g.cy, base + p * 14, 0, TAU);
          ctx.lineWidth = 1.25; ctx.strokeStyle = rgba(col, 0.45 * (1 - p) * a); ctx.stroke();
        }
        break;
      }
      case 'underline': {
        line(g.l, g.b + 5, g.l + g.w * pop, g.b + 5); stroke2(col, 2.5, a);
        break;
      }
      case 'arrow': default: {
        const dx = g.cx > innerWidth * 0.6 ? -1 : 1;
        const dy = g.cy > innerHeight * 0.4 ? -1 : 1;
        const ang = Math.atan2(dy, dx);
        const pad = g.pointOnly ? 8 : Math.hypot(g.w, g.h) / 2 * 0.72 + 6;
        const bob = reduced ? 0 : Math.sin(age * 4) * 3;
        const tx = g.cx + Math.cos(ang) * (pad + bob), ty = g.cy + Math.sin(ang) * (pad + bob);
        const len = 64 * pop;
        const sx = tx + Math.cos(ang) * len, sy = ty + Math.sin(ang) * len;
        line(sx, sy, tx, ty); stroke2(col, 2, a);
        const ha = ang + Math.PI;
        ctx.beginPath();
        ctx.moveTo(tx + Math.cos(ha - 0.5) * -10, ty + Math.sin(ha - 0.5) * -10);
        ctx.lineTo(tx, ty);
        ctx.lineTo(tx + Math.cos(ha + 0.5) * -10, ty + Math.sin(ha + 0.5) * -10);
        stroke2(col, 2, a);
        if (!g.pointOnly) brackets(g, 4, col, a * 0.8);
        h._labelAt = { x: sx + (dx < 0 ? -6 : 6), y: sy + (dy < 0 ? -30 : 6), right: dx < 0 };
        break;
      }
    }
    ctx.restore();

    // label tag + attached close button
    const label = h.label || '';
    if (label) {
      ctx.save(); ctx.font = `500 12.5px ${FONT}`; const w = ctx.measureText(label).width + 20; ctx.restore();
      let lx = g.l - 4, ly = g.t - 32;
      if (h.shape === 'arrow' && h._labelAt) { lx = h._labelAt.right ? h._labelAt.x - w - 24 : h._labelAt.x; ly = h._labelAt.y; }
      if (h.shape === 'underline') ly = g.b + 14;
      if (ly < 6) ly = g.b + 10;
      const box = tag(label, lx, ly, col, a, { reserve: 24 });
      h._closeAt = { x: box.x + box.w, y: box.y, a, solo: false };
      h._hit = [[g.l - 14, g.t - 14, g.r + 14, g.b + 14], [box.x - 4, box.y - 4, box.x + box.w + 28, box.y + box.h + 4]];
    } else {
      h._closeAt = { x: g.r + 8, y: g.t - 30, a, solo: true };
      h._hit = [[g.l - 14, g.t - 34, g.r + 38, g.b + 14]];
    }
  }
  syncCloseButtons();
}

function drawMarks(now) {
  S.marks = S.marks.filter((m) => now - m.t < 650);
  const col = stateColor('acting');
  for (const m of S.marks) {
    const p = ease((now - m.t) / 650);
    ctx.beginPath(); ctx.arc(m.x, m.y, 6 + p * 26, 0, TAU);
    ctx.lineWidth = 3 * (1 - p) + 0.5; ctx.strokeStyle = rgba(col, 1 - p); ctx.stroke();
    ctx.beginPath(); ctx.arc(m.x, m.y, 4 * (1 - p), 0, TAU);
    ctx.fillStyle = rgba(col, 1 - p); ctx.fill();
  }
}

// ---------------------------------------------------------------- close buttons (hover from real cursor position)
const closeLayer = document.createElement('div');
closeLayer.id = 'hlclose';
document.body.append(closeLayer);
const X_SVG = '<svg viewBox="0 0 10 10"><path d="M2 2l6 6M8 2l-6 6"/></svg>';
let interactive = false;
function setInteractive(on) {
  if (on === interactive) return;
  interactive = on;
  try { window.athena.overlayInteractive(on); } catch {}
}
const inRect = (x, y, r) => x >= r[0] && x <= r[2] && y >= r[1] && y <= r[3];
function closeRect(h) {
  const x = Math.max(4, Math.min(innerWidth - 28, h._closeAt.x));
  const y = Math.max(4, Math.min(innerHeight - 28, h._closeAt.y));
  return [x, y, x + 24, y + 24];
}
function updateHover() {
  const c = S.cursor;
  let hovered = null, overBtn = false;
  if (c.inside) {
    for (let i = S.highlights.length - 1; i >= 0; i--) {
      const h = S.highlights[i];
      if (!h._closeAt || (h.expires && h.expires - performance.now() < 260)) continue;
      const b = closeRect(h);
      if (inRect(c.x, c.y, [b[0] - 5, b[1] - 5, b[2] + 5, b[3] + 5])) { hovered = h; overBtn = true; break; }
      if (!hovered && (h._hit || []).some((r) => inRect(c.x, c.y, r))) hovered = h;
    }
  }
  S.hoverId = hovered ? hovered.id : null;
  setInteractive(overBtn);
}
function syncCloseButtons() {
  updateHover();
  const live = new Set();
  for (const h of S.highlights) {
    if (!h._closeAt || (h.expires && h.expires - performance.now() < 260)) continue;
    live.add(String(h.id));
    let b = closeLayer.querySelector(`[data-id="${h.id}"]`);
    if (!b) {
      b = document.createElement('button');
      b.className = 'hlx'; b.dataset.id = h.id; b.innerHTML = X_SVG;
      b.setAttribute('aria-label', 'Dismiss highlight');
      b.addEventListener('click', (e) => {
        e.stopPropagation();
        const now = performance.now();
        if (e.shiftKey) S.highlights.forEach((x) => { x.expires = now + 200; });
        else h.expires = now + 200;
        setInteractive(false);
      });
      closeLayer.append(b);
    }
    const [x, y] = closeRect(h);
    const hot = S.hoverId === h.id;
    b.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
    // Unlabelled highlights only show their dismiss button on hover.
    b.style.opacity = String(h._closeAt.a * (hot ? 1 : h._closeAt.solo ? 0 : 0.85));
    b.classList.toggle('hot', hot && interactive);
    b.classList.toggle('solo', !!h._closeAt.solo);
  }
  for (const b of [...closeLayer.children]) if (!live.has(b.dataset.id)) b.remove();
  if (!closeLayer.children.length) setInteractive(false);
}

// ---------------------------------------------------------------- caption
function showCaption(text, kind) {
  if (!text) return;
  const words = text.split(/\s+/).length;
  const ttl = kind === 'status' ? Math.max(1600, words * 300) : Math.max(2800, Math.min(14000, words * 360 + 1400));
  S.caption = { kind, until: performance.now() + ttl };
  capEl.className = kind;
  capEl.textContent = text.length > 400 ? text.slice(0, 397) + '…' : text;
  requestAnimationFrame(() => capEl.classList.add('show'));
}

function placeCaption(now) {
  if (!S.caption) return;
  if (now > S.caption.until || !S.cursor.inside) {
    capEl.classList.remove('show');
    if (now > S.caption.until + 300) S.caption = null;
    if (!S.cursor.inside) return;
  }
  const w = capEl.offsetWidth, h = capEl.offsetHeight;
  const gap = S.ringSize / 2 + 18;
  const below = S.pos.y + gap + 8 + (['acting', 'confirm', 'waiting'].includes(S.state) ? 30 : 0);
  const above = S.pos.y - h - gap + 6;
  const right = S.pos.x + gap - 4, left = S.pos.x - w - gap;
  // Stay clear of every highlight and its label tag.
  const boxes = S.highlights.flatMap((hl) => {
    const g = geom(hl);
    return [[g.l - 30, g.t - 30, g.r + 30, g.b + 30], ...(hl._hit || []).map((r) => [r[0] - 10, r[1] - 10, r[2] + 10, r[3] + 10])];
  });
  const fits = (x, y) => x >= 6 && y >= 6 && x + w <= innerWidth - 6 && y + h <= innerHeight - 6;
  const clear = (x, y) => !boxes.some(([l, t, r, b]) => x < r && x + w > l && y < b && y + h > t);
  const spots = [[right, below], [right, above], [left, below], [left, above]];
  const [x, y] = spots.find(([sx, sy]) => fits(sx, sy) && clear(sx, sy)) || spots.find(([sx, sy]) => fits(sx, sy)) || spots[0];
  capEl.style.left = `${Math.round(Math.max(6, Math.min(innerWidth - w - 6, x)))}px`;
  capEl.style.top = `${Math.round(Math.max(6, Math.min(innerHeight - h - 6, y)))}px`;
}

// ---------------------------------------------------------------- loop
let lastFrame = 0;
function frame(now) {
  requestAnimationFrame(frame);
  const k = S.state === 'acting' ? 0.55 : 0.62;
  S.pos.x = S.pos.x < -900 ? S.cursor.x : lerp(S.pos.x, S.cursor.x, k);
  S.pos.y = S.pos.y < -900 ? S.cursor.y : lerp(S.pos.y, S.cursor.y, k);
  const target = stateColor(S.state);
  S.color = S.color.map((c, i) => lerp(c, target[i], 0.14));
  S.mic *= 0.9; S.tts *= 0.9;
  S.highlights = S.highlights.filter((h) => !h.expires || now < h.expires);

  const animating = (S.state !== 'idle' && S.state !== 'paused') || S.highlights.length || S.marks.length ||
    S.caption || now - S.lastMove < 400 || now - S.stateAt < 1000;
  if (!animating && now - lastFrame < 250) return;
  lastFrame = now;
  ctx.clearRect(0, 0, innerWidth, innerHeight);
  drawHighlights(now);
  drawMarks(now);
  drawRing(now);
  placeCaption(now);
}
document.fonts.load(`500 12.5px "Instrument Sans"`).finally(() => requestAnimationFrame(frame));

// ---------------------------------------------------------------- wiring
window.athena.onCursor((c) => {
  S.cursor = c;
  S.origin = { x: c.ox, y: c.oy };
  S.lastMove = performance.now();
  if (S.state === 'acting') {
    S.trail.push({ x: c.x, y: c.y, t: performance.now() });
    if (S.trail.length > 40) S.trail.shift();
  }
});

function addHighlights(items, duration, keep) {
  const now = performance.now();
  if (!keep) for (const h of S.highlights) if (!h.expires || h.expires > now + 220) h.expires = now + 220;
  for (const it of items) {
    S.hlSeq += 1;
    S.highlights.push({
      ...it, id: S.hlSeq, born: now, expires: duration > 0 ? now + duration * 1000 : 0,
      alpha(t) {
        const fin = Math.min(1, (t - this.born) / 180);
        return this.expires ? Math.max(0, Math.min(fin, (this.expires - t) / 220)) : fin;
      },
    });
  }
}

window.athena.onEvent(({ event, data }) => {
  switch (event) {
    case 'state':
      if (data.state !== S.state) { S.state = data.state; S.stateAt = performance.now(); }
      if (data.state !== 'acting') S.trail = [];
      break;
    case 'level':
      if (data.mic != null) S.mic = Math.max(S.mic, data.mic);
      if (data.tts != null) S.tts = Math.max(S.tts, data.tts);
      break;
    case 'caption': showCaption(data.text, data.kind); break;
    case 'highlight.set': addHighlights(data.items || [], data.duration || 0, !!data.keep); break;
    case 'highlight.clear': addHighlights([], 0, false); break;
    case 'highlight.settle': {
      const now = performance.now(), ms = (data.seconds || 45) * 1000;
      for (const h of S.highlights) if (!h.expires || h.expires > now + ms) h.expires = now + ms;
      break;
    }
    case 'feedback': S.marks.push({ x: data.x - S.origin.x, y: data.y - S.origin.y, t: performance.now() }); break;
    case 'settings.changed': {
      const a = data.appearance || {};
      S.colors = { ...DEFAULT_COLORS, ...(a.colors || {}) };
      S.ringSize = a.ring_size || 34;
      S.showIdle = a.show_idle_ring !== false;
      S.name = (data.general && data.general.assistant_name) || 'Athena';
      break;
    }
  }
});
