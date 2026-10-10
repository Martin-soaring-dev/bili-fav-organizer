'use strict';
// Electron 壳：窗口、托盘、单实例，以及托管 Python 后端（拉起 → 握手 → 收摊）。
const { app, BrowserWindow, Menu, Tray, nativeImage, nativeTheme, ipcMain } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const pty = require('node-pty');

const REPO_ROOT = path.join(__dirname, '..');
const APP_ICON = path.join(REPO_ROOT, 'packaging', 'BiliFavOrganizer.ico');
const APP_DATA_ROOT = path.join(process.env.LOCALAPPDATA || app.getPath('appData'), 'BiliFavOrganizer');
const CONFIG_FILE = path.join(APP_DATA_ROOT, 'config.json');
const SHELL_LOG = path.join(APP_DATA_ROOT, 'shell.log');
const CAPTION_H = 32;

// 端口：托管模式下交给系统分配（--port 0），壳从后端的握手行拿真实端口。
// 设了 BFO_PORT 就改用固定端口，方便 dev 里跟旧后端双跑对比（此时允许复用已在跑的实例）。
const FIXED_PORT = Number(process.env.BFO_PORT || 0);
let backendUrl = FIXED_PORT ? `http://127.0.0.1:${FIXED_PORT}` : null;

// Electron 自己的缓存目录也收在同一个根下，别在 %APPDATA% 另开一处
app.setPath('userData', path.join(APP_DATA_ROOT, 'shell'));

let backend = null;        // 本进程拉起的 Python 后端
let win = null;
let tray = null;
let shuttingDown = false;

/* ---------------- 日志 ---------------- */
// 打包后没有控制台，壳自己的日志必须落盘，否则出问题只能靠猜。
// 用同步追加：退出路径上的最后几行不能被进程退出丢在缓冲区里。
function openShellLog() {
  try {
    fs.mkdirSync(APP_DATA_ROOT, { recursive: true });
    try {
      if (fs.statSync(SHELL_LOG).size > 2 * 1024 * 1024) fs.rmSync(SHELL_LOG);
    } catch { /* 首次运行还没有这个文件 */ }
  } catch (e) {
    console.warn('[shell] 无法准备 shell.log：', e.message);
  }
}

function logLine(tag, text) {
  const line = `[${tag}] ${text}`;
  console.log(line);
  try {
    fs.appendFileSync(SHELL_LOG, `${line}\n`);
  } catch { /* 磁盘写不进去也不能影响主流程 */ }
}

const log = (...a) => logLine('shell', a.join(' '));

// 后端输出按行转发：一行一条，方便与 shell.log 里的壳日志对照
function forwardBackend(tag, chunk) {
  const text = String(chunk);
  for (const l of text.split(/\r?\n/)) if (l.trim()) logLine(tag, l);
}

/* ---------------- 窗口与托盘 ---------------- */
function showWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function createTray() {
  if (tray) return;
  const img = nativeImage.createFromPath(APP_ICON);
  tray = new Tray(img.isEmpty() ? nativeImage.createEmpty() : img);
  tray.setToolTip('B站收藏夹智能整理');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '显示主界面', click: showWindow },
    {
      label: '设置',
      click: () => {
        showWindow();
        if (win && !win.isDestroyed()) win.webContents.send('shell:open-settings');
      },
    },
    { type: 'separator' },
    { label: '退出', click: () => { app.isQuitting = true; app.quit(); } },
  ]));
  tray.on('click', showWindow);   // 左键单击也唤起主界面
  log('托盘已就绪');
}

/* ---------------- 终端 PTY ---------------- */
const terminals = new Map();      // id -> node-pty 实例
const TERM_CWD = APP_DATA_ROOT;   // 与 /api/terminal/exec 的工作目录一致

function resolveShell() {
  const dirs = (process.env.PATH || '').split(';');
  for (const name of ['pwsh.exe', 'powershell.exe']) {
    for (const dir of dirs) {
      try {
        if (dir && fs.existsSync(path.join(dir, name))) {
          return { exe: path.join(dir, name), args: ['-NoLogo'] };
        }
      } catch { /* 路径里可能有非法字符，跳过 */ }
    }
  }
  return { exe: process.env.ComSpec || 'cmd.exe', args: [] };
}

// 与 /api/* 同一条门禁：拿一个确实被门禁保护的端点做判据
async function isAppUnlocked() {
  if (!backendUrl) return false;
  try {
    const r = await fetch(`${backendUrl}/api/status`, { signal: AbortSignal.timeout(2000) });
    return r.ok;
  } catch { return false; }
}

