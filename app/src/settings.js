'use strict';
// Athena settings. Fields are declared as data; a few custom rows handle models, voice, calibration and memory.

const A = window.athena;
const $ = (s, el = document) => el.querySelector(s);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
};

let S = null;
let presets = {};
let info = {};
let state = { state: 'starting' };
let voiceStatus = 'unknown';
let section = new URLSearchParams(location.search).get('section') || 'conversation';

const SECTIONS = [['conversation', 'Conversation'], ['general', 'General'], ['model', 'Model'], ['voice', 'Voice'], ['control', 'Control'],
  ['appearance', 'Appearance'], ['memory', 'Memory'], ['about', 'About']];
const DEFAULT_COLORS = { idle: '#A7ABA3', listening: '#A6B85A', thinking: '#9C8CD9', speaking: '#6FA8DC',
  acting: '#C8894A', confirm: '#E0B54A', user: '#A6B85A', error: '#D7654F', highlight: '#E8BE45' };

// ------------------------------------------------------------------ persistence
const get = (path) => path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), S);
function patchFor(path, value) {
  const out = {}; let node = out; const parts = path.split('.');
  parts.slice(0, -1).forEach((p) => { node = node[p] = {}; });
  node[parts[parts.length - 1]] = value;
  return out;
}
const timers = {};
function save(path, value, delay = 250) {
  const parts = path.split('.'); let node = S;
  parts.slice(0, -1).forEach((p) => { node = node[p] = node[p] || {}; });
  node[parts[parts.length - 1]] = value;
  clearTimeout(timers[path]);
  timers[path] = setTimeout(async () => {
    try { S = await A.rpc('settings.set', { patch: patchFor(path, value) }); toast('Saved'); renderStatus(); }
    catch (e) { toast(e.message, true); }
  }, delay);
}
let toastT;
function toast(msg, err) {
  const t = $('#toast'); t.textContent = msg; t.className = 'show' + (err ? ' err' : '');
  clearTimeout(toastT); toastT = setTimeout(() => { t.className = ''; }, err ? 4000 : 1100);
}

// ------------------------------------------------------------------ building blocks
function row(label, help, ctl, stack) {
  return h('div', { class: 'row' + (stack ? ' stack' : '') },
    h('div', { class: 'lbl' }, label ? h('div', { class: 't' }, label) : null, help ? h('div', { class: 'h' }, help) : null),
    h('div', { class: 'ctl' }, ctl));
}
function group(title, ...rows) { return h('section', {}, title ? h('h2', {}, title) : null, ...rows.flat().filter(Boolean)); }
function sw(checked, onchange) {
  const inp = h('input', { type: 'checkbox', role: 'switch' }); inp.checked = !!checked; inp.onchange = () => onchange(inp.checked);
  return h('label', { class: 'switch' }, inp, h('i'));
}
function seg(options, value, onpick) {
  return h('div', { class: 'seg', role: 'group' }, ...options.map(([v, l]) =>
    h('button', { 'aria-pressed': String(v === value), onclick: () => onpick(v) }, l)));
}

