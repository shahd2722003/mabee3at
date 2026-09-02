'use strict';
/**
 * Lightweight packaging checks. Plain Node, no dependencies, so `npm test`
 * works before `npm install` too. These verify the PACKAGE, never the
 * application's business rules.
 */
const assert = require('assert');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const p = (...s) => path.join(ROOT, ...s);
const read = (...s) => fs.readFileSync(p(...s), 'utf8');

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  \u2713 ${name}`);
  } catch (err) {
    failures.push({ name, err });
    console.log(`  \u2717 ${name}\n      ${err.message}`);
  }
}

function section(title) {
  console.log(`\n${title}`);
}

// ---------------------------------------------------------------------------
section('1) dist/index.html');

test('dist/index.html exists', () => {
  assert.ok(fs.existsSync(p('dist', 'index.html')), 'dist/index.html is missing');
});

test('dist/index.html is not empty', () => {
  const size = fs.statSync(p('dist', 'index.html')).size;
  assert.ok(size > 500, `dist/index.html is only ${size} bytes`);
});

test('dist/index.html is valid HTML with a script bridge', () => {
  const html = read('dist', 'index.html');
  assert.ok(/<!DOCTYPE html>/i.test(html), 'missing doctype');
  assert.ok(/<\/html>\s*$/i.test(html.trim()), 'unterminated document');
  assert.ok(html.includes('window.mabee3at'), 'shell does not use the preload bridge');
});

// ---------------------------------------------------------------------------
section('2) Electron entry points');

test('main.js exists and parses', () => {
  new (require('vm').Script)(read('main.js'), { filename: 'main.js' });
});

test('preload.js exists and parses', () => {
  new (require('vm').Script)(read('preload.js'), { filename: 'preload.js' });
});

test('main.js loads dist/index.html', () => {
  const src = read('main.js');
  assert.ok(/loadFile\(\s*path\.join\(__dirname,\s*'dist',\s*'index\.html'\)/.test(src),
    'main.js does not loadFile(dist/index.html)');
});

test('preload.js is wired into the window', () => {
  assert.ok(/preload:\s*path\.join\(__dirname,\s*'preload\.js'\)/.test(read('main.js')),
    'BrowserWindow does not reference preload.js');
});

// ---------------------------------------------------------------------------
section('3) Security posture');

test('context isolation on, node integration off, sandbox on', () => {
  const src = read('main.js');
  assert.ok(/contextIsolation:\s*true/.test(src), 'contextIsolation must be true');
  assert.ok(/nodeIntegration:\s*false/.test(src), 'nodeIntegration must be false');
  assert.ok(/sandbox:\s*true/.test(src), 'sandbox must be true');
});

test('preload exposes only the minimal bridge', () => {
  const src = read('preload.js');
  assert.ok(src.includes('contextBridge.exposeInMainWorld'), 'no contextBridge used');
  const allowed = ['onStatus', 'getInfo', 'retry', 'quit'];
  const exposed = [...src.matchAll(/^\s{2}(\w+):/gm)].map((m) => m[1]);
  exposed.forEach((k) => assert.ok(allowed.includes(k), `unexpected API exposed: ${k}`));
  assert.ok(!/require\(['"](fs|child_process|path|os)['"]\)/.test(src),
    'preload must not hand Node modules to the page');
});

// ---------------------------------------------------------------------------
section('4) package.json');

let pkg;
test('package.json is valid JSON', () => {
  pkg = JSON.parse(read('package.json'));
});

test('entry point matches an existing file', () => {
  assert.strictEqual(pkg.main, 'main.js');
  assert.ok(fs.existsSync(p(pkg.main)), 'package.json main does not exist');
});

test('required scripts are present', () => {
  ['test', 'start', 'build:win', 'build:win:portable', 'build:win:all']
    .forEach((s) => assert.ok(pkg.scripts[s], `missing script: ${s}`));
});

test('no script deletes the application source', () => {
  Object.entries(pkg.scripts).forEach(([name, cmd]) => {
    assert.ok(!/\bdist\b/.test(cmd) || !/(rimraf|rm\s+-rf|del\s)/i.test(cmd),
      `script "${name}" would delete dist/`);
  });
  if (fs.existsSync(p('build', 'clean.js'))) {
    const clean = read('build', 'clean.js');
    assert.ok(!/['"]dist['"]/.test(clean), 'clean.js must never touch dist/');
  }
});

// ---------------------------------------------------------------------------
section('5) electron-builder configuration');

test('build config targets Windows x64 with NSIS and portable', () => {
  const b = pkg.build;
  assert.ok(b, 'no build section');
  assert.strictEqual(b.directories.output, 'release', 'output must be release/');
  const targets = b.win.target.map((t) => t.target);
  assert.ok(targets.includes('nsis'), 'nsis target missing');
  assert.ok(targets.includes('portable'), 'portable target missing');
  b.win.target.forEach((t) => assert.ok(t.arch.includes('x64'), 'x64 arch missing'));
});

test('build output folder is not the source folder', () => {
  assert.notStrictEqual(pkg.build.directories.output, 'dist');
});

test('dist/ is packaged into the app bundle', () => {
  assert.ok(pkg.build.files.some((f) => f.startsWith('dist/')), 'dist/ not in files');
});

test('python app, vendor libs and runtime ship as extraResources', () => {
  const dests = pkg.build.extraResources.map((r) => r.to);
  ['app', 'vendor', 'runtime', 'data'].forEach((d) =>
    assert.ok(dests.includes(d), `extraResources missing: ${d}`));
});

// ---------------------------------------------------------------------------
section('6) Required application assets');

test('Flask application files are present', () => {
  ['app.py', 'db.py', 'engine.py', 'filters.py', 'schema.sql', 'seed.py', 'migrate.py', 'server.py']
    .forEach((f) => assert.ok(fs.existsSync(p('app', f)), `missing app/${f}`));
});

test('all Jinja templates are present', () => {
  const dir = p('app', 'templates');
  assert.ok(fs.existsSync(dir), 'templates folder missing');
  const tpl = fs.readdirSync(dir).filter((f) => f.endsWith('.html'));
  assert.ok(tpl.length >= 30, `expected 30+ templates, found ${tpl.length}`);
  ['base.html', 'login.html', 'quick.html', '_filters.html', 'rep_late.html']
    .forEach((f) => assert.ok(tpl.includes(f), `missing template: ${f}`));
});

test('stylesheet is present and non-trivial', () => {
  const css = p('app', 'static', 'style.css');
  assert.ok(fs.existsSync(css), 'style.css missing');
  assert.ok(fs.statSync(css).size > 2000, 'style.css looks truncated');
});

test('vendored python dependencies are present and portable', () => {
  ['flask', 'werkzeug', 'jinja2', 'markupsafe', 'click', 'itsdangerous', 'blinker']
    .forEach((m) => assert.ok(fs.existsSync(p('vendor', m)), `missing vendor/${m}`));
  const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(path.join(dir, e.name)) : [path.join(dir, e.name)]);
  const native = walk(p('vendor')).filter((f) => /\.(so|pyd|dll)$/i.test(f));
  assert.strictEqual(native.length, 0,
    `platform-specific binaries would break the Windows build: ${native.join(', ')}`);
});

test('a self-contained Windows Python runtime is bundled', () => {
  const root = p('runtime', 'python-win-x64');
  assert.ok(fs.existsSync(root), 'runtime/python-win-x64 is missing');
  ['python.exe', 'python312.dll', 'vcruntime140.dll']
    .forEach((f) => assert.ok(fs.existsSync(path.join(root, f)), `runtime missing ${f}`));
  const exe = fs.statSync(path.join(root, 'python.exe'));
  assert.ok(exe.size > 10000, 'python.exe looks like a placeholder');
});

test('runtime carries the modules the application needs', () => {
  const root = p('runtime', 'python-win-x64');
  ['_sqlite3.pyd', 'sqlite3.dll', '_socket.pyd', '_hashlib.pyd', 'select.pyd']
    .forEach((f) => assert.ok(fs.existsSync(path.join(root, 'DLLs', f)),
      `runtime missing DLLs/${f}`));
  ['json', 'email', 'http', 'urllib', 'logging', 'sqlite3']
    .forEach((m) => assert.ok(fs.existsSync(path.join(root, 'Lib', m)),
      `runtime missing stdlib module: ${m}`));
  ['hashlib.py', 'hmac.py', 'secrets.py', 'datetime.py', 'csv.py']
    .forEach((m) => assert.ok(fs.existsSync(path.join(root, 'Lib', m)),
      `runtime missing stdlib module: ${m}`));
});

test('runtime is a genuine Windows build', () => {
  const exe = fs.readFileSync(path.join(p('runtime', 'python-win-x64'), 'python.exe'));
  assert.strictEqual(exe.subarray(0, 2).toString('ascii'), 'MZ',
    'python.exe is not a Windows PE executable');
});

test('main.js prefers the bundled runtime over any system Python', () => {
  const src = read('main.js');
  assert.ok(/function bundledPython\(/.test(src), 'no bundled runtime lookup');
  assert.ok(/python-win-x64['"],\s*['"]python\.exe/.test(src),
    'main.js does not point at the bundled interpreter');
  const bundledAt = src.indexOf('list.push({ cmd: embedded');
  const systemAt = src.indexOf("list.push({ cmd: 'py'");
  assert.ok(bundledAt > -1 && bundledAt < systemAt,
    'the bundled runtime must be tried before system Python');
});

test('vendor libraries load even without PYTHONPATH', () => {
  const src = read('app', 'server.py');
  assert.ok(/VENDOR/.test(src) && /sys\.path\.insert/.test(src),
    'server.py does not add vendor/ to sys.path itself');
});

test('one-click browser launcher exists and is Windows-ready', () => {
  const bat = p('Start-Mabee3at.bat');
  assert.ok(fs.existsSync(bat), 'Start-Mabee3at.bat is missing');
  const raw = fs.readFileSync(bat);
  assert.ok(raw.includes('\r\n'), 'batch file needs CRLF line endings');
  assert.ok(raw[0] !== 0xEF, 'batch file must not start with a UTF-8 BOM');
  const src = raw.toString('utf8');
  assert.ok(/chcp 65001/.test(src), 'launcher must switch the console to UTF-8');
  assert.ok(/runtime\\python-win-x64\\python\.exe/.test(src),
    'launcher does not use the bundled runtime');
  assert.ok(/server\.py/.test(src), 'launcher never starts the server');
  assert.ok(/MABEE3AT_OPEN_BROWSER=1/.test(src), 'launcher does not open the browser');
});

test('browser mode picks a free port and opens the browser', () => {
  const src = read('app', 'server.py');
  assert.ok(/def pick_port/.test(src), 'no free-port selection');
  assert.ok(/webbrowser\.open/.test(src), 'server never opens the browser');
  assert.ok(/MABEE3AT_PORT/.test(src) && /os\.environ\["MABEE3AT_PORT"\]/.test(src),
    'an explicit port from Electron must still be honoured exactly');
});

test('shell page helps when opened directly in a browser', () => {
  const html = read('dist', 'index.html');
  assert.ok(/browserMode/.test(html), 'no browser fallback in the shell page');
  assert.ok(/Start-Mabee3at\.bat/.test(html), 'shell does not point at the launcher');
  assert.ok(/connect-src http:\/\/127\.0\.0\.1:\*/.test(html),
    'CSP must allow probing the local server');
});

test('server entry point starts the real Flask app', () => {
  const src = read('app', 'server.py');
  assert.ok(/from app import app/.test(src), 'server.py does not import the Flask app');
  assert.ok(/app\.run\(/.test(src), 'server.py never starts the server');
  assert.ok(/debug=False/.test(src), 'server must not run in debug mode');
});

// ---------------------------------------------------------------------------
section('7) Backend configuration is the real one, not a demo swap');

test('database path still honours SALES_DB', () => {
  assert.ok(/os\.environ\.get\("SALES_DB"/.test(read('app', 'db.py')),
    'db.py no longer reads SALES_DB — backend target may have been replaced');
});

test('main.js passes a real database path to the server', () => {
  const src = read('main.js');
  assert.ok(/SALES_DB:\s*dbPath/.test(src), 'main.js does not set SALES_DB');
  assert.ok(/process\.env\.SALES_DB/.test(src), 'an external SALES_DB override must win');
});

test('no demo/mock backend was substituted', () => {
  const src = read('main.js') + read('app', 'server.py');
  [/mock/i, /fixture/i, /sample[_-]?data/i, /in[_-]?memory\s*db/i]
    .forEach((re) => assert.ok(!re.test(src), `demo backend marker found: ${re}`));
});

test('authentication, roles and permissions are intact', () => {
  const src = read('app', 'app.py');
  assert.ok(/check_password_hash/.test(src), 'password verification removed');
  assert.ok(/def roles\(/.test(src), 'role decorator removed');
  ['admin', 'accountant', 'rep'].forEach((r) =>
    assert.ok(src.includes(`"${r}"`), `role missing: ${r}`));
  const guarded = (src.match(/@roles\(/g) || []).length;
  const anyAuth = (src.match(/@login_required/g) || []).length;
  assert.ok(guarded + anyAuth >= 20, `too few guarded routes (${guarded + anyAuth})`);
});

test('business logic modules were not stripped', () => {
  const engine = read('app', 'engine.py');
  ['def create_invoice', 'def create_aggregate_invoice', 'def create_collection',
    'def clear_cheque', 'def bounce_cheque', 'def run_late_reversals', 'def run_daily_jobs']
    .forEach((fn) => assert.ok(engine.includes(fn), `engine.py missing: ${fn}`));
});

// ---------------------------------------------------------------------------
section('8) No secrets introduced during packaging');

test('no private keys or service-role credentials in packaged files', () => {
  const targets = ['main.js', 'preload.js', 'package.json', 'dist/index.html', 'app/server.py',
    'app/app.py', 'app/db.py'];
  const patterns = [
    [/service[_-]?role/i, 'service-role reference'],
    [/BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY/, 'private key block'],
    [/\bsk_live_[A-Za-z0-9]{10,}/, 'live secret key'],
    [/eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\./, 'embedded JWT'],
    [/\b(password|passwd|pwd)\s*[:=]\s*['"][^'"]{6,}['"]/i, 'hard-coded password'],
    [/\bAKIA[0-9A-Z]{16}\b/, 'AWS access key'],
  ];
  targets.forEach((f) => {
    if (!fs.existsSync(p(f))) return;
    const src = read(f);
    patterns.forEach(([re, label]) => {
      assert.ok(!re.test(src), `${label} found in ${f}`);
    });
  });
});

test('session key is generated at runtime, never committed', () => {
  const src = read('main.js');
  assert.ok(/crypto\.randomBytes/.test(src), 'session key is not randomly generated');
  assert.ok(!fs.existsSync(p('session.key')), 'a session.key must not ship in the project');
});

test('.gitignore keeps build output and local data out of version control', () => {
  const gi = read('.gitignore');
  ['node_modules', 'release', 'session.key'].forEach((e) =>
    assert.ok(gi.includes(e), `.gitignore missing: ${e}`));
  assert.ok(!/^dist\/?$/m.test(gi), '.gitignore must not exclude dist/ — it is source');
});

// ---------------------------------------------------------------------------
const total = passed + failures.length;
console.log(`\n${'-'.repeat(52)}`);
if (failures.length) {
  console.log(`FAILED — ${passed}/${total} checks passed\n`);
  failures.forEach((f) => console.log(`  \u2717 ${f.name}: ${f.err.message}`));
  process.exit(1);
}
const hash = crypto.createHash('sha256').update(fs.readFileSync(p('dist', 'index.html'))).digest('hex');
console.log(`PASSED — ${passed}/${total} packaging checks`);
console.log(`dist/index.html  ${fs.statSync(p('dist', 'index.html')).size} bytes  sha256:${hash.slice(0, 16)}…`);
process.exit(0);
