'use strict';
// Electron 壳：窗口、托盘、单实例，以及托管 Python 后端（拉起 → 握手 → 收摊）。
const { app, BrowserWindow, Menu, Tray, nativeImage, nativeTheme, ipcMain, dialog, shell } = require('electron');
const { spawn } = require('node:child_process');
const { StringDecoder } = require('node:string_decoder');
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

// 后端输出按行转发：一行一条，方便与 shell.log 里的壳日志对照。
// 用 StringDecoder 拼接：直接 String(chunk) 会在多字节字符被 chunk 切断时产生乱码。
const backendDecoders = new WeakMap();

function backendText(stream, chunk) {
  let decoder = backendDecoders.get(stream);
  if (!decoder) {
    decoder = new StringDecoder('utf8');
    backendDecoders.set(stream, decoder);
  }
  return decoder.write(chunk);
}

function forwardBackend(tag, stream, chunk) {
  const text = backendText(stream, chunk);
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
  const fromPath = (name) => {
    for (const dir of dirs) {
      try {
        if (dir && fs.existsSync(path.join(dir, name))) return path.join(dir, name);
      } catch { /* 路径里可能有非法字符，跳过 */ }
    }
    return null;
  };
  // 用系统已装的 PowerShell 7：PATH 里没有就再查标准安装位置，
  // 免得环境变量被裁剪时悄悄掉回 Windows PowerShell 5.1
  const pf = process.env.ProgramFiles || 'C:\\Program Files';
  const local = process.env.LOCALAPPDATA || '';
  const pwsh = [
    fromPath('pwsh.exe'),
    path.join(pf, 'PowerShell', '7', 'pwsh.exe'),
    local && path.join(local, 'Microsoft', 'WindowsApps', 'pwsh.exe'),
  ].filter(Boolean);
  for (const exe of pwsh) {
    try {
      if (fs.existsSync(exe)) return { exe, args: ['-NoLogo'] };
    } catch { /* 跳过不可用的路径 */ }
  }
  const legacy = fromPath('powershell.exe');
  if (legacy) return { exe: legacy, args: ['-NoLogo'] };
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
  // 渲染进程的主题（自动/浅色/深色）决定标题栏按钮配色
  ipcMain.on('shell:set-theme', (_e, payload) => {
    const mode = typeof payload === 'string' ? payload : payload && payload.mode;
    const next = ['light', 'dark', 'system'].includes(mode) ? mode : 'system';
    const changed = next !== themeMode;
    themeMode = next;
    // 关键：那三个按钮的悬停/按下高亮由 Chromium 按「应用主题」画，不是我们 CSS 画的。
    // 系统是浅色而应用选了深色时，它会画浅色主题的深色高亮 —— 压在深色标题栏上等于看不见。
    // 把应用的明暗也告诉它，按钮反馈才跟得上。
    if (nativeTheme.themeSource !== next) nativeTheme.themeSource = next;
    if (changed) applyOverlay();
  });

  ipcMain.handle('shell:external-link-mode', (_event, mode) => {
    if (mode === undefined) return readConfig().external_link_mode === 'internal' ? 'internal' : 'system';
    if (!['internal', 'system'].includes(mode)) return { ok: false };
    patchConfig({ external_link_mode: mode });
    return { ok: true, mode };
  });

  // Windows Hello 的验证框由系统的凭据 broker（CredentialUIBroker.exe）绘制，不是本窗口的子窗口。
  // 它属于更高完整性级别，我们**没有权限**把那个窗口置顶（实测 SetWindowPos 返回 ACCESS_DENIED，
  // 这是 UIPI 的策略，不是参数问题），也没法让它一出现就是前台。
  // 既然抬不动它，就让自己让开：验证期间把本窗口最小化，结束后还原并抢回焦点。
  ipcMain.handle('shell:hello-aside', () => {
    if (win && !win.isDestroyed()) {
      win.setAlwaysOnTop(false);
      win.minimize();
      log('Hello 验证开始：本窗口已让开');
    }
    return { ok: true };
  });

  ipcMain.handle('shell:hello-restore', () => {
    if (win && !win.isDestroyed()) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
      log('Hello 验证结束：本窗口已还原');
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

/* ---------------- 数据包导出/导入 ---------------- */
// 导出走「原生保存对话框 + 主进程落盘」：页面里的隐藏 iframe 只能丢进浏览器下载目录，
// 既不能选路径，也拿不到进度，更没法取消。
let downloadAbort = null;

function stamp() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`;
}

async function streamToFile(url, filePath, headers, signal) {
  const res = await fetch(url, { headers, signal });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const total = Number(res.headers.get('content-length') || 0);
  let received = 0;
  let lastSent = 0;
  const out = fs.createWriteStream(filePath);
  const reader = res.body.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.length;
      await new Promise((resolve, reject) => {
        out.write(Buffer.from(value), err => (err ? reject(err) : resolve()));
      });
      // 分块很密，节流到 ~10 次/秒，别把 IPC 灌满
      const now = Date.now();
      if (now - lastSent > 100 && win && !win.isDestroyed()) {
        lastSent = now;
        win.webContents.send('data:progress', { received, total });
      }
    }
  } finally {
    await new Promise(resolve => out.end(resolve));
  }
  if (win && !win.isDestroyed()) win.webContents.send('data:progress', { received, total });
  return { bytes: received, total };
}

function registerDataIpc() {
  ipcMain.handle('data:choose-save', async () => {
    if (!win || win.isDestroyed()) return { ok: false, error: 'no-window' };
    const { canceled, filePath } = await dialog.showSaveDialog(win, {
      title: '导出项目数据',
      message: '此包为本机数据目录整包（明文）：含明文数据库、B 站 Cookie 与 API Key，请勿上传网盘',
      defaultPath: path.join(app.getPath('downloads'), `BiliFavOrganizer-${stamp()}.zip`),
      filters: [{ name: '项目数据包', extensions: ['zip'] }],
    });
    if (canceled || !filePath) return { ok: false, canceled: true };
    return { ok: true, filePath };
  });

  // 只接受本机后端地址，避免把主进程当任意下载器用
  ipcMain.handle('data:download', async (_e, opts) => {
    const url = String((opts && opts.url) || '');
    const filePath = String((opts && opts.filePath) || '');
    if (!backendUrl || !url.startsWith(`${backendUrl}/`)) return { ok: false, error: 'url' };
    if (!filePath) return { ok: false, error: 'path' };
    downloadAbort = new AbortController();
    try {
      const r = await streamToFile(url, filePath, (opts && opts.headers) || {}, downloadAbort.signal);
      log(`导出完成 ${filePath}（${r.bytes} 字节）`);
      return { ok: true, bytes: r.bytes, filePath };
    } catch (e) {
      const aborted = downloadAbort?.signal.aborted;
      try { fs.rmSync(filePath, { force: true }); } catch { /* 没写成就算了 */ }
      log(`导出${aborted ? '已取消' : '失败'}：${e.message}`);
      return { ok: false, canceled: Boolean(aborted), error: e.message };
    } finally {
      downloadAbort = null;
    }
  });

  ipcMain.on('data:cancel-download', () => {
    if (downloadAbort) downloadAbort.abort();
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

// 随包的冻结后端：打包版把它放在 resources/backend（electron-builder 的 extraResources），
// 有它就不用源码树与系统 Python 了。dev 下要试这条路可以设 BFO_BACKEND_EXE 指过去。
function resolveFrozenBackend() {
  const explicit = process.env.BFO_BACKEND_EXE;
  if (explicit && fs.existsSync(explicit)) return explicit;
  if (!app.isPackaged) return null;
  const bundled = path.join(process.resourcesPath, 'backend', 'BiliFavOrganizer.exe');
  return fs.existsSync(bundled) ? bundled : null;
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
      buf += backendText(child.stdout, d);
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
    child.stderr.on('data', d => forwardBackend('py!', child.stderr, d));
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
  const frozen = resolveFrozenBackend();
  const cmd = frozen || py;
  const args = frozen
    ? ['--port', String(port), '--no-browser', '--handshake']
    : ['server.py', '--port', String(port), '--no-browser', '--handshake'];
  log(`拉起后端：${cmd} ${args.join(' ')}${frozen ? '（随包冻结后端）' : ''}`);
  const child = spawn(cmd, args, {
    cwd: frozen ? path.dirname(cmd) : REPO_ROOT,
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
// 标题栏按钮的配色不能只看系统主题：应用自己可以选浅色/深色（渲染进程的 data-theme），
// 系统是浅色而应用切到深色时，跟着系统走会让按钮变成深色压在深底上——看不见。
// 所以由渲染进程把生效的 mode 报上来，系统主题只在 mode=system（自动）时才参与。
let themeMode = 'system';

function overlayTheme() {
  const dark = themeMode === 'dark'
    || (themeMode === 'system' && nativeTheme.shouldUseDarkColors);
  return {
    // 底色保持透明：按钮和它的悬停/按下层都由 Chromium 自己画在标题栏之上，
    // 给不透明底色反而会让标题栏变成唯一一块平板，跟别处的毛玻璃不搭。
    color: '#00000000',
    symbolColor: dark ? '#ffffff' : '#1b1b1b',
    height: CAPTION_H,
  };
}

function applyOverlay() {
  if (!win || win.isDestroyed()) return;
  const theme = overlayTheme();
  win.setTitleBarOverlay(theme);
  log(`标题栏配色更新 symbolColor=${theme.symbolColor} bg=${theme.color} mode=${themeMode}`);
}

// 系统明暗变化（自动模式下生效）：光重算符号色不够 —— 顶栏底色是页面算出来报上来的，
// 系统换了明暗页面才知道，所以还要让页面重新上报一次，否则按钮区域停在旧配色上。
function onSystemThemeChanged() {
  applyOverlay();
  if (win && !win.isDestroyed()) win.webContents.send('shell:system-theme-changed');
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

  // 外部 B 站页面必须交给系统默认浏览器。不要向网页窗口注入保存在应用内的 Cookie。
  win.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const parsed = new URL(url);
      if (parsed.protocol === 'https:' && parsed.hostname === 'www.bilibili.com') {
        if (readConfig().external_link_mode === 'internal') return { action: 'allow' };
        void shell.openExternal(parsed.href).catch((err) => log('外部链接打开失败: ' + err.message));
      } else {
        log('拒绝未知窗口请求');
      }
    } catch (_err) {
      log('拒绝无效窗口请求');
    }
    return { action: 'deny' };
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

  nativeTheme.on('updated', onSystemThemeChanged);

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
  registerDataIpc();
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
