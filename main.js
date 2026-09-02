'use strict';
/**
 * Mabee3at Desktop — Electron main process.
 *
 * This wrapper does NOT reimplement the application. It starts the existing
 * Flask application (app/app.py) as a local child process and displays it in a
 * desktop window. All business logic, authentication, roles and the SQLite
 * database remain exactly as they are in the web application.
 */
const { app, BrowserWindow, ipcMain, shell, dialog } = require('electron');
const { spawn, spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const net = require('net');
const path = require('path');

const HOST = '127.0.0.1';
const STARTUP_TIMEOUT_MS = 60000;

let mainWindow = null;
let serverProcess = null;
let serverUrl = null;
let shuttingDown = false;
let lastError = null;

// ---------------------------------------------------------------------------
// Paths — packaged builds keep the Python application outside the asar archive
// ---------------------------------------------------------------------------
const resourceRoot = app.isPackaged ? process.resourcesPath : __dirname;
const APP_DIR = path.join(resourceRoot, 'app');
const VENDOR_DIR = path.join(resourceRoot, 'vendor');
const RUNTIME_DIR = path.join(resourceRoot, 'runtime');
const BUNDLED_DB = path.join(resourceRoot, 'data', 'sales.db');

/**
 * Database location.
 *   SALES_DB (already supported by app/db.py) always wins — point it at a
 *   shared network path to let several users work on one database.
 *   Otherwise the database lives in the per-user data folder so that the
 *   installed application can write to it.
 */
function resolveDatabasePath() {
  if (process.env.SALES_DB) return process.env.SALES_DB;
  if (!app.isPackaged) return path.join(__dirname, 'data', 'sales.db');

  const target = path.join(app.getPath('userData'), 'data', 'sales.db');
  if (!fs.existsSync(target)) {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    if (fs.existsSync(BUNDLED_DB)) fs.copyFileSync(BUNDLED_DB, target);
  }
  return target;
}

/** A stable per-installation Flask session key, so logins survive restarts. */
function resolveSecretKey() {
  if (process.env.SECRET_KEY) return process.env.SECRET_KEY;
  const file = path.join(app.getPath('userData'), 'session.key');
  try {
    if (fs.existsSync(file)) return fs.readFileSync(file, 'utf8').trim();
    const key = crypto.randomBytes(32).toString('hex');
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, key, { mode: 0o600 });
    return key;
  } catch (err) {
    return crypto.randomBytes(32).toString('hex');
  }
}

// ---------------------------------------------------------------------------
// Python discovery — the bundled runtime always comes first
// ---------------------------------------------------------------------------
/** Interpreter shipped inside the application, so no system Python is needed. */
function bundledPython() {
  const candidates = process.platform === 'win32'
    ? [path.join(RUNTIME_DIR, 'python-win-x64', 'python.exe')]
    : [
      path.join(RUNTIME_DIR, 'python-linux-x64', 'bin', 'python3'),
      path.join(RUNTIME_DIR, 'python-mac-arm64', 'bin', 'python3'),
    ];
  return candidates.find((c) => fs.existsSync(c)) || null;
}

function pythonCandidates() {
  if (process.env.MABEE3AT_PYTHON) {
    return [{ cmd: process.env.MABEE3AT_PYTHON, args: [], bundled: false }];
  }

  const list = [];
  const embedded = bundledPython();
  if (embedded) list.push({ cmd: embedded, args: [], bundled: true });

  // Fallbacks, used only if the bundled runtime is missing or unusable.
  if (process.platform === 'win32') {
    list.push({ cmd: 'py', args: ['-3'], bundled: false });
    list.push({ cmd: 'python', args: [], bundled: false });
    list.push({ cmd: 'python3', args: [], bundled: false });
  } else {
    list.push({ cmd: 'python3', args: [], bundled: false });
    list.push({ cmd: 'python', args: [], bundled: false });
  }
  return list;
}

function findPython() {
  for (const cand of pythonCandidates()) {
    try {
      const res = spawnSync(cand.cmd, [...cand.args, '--version'], {
        encoding: 'utf8',
        windowsHide: true,
        timeout: 10000,
      });
      if (res.status === 0) {
        const version = ((res.stdout || '') + (res.stderr || '')).trim();
        const m = version.match(/(\d+)\.(\d+)/) || [];
        const major = parseInt(m[1] || '0', 10);
        const minor = parseInt(m[2] || '0', 10);
        if (major > 3 || (major === 3 && minor >= 9)) return { ...cand, version };
      }
    } catch (_) { /* try the next candidate */ }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Server lifecycle
// ---------------------------------------------------------------------------
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, HOST, () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function ping(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve(res.statusCode > 0);
    });
    req.on('error', () => resolve(false));
    req.setTimeout(2000, () => { req.destroy(); resolve(false); });
  });
}

async function waitForServer(url) {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (shuttingDown) return false;
    if (await ping(url + '/login')) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

function status(state, message, detail) {
  lastError = state === 'error' ? { message, detail } : null;
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('server-status', { state, message, detail, url: serverUrl });
  }
}