function field(f) {
  const v = get(f.path);
  let ctl;
  switch (f.type) {
    case 'toggle': ctl = sw(v, (on) => { save(f.path, on, 0); if (f.rerender) render(); }); break;
    case 'number': {
      const inp = h('input', { type: 'number', min: f.min, max: f.max, step: f.step || 1, value: v ?? '', 'aria-label': f.label });
      inp.oninput = () => { const n = Number(inp.value); if (inp.value !== '' && !Number.isNaN(n)) save(f.path, n, 500); };
      ctl = h('span', { class: 'num' + (f.unit ? ' has-unit' : '') }, inp, f.unit ? h('span', { class: 'unit' }, f.unit) : null);
      break;
    }
    case 'range': {
      const out = h('span', { class: 'val' }, String(v));
      const inp = h('input', { type: 'range', min: f.min, max: f.max, step: f.step || 1, value: v, 'aria-label': f.label });
      inp.oninput = () => { out.textContent = inp.value; save(f.path, Number(inp.value), 300); };
      ctl = [inp, out];
      break;
    }
    case 'optnumber': {
      // null = the server's own default; the value isn't sent.
      const isDef = v === null || v === undefined;
      const out = h('span', { class: 'val' }, isDef ? '' : String(v));
      const inp = f.range
        ? h('input', { type: 'range', min: f.min, max: f.max, step: f.step || 1, value: isDef ? f.fallback : v, 'aria-label': f.label })
        : h('input', { type: 'number', min: f.min, max: f.max, step: f.step || 1, value: isDef ? '' : v, placeholder: String(f.fallback), 'aria-label': f.label });
      const def = h('input', { type: 'checkbox' }); def.checked = isDef;
      const sync = () => { inp.disabled = def.checked; out.textContent = def.checked ? '' : inp.value; };
      def.onchange = () => { sync(); save(f.path, def.checked ? null : Number(inp.value || f.fallback), 0); };
      inp.oninput = () => { out.textContent = inp.value; const n = Number(inp.value); if (inp.value !== '' && !Number.isNaN(n)) save(f.path, n, 400); };
      sync();
      ctl = [inp, f.range ? out : null, h('label', { class: 'inline' }, h('span', { class: 'switch' }, def, h('i')), 'Server default')];
      break;
    }
    case 'select': {
      const sel = h('select', { 'aria-label': f.label }, ...f.options.map(([val, lab]) => h('option', { value: val }, lab)));
      sel.value = v ?? '';
      sel.onchange = () => { save(f.path, f.number ? Number(sel.value) : sel.value, 0); if (f.rerender) render(); };
      ctl = sel;
      break;
    }
    case 'seg':
      // The selected option's description sits under the label.
      return row(f.label, f.notes ? f.notes[v] : f.help, seg(f.options, v, (nv) => { save(f.path, nv, 0); render(); }));
    case 'textarea': {
      const ta = h('textarea', { placeholder: f.placeholder || '', 'aria-label': f.label }); ta.value = v || '';
      ta.oninput = () => save(f.path, ta.value, 600);
      return row(f.label, f.help, ta, true);
    }
    case 'list': {
      const ta = h('textarea', { 'aria-label': f.label }); ta.value = (v || []).join(', ');
      ta.oninput = () => save(f.path, ta.value.split(',').map((s) => s.trim()).filter(Boolean), 700);
      return row(f.label, f.help, ta, true);
    }
    case 'hotkey': ctl = hotkeyButton(f.path); break;
    default: {
      const inp = h('input', { type: f.type === 'password' ? 'password' : 'text', value: v ?? '', placeholder: f.placeholder || '', spellcheck: 'false', 'aria-label': f.label });
      inp.oninput = () => save(f.path, inp.value, 600);
      ctl = inp;
    }
  }
  return row(f.label, f.help, ctl);
}

// ------------------------------------------------------------------ hotkey recorder
const CODE_MAP = { ControlLeft: 'ctrl', ControlRight: 'ctrl', ShiftLeft: 'shift', ShiftRight: 'shift', AltLeft: 'alt', AltRight: 'alt',
  MetaLeft: 'win', MetaRight: 'win', Space: 'space', Enter: 'enter', Escape: 'esc', Backspace: 'backspace', Tab: 'tab',
  ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', Home: 'home', End: 'end', PageUp: 'pageup',
  PageDown: 'pagedown', Insert: 'insert', Delete: 'delete', CapsLock: 'capslock', Backquote: '`', Minus: '-', Equal: '=',
  BracketLeft: '[', BracketRight: ']', Backslash: '\\', Semicolon: ';', Quote: "'", Comma: ',', Period: '.', Slash: '/', Pause: 'pause',
  ScrollLock: 'scrolllock' };
const ORDER = ['ctrl', 'shift', 'alt', 'win'];
const NICE = { ctrl: 'Ctrl', shift: 'Shift', alt: 'Alt', win: 'Win', space: 'Space', esc: 'Esc' };
const pretty = (combo) => (combo || '').split('+').map((k) => NICE[k] || k.toUpperCase()).join(' + ');
function codeName(code) {
  if (CODE_MAP[code]) return CODE_MAP[code];
  let m = /^Key([A-Z])$/.exec(code); if (m) return m[1].toLowerCase();
  m = /^Digit(\d)$/.exec(code); if (m) return m[1];
  m = /^(F\d{1,2})$/.exec(code); if (m) return m[1].toLowerCase();
  return null;
}
function hotkeyButton(path) {
  const btn = h('button', { class: 'btn hotkey' }, pretty(get(path)));
  let held = new Set(), best = [];
  const finish = () => {
    removeEventListener('keydown', down, true); removeEventListener('keyup', up, true);
    btn.classList.remove('rec');
    if (best.length) {
      const combo = [...ORDER.filter((k) => best.includes(k)), ...best.filter((k) => !ORDER.includes(k))].join('+');
      btn.textContent = pretty(combo); save(path, combo, 0); renderNavFooter();
    } else btn.textContent = pretty(get(path));
  };
  const down = (e) => {
    e.preventDefault(); e.stopPropagation();
    const k = codeName(e.code); if (!k) return;
    held.add(k); if (held.size >= best.length) best = [...held];
    btn.textContent = pretty([...held].join('+'));
  };
  const up = (e) => { e.preventDefault(); held.delete(codeName(e.code)); if (held.size === 0) finish(); };
  btn.onclick = () => {
    held = new Set(); best = [];
    btn.classList.add('rec'); btn.textContent = 'Press keys';
    addEventListener('keydown', down, true); addEventListener('keyup', up, true);
  };
  return btn;
}

