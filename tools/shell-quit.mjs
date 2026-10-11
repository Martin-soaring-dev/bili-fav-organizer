// 退出验收（T2）：从主进程 inspector 触发 app.quit()，确认后端是「优雅退出」而非被强杀，
// 且退出后不留孤儿进程、端口已释放。
// 用法：以 `--inspect=9229` 启动壳，再跑 node tools/shell-quit.mjs [inspectPort]
import { readFileSync, statSync } from 'node:fs';
import { connect } from 'node:net';
import { join } from 'node:path';

const INSPECT = Number(process.argv[2] || 9229);
const LOG = join(process.env.LOCALAPPDATA, 'BiliFavOrganizer', 'shell.log');
const GRACEFUL_WINDOW_MS = 7000;   // main.js 里等优雅退出的上限

function tail(from) {
  try {
    return readFileSync(LOG, 'utf8').slice(from);
  } catch {
    return '';
  }
}

function probePort(port) {
  return new Promise(resolve => {
    const s = connect({ host: '127.0.0.1', port });
    const done = ok => { s.destroy(); resolve(ok); };
    s.once('connect', () => done(true));
    s.once('error', () => done(false));
    setTimeout(() => done(false), 800);
  });
}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code === 'EPERM';   // 存在但没权限——同样算活着
  }
}

const before = readFileSync(LOG, 'utf8');
const ready = [...before.matchAll(/\[shell\] 后端就绪 pid=(\d+) port=(\d+)/g)].pop();
if (!ready) {
  console.error('shell.log 里找不到「后端就绪」记录，壳可能不是托管模式');
  process.exit(1);
}
const backendPid = Number(ready[1]);
const backendPort = Number(ready[2]);
const offset = statSync(LOG).size;

console.log(`后端 pid=${backendPid} port=${backendPort}；触发 app.quit()…`);
const t0 = Date.now();

const targets = await (await fetch(`http://127.0.0.1:${INSPECT}/json/list`)).json();
const main = targets.find(t => t.webSocketDebuggerUrl);
if (!main) {
  console.error('主进程 inspector 不可用：', JSON.stringify(targets));
  process.exit(1);
}
const ws = new WebSocket(main.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r, { once: true }));
// 不等回包：app.quit() 会立刻拆进程，连接通常先断
ws.send(JSON.stringify({
  id: 1,
  method: 'Runtime.evaluate',
  params: { expression: "process.mainModule.require('electron').app.quit()", returnByValue: true },
}));

let exitedAt = null;
const deadline = Date.now() + 20000;
while (Date.now() < deadline) {
  if (!alive(backendPid)) { exitedAt = Date.now() - t0; break; }
  await new Promise(r => setTimeout(r, 100));
}

let after = tail(offset);
// 后端一退脚本就往下走，壳可能还在写最后几行，给它一点时间
const logDeadline = Date.now() + 3000;
while (!after.includes('壳退出') && Date.now() < logDeadline) {
  await new Promise(r => setTimeout(r, 100));
  after = tail(offset);
}
const portStillOpen = await probePort(backendPort);
const graceful = after.includes('Python 后端已优雅退出');
const killed = after.includes('强杀后端进程树');

const checks = [
  ['后端进程已退出', exitedAt !== null],
  ['走的是优雅退出（不是强杀）', graceful && !killed],
  [ `退出耗时在优雅窗口内（< ${GRACEFUL_WINDOW_MS}ms）`, exitedAt !== null && exitedAt < GRACEFUL_WINDOW_MS],
  ['端口已释放', portStillOpen === false],
  ['壳已记录退出', after.includes('壳退出')],
];

console.log(JSON.stringify({
  backendPid, backendPort,
  exitMs: exitedAt,
  newLog: after.trim().split('\n'),
  checks: checks.map(([n, ok]) => `${ok ? 'PASS' : 'FAIL'}  ${n}`),
  failed: checks.filter(([, ok]) => !ok).length,
}, null, 2));

process.exit(checks.some(([, ok]) => !ok) ? 1 : 0);
