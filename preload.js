'use strict';
/**
 * Minimal, sandboxed bridge. No Node.js API is handed to the page: the shell
 * can ask for startup status, request a retry, and quit. Nothing else.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('mabee3at', {
  onStatus: (callback) => {
    if (typeof callback !== 'function') return () => {};
    const handler = (_event, payload) => callback(payload);
    ipcRenderer.on('server-status', handler);
    return () => ipcRenderer.removeListener('server-status', handler);
  },
  getInfo: () => ipcRenderer.invoke('app-info'),
  retry: () => ipcRenderer.invoke('retry'),
  quit: () => ipcRenderer.invoke('quit'),
});