// ------------------------------------------------------------------ pages
const PAGES = {};

// The whole conversation, newest at the bottom, with its own message box.
const convo = { log: null, list: null, typing: null, mic: null, input: null, send: null };
PAGES.conversation = () => {
  const CV = window.ChatView;
  const list = h('div', { class: 'thread' });
  const typing = h('div', { class: 'working', hidden: true }, h('span', { class: 'sweep' }), h('span', {}, 'Thinking'));
  const input = h('textarea', { rows: 1, placeholder: 'Message Athena', 'aria-label': 'Message Athena' });
  const ICON_SEND = '<svg viewBox="0 0 16 16"><path d="M8 13V3.5M3.75 7.5 8 3.25l4.25 4.25"/></svg>';
  const mic = h('button', { class: 'icon' });
  const send = h('button', { class: 'icon send', title: 'Send', 'aria-label': 'Send', html: ICON_SEND });
  const grow = () => { input.style.height = 'auto'; input.style.height = Math.min(140, input.scrollHeight) + 'px'; send.classList.toggle('ready', !!input.value.trim()); };
  const go = () => { const t = input.value.trim(); if (!t) return; input.value = ''; grow(); A.rpc('agent.ask', { text: t }).catch((e) => toast(e.message, true)); };
  input.addEventListener('input', grow);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); } });
  send.onclick = go;
  const composer = h('div', { class: 'composer' }, input, mic, send);
  const micCtl = window.ChatView.micControl(mic, composer, input);
  mic.onclick = () => A.rpc('chat.mic', { action: micCtl.action() }).catch((e) => toast(e.message, true));
  Object.assign(convo, { list, typing, mic: micCtl, input, send });

  const fill = (log) => {
    convo.log = log;
    CV.clearConfirms();
    list.replaceChildren(...(log.length ? log.map((e) => CV.entryNode(e, { mode: 'log' })).filter(Boolean)
      : [h('div', { class: 'convo-empty' }, `Nothing yet. Press ${pretty(get('general.hotkey'))} and ask Athena something.`)]));
    requestAnimationFrame(() => { $('#content').scrollTop = $('#content').scrollHeight; });
  };
  if (convo.log) fill(convo.log); else A.rpc('chat.history').then(fill).catch(() => fill([]));
  syncConvoState();

  const head = h('div', { class: 'pagehead' }, h('h1', {}, 'Conversation'), h('div', { class: 'actions' },
    h('button', { class: 'btn', onclick: () => A.rpc('agent.reset').catch(() => {}) }, 'New conversation'),
    h('button', { class: 'btn quiet danger', onclick: async () => { if (confirm('Delete the whole conversation history?')) await A.rpc('chat.clear'); } }, 'Clear history')));
  return [h('div', { class: 'convo' }, head, list, typing,
    h('div', { class: 'convo-compose' }, composer))];
};

function convoAppend(entry) {
  if (!convo.log) return;
  convo.log.push(entry);
  if (section !== 'conversation' || !convo.list) return;
  const stick = $('#content').scrollHeight - $('#content').scrollTop - $('#content').clientHeight < 120;
  convo.list.querySelector('.convo-empty')?.remove();
  const node = window.ChatView.entryNode(entry, { mode: 'log' });
  if (node) convo.list.append(node);
  if (stick || entry.role === 'user') $('#content').scrollTop = $('#content').scrollHeight;
}

function syncConvoState() {
  if (!convo.typing) return;
  const s = state.state;
  const busy = ['transcribing', 'thinking', 'acting'].includes(s);
  convo.typing.hidden = !busy;
  convo.typing.classList.toggle('acting', s === 'acting');
  convo.typing.lastChild.textContent = s === 'acting' ? 'Working' : s === 'transcribing' ? 'Transcribing' : 'Thinking';
  convo.mic.set(s);
  if (busy && section === 'conversation') $('#content').scrollTop = $('#content').scrollHeight;
}

