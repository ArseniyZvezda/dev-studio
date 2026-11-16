// VS-43 / dev spec §5.3.2. Opt-in Chromium test, not desktop tabs or backend E2E.
// node desktop/test/plugin-project-scope.cjs --run-native [--record] [--precompute-urls]
// Prerequisites: frontend npm ci; installed Windows Studio (or VS43_INSTALLED_DIR).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { spawn, execFile, execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '../..');
const fixtures = path.join(__dirname, 'fixtures/plugin-project-scope');
const temp = process.env.VS43_TEMP_DIR || 'D:\\VoxariumStudio\\.voxarium_studio\\temp\\TASK-43';
const hash = value => createHash('sha256').update(value).digest('hex');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function baseline() {
  const staticRoot = path.join(root, 'backend/voxarium_studio/static');
  const files = [];
  function visit(dir) {
    if (!fs.existsSync(dir)) return;
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const file = path.join(dir, entry.name);
      if (entry.isDirectory()) visit(file);
      else files.push([path.relative(staticRoot, file), hash(fs.readFileSync(file))]);
    }
  }
  visit(staticRoot);
  // netstat can transiently show LISTENING owner 0 on Windows. Compare actual
  // backend process identities/creation times instead of unstable socket rows.
  const backendProcesses = JSON.parse(execFileSync('powershell.exe', ['-NoProfile', '-Command',
    `$items = @(Get-CimInstance Win32_Process -Filter "name = 'python.exe'" | Where-Object { $_.CommandLine -match '(voxarium_studio|uvicorn)' -and $_.CommandLine -match '(--port[= ]+8765|:8765)' } | Sort-Object ProcessId | Select-Object ProcessId,CreationDate,CommandLine); ConvertTo-Json -InputObject $items -Compress`],
    { encoding: 'utf8', windowsHide: true }));
  return { staticDigest: hash(JSON.stringify(files.sort())), staticFiles: files.length, backendProcesses };
}