async function startServer() {
  const python = findPython();
  if (!python) {
    status('error', 'تعذّر تشغيل بيئة التشغيل المضمّنة.',
      'النسخة المضمّنة من Python غير موجودة أو تالفة، ولا توجد نسخة على الجهاز كبديل.\n'
      + `المسار المتوقع: ${path.join(RUNTIME_DIR, 'python-win-x64', 'python.exe')}\n`
      + 'أعد تثبيت البرنامج، أو حدّد مساراً بديلاً بمتغيّر البيئة MABEE3AT_PYTHON.');
    return;
  }

  if (!fs.existsSync(path.join(APP_DIR, 'app.py'))) {
    status('error', 'ملفات التطبيق غير موجودة.', `المسار المتوقع: ${APP_DIR}`);
    return;
  }

  status('starting', python.bundled
    ? `تشغيل الخادم المحلي… (${python.version} — نسخة مضمّنة)`
    : `تشغيل الخادم المحلي… (${python.version} — نسخة الجهاز)`);

  let port;
  try {
    port = await freePort();
  } catch (err) {
    status('error', 'تعذّر حجز منفذ محلي.', String(err));
    return;
  }
  serverUrl = `http://${HOST}:${port}`;

  const dbPath = resolveDatabasePath();
  const env = {
    ...process.env,
    SALES_DB: dbPath,
    PYTHONNOUSERSITE: '1',
    SECRET_KEY: resolveSecretKey(),
    PYTHONPATH: [VENDOR_DIR, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
    MABEE3AT_HOST: HOST,
    MABEE3AT_PORT: String(port),
  };

  const pyArgs = [...python.args];
  // -s فقط: يتجاهل site-packages الخاصة بالمستخدم دون أن يبطل PYTHONPATH
  if (python.bundled) pyArgs.push('-s');
  pyArgs.push('server.py');

  // النسخة المضمّنة يجب ألا ترث PYTHONHOME من تثبيت آخر على الجهاز
  if (python.bundled) delete env.PYTHONHOME;

  serverProcess = spawn(python.cmd, pyArgs, {
    cwd: APP_DIR,
    env,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  let stderrTail = '';
  serverProcess.stdout.on('data', (d) => process.stdout.write(`[flask] ${d}`));
  serverProcess.stderr.on('data', (d) => {
    stderrTail = (stderrTail + d.toString()).slice(-4000);
    process.stderr.write(`[flask] ${d}`);
  });
  serverProcess.on('error', (err) => {
    status('error', 'تعذّر تشغيل خادم التطبيق.', String(err));
  });
  serverProcess.on('exit', (code) => {
    serverProcess = null;
    if (!shuttingDown && code !== 0) {
      status('error', `توقّف خادم التطبيق (رمز ${code}).`, stderrTail.trim());
    }
  });

  const up = await waitForServer(serverUrl);
  if (shuttingDown) return;
  if (!up) {
    status('error', 'لم يستجب خادم التطبيق خلال المهلة المحددة.', stderrTail.trim());
    return;
  }

  status('ready', 'جاهز');
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.loadURL(serverUrl);
  }
}

function stopServer() {
  shuttingDown = true;
  if (!serverProcess) return;
  const proc = serverProcess;
  serverProcess = null;
  try {
    if (process.platform === 'win32') {
      spawnSync('taskkill', ['/pid', String(proc.pid), '/f', '/t'], { windowsHide: true });
    } else {
      proc.kill('SIGTERM');
      setTimeout(() => { try { proc.kill('SIGKILL'); } catch (_) {} }, 3000).unref();
    }
  } catch (_) { /* the process is already gone */ }
}

// ---------------------------------------------------------------------------
// Window
// ---------------------------------------------------------------------------
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 680,
    show: false,
    backgroundColor: '#EDF1F5',
    title: 'نظام إدارة المبيعات',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      nodeIntegrationInWorker: false,
      sandbox: true,
      webviewTag: false,
      spellcheck: false,
    },
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.loadFile(path.join(__dirname, 'dist', 'index.html'));

  // Keep the app inside its own local server; anything else opens in the browser.
  const isOwn = (url) => serverUrl && url.startsWith(serverUrl);
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!isOwn(url)) { shell.openExternal(url); return { action: 'deny' }; }
    return { action: 'allow' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!isOwn(url) && !url.startsWith('file://')) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  mainWindow.on('closed', () => { mainWindow = null; });
}

// ---------------------------------------------------------------------------
// IPC — the small surface exposed through preload.js
// ---------------------------------------------------------------------------
ipcMain.handle('app-info', () => ({
  version: app.getVersion(),
  serverUrl,
  dataDir: app.isPackaged ? app.getPath('userData') : __dirname,
  dbPath: process.env.SALES_DB || null,
  error: lastError,
}));

ipcMain.handle('retry', async () => {
  if (serverProcess) return true;
  shuttingDown = false;
  await startServer();
  return true;
});

ipcMain.handle('quit', () => { app.quit(); });

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    createWindow();
    startServer().catch((err) => {
      status('error', 'خطأ غير متوقع عند بدء التشغيل.', String(err));
    });

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', () => {
    stopServer();
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', stopServer);
  process.on('exit', stopServer);
}