PAGES.general = () => [
  h('h1', {}, 'General'),
  group('Activation',
    field({ path: 'general.hotkey', type: 'hotkey', label: 'Hotkey' }),
    field({ path: 'general.hotkey_mode', type: 'seg', label: 'Mode',
      options: [['hybrid', 'Hybrid'], ['hold', 'Hold'], ['toggle', 'Toggle']],
      notes: { hybrid: 'Hold to talk, or tap to start and tap to stop.', hold: 'Listens while held.', toggle: 'Tap to start, tap to stop.' } }),
    get('general.hotkey_mode') === 'hybrid'
      ? field({ path: 'general.hybrid_hold_ms', type: 'number', label: 'Hold threshold', min: 150, max: 1500, unit: 'ms' }) : null,
    field({ path: 'general.auto_stop_on_silence', type: 'toggle', label: 'Stop when I pause', help: 'Tap and toggle modes', rerender: true }),
    get('general.auto_stop_on_silence')
      ? field({ path: 'general.silence_stop_ms', type: 'number', label: 'Pause length', min: 600, max: 6000, step: 100, unit: 'ms' }) : null,
    field({ path: 'general.max_listen_s', type: 'number', label: 'Listen for at most', min: 10, max: 600, unit: 's' }),
  ),
  group('Conversation',
    field({ path: 'general.followup_window_s', type: 'number', label: 'Follow-up window', help: 'Replies within this time reuse the last screenshot', min: 0, max: 3600, unit: 's' }),
    field({ path: 'general.conversation_idle_reset_min', type: 'number', label: 'Start fresh after', help: '0 keeps the conversation going', min: 0, max: 600, unit: 'min' }),
    field({ path: 'general.assistant_name', type: 'text', label: 'Assistant name' }),
    field({ path: 'general.user_name', type: 'text', label: 'Your name' }),
  ),
  group('Screenshots',
    field({ path: 'general.screenshot_scope', type: 'select', label: 'Capture', options: [['cursor_monitor', 'Monitor under the cursor'], ['all_monitors', 'All monitors']] }),
    field({ path: 'general.save_screenshots', type: 'toggle', label: 'Keep copies on this PC', rerender: true }),
    get('general.save_screenshots')
      ? field({ path: 'general.screenshot_retention_days', type: 'number', label: 'Delete copies after', help: '0 keeps them', min: 0, max: 365, unit: 'days' }) : null,
  ),
  group('System',
    field({ path: 'general.launch_at_login', type: 'toggle', label: 'Open at sign-in' }),
    field({ path: 'general.paused', type: 'toggle', label: 'Pause Athena', help: 'The hotkey does nothing while paused' }),
  ),
];

function screenshotSize() {
  const opts = [[768, '768 px'], [1024, '1024 px'], [1120, '1120 px'], [1344, '1344 px'], [1680, '1680 px'], [1920, '1920 px']];
  const cur = get('model.image_max_width');
  if (!opts.some(([v]) => v === cur)) opts.push([cur, `${cur} px`]);
  const sel = h('select', { 'aria-label': 'Screenshot width' }, ...opts.map(([v, l]) => h('option', { value: v }, l)));
  const help = h('div', { class: 'h' });
  const upd = () => { const w = Number(sel.value); help.textContent = `About ${Math.round((w / 28) * (w * 9 / 16 / 28)).toLocaleString()} image tokens`; };
  sel.value = cur; upd();
  sel.onchange = () => { upd(); save('model.image_max_width', Number(sel.value), 0); };
  return h('div', { class: 'row' }, h('div', { class: 'lbl' }, h('div', { class: 't' }, 'Screenshot width'), help), h('div', { class: 'ctl' }, sel));
}

