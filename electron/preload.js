'use strict';
// 壳层注入：只补「窗口需要、而页面本来不需要」的两样东西——
// 顶部的系统标题栏占位带，以及让 Mica 能透上来的透明底。
// 刻意不改 static/ 下的任何文件：视觉整合是 T8 的范围。
const { contextBridge, ipcRenderer } = require('electron');

const CAPTION_H = 32;
const CAPTION_BUTTONS_W = 138;   // Win11 三个系统按钮：46 × 3

const CSS = `
  html, body { background: transparent !important; }
  body { padding-top: ${CAPTION_H}px; box-sizing: border-box; }
  #app { height: calc(100vh - ${CAPTION_H}px) !important; }
  #shell-caption {
    position: fixed; top: 0; left: 0; right: ${CAPTION_BUTTONS_W}px;
    height: ${CAPTION_H}px; z-index: 2147483000;
    -webkit-app-region: drag;
    user-select: none;
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

  const strip = document.createElement('div');
  strip.id = 'shell-caption';
  strip.setAttribute('aria-hidden', 'true');
  document.body.appendChild(strip);

  if (pendingSettings) {
    pendingSettings = false;
    document.getElementById('settings-btn')?.click();
  }
});

contextBridge.exposeInMainWorld('shell', {
  captionHeight: CAPTION_H,
  isDesktopShell: true,
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
