// Athena — Electron shell.
// Owns everything visual (tray, cursor overlay, chat panel, settings, calibration)
// and supervises the Python core, which owns hotkeys, audio, screen and the agent.
'use strict';

const { app, BrowserWindow, Tray, Menu, screen, ipcMain, nativeImage, dialog, clipboard, shell, nativeTheme } = require('electron');
const { spawn, spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const WebSocket = require('ws');

if (!app.requestSingleInstanceLock()) {
  app.quit();
  process.exit(0);
}

const IS_WIN = process.platform === 'win32';
const CORE_DIR = path.join(__dirname, '..', 'core');
const DATA_DIR = path.join(process.env.ATHENA_HOME ||
  (IS_WIN ? path.join(process.env.APPDATA || '', 'Athena') : path.join(require('os').homedir(), '.config', 'Athena')));
const SETTINGS_PATH = path.join(DATA_DIR, 'settings.json');
const ASSETS = path.join(__dirname, 'assets');
const PRELOAD = path.join(__dirname, 'preload.js');
const TOKEN = process.env.ATHENA_DEV_TOKEN || crypto.randomBytes(24).toString('hex');

let tray = null;
let core = null;           // child process
let link = null;           // CoreLink
let quitting = false;
let settings = null;       // public settings from core
let coreState = { state: 'starting', detail: '' };
let voiceStatus = 'unknown';
const overlays = new Map(); // displayId -> BrowserWindow
let chatWin = null;
let settingsWin = null;
let calibWin = null;
let chatBounds = null;     // remembered size/position of the chat panel

// Window chrome follows the Windows light/dark setting (matches theme.css).
const chrome = () => (nativeTheme.shouldUseDarkColors
  ? { bg: '#17191C', symbol: '#A3A59E' }
  : { bg: '#F4F4F1', symbol: '#5E615A' });

app.setAppUserModelId('com.athena.assistant');
app.commandLine.appendSwitch('disable-renderer-backgrounding');

// --------------------------------------------------------------------------
// Settings bootstrap (only to find Python before the core is up)
// --------------------------------------------------------------------------
function readSettingsFile() {
  try { return JSON.parse(fs.readFileSync(SETTINGS_PATH, 'utf8')); } catch { return {}; }
}

function findPython() {
  const s = readSettingsFile();
  const configured = s.shell && s.shell.python_path;
  if (configured && fs.existsSync(configured)) return { cmd: configured, pre: [] };
  if (process.env.ATHENA_PYTHON && fs.existsSync(process.env.ATHENA_PYTHON)) return { cmd: process.env.ATHENA_PYTHON, pre: [] };
  if (IS_WIN) {
    const local = process.env.LOCALAPPDATA || '';
    for (const v of ['Python311', 'Python312', 'Python313']) {
      const p = path.join(local, 'Programs', 'Python', v, 'python.exe');
      if (fs.existsSync(p)) return { cmd: p, pre: [] };
    }
    const py = spawnSync('py', ['-3.11', '-c', 'print(1)'], { windowsHide: true });
    if (py.status === 0) return { cmd: 'py', pre: ['-3.11'] };
    return { cmd: 'python', pre: [] };
  }
  return { cmd: 'python3', pre: [] };
}

function corePort() {
  const s = readSettingsFile();
  return (s.shell && s.shell.ws_port) || 47321;
}

// --------------------------------------------------------------------------
// Core process + WebSocket link
// --------------------------------------------------------------------------
class CoreLink {
  constructor(port) {
    this.port = port;
    this.ws = null;
    this.seq = 0;
    this.pending = new Map();
    this.open = false;
  }
  connect() {
    const ws = new WebSocket(`ws://127.0.0.1:${this.port}/?token=${TOKEN}`);
    this.ws = ws;
    ws.on('open', () => { this.open = true; onCoreConnected(); });
    ws.on('message', (raw) => {
      let msg; try { msg = JSON.parse(raw.toString()); } catch { return; }
      if (msg.type === 'rpc_result') {
        const p = this.pending.get(msg.id);
        if (p) { this.pending.delete(msg.id); msg.error ? p.reject(new Error(msg.error)) : p.resolve(msg.result); }
      } else if (msg.type === 'event') {
        onCoreEvent(msg.event, msg.data);
      }
    });
    ws.on('close', () => {
      this.open = false;
      for (const p of this.pending.values()) p.reject(new Error('core disconnected'));
      this.pending.clear();
      if (!quitting && core) setTimeout(() => this.connect(), 800);
    });
    ws.on('error', () => {});
  }
  rpc(method, params = {}, timeoutMs = 120000) {
    return new Promise((resolve, reject) => {
      if (!this.open) return reject(new Error('Athena core is not connected yet'));
      const id = ++this.seq;
      const t = setTimeout(() => { this.pending.delete(id); reject(new Error(`${method} timed out`)); }, timeoutMs);
      this.pending.set(id, { resolve: (v) => { clearTimeout(t); resolve(v); }, reject: (e) => { clearTimeout(t); reject(e); } });
      this.ws.send(JSON.stringify({ type: 'rpc', id, method, params }));
    });
  }
  emit(event, data = {}) {
    if (this.open) this.ws.send(JSON.stringify({ type: 'event', event, data }));
  }
}

let restarts = 0;
let coreLog = [];
function startCore() {
  const { cmd, pre } = findPython();
  const port = corePort();
  const args = [...pre, '-u', '-m', 'athena_core', '--port', String(port), '--token', TOKEN, '--parent-pid', String(process.pid)];
  setState({ state: 'starting', detail: 'Starting Athena core…' });
  try {
    core = spawn(cmd, args, { cwd: CORE_DIR, windowsHide: true, env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' } });
  } catch (e) {
    return coreFailed(`Could not launch Python (${cmd}): ${e.message}`);
  }
  let connected = false;
  const onData = (buf) => {
    const s = buf.toString();
    coreLog.push(s); if (coreLog.length > 200) coreLog = coreLog.slice(-200);
    process.stdout.write('[core] ' + s);
    if (!connected && s.includes('ATHENA_READY')) {
      connected = true;
      link = new CoreLink(port);
      link.connect();
    }
  };
  core.stdout.on('data', onData);
  core.stderr.on('data', onData);
  core.on('error', (e) => coreFailed(`Could not launch Python (${cmd}): ${e.message}`));
  core.on('exit', (code) => {
    core = null;
    if (quitting) return;
    restarts += 1;
    if (restarts > 4) {
      return coreFailed(`Athena core keeps exiting (code ${code}).\n\nLast output:\n${coreLog.join('').slice(-1500)}`);
    }
    setState({ state: 'error', detail: 'Core restarting…' });
    setTimeout(startCore, 1000 * restarts);
  });
}

function coreFailed(message) {
  setState({ state: 'error', detail: 'Core failed' });
  dialog.showMessageBox({
    type: 'error', title: 'Athena', message: 'Athena could not start its engine',
    detail: message + '\n\nCheck Python 3.11 is installed and run setup.bat, or set the Python path in settings.json → shell.python_path.',
  });
}

async function stopCore() {
  if (link && link.open) { try { await link.rpc('app.shutdown', {}, 3000); } catch {} }
  await new Promise((r) => setTimeout(r, 400));
  if (core) { try { core.kill(); } catch {} }
}

async function onCoreConnected() {
  restarts = 0;
  try {
    const r = await link.rpc('settings.get');
    applySettings(r.settings);
    setState(await link.rpc('state.get'));
  } catch (e) { console.error(e); }
}

// --------------------------------------------------------------------------
// Core events -> windows
// --------------------------------------------------------------------------
function toDip(x, y) {
  if (IS_WIN && screen.screenToDipPoint) return screen.screenToDipPoint({ x, y });
  return { x, y };
}

function sendOverlays(channel, data) {
  for (const w of overlays.values()) if (!w.isDestroyed()) w.webContents.send(channel, data);
}

function convertItems(items) {
  return (items || []).map((it) => {
    const o = { ...it };
    if (it.rect) {
      const a = toDip(it.rect[0], it.rect[1]); const b = toDip(it.rect[2], it.rect[3]);
      o.rect = [a.x, a.y, b.x, b.y];
    }
    if (it.point) { const p = toDip(it.point[0], it.point[1]); o.point = [p.x, p.y]; }
    return o;
  });
}

function onCoreEvent(event, data) {
  switch (event) {
    case 'state': setState(data); break;
    case 'settings.changed': applySettings(data); break;
    case 'voice.status': voiceStatus = data.status; refreshTray(); broadcastAll('core-event', { event, data }); break;
    case 'level': sendOverlays('core-event', { event, data }); sendChat('core-event', { event, data }); sendApp('core-event', { event, data }); break;
    case 'caption': sendOverlays('core-event', { event, data }); break;
    case 'highlight.set': sendOverlays('core-event', { event, data: { ...data, items: convertItems(data.items) } }); break;
    case 'highlight.clear': case 'highlight.settle': sendOverlays('core-event', { event, data }); break;
    case 'feedback': { const p = toDip(data.x, data.y); sendOverlays('core-event', { event, data: { ...data, x: p.x, y: p.y } }); break; }
    case 'chat.message': sendChat('core-event', { event, data }); sendApp('core-event', { event, data }); if (data.open) showChat(); break;
    case 'chat.update': case 'chat.cleared': sendChat('core-event', { event, data }); sendApp('core-event', { event, data }); break;
    case 'toast': sendOverlays('core-event', { event: 'caption', data: { text: data.text, kind: data.level === 'error' ? 'error' : 'status' } });
      if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('core-event', { event, data });
      break;
    case 'calibration.show': showCalibration(data); break;
    case 'calibration.status': case 'calibration.result':
      if (calibWin && !calibWin.isDestroyed()) calibWin.webContents.send('core-event', { event, data: convertCalib(data) });
      if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('core-event', { event, data });
      break;
    case 'calibration.hide': if (calibWin && !calibWin.isDestroyed()) calibWin.close(); break;
    default: broadcastAll('core-event', { event, data });
  }
}

function broadcastAll(channel, data) {
  for (const w of BrowserWindow.getAllWindows()) if (!w.isDestroyed()) w.webContents.send(channel, data);
}

function setState(s) {
  coreState = { ...coreState, ...s };
  sendOverlays('core-event', { event: 'state', data: coreState });
  if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send('core-event', { event: 'state', data: coreState });
  sendChat('core-event', { event: 'state', data: coreState });   // the popup's status and "Working" line
  refreshTray();
}

function applySettings(s) {
  settings = s;
  const hide = !!(s.appearance && s.appearance.hide_from_screenshots);
  for (const w of overlays.values()) if (!w.isDestroyed()) w.setContentProtection(hide);
  if (chatWin && !chatWin.isDestroyed()) chatWin.setContentProtection(hide);
  if (IS_WIN) {
    try {
      app.setLoginItemSettings({ openAtLogin: !!s.general.launch_at_login, path: process.execPath,
        args: app.isPackaged ? [] : [path.resolve(__dirname)] });
    } catch {}
  }
  broadcastAll('core-event', { event: 'settings.changed', data: s });
  refreshTray();
}

// --------------------------------------------------------------------------
// Overlay windows (one transparent, click-through window per display)
// --------------------------------------------------------------------------
function createOverlay(display) {
  const b = display.bounds;
  const w = new BrowserWindow({
    x: b.x, y: b.y, width: b.width, height: b.height,
    transparent: true, frame: false, resizable: false, movable: false, minimizable: false, maximizable: false,
    focusable: false, skipTaskbar: true, hasShadow: false, show: false, alwaysOnTop: true, fullscreenable: false,
    backgroundColor: '#00000000', type: IS_WIN ? 'toolbar' : undefined,
    webPreferences: { preload: PRELOAD, backgroundThrottling: false, contextIsolation: true, sandbox: true },
  });
  w.setAlwaysOnTop(true, 'screen-saver');
  w.setIgnoreMouseEvents(true, { forward: true });  // click-through, but hover reaches highlight close buttons
  w.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  w.setContentProtection(!settings || settings.appearance.hide_from_screenshots !== false);
  w.loadFile(path.join(__dirname, 'src', 'overlay.html'), { query: { display: String(display.id) } });
  w.once('ready-to-show', () => {
    w.setBounds(b);
    w.showInactive();
    w.webContents.send('core-event', { event: 'state', data: coreState });
    if (settings) w.webContents.send('core-event', { event: 'settings.changed', data: settings });
  });
  w.displayBounds = b;
  overlays.set(display.id, w);
}

function rebuildOverlays() {
  for (const w of overlays.values()) if (!w.isDestroyed()) w.destroy();
  overlays.clear();
  for (const d of screen.getAllDisplays()) createOverlay(d);
}

let lastCursor = { x: -1, y: -1 };
function cursorPump() {
  const p = screen.getCursorScreenPoint();
  if (p.x === lastCursor.x && p.y === lastCursor.y) return;
  lastCursor = p;
  for (const w of overlays.values()) {
    if (w.isDestroyed()) continue;
    const b = w.displayBounds;
    const inside = p.x >= b.x && p.x < b.x + b.width && p.y >= b.y && p.y < b.y + b.height;
    w.webContents.send('cursor', { x: p.x - b.x, y: p.y - b.y, inside, ox: b.x, oy: b.y });
  }
}

// --------------------------------------------------------------------------
// Chat panel (Athena's detailed answers, follow-ups by text or voice, approvals)
// --------------------------------------------------------------------------
function sendApp(channel, data) {
  if (settingsWin && !settingsWin.isDestroyed()) settingsWin.webContents.send(channel, data);
}

function sendChat(channel, data) {
  if (chatWin && !chatWin.isDestroyed()) chatWin.webContents.send(channel, data);
}

function ensureChat() {
  if (chatWin && !chatWin.isDestroyed()) return chatWin;
  const width = (settings && settings.appearance.popup_width) || 440;
  chatWin = new BrowserWindow({
    width, height: 560, minWidth: 340, minHeight: 280, frame: false, resizable: true, show: false,
    alwaysOnTop: true, skipTaskbar: true, fullscreenable: false, maximizable: false, minimizable: false,
    title: 'Athena Chat', backgroundColor: chrome().bg, icon: path.join(ASSETS, 'icon.png'),
    webPreferences: { preload: PRELOAD, contextIsolation: true, sandbox: true },
  });
  chatWin.setAlwaysOnTop(true, 'pop-up-menu');
  chatWin.setContentProtection(!settings || settings.appearance.hide_from_screenshots !== false);
  chatWin.loadFile(path.join(__dirname, 'src', 'popup.html'));
  chatWin.on('show', () => link && link.emit('chat.visible', { visible: true }));
  chatWin.on('hide', () => link && link.emit('chat.visible', { visible: false }));
  chatWin.on('resized', () => { chatBounds = chatWin.getBounds(); });
  chatWin.on('moved', () => { chatBounds = chatWin.getBounds(); });
  chatWin.on('closed', () => { chatWin = null; link && link.emit('chat.visible', { visible: false }); });
  chatWin.webContents.on('did-finish-load', () => {
    chatWin.webContents.send('core-event', { event: 'state', data: coreState });
    if (settings) chatWin.webContents.send('core-event', { event: 'settings.changed', data: settings });
  });
  return chatWin;
}

function placeNearCursor(width, height) {
  const p = screen.getCursorScreenPoint();
  const wa = screen.getDisplayNearestPoint(p).workArea;
  let x = p.x + 28, y = p.y + 24;
  if (x + width > wa.x + wa.width - 8) x = p.x - width - 28;
  if (y + height > wa.y + wa.height - 8) y = Math.max(wa.y + 8, wa.y + wa.height - height - 8);
  x = Math.max(wa.x + 8, x);
  return { x: Math.round(x), y: Math.round(y), width, height: Math.min(height, wa.height - 16) };
}

function showChat(focus = false) {
  const w = ensureChat();
  if (!w.isVisible()) {
    const width = (chatBounds && chatBounds.width) || (settings && settings.appearance.popup_width) || 440;
    const height = (chatBounds && chatBounds.height) || 560;
    w.setBounds(chatBounds || placeNearCursor(width, height));
    const reveal = () => (focus ? w.show() : w.showInactive());
    if (w.webContents.isLoading()) w.webContents.once('did-finish-load', reveal); else reveal();
  } else if (focus) w.focus();
}

function hideChat() { if (chatWin && !chatWin.isDestroyed()) chatWin.hide(); }

ipcMain.on('chat:hide', () => hideChat());
ipcMain.on('chat:pin', (_e, on) => { if (chatWin) chatWin.setAlwaysOnTop(!!on, 'pop-up-menu'); });
ipcMain.on('chat:emit', (_e, { event, data }) => { if (link) link.emit(event, data || {}); });
ipcMain.on('chat:copy', (_e, text) => clipboard.writeText(text || ''));
ipcMain.on('chat:open', (_e, url) => { if (/^https?:\/\//i.test(url || '')) shell.openExternal(url); });
ipcMain.on('overlay:interactive', (e, on) => {
  const w = BrowserWindow.fromWebContents(e.sender);
  if (w && !w.isDestroyed()) w.setIgnoreMouseEvents(!on, { forward: true });
});

// --------------------------------------------------------------------------
// Settings + calibration windows
// --------------------------------------------------------------------------
function openSettings(section) {
  if (settingsWin && !settingsWin.isDestroyed()) {
    settingsWin.show(); settingsWin.focus();
    if (section) settingsWin.webContents.send('navigate', section);
    return;
  }
  settingsWin = new BrowserWindow({
    width: 1040, height: 740, minWidth: 820, minHeight: 560, show: false, title: 'Athena',
    backgroundColor: chrome().bg, icon: path.join(ASSETS, 'icon.png'),
    titleBarStyle: 'hidden', titleBarOverlay: { color: chrome().bg, symbolColor: chrome().symbol, height: 52 },
    webPreferences: { preload: PRELOAD, contextIsolation: true, sandbox: true },
  });
  settingsWin.setMenu(null);
  settingsWin.loadFile(path.join(__dirname, 'src', 'settings.html'), { query: section ? { section } : {} });
  settingsWin.once('ready-to-show', () => settingsWin.show());
  settingsWin.on('closed', () => { settingsWin = null; });
}

function convertCalib(data) {
  if (!data || !data.mapped) return data;
  return { ...data, mapped: data.mapped.map((m) => ({ ...m, ...toDip(m.x, m.y) })) };
}

function showCalibration(data) {
  const [mx, my] = data.monitor;
  const tl = toDip(mx, my);
  const display = screen.getDisplayNearestPoint(tl);
  const b = display.bounds;
  if (calibWin && !calibWin.isDestroyed()) calibWin.destroy();
  calibWin = new BrowserWindow({
    x: b.x, y: b.y, width: b.width, height: b.height, frame: false, resizable: false, movable: false,
    alwaysOnTop: true, skipTaskbar: true, show: false, backgroundColor: '#17191C', fullscreenable: false,
    webPreferences: { preload: PRELOAD, contextIsolation: true, sandbox: true },
  });
  calibWin.setAlwaysOnTop(true, 'screen-saver', 1);
  const targets = data.targets.map((t) => { const p = toDip(t.x, t.y); return { label: t.label, x: p.x - b.x, y: p.y - b.y }; });
  calibWin.loadFile(path.join(__dirname, 'src', 'calibrate.html'));
  calibWin.webContents.once('did-finish-load', () => {
    calibWin.webContents.send('calibration', { targets, origin: { x: b.x, y: b.y }, settings });
  });
  calibWin.once('ready-to-show', () => {
    calibWin.setBounds(b);
    calibWin.show();
    setTimeout(() => link && link.emit('calibration.ready', {}), 450);
  });
  calibWin.on('closed', () => { calibWin = null; });
}

// --------------------------------------------------------------------------
// IPC from renderers
// --------------------------------------------------------------------------
ipcMain.handle('rpc', async (_e, method, params) => {
  if (!link) throw new Error('Athena core is not connected yet');
  return link.rpc(method, params || {}, method === 'calibration.run' ? 300000 : 120000);
});
ipcMain.handle('shell:info', () => ({ coreState, voiceStatus, settings, platform: process.platform,
  version: app.getVersion(), dataDir: DATA_DIR, python: findPython() }));
ipcMain.on('shell:open-settings', (_e, section) => openSettings(section));
ipcMain.handle('shell:restart-core', async () => { await restartCore(); return true; });

async function restartCore() {
  restarts = 0;
  const old = core;
  if (old) {
    await stopCore();
    await new Promise((r) => { if (!core) return r(); old.once('exit', r); setTimeout(r, 3000); });
  }
  if (!core) startCore();
}

// --------------------------------------------------------------------------
// Tray
// --------------------------------------------------------------------------
const STATE_LABEL = {
  starting: 'Starting', idle: 'Ready', listening: 'Listening', transcribing: 'Transcribing', thinking: 'Thinking',
  speaking: 'Speaking', acting: 'Athena has control', waiting: 'Your turn', confirm: 'Needs approval',
  user: 'Ready', error: 'Something went wrong', paused: 'Paused',
};

function trayIcon(state) {
  const map = { listening: 'listening', transcribing: 'thinking', thinking: 'thinking', speaking: 'thinking',
    acting: 'acting', confirm: 'acting', waiting: 'listening', error: 'error', paused: 'paused', starting: 'paused' };
  const name = map[state] || 'idle';
  const img = nativeImage.createFromPath(path.join(ASSETS, `tray-${name}.png`));
  return img.isEmpty() ? nativeImage.createFromPath(path.join(ASSETS, 'icon.png')) : img;
}

function call(method, params) { if (link) link.rpc(method, params).catch((e) => console.error(e.message)); }

function refreshTray() {
  if (!tray) return;
  const st = coreState.state || 'starting';
  const paused = !!(settings && settings.general.paused);
  const hotkey = settings ? settings.general.hotkey : '';
  const model = coreState.model || (settings && (settings.model.providers[settings.model.active_provider] || {}).model) || 'No model';
  tray.setImage(trayIcon(st));
  tray.setToolTip(`Athena\n${STATE_LABEL[st] || st}${hotkey ? `, ${hotkey.toUpperCase()}` : ''}`);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: STATE_LABEL[st] || st, enabled: false },
    { label: voiceStatus === 'ready' ? 'Voice ready' : voiceStatus === 'starting' ? 'Voice starting' : 'Voice offline', enabled: false },
    { label: model, enabled: false },
    { type: 'separator' },
    { label: 'Conversation', click: () => openSettings('conversation') },
    { label: 'Settings', click: () => openSettings('general') },
    { type: 'separator' },
    { label: 'Pause', type: 'checkbox', checked: paused,
      click: (mi) => call('settings.set', { patch: { general: { paused: mi.checked } } }) },
    { label: 'Stop current task', click: () => call('agent.stop') },
    { label: 'New conversation', click: () => call('agent.reset') },
    { type: 'separator' },
    { label: 'Screenshots folder', click: () => call('app.open_folder', { name: 'sessions' }) },
    { label: 'Restart engine', click: () => restartCore() },
    { label: 'Quit', click: () => quit() },
  ]));
}

async function quit() {
  quitting = true;
  await stopCore();
  if (tray) tray.destroy();
  app.exit(0);
}

// --------------------------------------------------------------------------
app.whenReady().then(() => {
  tray = new Tray(trayIcon('starting'));
  tray.on('click', () => openSettings('conversation'));
  refreshTray();
  rebuildOverlays();
  let rebuildTimer = null;
  const scheduleRebuild = () => { clearTimeout(rebuildTimer); rebuildTimer = setTimeout(rebuildOverlays, 400); };
  screen.on('display-added', scheduleRebuild);
  screen.on('display-removed', scheduleRebuild);
  screen.on('display-metrics-changed', (_e, _d, changed) => {
    if (changed.includes('bounds') || changed.includes('scaleFactor')) scheduleRebuild();
  });
  setInterval(cursorPump, 8);
  startCore();
  const first = !fs.existsSync(SETTINGS_PATH);
  if (first) setTimeout(() => openSettings('model'), 2500);
});

app.on('second-instance', () => openSettings('conversation'));
nativeTheme.on('updated', () => {
  const c = chrome();
  if (settingsWin && !settingsWin.isDestroyed()) {
    settingsWin.setBackgroundColor(c.bg);
    try { settingsWin.setTitleBarOverlay({ color: c.bg, symbolColor: c.symbol, height: 52 }); } catch {}
  }
  if (chatWin && !chatWin.isDestroyed()) chatWin.setBackgroundColor(c.bg);
});
app.on('window-all-closed', (e) => { /* keep running in tray */ });
app.on('before-quit', () => { quitting = true; if (core) try { core.kill(); } catch {} });