PAGES.model = () => {
  const pid = get('model.active_provider');
  const base = `model.providers.${pid}`;
  const modelInput = h('input', { type: 'text', value: get(`${base}.model`) || '', list: 'models-dl', placeholder: 'Model id', spellcheck: 'false', 'aria-label': 'Model' });
  const dl = h('datalist', { id: 'models-dl' });
  const caps = h('div', { class: 'caps' });
  let catalog = [];
  const showCaps = () => {
    const m = catalog.find((x) => x.id === modelInput.value);
    caps.replaceChildren();
    if (!m) return;
    if (m.vision != null) caps.append(h('span', { class: m.vision ? 'on' : '' }, m.vision ? 'Sees images' : 'No image input'));
    if (m.tools != null) caps.append(h('span', { class: m.tools ? 'on' : '' }, m.tools ? 'Tool calling' : 'No tool calling'));
  };
  modelInput.oninput = () => { save(`${base}.model`, modelInput.value.trim(), 500); showCaps(); renderStatus(); };
  const refresh = h('button', { class: 'btn quiet' }, 'Refresh');
  const loadModels = async () => {
    refresh.disabled = true;
    try {
      catalog = await A.rpc('models.list', { provider: pid });
      dl.replaceChildren(...catalog.map((m) => h('option', { value: m.id })));
      showCaps();
    } catch (e) { toast(e.message, true); }
    refresh.disabled = false;
  };
  refresh.onclick = loadModels;
  setTimeout(loadModels, 50);

  const testOut = h('div', { class: 'result' });
  const test = h('button', { class: 'btn' }, 'Test connection');
  test.onclick = async () => {
    test.disabled = true; testOut.className = 'result'; testOut.textContent = 'Connecting';
    try {
      const r = await A.rpc('llm.test');
      testOut.className = 'result ok'; testOut.textContent = `Connected in ${r.seconds} s`;
    } catch (e) { testOut.className = 'result err'; testOut.textContent = e.message; }
    test.disabled = false;
  };

  return [
    h('h1', {}, 'Model'),
    group('Connection',
      field({ path: 'model.active_provider', type: 'select', label: 'Provider', rerender: true,
        options: Object.entries(presets).map(([k, p]) => [k, p.name]) }),
      field({ path: `${base}.base_url`, type: 'text', label: 'Server URL' }),
      (presets[pid] && presets[pid].needs_key) || get(`${base}.api_key`)
        ? field({ path: `${base}.api_key`, type: 'password', label: 'API key', help: 'Stored encrypted' }) : null,
      row('Model', null, [modelInput, dl, refresh]),
      h('div', { class: 'row' }, h('div', { class: 'lbl' }, caps), h('div', { class: 'ctl' }, testOut, test)),
    ),
    calibrationGroup(),
    group('Generation',
      field({ path: 'model.temperature', type: 'optnumber', range: true, label: 'Temperature', min: 0, max: 1.5, step: 0.05, fallback: 0.3 }),
      field({ path: 'model.top_p', type: 'optnumber', range: true, label: 'Top P', min: 0.05, max: 1, step: 0.05, fallback: 0.9 }),
      field({ path: 'model.max_tokens', type: 'optnumber', label: 'Max tokens', min: 256, max: 65536, step: 256, fallback: 4096 }),
      field({ path: 'model.request_timeout_s', type: 'number', label: 'Timeout', min: 20, max: 900, unit: 's' }),
      field({ path: 'model.tool_mode', type: 'select', label: 'Tool protocol',
        options: [['auto', 'Automatic'], ['native', 'Function calling'], ['json', 'JSON']] }),
      field({ path: 'model.supports_vision', type: 'toggle', label: 'Model sees images' }),
    ),
    group('Screen',
      screenshotSize(),
      field({ path: 'model.jpeg_quality', type: 'range', label: 'Image quality', min: 50, max: 95 }),
      field({ path: 'model.keep_images', type: 'number', label: 'Screenshots in context', min: 1, max: 10 }),
      field({ path: 'model.use_accessibility', type: 'toggle', label: 'Use accessibility data', help: 'Exact positions for buttons and fields' }),
      field({ path: 'model.draw_marks', type: 'toggle', label: 'Number elements on screenshots' }),
      field({ path: 'model.max_elements', type: 'number', label: 'Elements listed', min: 20, max: 400 }),
      field({ path: 'model.coordinate_mode', type: 'select', label: 'Coordinate format',
        options: [['auto', 'Automatic'], ['pixels', 'Pixels'], ['norm1000', '0–1000'], ['norm1', '0–1']] }),
    ),
    group('Instructions',
      field({ path: 'model.extra_instructions', type: 'textarea', label: '', placeholder: 'Keep emails short and warm.' })),
  ];
};

