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
  const topbar = document.querySelector('.topbar');
  const bodyStyle = getComputedStyle(document.body);
  const app = document.getElementById('app');
  // 页面底色是 color-mix(...) 也算半透明，这里把 alpha 抠出来判断
  const bg = bodyStyle.backgroundColor;
  const alpha = bg.match(/\\/\\s*([\\d.]+)\\s*\\)/) || bg.match(/rgba?\\([^)]*,\\s*([\\d.]+)\\s*\\)$/);
  return {
    url: location.href,
    emptyStrip: !!strip,
    topbarDrag: topbar ? getComputedStyle(topbar).webkitAppRegion : null,
    topbarPadRight: topbar ? getComputedStyle(topbar).paddingRight : null,
    bodyBackground: bg,
    bodyAlpha: alpha ? Number(alpha[1]) : 1,
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

// 窗口尺寸落盘：从主进程改一次窗口大小，等一下再读 config.json。
// 跑之前先把原尺寸记下来，验完还原 —— 这个检查不该顺手改掉用户的窗口几何。
const TARGET_BOUNDS = { width: 1120, height: 720, x: 120, y: 90 };
let bounds = null;
let persisted = null;
let originalBounds = null;
let restored = null;
const evalInMain = async (cdpConn, expression) => {
  const res = await cdpConn.send('Runtime.evaluate', { expression, returnByValue: true });
  if (res.exceptionDetails) throw new Error(JSON.stringify(res.exceptionDetails));
  return res.result.value;
};
// 主进程里 require 不是全局的，得从 process.mainModule 上取
const BOUNDS_EXPR = method => `(() => {
  try {
    const BW = process.mainModule.require('electron').BrowserWindow;
    const w = BW.getAllWindows()[0];
    if (!w) return { error: 'no window' };
    ${method}
  } catch (e) { return { error: e.message }; }
})()`;
try {
  const mainTargets = await (await fetch(`http://127.0.0.1:${INSPECT}/json/list`)).json();
  const main = mainTargets[0];
  if (!main?.webSocketDebuggerUrl) throw new Error(`主进程 inspector 不可用：${JSON.stringify(mainTargets)}`);
  const mainCdp = await cdp(main.webSocketDebuggerUrl);
  const original = await evalInMain(mainCdp, BOUNDS_EXPR('return w.getBounds();'));
  originalBounds = original;
  bounds = await evalInMain(mainCdp,
    BOUNDS_EXPR(`w.setBounds(${JSON.stringify(TARGET_BOUNDS)}); return w.getBounds();`));
  await new Promise(r => setTimeout(r, 1500));
  persisted = JSON.parse(readFileSync(CONFIG, 'utf8')).shell_window || null;

  if (original && !original.error) {
    restored = await evalInMain(mainCdp,
      BOUNDS_EXPR(`w.setBounds(${JSON.stringify(original)}); return w.getBounds();`));
    await new Promise(r => setTimeout(r, 1500));   // 等防抖落盘
    restored.persisted = JSON.parse(readFileSync(CONFIG, 'utf8')).shell_window || null;
  }
  mainCdp.close();
} catch (e) {
  bounds = { error: e.message };
}

const checks = [
  ['preload 桥已注入', results.preloadBridge === true],
  // 顶栏本身就是标题栏：不再另垫一条 32px 空拖动带
  ['没有空拖动带', results.emptyStrip === false],
  ['顶栏是 drag 区域', results.topbarDrag === 'drag'],
  ['顶栏给系统按钮让位 138px', results.topbarPadRight === '138px'],
  ['页面底色半透明（Mica 能透，但仍由主题底色主导）',
    results.bodyAlpha > 0.5 && results.bodyAlpha < 1],
  ['页面不再额外让高', results.bodyPaddingTop === '0px'],
  ['内容区满高（视口高度）', Math.abs(results.appHeight - results.innerHeight) < 1],
  ['应用已启动（topbar 存在）', results.appBooted === true],
  // 用版本号而不是统计数字：统计要 vault 解锁后才有，版本来自放行端点，与解锁状态无关
  ['已完成一次后端往返（版本号已渲染）',
    !!results.appVersion && !results.appVersion.includes('—')],
  // T2：页面必须来自后端回传的动态端口，而不是写死的 8080
  ['页面来自动态端口', /^http:\/\/127\.0\.0\.1:(?!8080)\d+\/$/.test(results.url)],
  ['窗口尺寸改到目标值',
    bounds && bounds.width === TARGET_BOUNDS.width && bounds.height === TARGET_BOUNDS.height],
  ['窗口尺寸已写入 config.json', !!persisted && persisted.width === TARGET_BOUNDS.width],
  ['跑完已还原原窗口尺寸',
    !!restored?.persisted && restored.persisted.width === originalBounds?.width
      && restored.persisted.height === originalBounds?.height],
];

console.log(JSON.stringify({
  page: results,
  windowBounds: bounds,
  persistedShellWindow: persisted,
  restoredWindow: restored,
  checks: checks.map(([name, ok]) => `${ok ? 'PASS' : 'FAIL'}  ${name}`),
  failed: checks.filter(([, ok]) => !ok).length,
}, null, 2));

renderer.close();
process.exit(checks.some(([, ok]) => !ok) ? 1 : 0);
