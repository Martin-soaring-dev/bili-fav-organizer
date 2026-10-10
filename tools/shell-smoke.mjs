// Electron 壳冒烟检查：通过 CDP 连进渲染进程做断言，并顺带验证窗口尺寸落盘。
// 用法：以 `--remote-debugging-port=9222 --inspect=9229` 启动壳，再跑
//       node tools/shell-smoke.mjs [debugPort] [inspectPort]
//
// 注意：Electron 没有实现 CDP 的 Browser.getWindowForTarget，改窗口尺寸只能走
// 主进程的 Node inspector（--inspect），在那边直接调 BrowserWindow.setBounds。
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const PORT = Number(process.argv[2] || 9222);
const INSPECT = Number(process.argv[3] || 9229);
const CONFIG = join(process.env.LOCALAPPDATA, 'BiliFavOrganizer', 'config.json');
const CAPTION_H = 32;

async function cdp(endpoint) {
  const ws = new WebSocket(endpoint);
  let seq = 0;
  const pending = new Map();
  ws.addEventListener('message', ev => {
    const msg = JSON.parse(ev.data);
    const slot = pending.get(msg.id);
    if (!slot) return;
    pending.delete(msg.id);
    msg.error ? slot.reject(new Error(JSON.stringify(msg.error))) : slot.resolve(msg.result);
  });
  await new Promise(r => ws.addEventListener('open', r, { once: true }));
  return {
    send: (method, params) => new Promise((resolve, reject) => {
      const id = ++seq;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params }));
    }),
    close: () => ws.close(),
  };
}

const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const page = targets.find(t => t.type === 'page');
if (!page) {
  console.error('找不到页面目标：', JSON.stringify(targets));
  process.exit(1);
}

const renderer = await cdp(page.webSocketDebuggerUrl);
const evaluate = async expression =>
  (await renderer.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })).result.value;

const results = await evaluate(`(() => {
  const strip = document.getElementById('shell-caption');
  const bodyStyle = getComputedStyle(document.body);
  const app = document.getElementById('app');
  return {
    url: location.href,
    captionStrip: !!strip,
    captionDrag: strip ? getComputedStyle(strip).webkitAppRegion : null,
    captionHeight: strip ? strip.getBoundingClientRect().height : null,
    bodyTransparent: bodyStyle.backgroundColor,
    bodyPaddingTop: bodyStyle.paddingTop,
    appHeight: app ? app.getBoundingClientRect().height : null,
    innerHeight: window.innerHeight,
    preloadBridge: !!(window.shell && window.shell.isDesktopShell),
    appBooted: !!document.querySelector('.topbar'),
    appVersion: (document.getElementById('app-version') || {}).textContent || null,
    connText: (document.getElementById('conn-text') || {}).textContent || null,
    lockVisible: !!document.querySelector('#app-lock-screen:not([hidden])'),
    statVideos: (document.getElementById('st-videos') || {}).textContent || null,
    statFolders: (document.getElementById('st-folders') || {}).textContent || null,
  };
})()`);

// 窗口尺寸落盘：从主进程改一次窗口大小，等一下再读 config.json
const TARGET_BOUNDS = { width: 1120, height: 720, x: 120, y: 90 };
let bounds = null;
let persisted = null;
try {
  const mainTargets = await (await fetch(`http://127.0.0.1:${INSPECT}/json/list`)).json();
  const main = mainTargets[0];
  if (!main?.webSocketDebuggerUrl) throw new Error(`主进程 inspector 不可用：${JSON.stringify(mainTargets)}`);
  const mainCdp = await cdp(main.webSocketDebuggerUrl);
  // 主进程里 require 不是全局的，得从 process.mainModule 上取
  const expr = `(() => {
    try {
      const BW = process.mainModule.require('electron').BrowserWindow;
      const w = BW.getAllWindows()[0];
      w.setBounds(${JSON.stringify(TARGET_BOUNDS)});
      return w.getBounds();
    } catch (e) { return { error: e.message }; }
  })()`;
  const res = await mainCdp.send('Runtime.evaluate', { expression: expr, returnByValue: true });
  if (res.exceptionDetails) throw new Error(JSON.stringify(res.exceptionDetails));
  bounds = res.result.value;
  mainCdp.close();
  await new Promise(r => setTimeout(r, 1500));
  persisted = JSON.parse(readFileSync(CONFIG, 'utf8')).shell_window || null;
} catch (e) {
  bounds = { error: e.message };
}

const checks = [
  ['preload 桥已注入', results.preloadBridge === true],
  ['拖动带存在', results.captionStrip === true],
  ['拖动带是 drag 区域', results.captionDrag === 'drag'],
  ['拖动带高度 = 32', results.captionHeight === CAPTION_H],
  ['页面底色透明（Mica 能透）', /rgba\(0, 0, 0, 0\)|transparent/.test(results.bodyTransparent)],
  ['内容区高度 = 视口 - 32', Math.abs(results.appHeight - (results.innerHeight - CAPTION_H)) < 1],
  ['应用已启动（topbar 存在）', results.appBooted === true],
  // 用版本号而不是统计数字：统计要 vault 解锁后才有，版本来自放行端点，与解锁状态无关
  ['已完成一次后端往返（版本号已渲染）',
    !!results.appVersion && !results.appVersion.includes('—')],
  ['窗口尺寸改到目标值',
    bounds && bounds.width === TARGET_BOUNDS.width && bounds.height === TARGET_BOUNDS.height],
  ['窗口尺寸已写入 config.json', !!persisted && persisted.width === TARGET_BOUNDS.width],
];

console.log(JSON.stringify({
  page: results,
  windowBounds: bounds,
  persistedShellWindow: persisted,
  checks: checks.map(([name, ok]) => `${ok ? 'PASS' : 'FAIL'}  ${name}`),
  failed: checks.filter(([, ok]) => !ok).length,
}, null, 2));

renderer.close();
process.exit(checks.some(([, ok]) => !ok) ? 1 : 0);