function calibrationGroup() {
  const out = h('div', { class: 'h' }, ' ');
  const show = (c) => {
    if (!c || !c.data) { out.textContent = 'Not calibrated for this model'; return; }
    out.textContent = `${{ pixels: 'Pixels', norm1000: '0–1000 grid', norm1: '0–1' }[c.data.convention] || c.data.convention}, average error ${c.data.error_px} px`;
  };
  A.rpc('calibration.get').then(show).catch(() => {});
  const run = h('button', { class: 'btn' }, 'Calibrate');
  run.onclick = async () => {
    run.disabled = true; out.textContent = 'Keep the mouse still';
    try { const r = await A.rpc('calibration.run'); show({ data: r }); }
    catch (e) { out.textContent = e.message; }
    run.disabled = false;
  };
  const reset = h('button', { class: 'btn quiet' }, 'Reset');
  reset.onclick = async () => { await A.rpc('calibration.clear'); show(null); };
  return group('Pointer',
    h('div', { class: 'row' }, h('div', { class: 'lbl' }, h('div', { class: 't' }, 'Calibration'), out), h('div', { class: 'ctl' }, reset, run)));
}

// Last lines of the voice server's log, shown when it isn't running so the reason is visible.
function voiceLog() {
  const pre = h('pre', { class: 'log' }, 'Loading log');
  A.rpc('voice.log', { lines: 14 }).then((lines) => { pre.textContent = (lines && lines.length) ? lines.join('\n') : 'No log yet.'; })
    .catch(() => { pre.textContent = 'Log unavailable.'; });
  return h('div', { class: 'row stack' }, h('div', { class: 'lbl' }, h('div', { class: 't' }, 'Recent output')),
    h('div', { class: 'ctl' }, pre));
}

PAGES.voice = () => {
  const voiceSel = h('select', { 'aria-label': 'Voice' });
  const fillVoices = (list) => {
    const cur = get('voice.voice');
    const all = list && list.length ? list : [cur];
    voiceSel.replaceChildren(...all.map((v) => h('option', { value: v }, v.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase()))));
    voiceSel.value = cur;
  };
  fillVoices([]);
  A.rpc('voice.voices').then(fillVoices).catch(() => {});
  voiceSel.onchange = () => save('voice.voice', voiceSel.value, 0);
  const play = h('button', { class: 'btn quiet' }, 'Play sample');
  play.onclick = () => A.rpc('voice.test', {}).catch((e) => toast(e.message, true));

  const micSel = h('select', { 'aria-label': 'Microphone' }), outSel = h('select', { 'aria-label': 'Speaker' });
  A.rpc('audio.devices').then((d) => {
    const fill = (sel, list, path) => {
      sel.replaceChildren(h('option', { value: '' }, 'System default'), ...list.map((x) => h('option', { value: String(x.index) }, x.name)));
      const v = get(path); sel.value = v == null ? '' : String(v);
      sel.onchange = () => save(path, sel.value === '' ? null : Number(sel.value), 0);
    };
    fill(micSel, d.inputs, 'voice.mic_device'); fill(outSel, d.outputs, 'voice.output_device');
  }).catch(() => {});

  const LABEL = { ready: 'Running', starting: 'Starting', offline: 'Not running', crashed: 'Stopped unexpectedly', missing: 'Not found', timeout: 'Did not start', unknown: 'Checking' };
  const start = h('button', { class: 'btn' }, voiceStatus === 'ready' ? 'Reconnect' : 'Start');
  start.onclick = () => { A.rpc('voice.start'); toast('Starting voice server'); };

  return [
    h('h1', {}, 'Voice'),
    group('Voice server',
      row('Status', LABEL[voiceStatus] || voiceStatus, start),
      voiceStatus === 'ready' || voiceStatus === 'starting' ? null : voiceLog(),
      field({ path: 'voice.server_url', type: 'text', label: 'Address' }),
      field({ path: 'voice.autostart_server', type: 'toggle', label: 'Start with Athena' }),
      field({ path: 'voice.server_dir', type: 'text', label: 'Folder' }),
      field({ path: 'voice.server_python', type: 'text', label: 'Python', placeholder: 'Same as Athena' }),
    ),
    group('Speech',
      row('Voice', null, [play, voiceSel]),
      field({ path: 'voice.speak_replies', type: 'toggle', label: 'Speak replies' }),
      field({ path: 'voice.max_spoken_words', type: 'number', label: 'Longest spoken reply', help: 'Anything longer opens the chat panel', min: 10, max: 200, unit: 'words' }),
      field({ path: 'voice.tts_language', type: 'select', label: 'Speech language',
        options: ['english', 'english_2026-01', 'english_2026-04', 'french_24l', 'german', 'german_24l', 'italian', 'italian_24l', 'portuguese', 'portuguese_24l', 'spanish', 'spanish_24l']
          .map((x) => [x, x.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())]) }),
      field({ path: 'voice.stt_language', type: 'text', label: 'Transcription language', placeholder: 'en' }),
    ),
    group('Devices', row('Microphone', null, micSel), row('Speaker', null, outSel)),
  ];
};