function killAllTerminals() {
  for (const [id, p] of terminals) {
    try { p.kill(); } catch { /* 已退出 */ }
    terminals.delete(id);
  }
}

function registerShellIpc() {
  // Windows Hello 的验证框跟着前台窗口走：先把本窗口提到最前，它才不会被压在下面
  ipcMain.handle('shell:raise-window', () => {
    if (win && !win.isDestroyed()) {
      win.setAlwaysOnTop(true);
      win.moveTop();
      win.focus();
      setTimeout(() => {
        if (win && !win.isDestroyed()) win.setAlwaysOnTop(false);
      }, 2500);
    }
    return { ok: true };
  });
}

function registerTerminalIpc() {
  ipcMain.handle('terminal:spawn', async (_e, opts) => {
    // PTY 走主进程，天然绕过 HTTP 门禁 —— 必须显式复刻同一条锁定规则
    if (!(await isAppUnlocked())) {
      return { ok: false, error: 'app_locked', message: '应用已锁定，请先解锁' };
    }
    const id = String((opts && opts.id) || 'main');
    const prev = terminals.get(id);
    if (prev) {
      try { prev.kill(); } catch { /* 已退出 */ }
      terminals.delete(id);
    }
    const shell = resolveShell();
    let p;
    try {
      p = pty.spawn(shell.exe, shell.args, {
        name: 'xterm-256color',
        cols: Math.max(20, Number(opts && opts.cols) || 100),
        rows: Math.max(5, Number(opts && opts.rows) || 30),
        cwd: TERM_CWD,
        env: process.env,
      });
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
    terminals.set(id, p);
    log(`终端已启动 id=${id} shell=${shell.exe}`);
    p.onData(data => {
      if (win && !win.isDestroyed()) win.webContents.send('terminal:data', { id, data });
    });
    p.onExit(e => {
      terminals.delete(id);
      if (win && !win.isDestroyed()) {
        win.webContents.send('terminal:exit', { id, code: e.exitCode });
      }
    });
    return { ok: true, id, shell: shell.exe };
  });

  ipcMain.handle('terminal:write', (_e, m) => {
    const p = terminals.get(String(m && m.id));
    if (!p) return { ok: false, error: '终端不存在' };
    p.write(String((m && m.data) || ''));
    return { ok: true };
  });

  ipcMain.handle('terminal:resize', (_e, m) => {
    const p = terminals.get(String(m && m.id));
    if (!p) return { ok: false };
    try {
      p.resize(Math.max(20, (m && m.cols) | 0), Math.max(5, (m && m.rows) | 0));
    } catch { /* 尺寸非法，忽略 */ }
    return { ok: true };
  });

  ipcMain.handle('terminal:kill', (_e, m) => {
    const id = String(m && m.id);
    const p = terminals.get(id);
    if (p) {
      try { p.kill(); } catch { /* 已退出 */ }
      terminals.delete(id);
    }
    return { ok: true };
  });

  // 锁定时回收所有 PTY，不留后台 shell
  ipcMain.handle('terminal:lockdown', () => {
    const n = terminals.size;
    killAllTerminals();
    log(`锁定时回收终端 ${n} 个`);
    return { ok: true, killed: n };
  });
}

/* ---------------- config.json（沿用应用自己的文件，不新建） ---------------- */
function readConfig() {
  try { return JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')); } catch { return {}; }
}
function patchConfig(patch) {
  const cfg = readConfig();
  Object.assign(cfg, patch);
  try {
    fs.mkdirSync(APP_DATA_ROOT, { recursive: true });
    const tmp = `${CONFIG_FILE}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2), 'utf8');
    fs.renameSync(tmp, CONFIG_FILE);
  } catch (e) {
    console.warn('[shell] 写入 config.json 失败：', e.message);
  }
}
function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ---------------- 后端托管 ---------------- */
// 就绪探针只能用「解锁前也放行」的端点：/api/* 在 vault 未解锁时一律 403，
// 所以拿 /api/status 当探针会永远等不到。这两个在 server.py 的放行名单里。
async function probeBackend(ms = 1500) {
  if (!backendUrl) return { ok: false, status: 0, error: '端口未知' };
  for (const path of ['/api/vault/status', '/api/version']) {
    try {
      const r = await fetch(`${backendUrl}${path}`, { signal: AbortSignal.timeout(ms) });
      if (r.ok) return { ok: true, path, status: r.status };
      return { ok: false, path, status: r.status };
    } catch (e) {
      return { ok: false, path, status: 0, error: e.message };
    }
  }
  return { ok: false, status: 0 };
}

function resolvePython() {
  const local = process.env.LOCALAPPDATA || '';
  const candidates = [
    process.env.BFO_PYTHON,
    local && path.join(local, 'BiliFavOrganizer', 'runtime', 'py312', 'Scripts', 'python.exe'),
  ].filter(Boolean);
  for (const c of candidates) if (fs.existsSync(c)) return c;
  return 'python';
}

const HANDSHAKE_TIMEOUT = 30000;

// 等后端那一行 BFO_READY：它既给出真实端口，也等于「已在收连接」的就绪信号
function awaitHandshake(child) {
  return new Promise((resolve, reject) => {
    let buf = '';
    let settled = false;
    const finish = (fn, arg) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener('exit', onExit);
      fn(arg);
    };
    const onExit = code => finish(reject, new Error(`后端在就绪前退出（code=${code}），详见 shell.log`));
    const timer = setTimeout(() => finish(reject, new Error('后端 30 秒内未回传端口')), HANDSHAKE_TIMEOUT);

    child.stdout.on('data', d => {
      buf += String(d);
      let i;
      while (!settled && (i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (!line) continue;
        if (line.startsWith('BFO_READY ')) {
          try {
            finish(resolve, JSON.parse(line.slice('BFO_READY '.length)));
          } catch (e) {
            finish(reject, new Error(`握手行无法解析：${line}（${e.message}）`));
          }
        } else if (line.startsWith('BFO_READY_FAILED')) {
          finish(reject, new Error(line));
        } else {
          logLine('py', line);
        }
      }
    });
    child.stderr.on('data', d => forwardBackend('py!', d));
    child.on('exit', onExit);
  });
}

function waitExit(child, ms) {
  if (child.exitCode !== null || child.signalCode) return Promise.resolve(true);
  return new Promise(resolve => {
    const onExit = () => { clearTimeout(t); resolve(true); };
    const t = setTimeout(() => { child.removeListener('exit', onExit); resolve(false); }, ms);
    child.once('exit', onExit);
  });
}

// 整棵进程树：后端自己也会拉 powershell（Hello/DPAPI 自检），只杀直接子进程会漏
function treeKill(pid) {
  return new Promise(resolve => {
    if (process.platform !== 'win32') {
      try { process.kill(pid, 'SIGKILL'); } catch { /* 已经没了 */ }
      return resolve();
    }
    const k = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    k.on('exit', () => resolve());
    k.on('error', () => resolve());
  });
}

// 收摊：先礼后兵。关掉 stdin 让后端自己走优雅退出（uvicorn timeout_graceful_shutdown=5），
// 超时才整棵树强杀。这样不会留下孤儿后端占着端口与 vault。
async function stopBackend() {
  const child = backend;
  if (!child) return;
  backend = null;
  if (child.exitCode !== null || child.signalCode) return;
  try { child.stdin.end(); } catch { /* 管道已经关了 */ }
  if (await waitExit(child, 7000)) {
    log('Python 后端已优雅退出');
    return;
  }
  log('优雅退出超时，强杀后端进程树');
  await treeKill(child.pid);
}

async function ensureBackend() {
  // 只有固定端口模式才谈得上「复用已在跑的实例」（比如 dev 里手工起的旧后端）
  if (FIXED_PORT) {
    const existing = await probeBackend();
    if (existing.ok) {
      log(`复用已在运行的后端 ${backendUrl}（探针 ${existing.path}）`);
      return;
    }
  }

  const py = resolvePython();
  const port = FIXED_PORT;   // 0 = 交给系统分配
  log(`拉起 Python 后端：${py} server.py --port ${port} --no-browser --handshake`);
  const child = spawn(py, ['server.py', '--port', String(port), '--no-browser', '--handshake'], {
    cwd: REPO_ROOT,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
    env: {
      ...process.env,
      BFO_PARENT_PIPE: '1',      // 壳一死，管道 EOF，后端自己收摊
      PYTHONUNBUFFERED: '1',     // 否则日志卡在 Python 的块缓冲里，落不到 shell.log
      PYTHONIOENCODING: 'utf-8',
    },
  });
  backend = child;
  child.on('exit', (code, sig) => {
    log(`Python 后端退出 code=${code} signal=${sig}`);
    if (backend === child) backend = null;
  });

  let info;
  try {
    info = await awaitHandshake(child);
  } catch (e) {
    await stopBackend();
    throw e;
  }
  backendUrl = `http://127.0.0.1:${info.port}`;
  log(`后端就绪 pid=${info.pid} port=${info.port}`);

  const probe = await probeBackend(3000);
  if (!probe.ok) {
    await stopBackend();
    throw new Error(`后端回传端口 ${info.port}，但端点不可用：${JSON.stringify(probe)}`);
  }
  log(`门禁探针通过（${probe.path} → ${probe.status}）`);
}

/* ---------------- 窗口 ---------------- */
function overlayTheme() {
  const dark = nativeTheme.shouldUseDarkColors;
  return {
    color: '#00000000',
    symbolColor: dark ? '#ffffff' : '#1b1b1b',
    height: CAPTION_H,
  };
}

function createWindow() {
  const saved = readConfig().shell_window || {};
  win = new BrowserWindow({
    width: Number(saved.width) || 1200,
    height: Number(saved.height) || 800,
    x: Number.isFinite(saved.x) ? saved.x : undefined,
    y: Number.isFinite(saved.y) ? saved.y : undefined,
    minWidth: 940,
    minHeight: 640,
    show: false,
    icon: APP_ICON,
    autoHideMenuBar: true,
    titleBarStyle: 'hidden',
    titleBarOverlay: overlayTheme(),
    backgroundColor: '#00000000',
    backgroundMaterial: 'mica',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // 关闭 = 收进托盘，不退出；托盘菜单「退出」才真正退出
  win.on('close', (e) => {
    if (app.isQuitting) return;
    e.preventDefault();
    win.hide();
    log('窗口已关闭，转入托盘运行');
  });

  win.once('ready-to-show', () => {
    win.show();
    const b = win.getBounds();
    log(`窗口就绪 material=mica titleBar=hidden url=${backendUrl} bounds=${b.width}x${b.height}+${b.x}+${b.y}`);
  });
  win.webContents.on('did-fail-load', (_e, code, desc, url) => {
    log(`加载失败 code=${code} ${desc} ${url}`);
  });

  const saveBounds = debounce(() => {
    if (!win || win.isDestroyed() || win.isMaximized() || win.isMinimized()) return;
    patchConfig({ shell_window: win.getBounds() });
  }, 400);
  win.on('resize', saveBounds);
  win.on('move', saveBounds);

  nativeTheme.on('updated', () => {
    if (win && !win.isDestroyed()) win.setTitleBarOverlay(overlayTheme());
  });

  win.loadURL(backendUrl);
}

/* ---------------- 生命周期 ---------------- */
if (!app.requestSingleInstanceLock()) {
  log('已有实例在运行，退出本次启动');
  app.quit();
  return;
}

app.on('second-instance', () => {
  if (win && !win.isDestroyed()) {
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  }
});

Menu.setApplicationMenu(null);

app.whenReady().then(async () => {
  openShellLog();
  log(`壳启动 pid=${process.pid} 端口模式=${FIXED_PORT ? `固定 ${FIXED_PORT}` : '系统分配'}`);
  try {
    await ensureBackend();
  } catch (e) {
    log(`后端不可用：${e.message}`);
    app.quit();
    return;
  }
  registerShellIpc();
  registerTerminalIpc();
  createTray();
  createWindow();
});

app.on('window-all-closed', () => {
  // 收摊期间窗口会先被拆掉，这里不能再触发一轮退出
  if (shuttingDown) return;
  app.quit();
});

app.on('before-quit', (e) => {
  app.isQuitting = true;
  // 收摊期间把所有退出请求都拦下：真正的退出只走收摊末尾的 app.exit(0)，
  // 否则任何一次再入（window-all-closed、重复 app.quit()）都会打断收摊，
  // 把后端变成孤儿。
  e.preventDefault();
  if (shuttingDown) return;
  shuttingDown = true;
  (async () => {
    killAllTerminals();
    // 先拆窗口：页面上的长轮询（登录二维码那种）会一直挂着，不拆的话后端要卡满
    // uvicorn 的 timeout_graceful_shutdown（5s）才强拆连接。拆掉渲染进程等于把这些
    // 连接关掉，后端才可能立刻收摊。destroy() 不触发 close，不会走进「收进托盘」。
    if (win && !win.isDestroyed()) win.destroy();
    await stopBackend();
    log('壳退出');
    app.exit(0);
  })();
});
