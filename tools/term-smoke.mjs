// 临时验收脚本：点开终端面板，确认 xterm 起来并拿到 PTY 输出（跑完即删）
const PORT = Number(process.argv[2] || 9222);
const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const page = list.find(t => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r, { once: true }));
let seq = 0;
const pending = new Map();
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  const s = pending.get(m.id);
  if (!s) return;
  pending.delete(m.id);
  m.error ? s.reject(new Error(JSON.stringify(m.error))) : s.resolve(m.result);
});
const evalJs = async expression =>
  (await new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: { expression, returnByValue: true, awaitPromise: true } }));
  })).result.value;

// 1. 切到终端面板
const clicked = await evalJs(`(() => {
  const tab = document.querySelector('[data-panel="term"]');
  if (!tab) return 'no-tab';
  tab.click();
  return 'clicked';
})()`);
console.log('switch panel:', clicked);

// 2. 等 PTY 起来
await new Promise(r => setTimeout(r, 3500));

// 3. 检查状态
const state = await evalJs(`(() => {
  const host = document.getElementById('term-host');
  const xt = host && host.querySelector('.xterm');
  const rows = xt ? xt.querySelectorAll('.xterm-rows > div').length : 0;
  const text = host ? (host.innerText || '') : '';
  return JSON.stringify({
    hasApi: !!(window.shell && window.shell.terminal),
    hasXtermClass: typeof Terminal !== 'undefined',
    hasFitAddon: typeof FitAddon !== 'undefined',
    rendered: !!xt,
    rowNodes: rows,
    status: (document.getElementById('term-status') || {}).textContent || '',
    sample: text.replace(/\\s+/g, ' ').trim().slice(0, 160),
  });
})()`);
console.log('state:', state);

ws.close();
process.exit(0);