PAGES.control = () => [
  h('h1', {}, 'Control'),
  group('Mouse and keyboard',
    field({ path: 'control.allow_control', type: 'toggle', label: 'Let Athena use the mouse and keyboard', rerender: true }),
    field({ path: 'control.safety_mode', type: 'seg', label: 'Ask first',
      options: [['confirm_risky', 'Risky actions'], ['confirm_all', 'Every action'], ['autonomous', 'Never']],
      notes: { confirm_risky: 'Sending, deleting, buying and submitting need your OK.', confirm_all: 'Each click and keystroke waits for you.', autonomous: 'You can still stop her at any time.' } }),
    field({ path: 'control.risky_keywords', type: 'list', label: 'Risky words', help: 'A button with one of these names needs your OK' }),
  ),
  group('Taking over',
    field({ path: 'control.takeover_mouse', type: 'toggle', label: 'Stop when I move the mouse' }),
    field({ path: 'control.takeover_threshold_px', type: 'number', label: 'Movement', min: 5, max: 400, unit: 'px' }),
    field({ path: 'control.takeover_esc', type: 'toggle', label: 'Stop when I press Esc' }),
  ),
  group('Pace',
    row('Pointer speed', null, seg([['instant', 'Instant'], ['fast', 'Fast'], ['natural', 'Natural'], ['slow', 'Slow']],
      get('control.mouse_speed'), (v) => { save('control.mouse_speed', v, 0); render(); })),
    field({ path: 'control.auto_screenshot', type: 'toggle', label: 'Check the screen after each action' }),
    field({ path: 'control.settle_ms', type: 'number', label: 'Wait before checking', min: 0, max: 5000, step: 50, unit: 'ms' }),
    field({ path: 'control.max_steps', type: 'number', label: 'Steps per task', min: 3, max: 200 }),
  ),
];

PAGES.appearance = () => {
  const colours = Object.keys(DEFAULT_COLORS).map((k) => {
    const inp = h('input', { type: 'color', class: 'swatch', value: get(`appearance.colors.${k}`) || DEFAULT_COLORS[k], 'aria-label': k });
    inp.oninput = () => save(`appearance.colors.${k}`, inp.value, 300);
    return [k, inp];
  });
  const NAMES = { idle: 'Idle', listening: 'Listening', thinking: 'Thinking', speaking: 'Speaking', acting: 'In control',
    confirm: 'Needs approval', user: 'You took over', error: 'Error', highlight: 'Highlights' };
  return [
    h('h1', {}, 'Appearance'),
    group('Cursor',
      field({ path: 'appearance.show_idle_ring', type: 'toggle', label: 'Show the ring when idle' }),
      field({ path: 'appearance.ring_size', type: 'range', label: 'Ring size', min: 20, max: 72 }),
      field({ path: 'appearance.show_captions', type: 'toggle', label: 'Captions' }),
      field({ path: 'appearance.hide_from_screenshots', type: 'toggle', label: 'Hide from screen capture', help: 'Keeps the overlay out of what the model sees' }),
    ),
    group('Chat and highlights',
      field({ path: 'appearance.popup_width', type: 'number', label: 'Panel width', min: 300, max: 900, step: 10, unit: 'px' }),
      field({ path: 'appearance.highlight_seconds', type: 'number', label: 'Highlights fade after', help: '0 keeps them until dismissed', min: 0, max: 600, unit: 's' }),
    ),
    group('Colours',
      ...colours.map(([k, inp]) => row(NAMES[k], null, inp)),
      h('div', { class: 'actions' }, h('button', { class: 'btn quiet', onclick: () => { save('appearance.colors', { ...DEFAULT_COLORS }, 0); setTimeout(render, 300); } }, 'Reset colours')),
    ),
  ];
};

PAGES.memory = () => {
  const list = h('div');
  const draw = (facts) => {
    list.replaceChildren(...(facts.length ? facts.map((f, i) => h('div', { class: 'mem' }, h('span', {}, f),
      h('button', { class: 'btn quiet danger', onclick: async () => draw(await A.rpc('memory.delete', { index: i })) }, 'Forget')))
      : [h('div', { class: 'empty' }, 'Say “remember that…” and Athena keeps it here.')]));
  };
  A.rpc('memory.list').then(draw).catch(() => draw([]));
  return [
    h('h1', {}, 'Memory'),
    group(null, list),
    h('div', { class: 'actions' },
      h('button', { class: 'btn', onclick: async () => { await A.rpc('agent.reset'); toast('New conversation'); } }, 'New conversation'),
      h('button', { class: 'btn quiet danger', onclick: async () => { if (confirm('Forget everything Athena remembers?')) draw(await A.rpc('memory.clear')); } }, 'Forget everything')),
  ];
};

