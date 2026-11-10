'use strict';
const { contextBridge, ipcRenderer } = require('electron');

const listeners = new Map();
function on(channel, cb) {
  const fn = (_e, data) => cb(data);
  ipcRenderer.on(channel, fn);
  return () => ipcRenderer.removeListener(channel, fn);
}

contextBridge.exposeInMainWorld('athena', {
  rpc: (method, params) => ipcRenderer.invoke('rpc', method, params),
  info: () => ipcRenderer.invoke('shell:info'),
  restartCore: () => ipcRenderer.invoke('shell:restart-core'),
  openSettings: (section) => ipcRenderer.send('shell:open-settings', section),
  onEvent: (cb) => on('core-event', cb),
  onCursor: (cb) => on('cursor', cb),
  onCalibration: (cb) => on('calibration', cb),
  onNavigate: (cb) => on('navigate', cb),
  chatHide: () => ipcRenderer.send('chat:hide'),
  chatPin: (on) => ipcRenderer.send('chat:pin', on),
  emit: (event, data) => ipcRenderer.send('chat:emit', { event, data }),
  copy: (text) => ipcRenderer.send('chat:copy', text),
  openUrl: (url) => ipcRenderer.send('chat:open', url),
  overlayInteractive: (on) => ipcRenderer.send('overlay:interactive', on),
});
