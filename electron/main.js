'use strict';
// Electron 壳（T1）：只负责窗口、单实例、以及把现有 Python 后端拉起来。
// 后端契约、动态端口与握手在 T2 处理；这里刻意保持最小。
const { app, BrowserWindow, Menu, Tray, nativeImage, nativeTheme, ipcMain } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const REPO_ROOT = path.join(__dirname, '..');
const APP_ICON = path.join(REPO_ROOT, 'packaging', 'BiliFavOrganizer.ico');
const APP_DATA_ROOT = path.join(process.env.LOCALAPPDATA || app.getPath('appData'), 'BiliFavOrganizer');
const CONFIG_FILE = path.join(APP_DATA_ROOT, 'config.json');
const PORT = Number(process.env.BFO_PORT || 8080);
const BASE_URL = `http://127.0.0.1:${PORT}`;
const CAPTION_H = 32;

// Electron 自己的缓存目录也收在同一个根下，别在 %APPDATA% 另开一处
app.setPath('userData', path.join(APP_DATA_ROOT, 'shell'));

let backend = null;        // 本进程拉起的 Python 后端；复用已有实例时为 null
let win = null;
let tray = null;

const log = (...a) => console.log('[shell]', ...a);

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

/* ---------------- 后端 ---------------- */
// 就绪探针只能用「解锁前也放行」的端点：/api/* 在 vault 未解锁时一律 403，
// 所以拿 /api/status 当探针会永远等不到。这两个在 server.py 的放行名单里。
async function probeBackend(ms = 1500) {
  for (const path of ['/api/vault/status', '/api/version']) {
    try {
      const r = await fetch(`${BASE_URL}${path}`, { signal: AbortSignal.timeout(ms) });
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

async function ensureBackend() {
  const existing = await probeBackend();
  if (existing.ok) {
    log(`复用已在运行的后端 ${BASE_URL}（探针 ${existing.path}）`);
    return;
  }
  const py = resolvePython();
  log(`拉起 Python 后端：${py} server.py --port ${PORT} --no-browser`);
  backend = spawn(py, ['server.py', '--port', String(PORT), '--no-browser'], {
    cwd: REPO_ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  backend.stdout.on('data', d => process.stdout.write(`[py] ${d}`));
  backend.stderr.on('data', d => process.stderr.write(`[py] ${d}`));
  backend.on('exit', (code, sig) => {
    log(`Python 后端退出 code=${code} signal=${sig}`);
    backend = null;
  });

  const deadline = Date.now() + 30000;
  let last = null;
  while (Date.now() < deadline) {
    if (!backend) throw new Error('Python 后端启动后立即退出，见上方日志');
    last = await probeBackend();
    if (last.ok) {
      log(`后端就绪（探针 ${last.path}）`);
      return;
    }
    await new Promise(r => setTimeout(r, 400));
  }
  throw new Error(`后端在 30 秒内未就绪，最后探针结果：${JSON.stringify(last)}`);
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
    log(`窗口就绪 material=mica titleBar=hidden url=${BASE_URL} bounds=${b.width}x${b.height}+${b.x}+${b.y}`);
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

  win.loadURL(BASE_URL);
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
  try {
    await ensureBackend();
  } catch (e) {
    log(`后端不可用：${e.message}`);
    app.quit();
    return;
  }
  createTray();
  createWindow();
});

app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => {
  app.isQuitting = true;
  if (backend) {
    log('关闭 Python 后端');
    backend.kill();
    backend = null;
  }
});