async function launch() {
  if (!process.argv.includes('--run-native')) {
    console.log('Opt-in: node desktop/test/plugin-project-scope.cjs --run-native [--record] [--precompute-urls]'); return;
  }
  assert.equal(process.platform, 'win32');
  const run = path.join(temp, `run-${new Date().toISOString().replace(/[:.]/g, '-')}-${process.pid}`);
  fs.mkdirSync(run, { recursive: true });
  console.log(`VS-43 evidence: ${run}`);
  const before = baseline();
  const req = createRequire(path.join(root, 'frontend/package.json'));
  const { build } = await import(pathToFileURL(req.resolve('vite')).href);
  const { default: vue } = await import(pathToFileURL(req.resolve('@vitejs/plugin-vue')).href);
  const sources = {};
  for (const file of [__filename, ...['index.html', 'renderer.js', 'component.js'].map(name => path.join(fixtures, name))]) {
    sources[path.relative(root, file).replaceAll('\\', '/')] = hash(fs.readFileSync(file));
  }
  const buildLog = message => fs.appendFileSync(path.join(run, 'build.log'), String(message) + '\n');
  await build({ configFile: false, root: fixtures, base: '/', plugins: [vue(), {
    name: 'vs43-source-evidence',
    generateBundle() {
      for (const name of this.getModuleIds()) {
        const clean = name.split('?')[0];
        if (clean.startsWith(path.join(root, 'frontend/src').replaceAll('\\', '/')) && fs.existsSync(clean)) {
          sources[path.relative(root, clean).replaceAll('\\', '/')] = hash(fs.readFileSync(clean));
        }
      }
    },
  }], define: { __STUDIO_VERSION__: JSON.stringify('vs43-fixture'),
    __VUE_OPTIONS_API__: true, __VUE_PROD_DEVTOOLS__: false, __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: false },
  resolve: { alias: [
    { find: '@', replacement: path.join(root, 'frontend/src') },
    { find: /^vue$/, replacement: path.join(root, 'frontend/node_modules/vue/dist/vue.runtime.esm-bundler.js') },
    { find: /^vue-router$/, replacement: path.join(root, 'frontend/node_modules/vue-router/dist/vue-router.js') },
  ] },
  customLogger: { info: buildLog, warn: buildLog, warnOnce: buildLog, error: buildLog,
    clearScreen() {}, hasWarned: false, hasErrorLogged() { return false; } },
  build: { outDir: path.join(run, 'build'), emptyOutDir: true, sourcemap: true, minify: false },
  });
  for (const required of ['frontend/src/plugin-runtime/loader.ts', 'frontend/src/plugin-runtime/index.ts',
    'frontend/src/lib/highlight/highlighter.ts', 'frontend/src/ws/connection.ts']) assert.ok(sources[required], `Production module bundled: ${required}`);
  fs.writeFileSync(path.join(run, 'sources.json'), JSON.stringify(sources, null, 2));

  // A packaged exe ignores CLI app paths. Copy ONLY Electron's runtime to task temp,
  // replacing resources/app, never loading Studio main.cjs or its backend launcher.
  const installed = process.env.VS43_INSTALLED_DIR || path.join(process.env.LOCALAPPDATA, 'Programs/Voxarium Studio');
  const runtime = path.join(run, 'runtime');
  fs.mkdirSync(path.join(runtime, 'resources/app'), { recursive: true });
  for (const entry of fs.readdirSync(installed, { withFileTypes: true })) {
    if (entry.isFile() && !/^uninstall/i.test(entry.name)) fs.copyFileSync(path.join(installed, entry.name), path.join(runtime, entry.name));
  }
  fs.cpSync(path.join(installed, 'locales'), path.join(runtime, 'locales'), { recursive: true });
  fs.writeFileSync(path.join(runtime, 'resources/app/package.json'), JSON.stringify({ name: 'vs43-plugin-fixture', version: '0.0.0', main: 'entry.cjs' }));
  fs.writeFileSync(path.join(runtime, 'resources/app/entry.cjs'), `require(${JSON.stringify(__filename)});`);
  const osTemp = path.join(run, 'os-temp'); fs.mkdirSync(osTemp);
  const env = { ...process.env, VS43_WORKER: '1', VS43_RUN: run, TEMP: osTemp, TMP: osTemp,
    VS43_PRECOMPUTE: process.argv.includes('--precompute-urls') ? '1' : '0' };
  delete env.ELECTRON_RUN_AS_NODE; delete env.NODE_OPTIONS;
  const exe = path.join(runtime, 'Voxarium Studio.exe');
  const log = fs.openSync(path.join(run, 'electron.log'), 'a');
  const child = spawn(exe, [], { env, stdio: ['ignore', log, log], windowsHide: true });
  const code = await new Promise((resolve, reject) => {
    const watchdog = setTimeout(() => {
      // Kill only the launched helper tree; never Studio by process name.
      execFile('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true }, () => {});
      reject(new Error('VS-43 helper exceeded 180 seconds'));
    }, 180_000);
    child.once('error', error => { clearTimeout(watchdog); reject(error); });
    child.once('exit', code => { clearTimeout(watchdog); resolve(code); });
  });
  fs.closeSync(log);
  const after = baseline();
  const helperRemaining = JSON.parse(execFileSync('powershell.exe', ['-NoProfile', '-Command',
    `$items = @(Get-CimInstance Win32_Process -Filter "name = 'Voxarium Studio.exe'" | Where-Object { $_.ExecutablePath -eq '${exe.replaceAll("'", "''")}' } | Select-Object ProcessId,ExecutablePath); ConvertTo-Json -InputObject $items -Compress`],
    { encoding: 'utf8', windowsHide: true }));
  const lifecycle = { helperPid: child.pid, helperExited: true, helperExitCode: code,
    helperRemaining,
    sourceExeSha256: hash(fs.readFileSync(path.join(installed, 'Voxarium Studio.exe'))),
    copiedExeSha256: hash(fs.readFileSync(exe)), before, after };
  fs.writeFileSync(path.join(run, 'lifecycle.json'), JSON.stringify(lifecycle, null, 2));
  assert.deepEqual(after, before, 'Working :8765 listener and production static are unchanged');
  const result = JSON.parse(fs.readFileSync(path.join(run, 'result.json'), 'utf8'));
  assert.equal(helperRemaining.length, 0, 'No helper renderer/GPU/utility process survives');
  const evidence = { ...result, sources, lifecycle };
  fs.writeFileSync(path.join(run, 'evidence.json'), JSON.stringify(evidence, null, 2));
  if (process.argv.includes('--record')) fs.writeFileSync(path.join(fixtures,
    process.argv.includes('--precompute-urls') ? 'evidence-precomputed.json' : 'evidence.json'), JSON.stringify(evidence, null, 2) + '\n');
  assert.equal(code, 0, `Chromium assertions failed; see ${path.join(run, 'result.json')}`);
  assert.equal(result.status, 'passed');
  console.log(`PASS: ${result.assertions.length} assertions; helper exited; :8765 backend identity/static unchanged.`);
}

async function nativeMain() {
  const { app, BrowserWindow, protocol, session: electronSession } = require('electron');
  const run = process.env.VS43_RUN;
  assert.ok(path.resolve(run).startsWith(path.resolve(temp) + path.sep));
  app.setName('VS-43 isolated Chromium plugin harness');
  for (const key of ['appData', 'userData', 'sessionData', 'crashDumps']) {
    const dir = path.join(run, key); fs.mkdirSync(dir, { recursive: true }); app.setPath(key, dir);
  }
  app.setAppLogsPath(path.join(run, 'logs'));
  app.commandLine.appendSwitch('disable-background-networking');
  app.commandLine.appendSwitch('disable-component-update');
  protocol.registerSchemesAsPrivileged([{ scheme: 'fixture', privileges: {
    standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, codeCache: true,
  } }]);
  await app.whenReady();
  const result = { task: 'VS-43', at: new Date().toISOString(), status: 'running',
    mode: process.env.VS43_PRECOMPUTE === '1' ? 'control: fixture URLs precomputed outside render' : 'strict: fixture runtime URLs in render',
    transport: 'Electron fixture protocol; native import/fetch/CSS/DOM/Shiki; injected envelopes through production WsConnection.dispatch, not network WS or backend E2E; not desktop tabs',
    versions: process.versions, assertions: [], snapshots: {}, requests: [], console: [], expectedWarnings: [], violations: [] };
  const windows = {};
  let stage = 'bootstrap';
  const configs = { A: { revision: 1, enabled: true }, B: { revision: 1, enabled: true } };
  const gates = new Map();
  function gate(key) {
    let release;
    const promise = new Promise(resolve => { release = resolve; });
    gates.set(key, { promise, release, hits: 0 });
  }
  function release(key) { const value = gates.get(key); assert.ok(value, key); value.release(); gates.delete(key); }
  gate('A:/plugins/pages'); gate('A:page.js');
  function check(name, condition, details) {
    if (!condition) result.assertions.push({ name, passed: false, details });
    assert.ok(condition, `${name}: ${JSON.stringify(details)}`);
    result.assertions.push({ name, passed: true, ...(details === undefined ? {} : { details }) });
  }
  function descriptor(id, name) {
    return `/api/projects/${id}/plugins/same-plugin/assets/${name}?v=same-stamp&project_id=${id}&epoch=epoch-${id}`;
  }
  function inventory(id) { return [{ id: 'same-plugin', enabled: configs[id].enabled, status: 'loaded' }]; }
  const response = (body, mime = 'application/json', status = 200) => new Response(
    mime === 'application/json' ? JSON.stringify(body) : body,
    { status, headers: { 'Content-Type': mime, 'Cache-Control': 'no-store' } });
  const partition = electronSession.fromPartition('vs43-fixture');
  // No actual network request (including :8765) can escape the isolated session.
  partition.webRequest.onBeforeRequest((details, done) => {
    const allowed = details.url.startsWith('fixture://studio/');
    if (!allowed) result.violations.push({ type: 'external-network', url: details.url });
    done({ cancel: !allowed });
  });
  await partition.protocol.handle('fixture', async request => {
    const url = new URL(request.url);
    if (/^\/p\/[AB]\/$/.test(url.pathname)) return response(fs.readFileSync(path.join(run, 'build/index.html')), 'text/html');
    if (url.pathname.startsWith('/assets/')) {
      const file = path.resolve(run, 'build', '.' + url.pathname);
      if (!file.startsWith(path.join(run, 'build') + path.sep) || !fs.existsSync(file)) return response('missing', 'text/plain', 404);
      return response(fs.readFileSync(file), file.endsWith('.css') ? 'text/css' : file.endsWith('.map') ? 'application/json' : 'text/javascript');
    }
    const match = /^\/api\/projects\/([AB])(\/.*)$/.exec(url.pathname);
    if (!match) { result.violations.push({ type: 'unknown-url', url: request.url }); return response({ detail: 'Unknown fixture URL' }, undefined, 404); }
    const [, id, endpoint] = match;
    const config = { ...configs[id] }; // responses retain the version at request start
    const label = `${id}${config.revision}`;
    const asset = /^\/plugins\/same-plugin\/assets\/(.+)$/.exec(endpoint)?.[1];
    const record = { id, endpoint, url: request.url, method: request.method, label,
      authorization: request.headers.get('authorization'), started: Date.now() };
    result.requests.push(record);
    if (url.searchParams.get('project_id') !== id || url.searchParams.get('epoch') !== 'epoch-' + id
      || !(url.searchParams.get('token') === 'fixture secret /&' || request.headers.get('authorization') === 'Bearer fixture secret /&')) {
      result.violations.push({ type: 'scope-or-auth', ...record });
      return response({ detail: 'Bad scope/auth' }, undefined, 403);
    }
    if (request.method !== 'GET') {
      const body = await request.json(); record.body = body;
      const origin = body.origin || body.settings?.origin;
      if (!origin?.startsWith(id)) result.violations.push({ type: 'cross-project-write', ...record });
    }
    const held = gates.get(`${id}:${asset || endpoint}`);
    if (held) { held.hits++; await held.promise; }
    record.completed = Date.now();
    if (asset?.endsWith('.js')) return response(fs.readFileSync(path.join(fixtures, 'component.js'), 'utf8')
      .replaceAll('__LABEL__', label).replaceAll('__KIND__', asset.replace('.js', '')), 'text/javascript');
    const color = id === 'A' ? 'rgb(180, 20, 30)' : 'rgb(20, 60, 190)';
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><rect width="24" height="24" fill="${color}"/></svg>`;
    if (asset === 'pixel.svg' || endpoint === '/explorer/raw') return response(svg, 'image/svg+xml');
    if (asset === 'style.css') return response(`.fixture-page,.fixture-node,.fixture-previewer { color: ${color}; background-color: ${id === 'A' ? 'rgb(250, 220, 220)' : 'rgb(220, 230, 250)'}; padding: 8px; margin: 4px; }`, 'text/css');
    if (asset === 'grammar.json') return response({ scopeName: 'source.same-fixture', patterns: [
      { match: id === 'A' ? 'ALPHA' : 'BETA', name: 'keyword.control.fixture' },
    ] });
    if (endpoint === '/plugins/pages') return response(config.enabled ? [{ id: 'same-page', plugin_id: 'same-plugin', title: 'Same page',
      route: config.badPages ? '/p/invalid' : '/same-page', icon: null, component_url: descriptor(id, 'page.js') }] : []);
    if (endpoint === '/plugins') return response(inventory(id));
    if (endpoint === '/plugins/grammars') return response(config.enabled ? [{ id: 'same-language', plugin_id: 'same-plugin', name: 'Same language',
      extensions: ['.fixture'], scope_name: 'source.same-fixture', aliases: [], grammar_url: descriptor(id, 'grammar.json') }] : []);
    if (endpoint === '/explorer/file') return response({ kind: 'image', path: `${label}:${url.searchParams.get('path')}`,
      previewer: { id: 'same-previewer', plugin_id: 'same-plugin', extensions: ['.fixture'], component_url: descriptor(id, 'previewer.js') } });
    if (endpoint === '/plugins/same-plugin/settings') return response({ id: 'same-plugin', settings: { origin: label } });
    if (endpoint === '/plugins/same-plugin/actions/record') return response({ origin: label });
    result.violations.push({ type: 'unknown-endpoint', ...record });
    return response({ detail: 'Unknown endpoint' }, undefined, 404);
  });
  const call = (id, method, ...args) => windows[id].webContents.executeJavaScript(`window.harness[${JSON.stringify(method)}](...${JSON.stringify(args)})`);
  const snap = id => call(id, 'snapshot');
  async function until(name, probe) {
    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline) { if (await probe()) return; await delay(30); }
    throw new Error(`Timed out: ${name}`);
  }
  async function domReady(id, page = true, label = `${id}${configs[id].revision}`) {
    await until(`${id} DOM/CSS/assets ${label}`, async () => {
      const value = await snap(id);
      return ['fixture-node', 'fixture-previewer', ...(page ? ['fixture-page'] : [])].every(kind =>
        value.elements.some(el => el.kind === kind && el.label === label && el.images.every(img => img.loaded
          && JSON.stringify(img.pixel) === JSON.stringify(id === 'A' ? [180, 20, 30, 255] : [20, 60, 190, 255]))
          && el.background === (id === 'A' ? 'rgb(250, 220, 220)' : 'rgb(220, 230, 250)')
          && el.color === (id === 'A' ? 'rgb(180, 20, 30)' : 'rgb(20, 60, 190)')));
    });
  }
  try {
    for (const id of ['A', 'B']) {
      const win = windows[id] = new BrowserWindow({ show: false, width: 960, height: 720,
        webPreferences: { session: partition, nodeIntegration: false, contextIsolation: true, sandbox: true, backgroundThrottling: false } });
      win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      win.webContents.on('console-message', event => {
        result.console.push({ id, stage, at: Date.now(), level: event.level, message: event.message,
          line: event.lineNumber, source: event.sourceId });
      });
      win.webContents.on('render-process-gone', (_event, details) => result.violations.push({ type: 'renderer-gone', id, details }));
      await win.loadURL(`fixture://studio/p/${id}/${process.env.VS43_PRECOMPUTE === '1' ? '?precompute=1' : ''}`);
      await until(`${id} bootstrap`, () => win.webContents.executeJavaScript('!!window.harness'));
    }
    check('Two simultaneous isolated Chromium renderer processes', windows.A.webContents.getOSProcessId() !== windows.B.webContents.getOSProcessId(),
      { A: windows.A.webContents.getOSProcessId(), B: windows.B.webContents.getOSProcessId() });
    await domReady('A', false); await domReady('B', false);
    const initialA = await snap('A');
    await call('A', 'save', 'initial-node', 'node');
    await call('A', 'save', 'initial-preview', 'previewer');
    await call('B', 'page'); await domReady('B');
    release('A:/plugins/pages');
    await call('A', 'page'); // push resolves before async component import finishes
    await until('A native page import held', () => gates.get('A:page.js')?.hits > 0);
    check('B page finishes before delayed A native import', (await snap('B')).elements.some(x => x.kind === 'fixture-page' && x.label === 'B1')
      && !(await snap('A')).elements.some(x => x.kind === 'fixture-page'));
    release('A:page.js'); await domReady('A');
    check('A/B real DOM computed CSS backgrounds and decoded asset/raw pixels differ', true,
      { A: (await snap('A')).elements, B: (await snap('B')).elements });
    const loadedA = await snap('A');
    check('VS-41 delayed first pages inventory preserves mounted node/Explorer wrappers',
      ['fixture-node', 'fixture-previewer'].every(kind => initialA.elements.find(x => x.kind === kind).evaluation === loadedA.elements.find(x => x.kind === kind).evaluation)
      && initialA.grammarGeneration === loadedA.grammarGeneration);
    check('Initial node callback remains live after first page inventory', (await call('A', 'retained', 'initial-node', 'write')).ok);
    for (const id of ['A', 'B']) {
      const beforeClick = (await snap(id)).writes.length;
      await call(id, 'clickNode');
      check(`${id} actual node UI click writes captured same-node params`, (await snap(id)).writes.length === beforeClick + 1);
      check(`${id} refuses legacy/foreign loader asset URLs`, (await call(id, 'invalidUrls')).every(Boolean));
      await call(id, 'call', 'node', 'save'); await call(id, 'call', 'node', 'action');
      await call(id, 'call', 'node', 'raw');
      const tokens = await call(id, 'highlight');
      check(`${id} real Oniguruma/Shiki grammar loads`, tokens?.language === 'same-language', tokens);
    }
    const highA = (await snap('A')).highlighted, highB = (await snap('B')).highlighted;
    const tokenColor = (list, word) => list.find(x => x.text === word)?.color;
    check('Equal language id and scope select different real grammar tokens/DOM colors',
      tokenColor(highA, 'ALPHA') && tokenColor(highB, 'BETA') && tokenColor(highA, 'ALPHA') === tokenColor(highB, 'BETA')
      && highA.find(x => x.text.includes('BETA')).color !== tokenColor(highA, 'ALPHA')
      && highB.find(x => x.text.includes('ALPHA')).color !== tokenColor(highB, 'BETA'), { A: highA, B: highB });

    // VS-41 page created with no editor lifetime must survive a pipeline visit.
    stage = 'VS-41 page pipeline page';
    await call('A', 'save', 'page-before-pipeline', 'page');
    await call('A', 'pipeline'); await call('A', 'page'); await domReady('A');
    await call('A', 'call', 'page', 'open', 'same.fixture');
    await until('late page nested preview DOM', async () => (await snap('A')).elements.filter(x => x.kind === 'fixture-previewer').length >= 2);
    check('VS-41 page -> pipeline -> page can open nested previewer',
      (await call('A', 'retained', 'page-before-pipeline', 'raw')).ok);
    const beforeStale = (await snap('A')).writes.length;
    await call('A', 'retained', 'initial-node', 'write');
    check('Same node id stale editor callback cannot acquire replacement writer', (await snap('A')).writes.length === beforeStale);
    // New node wrapper captures the replacement writer; nested delayed preview cannot.
    await domReady('A');
    gate('A:/explorer/file');
    stage = 'VS-41 delayed nested previewer';
    await call('A', 'beginPreview', 'node', 'delayed.fixture');
    await until('delayed nested preview request', () => gates.get('A:/explorer/file')?.hits > 0);
    await call('A', 'replaceWriter'); release('A:/explorer/file');
    check('VS-41 delayed node nested preview rejects replacement editor', !(await call('A', 'finishPreview')).ok);
    await domReady('A');
    const baselineB = await snap('B');
    result.snapshots.initialA = await snap('A'); result.snapshots.initialB = baselineB;
    const oldPage = (await snap('A')).captures.filter(x => x.kind === 'page').at(-1);
    const nativeFirst = await call('A', 'nativeProbe', oldPage.source);
    check('Native Chromium import caches identical full URL/module evaluation', nativeFirst.same && nativeFirst.evaluation === oldPage.evaluation);
    await call('A', 'save', 'old-page', 'page'); await call('A', 'save', 'old-node', 'node');
    configs.A.revision = 2;
    stage = 'VS-40 equal-stamp reload';
    // An unchanged stamp alone keeps native cache; the production generation is essential.
    check('Native same-URL control retains A1 after transport changes to A2', (await call('A', 'nativeProbe', oldPage.source)).label === 'A1');
    const pendingA = result.requests.filter(x => x.id === 'A' && x.endpoint === '/plugins/pages').length;
    gate('A:/plugins/pages');
    await call('A', 'event', 'plugin.changed', { plugins: inventory('A') });
    const requestsBeforeRetained = result.requests.length;
    const writesBeforeRetained = (await snap('A')).writes.length;
    check('VS-40 retained API writes revoked immediately before REST refresh', !(await call('A', 'retained', 'old-page', 'save')).ok);
    check('VS-40 retained node writes revoked immediately', !(await call('A', 'retained', 'old-node', 'write')).ok);
    check('Revoked A callbacks issue no HTTP or node writes', result.requests.length === requestsBeforeRetained
      && (await snap('A')).writes.length === writesBeforeRetained);
    await call('A', 'event', 'plugin.changed', { plugins: inventory('A') });
    await call('A', 'beginLocalRefresh');
    release('A:/plugins/pages'); await call('A', 'settled'); await domReady('A');
    const reloadedA = await snap('A');
    const newPage = reloadedA.captures.filter(x => x.kind === 'page').at(-1);
    check('Equal-stamp lifecycle reload obtains A2 JS via new native generation URL',
      newPage.label === 'A2' && newPage.evaluation !== oldPage.evaluation && new URL(newPage.source).searchParams.get('v') === 'same-stamp'
      && new URL(newPage.source).searchParams.get('studio_generation') !== new URL(oldPage.source).searchParams.get('studio_generation'));
    check('Coalesced repeated plugin.changed converges without unbounded requests',
      result.requests.filter(x => x.id === 'A' && x.endpoint === '/plugins/pages').length - pendingA <= 2);
    check('Local refresh joins event lifecycle without a second generation reset', (await call('A', 'finishLocalRefresh')).ok
      && Number(new URL(newPage.source).searchParams.get('studio_generation')) === Number(new URL(oldPage.source).searchParams.get('studio_generation')) + 1);
    check('Same native module URL is fetched only once despite repeated imports', result.requests.filter(x => x.url === oldPage.source).length === 1);
    await call('A', 'highlight');
    await call('B', 'highlight');
    check('Reload A preserves B DOM/component/native module/grammar/writer state', JSON.stringify(await snap('B')) === JSON.stringify(baselineB));

    // Start a real native import, then invalidate it via gap; never inject factories.
    gate('A:late.js'); await call('A', 'lateImport', 'late');
    stage = 'VS-40 repeated gap and late import';
    await until('late native import pending', () => gates.get('A:late.js')?.hits > 0);
    gate('A:/plugins/pages');
    await call('A', 'event', 'system.dropped', { count: 3, resync: true });
    await until('first gap REST pending', () => gates.get('A:/plugins/pages')?.hits > 0);
    await call('A', 'event', 'system.dropped', { count: 2, resync: true });
    release('A:/plugins/pages'); release('A:late.js');
    await call('A', 'settled'); await domReady('A');
    check('VS-40 repeated gap invalidates late native import/factory', !(await call('A', 'finishImport')).ok
      && !(await snap('A')).captures.some(x => x.kind === 'late'));
    check('Gap recovery fetches authoritative project plugin inventory', result.requests.some(x => x.id === 'A' && x.endpoint === '/plugins'));
    const unchangedA = await snap('A');
    await call('A', 'refresh');
    check('Unchanged ordinary inventory does not remount or reset grammars/editors', JSON.stringify(await snap('A')) === JSON.stringify(unchangedA));
    await call('A', 'event', 'plugin.changed', { plugins: [] }, true);
    check('Production WsConnection rejects foreign scoped envelope', JSON.stringify(await snap('A')) === JSON.stringify(unchangedA));

    // An inventory validation failure is observable without manufacturing browser console errors.
    configs.A.badPages = true;
    stage = 'VS-40 refresh failure';
    await call('A', 'event', 'system.dropped', { count: 1, resync: true });
    const failedRefresh = await call('A', 'settledResult');
    check('VS-40 refresh failure remains observable, not reported as success', !failedRefresh.ok && !!(await snap('A')).error, failedRefresh);
    check('Failed refresh A leaves B untouched', JSON.stringify(await snap('B')) === JSON.stringify(baselineB));
    configs.A.badPages = false;
    stage = 'VS-40 recovery from failure';
    await call('A', 'event', 'system.dropped', { count: 1, resync: true });
    await call('A', 'settled'); await domReady('A');
    check('VS-40 retry after failed refresh recovers current registry', (await snap('A')).error === null);
    // Hold an enabled inventory response, then disable before it arrives.
    await call('A', 'save', 'pre-disable', 'page');
    stage = 'VS-40 disable during delayed inventory';
    gate('A:/plugins/pages');
    await call('A', 'event', 'plugin.changed', { plugins: inventory('A') });
    await until('enabled inventory held before disable', () => gates.get('A:/plugins/pages')?.hits > 0);
    configs.A.enabled = false;
    await call('A', 'event', 'plugin.changed', { plugins: inventory('A') });
    release('A:/plugins/pages');
    await call('A', 'settled');
    await until('disabled A route and DOM removed', async () => !(await snap('A')).elements.length);
    check('Disable A withdraws page/node/previewer and late response cannot resurrect routes', !(await call('A', 'retained', 'pre-disable', 'action')).ok
      && (await snap('A')).nav.length === 0);
    check('Disabled A cannot highlight removed language', await call('A', 'highlight') === null);
    await call('B', 'highlight');
    check('Disable A leaves B byte-for-byte identical', JSON.stringify(await snap('B')) === JSON.stringify(baselineB));
    const bBefore = (await snap('B')).writes.length;
    await call('B', 'event', 'team.doc.changed', { path: 'same.md' });
    check('B event callback/writer still operates after A disable', (await snap('B')).writes.length === bBefore + 1);
    const aWrites = (await snap('A')).writes.length;
    await call('A', 'event', 'team.doc.changed', { path: 'same.md' });
    check('Disabled A subscriptions do not write', (await snap('A')).writes.length === aWrites);
    configs.A.enabled = true; configs.A.revision = 3;
    stage = 'VS-40 reenable';
    await call('A', 'event', 'plugin.changed', { plugins: inventory('A') }); await call('A', 'settled');
    await call('A', 'page'); await domReady('A');
    check('Recovery after disable reinstalls only current A3 components', (await snap('A')).elements.every(x => x.label === 'A3'));
    result.snapshots.finalA = await snap('A'); result.snapshots.finalB = await snap('B');
    result.diagnostics = {};
    for (const id of ['A', 'B']) result.diagnostics[id] = await windows[id].webContents.executeJavaScript('window.fixtureDiagnostics');
    for (const id of ['A', 'B']) fs.writeFileSync(path.join(run, `${id}.png`), (await windows[id].webContents.capturePage()).toPNG());
    check('Every fixture request retained URL scope and token/header authentication', result.violations.length === 0, result.violations);
    check('All node writes target own project and identical node id', ['A', 'B'].every(id => result.snapshots['final' + id].writes.every(
      x => x.node === 'same-node' && Object.values(x.patch).every(value => value.startsWith(id)))));
    check('Native module requests retain equal stamp and generation/token selectors', result.requests.filter(x => /\.js$/.test(x.endpoint)).every(x => {
      const url = new URL(x.url); return url.searchParams.get('v') === 'same-stamp' && url.searchParams.has('studio_generation') && url.searchParams.get('token') === 'fixture secret /&';
    }));
    check('Grammar JSON and registry REST use bearer headers (not query credentials)', result.requests.filter(x =>
      x.endpoint.endsWith('grammar.json') || ['/plugins', '/plugins/pages', '/plugins/grammars', '/explorer/file'].includes(x.endpoint))
      .every(x => x.authorization === 'Bearer fixture secret /&' && !new URL(x.url).searchParams.has('token')));
    check('Native CSS/image assets use scope plus query authentication', result.requests.filter(x =>
      /\/(?:style\.css|pixel\.svg)$/.test(x.endpoint)).every(x => new URL(x.url).searchParams.get('token') === 'fixture secret /&'));
    check('No uncaught renderer exceptions or unhandled rejections', Object.values(result.diagnostics).every(x => !x.uncaught.length && !x.rejections.length));
    // Expected stale editor warnings are evidence, not silently filtered errors.
    result.expectedWarnings = result.console.filter(x => /setNodeParams\(same-node\) ignored: pipeline editor is no longer mounted/.test(x.message));
    const errors = result.console.filter(x => x.level === 'error' || x.level === 3);
    const unexpectedWarnings = result.console.filter(x => (x.level === 'warning' || x.level === 2) && !result.expectedWarnings.includes(x));
    for (const id of ['A', 'B']) await call(id, 'cleanup');
    check('No Chromium/production console errors or unexpected warnings', !errors.length && !unexpectedWarnings.length, { errors, unexpectedWarnings });
    result.status = 'passed';
  } catch (error) { result.status = 'failed'; result.error = error.stack; }
  finally {
    for (const value of gates.values()) value.release();
    for (const win of Object.values(windows)) if (!win.isDestroyed()) win.destroy();
    fs.writeFileSync(path.join(run, 'result.json'), JSON.stringify(result, null, 2));
    app.exit(result.status === 'passed' ? 0 : 1);
  }
}

if (process.env.VS43_WORKER === '1') nativeMain().catch(error => {
  fs.writeFileSync(path.join(process.env.VS43_RUN, 'fatal.txt'), error.stack); require('electron').app.exit(1);
});
else launch().catch(error => { console.error(error); process.exitCode = 1; });
