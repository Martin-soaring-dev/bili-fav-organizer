'use strict';
// 壳层注入：只补「窗口需要、而页面本来不需要」的两样东西——
// 顶部的系统标题栏占位带，以及让 Mica 能透上来的透明底。
// static/ 的视觉本批已跟着壳一起调（标题栏高度、半透与 blur 由页面自己的变量主导），
// 这里只做兜底，不再假设页面没被动过。
const { contextBridge, ipcRenderer } = require('electron');

const CAPTION_H = 32;
const CAPTION_BUTTONS_W = 154;   // Win11 三个系统按钮 46×3，再多留一点空隙，别贴着状态信息

const CSS = `
  /* 整块页面就是唯一那层毛玻璃：--surface-alpha / --surface-blur 由页面定义，
     这里做兜底。Mica 要透上来，但必须由主题底色主导 —— 否则暗色模式整块是白的。 */
  html, body {
    background: color-mix(in srgb, var(--bg2, #262626) var(--surface-alpha, 72%), transparent) !important;
    -webkit-backdrop-filter: blur(var(--surface-blur, 24px)) saturate(1.4);
    backdrop-filter: blur(var(--surface-blur, 24px)) saturate(1.4);
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
  // 托盘/壳层说「打开设置」只该是打开（齿轮本身现在是开关，模拟点击会被 toggle 语义带偏），
  // 所以派发一个专用事件给页面自己处理。
  if (document.readyState === 'loading') {
    pendingSettings = true;
    return;
  }
  document.dispatchEvent(new CustomEvent('shell:open-settings'));
});

window.addEventListener('DOMContentLoaded', () => {
  const style = document.createElement('style');
  style.id = 'shell-style';
  style.textContent = CSS;
  document.head.appendChild(style);

  if (pendingSettings) {
    pendingSettings = false;
    document.dispatchEvent(new CustomEvent('shell:open-settings'));
  }
});

contextBridge.exposeInMainWorld('shell', {
  captionHeight: CAPTION_H,
  isDesktopShell: true,
  // Hello 验证期间把窗口让开（那个验证框我们无权置顶，只能自己退让）
  helloAside: () => ipcRenderer.invoke('shell:hello-aside'),
  // 系统明暗变化（自动模式）：页面要重新算一次顶栏底色并上报
  onSystemThemeChanged: cb => ipcRenderer.on('shell:system-theme-changed', () => cb()),
  helloRestore: () => ipcRenderer.invoke('shell:hello-restore'),
  // 告诉壳当前生效的明暗 + 顶栏底色：系统画的那三个按钮要用它自己的背景色，
  // 否则透明底在深色下会「看起来没有悬停/按下反馈」
  setTheme: mode => ipcRenderer.send('shell:set-theme', { mode }),
  // 数据包导入/导出：路径选完由主进程落盘，进度回传给页面里的进度弹窗
  data: {
    chooseSavePath: () => ipcRenderer.invoke('data:choose-save'),
    download: opts => ipcRenderer.invoke('data:download', opts),
    cancelDownload: () => ipcRenderer.send('data:cancel-download'),
    onProgress: cb => ipcRenderer.on('data:progress', (_e, m) => cb(m)),
    offProgress: () => ipcRenderer.removeAllListeners('data:progress'),
  },
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