PAGES.about = () => [
  h('h1', {}, 'About'),
  group(null,
    row('Version', null, h('span', { class: 'path' }, info.version || '')),
    row('Data', null, h('span', { class: 'path' }, info.dataDir || '')),
    row('Open folder', null, [
      h('button', { class: 'btn quiet', onclick: () => A.rpc('app.open_folder', { name: 'sessions' }) }, 'Screenshots'),
      h('button', { class: 'btn quiet', onclick: () => A.rpc('app.open_folder', { name: 'logs' }) }, 'Logs'),
      h('button', { class: 'btn quiet', onclick: () => A.rpc('app.open_folder', { name: 'data' }) }, 'Data')]),
    field({ path: 'shell.python_path', type: 'text', label: 'Python', placeholder: info.python ? info.python.cmd : '', help: 'Applies after a restart' }),
    row('Engine', null, h('button', { class: 'btn', onclick: async () => { toast('Restarting'); await A.restartCore(); } }, 'Restart')),
  ),
];

// ------------------------------------------------------------------ chrome
function renderNav() {
  $('#nav').replaceChildren(...SECTIONS.map(([id, label]) =>
    h('button', { 'aria-current': id === section ? 'page' : null, onclick: () => { section = id; $('#content').scrollTop = 0; render(); } }, label)));
}
function renderNavFooter() { renderStatus(); }
function renderStatus() {
  const st = state.state || 'starting';
  const STATE = { starting: 'Starting', idle: 'Ready', listening: 'Listening', transcribing: 'Transcribing', thinking: 'Thinking',
    speaking: 'Speaking', acting: 'In control', waiting: 'Waiting for you', confirm: 'Needs approval', user: 'Ready', error: 'Problem', paused: 'Paused' };
  const pid = S && get('model.active_provider');
  const model = S ? (get(`model.providers.${pid}.model`) || '') : '';
  const stCls = st === 'error' ? 'err' : ['starting', 'paused'].includes(st) ? 'warn' : 'ok';
  const vCls = voiceStatus === 'ready' ? 'ok' : voiceStatus === 'starting' || voiceStatus === 'unknown' ? 'warn' : 'err';
  const line = (cls, text) => h('div', {}, h('span', { class: `dot ${cls}` }), h('span', {}, text));
  $('#status').replaceChildren(
    line(stCls, `${STATE[st] || st}${S ? `, ${pretty(get('general.hotkey'))}` : ''}`),
    line(vCls, voiceStatus === 'ready' ? 'Voice ready' : voiceStatus === 'starting' ? 'Voice starting' : 'Voice offline'),
    line(model ? 'ok' : 'err', model ? model.split('/').pop() : 'No model'));
}

function render() {
  renderNav();
  renderStatus();
  if (!S) { $('#page').replaceChildren(h('div', { class: 'top' }), h('h1', {}, 'Starting')); return; }
  $('#content').classList.toggle('is-convo', section === 'conversation');
  $('#page').replaceChildren(h('div', { class: 'top' }), ...PAGES[section]().flat().filter(Boolean));
}

async function load() {
  info = await A.info();
  state = info.coreState || state;
  voiceStatus = info.voiceStatus || voiceStatus;
  try { const r = await A.rpc('settings.get'); S = r.settings; presets = r.presets; }
  catch { setTimeout(load, 1000); }
  render();
}

A.onEvent(({ event, data }) => {
  if (event === 'state') { state = data; renderStatus(); syncConvoState(); }
  else if (event === 'chat.message') convoAppend(data.entry);
  else if (event === 'chat.update') window.ChatView.setConfirm(data.id, data.status);
  else if (event === 'chat.cleared') { convo.log = []; if (section === 'conversation') render(); }
  else if (event === 'level') { if (convo.mic && data.mic != null) convo.mic.level(data.mic); }
  else if (event === 'voice.status') { voiceStatus = data.status; renderStatus(); if (section === 'voice') render(); }
  else if (event === 'settings.changed' && !S) load();
  else if (event === 'toast') toast(data.text, data.level === 'error');
});
A.onNavigate((s) => { section = s; render(); });
load();
