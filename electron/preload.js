'use strict';
// 壳层注入：只补「窗口需要、而页面本来不需要」的两样东西——
// 顶部的系统标题栏占位带，以及让 Mica 能透上来的透明底。
// 刻意不改 static/ 下的任何文件：视觉整合是 T8 的范围。
const { contextBridge, ipcRenderer } = require('electron');

const CAPTION_H = 32;
const CAPTION_BUTTONS_W = 138;   // Win11 三个系统按钮：46 × 3

const CSS = `
  /* Mica 要透上来，但必须由主题底色主导 —— 否则暗色模式整块是白的 */
  html, body {
    background: color-mix(in srgb, var(--bg2, #262626) 90%, transparent) !important;
  }
  :root { --shell-caption: ${CAPTION_H}px; --topbar-h: ${CAPTION_H}px; }
  /* 顶栏即标题栏：整体可拖动，右侧让开系统按钮，不再垫一条空带 */
  .topbar {
    -webkit-app-region: drag;
    padding-right: ${CAPTION_BUTTONS_W}px !important;
    min-height: ${CAPTION_H}px;
  }
  .topbar button, .topbar a, .topbar select, .topbar input {
    -webkit-app-region: no-drag;
  }
`;

let pendingSettings = false;
ipcRenderer.on('shell:open-settings', () => {
  const btn = document.getElementById('settings-btn');
  if (btn) btn.click();
  else pendingSettings = true;
});

window.addEventListener('DOMContentLoaded', () => {
  const style = document.createElement('style');
  style.id = 'shell-style';
  style.textContent = CSS;
  document.head.appendChild(style);

  if (pendingSettings) {
    pendingSettings = false;
    document.getElementById('settings-btn')?.click();
  }
});

contextBridge.exposeInMainWorld('shell', {
  captionHeight: CAPTION_H,
  isDesktopShell: true,
  raiseWindow: () => ipcRenderer.invoke('shell:raise-window'),
  terminal: {
    spawn: opts => ipcRenderer.invoke('terminal:spawn', opts),
    write: (id, data) => ipcRenderer.invoke('terminal:write', { id, data }),
    resize: (id, cols, rows) => ipcRenderer.invoke('terminal:resize', { id, cols, rows }),
    kill: id => ipcRenderer.invoke('terminal:kill', { id }),
    lockdown: () => ipcRenderer.invoke('terminal:lockdown'),
    onData: cb => ipcRenderer.on('terminal:data', (_e, m) => cb(m)),
    onExit: cb => ipcRenderer.on('terminal:exit', (_e, m) => cb(m)),
  },
});
