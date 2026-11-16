// VS-21 opt-in native spike. Never imports desktop/main.cjs or starts a backend.
// node desktop/test/native-transfer-spike.cjs --run-native [--interactive] [--en]
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { spawn, execFile } = require('node:child_process');
const { createHash, randomUUID } = require('node:crypto');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const root = path.resolve(__dirname, '../..');
const temp = path.join(root, '.voxarium_studio/temp/TASK-21');
const fixtures = path.join(__dirname, 'fixtures/native-transfer');

async function launch() {
  if (!process.argv.includes('--run-native')) {
    console.log('Opt-in only: node desktop/test/native-transfer-spike.cjs --run-native [--interactive] [--en]');
    return;
  }
  assert.equal(process.platform, 'win32', 'This spike targets installed Windows Electron');
  const installed = process.env.NATIVE_SPIKE_INSTALLED_DIR ||
    path.join(process.env.LOCALAPPDATA, 'Programs/Voxarium Studio');
  const sourceExe = path.join(installed, 'Voxarium Studio.exe');
  const digest = createHash('sha256').update(fs.readFileSync(sourceExe)).digest('hex');
  const run = path.join(temp, `run-${new Date().toISOString().replace(/[:.]/g, '-')}-${process.pid}`);
  const runtime = path.join(run, 'runtime');
  fs.mkdirSync(path.join(runtime, 'resources/app'), { recursive: true });
  // Copy the installed runtime verbatim, EXCLUDING its resources/app.asar and updater.
  // A packaged executable ignores a CLI script path; an isolated resources/app is required.
  for (const entry of fs.readdirSync(installed, { withFileTypes: true })) {
    if (entry.isFile() && !/^uninstall/i.test(entry.name)) {
      fs.copyFileSync(path.join(installed, entry.name), path.join(runtime, entry.name));
    }
  }
  fs.cpSync(path.join(installed, 'locales'), path.join(runtime, 'locales'), { recursive: true });
  fs.writeFileSync(path.join(runtime, 'resources/app/package.json'),
    JSON.stringify({ name: 'vs21-native-spike', version: '0.0.0', main: 'entry.cjs' }));
  fs.writeFileSync(path.join(runtime, 'resources/app/entry.cjs'), `require(${JSON.stringify(__filename)});`);
  fs.writeFileSync(path.join(run, 'runtime-source.json'), JSON.stringify({ sourceExe, sha256: digest,
    copiedSha256: createHash('sha256').update(fs.readFileSync(path.join(runtime, 'Voxarium Studio.exe'))).digest('hex'),
    argv: process.argv.slice(2), startedAt: new Date().toISOString() }, null, 2));
  console.log(`VS-21 evidence: ${run}`);
  const osTemp = path.join(run, 'os-temp'); fs.mkdirSync(osTemp);
  const env = { ...process.env, NATIVE_SPIKE_WORKER: '1', NATIVE_SPIKE_RUN: run,
    TEMP: osTemp, TMP: osTemp,
    NATIVE_SPIKE_INTERACTIVE: process.argv.includes('--interactive') ? '1' : '0',
    NATIVE_SPIKE_LOCALE: process.argv.includes('--en') ? 'en' : 'ru' };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.NODE_OPTIONS;
  const child = spawn(path.join(runtime, 'Voxarium Studio.exe'), [], { env, stdio: 'inherit' });
  const timeout = setTimeout(() => {
    console.error('Harness watchdog timeout');
    // Only the PID launched here and its descendants; never kill Studio by image name.
    execFile('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true }, () => {});
  },
    env.NATIVE_SPIKE_INTERACTIVE === '1' ? 30 * 60_000 : 120_000);
  child.once('error', error => { clearTimeout(timeout); console.error(error); process.exitCode = 1; });
  child.once('exit', code => { clearTimeout(timeout); process.exitCode = code ?? 1; });
}

async function nativeMain() {
  const { app, BrowserWindow, WebContentsView, ipcMain, screen, session } = require('electron');
  const run = process.env.NATIVE_SPIKE_RUN;
  assert.ok(run && path.resolve(run).startsWith(path.resolve(temp) + path.sep));
  app.setName('VS-21 Native Transfer Spike');
  for (const key of ['appData', 'userData', 'sessionData', 'crashDumps']) {
    const dir = path.join(run, key); fs.mkdirSync(dir, { recursive: true }); app.setPath(key, dir);
  }
  app.setAppLogsPath(path.join(run, 'logs'));
  app.commandLine.appendSwitch('disable-background-networking');
  app.commandLine.appendSwitch('disable-component-update');
  const log = (event, data = {}) => fs.appendFileSync(path.join(run, 'events.jsonl'),
    JSON.stringify({ at: new Date().toISOString(), event, ...data }) + '\n');
  const interactive = process.env.NATIVE_SPIKE_INTERACTIVE === '1';
  const windows = new Map();
  const { NativeTerminal } = require('./fixtures/native-transfer/terminal-client.cjs');
  const { classify, assertLease } = require('./fixtures/native-transfer/terminal-policy.cjs');
  let input;
  let view, owner, baseline, baselineCounts, drag, revision = 0, busy = false, quitting = false;
  let failNext = false, home, sequence = Promise.resolve(), gestureCommitting = false, quitStarted = false;
  const counters = { navigation: 0, destroyed: 0, bootstrap: 0, load: 0, gone: 0 };
  const stripHeight = 116;
  const serial = operation => {
    const result = sequence.then(operation);
    sequence = result.catch(error => { log('operation-error', { error: error.stack }); });
    return result;
  };
  function state(message = '') {
    for (const win of windows.values()) if (!win.isDestroyed()) win.webContents.send('spike:state', {
      windowId: win.id, owner: owner?.id, dragging: !!drag, message,
      locale: process.env.NATIVE_SPIKE_LOCALE,
    });
  }
  const boundsFor = win => { const b = win.getContentBounds();
    return { x: 0, y: stripHeight, width: b.width, height: Math.max(1, b.height - stripHeight) }; };
  function layout() { if (owner && !owner.isDestroyed() && view) view.setBounds(boundsFor(owner)); }
  const preferences = () => ({ preload: path.join(fixtures, 'preload.cjs'), contextIsolation: true,
    nodeIntegration: false, sandbox: true, backgroundThrottling: false });
  function guard(contents) {
    contents.setWindowOpenHandler(() => ({ action: 'deny' }));
    contents.on('will-navigate', event => event.preventDefault());
    contents.on('will-redirect', event => event.preventDefault());
    contents.on('will-attach-webview', event => event.preventDefault());
  }
  async function shell(label, position, show = true) {
    const win = new BrowserWindow({ title: `VS-21 — ${label}`, width: 660, height: 530,
      ...position, show: false, webPreferences: preferences() });
    win.setMenu(null); windows.set(win.id, win); guard(win.webContents);
    win.spikeGeneration = 0;
    for (const event of ['move', 'resize', 'hide', 'closed']) win.on(event, () => { win.spikeGeneration++; });
    win.webContents.on('before-input-event', (_event, input) => {
      if (input.key === 'Escape' && drag?.source === win.id) cancel('native-before-input-Escape');
    });
    win.on('resize', layout);
    win.on('blur', () => { if (drag?.source === win.id) cancel('native-window-blur'); });
    win.on('close', event => {
      if (!quitting && (owner === win || busy)) {
        event.preventDefault(); state('Use Quit / Выход to finish; cancel active gesture first.');
      }
    });
    win.on('closed', () => { windows.delete(win.id); log('shell-closed', { windowId: win.id }); });
    await win.loadFile(path.join(fixtures, 'shell.html'));
    if (show) win.show(); state(); log('shell-ready', { windowId: win.id, label, bounds: win.getBounds(), show });
    return win;
  }
  async function inspect(label, compare = true) {
    assert.ok(view && !view.webContents.isDestroyed(), 'live contents required');
    const snapshot = await view.webContents.executeJavaScript('window.spikeSnapshot()');
    const record = { label, wcId: view.webContents.id, owner: owner.id, revision,
      snapshot, counters: { ...counters }, bounds: view.getBounds(), expectedBounds: boundsFor(owner),
      ownerFocused: owner.isFocused(), contentsFocused: view.webContents.isFocused(),
      children: [...windows.values()].map(w => ({ id: w.id, ownsView: w.contentView.children.includes(view) })) };
    log('snapshot', record);
    assert.deepEqual(record.bounds, record.expectedBounds);
    assert.equal(record.children.filter(w => w.ownsView).length, 1);
    assert.ok(record.children.find(w => w.id === owner.id).ownsView);
    if (compare && baseline) {
      assert.equal(record.wcId, baseline.wcId);
      for (const key of ['sentinel', 'timeOrigin', 'bootstrap', 'nodeSame', 'fileSame', 'fileText',
        'fileName', 'fileSize', 'draft', 'selectionStart', 'selectionEnd', 'scrollTop', 'domSelection']) {
        assert.deepEqual(snapshot[key], baseline.snapshot[key], `state changed: ${key}`);
      }
      assert.deepEqual(counters, baselineCounts, 'navigation/destroy/bootstrap/load/crash during transfer');
    }
    return record;
  }
  async function focus(win) {
    win.show(); win.focus(); view.webContents.focus();
    await delay(180);
  }
  // VS-18 §5.1 transaction: keep the exact View + contents, rollback before owner commit.
  async function transfer(target, reason, injectFailure = false, beforeCommit) {
    assert.ok(!busy, 'transfer busy');
    assert.ok(target && !target.isDestroyed() && windows.get(target.id) === target, 'target unavailable');
    if (target === owner) return;
    const source = owner;
    const oldBounds = view.getBounds();
    const before = await inspect(`before:${reason}`, false);
    busy = true;
    try {
      source.contentView.removeChildView(view);
      target.contentView.addChildView(view);
      view.setBounds(boundsFor(target));
      assert.ok(target.contentView.children.includes(view));
      assert.ok(!source.contentView.children.includes(view));
      if (injectFailure === 'close-target') target.destroy();
      else if (injectFailure || failNext) { failNext = false; throw new Error('injected target attach failure'); }
      // Native attach is synchronous; renderer ACK proves it remains responsive, not recreated.
      await view.webContents.executeJavaScript('window.spikeSnapshot().then(s => s.sentinel)');
      assert.ok(!target.isDestroyed(), 'target closed during ACK');
      if (beforeCommit) await beforeCommit();
      assert.ok(!target.isDestroyed(), 'target closed before commit');
      owner = target; revision++;
      await focus(target);
      const after = await inspect(`commit:${reason}`, false);
      assert.equal(after.wcId, before.wcId);
      assert.deepEqual(after.snapshot, { ...before.snapshot,
        activeElement: after.snapshot.activeElement, documentFocused: after.snapshot.documentFocused });
      assert.equal(after.snapshot.activeElement, before.snapshot.activeElement, 'active element changed');
      assert.ok(after.ownerFocused && after.contentsFocused && after.snapshot.documentFocused, 'target focus not restored');
      assert.deepEqual(after.counters, before.counters);
      log('transfer-commit', { reason, source: source.id, target: target.id, wcId: view.webContents.id });
    } catch (error) {
      if (!target.isDestroyed()) target.contentView.removeChildView(view);
      assert.ok(!source.isDestroyed(), 'source unavailable: recovery shell required');
      source.contentView.addChildView(view); view.setBounds(oldBounds); owner = source;
      await focus(source);
      log('transfer-rollback', { reason, error: error.message, source: source.id });
      const after = await inspect(`rollback:${reason}`, false);
      assert.equal(after.wcId, before.wcId);
      assert.deepEqual(after.counters, before.counters);
      assert.deepEqual(after.snapshot, { ...before.snapshot,
        activeElement: after.snapshot.activeElement, documentFocused: after.snapshot.documentFocused });
      assert.equal(after.snapshot.activeElement, before.snapshot.activeElement, 'rollback active element changed');
      assert.ok(after.ownerFocused && after.contentsFocused && after.snapshot.documentFocused, 'rollback focus not restored');
      throw error;
    } finally { busy = false; state(reason); }
  }
  async function detach(reason, point, beforeCommit, terminalToken) {
    // Keep a prepared tear-out shell hidden: it must not occlude the captured target
    // before the terminal lease is checked and committed.
    const target = await shell('detached', point ? { x: point.x - 80, y: point.y - 25 } : undefined, !beforeCommit);
    try {
      if (terminalToken) await input.request('prepare', { token: terminalToken, hwnd: hwnd(target), pid: process.pid });
      await transfer(target, reason, false, beforeCommit);
    }
    catch (error) { if (!target.isDestroyed() && owner !== target) target.close(); throw error; }
  }
  function validShell(event) {
    const win = [...windows.values()].find(w => w.webContents === event.sender);
    assert.ok(win && event.senderFrame === event.sender.mainFrame &&
      event.sender.getURL() === require('node:url').pathToFileURL(path.join(fixtures, 'shell.html')).href, 'unregistered shell');
    return win;
  }
  function cancel(reason) {
    const token = drag?.token;
    if (token) { log('gesture-cancel', { reason, token }); input?.request('cancel', { token }).catch(() => {}); }
    drag = undefined; state(reason);
  }
  function shellSnapshot() {
    return [...windows.values()].filter(w => !w.isDestroyed() && w.isVisible()).map(w => ({
      id: w.id, hwnd: hwnd(w), generation: w.spikeGeneration, content: w.getContentBounds(),
    }));
  }
  async function begin(win, point, pointerId, synthetic = false) {
    assert.ok(!busy && !drag && !gestureCommitting && win === owner, 'not current owner / busy');
    assert.ok(Number.isFinite(point.x) && Number.isFinite(point.y), 'invalid begin coordinates');
    drag = { source: win.id, revision, pointerId, point, token: randomUUID(), expires: Date.now() + 30_000,
      shells: shellSnapshot(), synthetic };
    const token = drag.token;
    setTimeout(() => { if (drag?.token === token) cancel('gesture-timeout'); }, 30_000).unref();
    log('gesture-begin', { ...drag }); state();
    if (!synthetic) {
      try { await input.request('arm', { token, synthetic: false }); }
      catch (error) { if (drag?.token === token) cancel(`native-arm:${error.message}`); }
    }
    return token;
  }
  const hwnd = win => {
    const handle = win.getNativeWindowHandle();
    return handle.length === 8 ? handle.readBigUInt64LE().toString() : String(handle.readUInt32LE());
  };
  async function terminal(record) {
    const d = drag;
    if (!d || d.token !== record.token) { log('terminal-rejected', { reason: 'cancelled/stale token', record }); return false; }
    if (record.kind !== 'up') { cancel('native-terminal-cancel'); return false; }
    if (record.provenance !== 'native-hook' && !(d.synthetic && !interactive)) { cancel('unapproved terminal provenance'); return false; }
    const point = screen.screenToDipPoint(record.point);
    if (Math.hypot(point.x - d.point.x, point.y - d.point.y) < 12) { cancel('click-without-drag'); return false; }
    const lease = { token: d.token, consumed: false, record,
      decision: classify({ ...record, dipPoint: point }, d.shells) };
    log('terminal-decision', { ...lease, point });
    if (lease.decision.kind === 'cancel') { cancel('unsupported terminal target'); return false; }
    gestureCommitting = true;
    try {
      const validate = async () => {
        assertLease(lease, drag, windows, revision);
        assert.equal(owner.id, d.source, 'source owner changed');
        if (lease.decision.kind === 'strip') {
          const old = d.shells.find(w => w.id === lease.decision.windowId);
          assert.deepEqual(windows.get(old.id).getContentBounds(), old.content, 'terminal shell geometry changed');
        }
        const result = await input.request('validate', { token: d.token });
        assert.ok(result.valid, `terminal target invalidated: ${result.reason}`);
        assertLease(lease, drag, windows, revision);
      };
      await validate();
      const commit = async () => {
        await validate();
        const finished = await input.request('finish', { token: d.token });
        assert.ok(finished.valid && finished.consumed, `terminal finish invalidated: ${finished.reason}`);
        assertLease(lease, drag, windows, revision);
        lease.consumed = true; drag = undefined;
        log('terminal-linearized', { token: d.token, decision: lease.decision });
      };
      if (lease.decision.kind === 'strip') {
        const target = windows.get(lease.decision.windowId);
        if (target === owner) { await commit(); state('same shell drop'); }
        else await transfer(target, `terminal-strip:${record.provenance}`, false, commit);
      } else {
        await detach(`terminal-outside:${record.provenance}`, point, commit, d.token);
      }
      return true;
    } catch (error) { log('terminal-rejected', { reason: error.message, token: d.token }); cancel(error.message); return false;
    } finally { gestureCommitting = false; }
  }
  async function quit(code = 0) {
    if (quitStarted) return;
    quitStarted = true;
    quitting = true; cancel('quit');
    log('test-end', { code, counters, wcId: view?.webContents.id });
    if (view && !view.webContents.isDestroyed()) { owner?.contentView.removeChildView(view); view.webContents.close(); }
    if (input) { try { await input.stop(); } catch (error) { log('helper-stop-error', { error: error.message }); } }
    app.exit(code);
  }
  ipcMain.on('spike:event', (event, name, payload) => {
    if (name === 'bootstrap' && view && event.sender === view.webContents && event.senderFrame === event.sender.mainFrame) {
      counters.bootstrap++; log('project-bootstrap', { wcId: event.sender.id }); return;
    }
    let win;
    try {
      win = validShell(event);
      if (name === 'pointer-begin') begin(win, { x: payload?.screenX, y: payload?.screenY }, payload?.pointerId).catch(error => cancel(error.message));
      else if (name === 'pointer-release') {
        // Renderer release is diagnostic only: never classify a target from IPC/cursor.
        log('renderer-release-notice', { source: win.id, ...payload });
      } else if (name === 'pointer-cancel' && drag?.source === win.id) cancel('Escape/capture-lost/pointer-cancel');
    } catch (error) {
      log('event-rejected', { name, error: error.message });
      if (name === 'pointer-release' && win && drag?.source === win.id) cancel('invalid-release');
    }
  });
  ipcMain.handle('spike:action', (event, name, payload) => {
    const win = validShell(event);
    return serial(async () => {
      try {
        if (name === 'quit') { await quit(); return; }
        if (name === 'locale') {
          assert.ok(['ru', 'en'].includes(payload?.locale)); process.env.NATIVE_SPIKE_LOCALE = payload.locale; state(); return;
        }
        if (name === 'snapshot') return await inspect('manual-snapshot', false);
        if (name === 'fail-target') { failNext = !failNext; state(`failNext=${failNext}`); return; }
        if (name === 'close-target') {
          const target = [...windows.values()].find(w => w !== owner);
          target?.close(); state(); return;
        }
        assert.ok(!drag && !gestureCommitting && win === owner, 'cancel gesture / command from non-owner');
        if (name === 'detach') await detach('command-detach');
        else if (name === 'return') {
          let target = windows.get(home.id);
          if (!target || target === owner) target = [...windows.values()].find(w => w !== owner);
          if (!target) target = await shell('return');
          await transfer(target, 'command-return');
        } else throw new Error('unknown action');
        return { ok: true };
      } catch (error) { state(error.message); log('command-error', { name, error: error.message }); return { ok: false, error: error.message }; }
    });
  });
  app.on('window-all-closed', () => { if (!quitting) quit(); });
  try {
    await app.whenReady();
    input = new NativeTerminal(log, record => {
      serial(() => terminal(record)).catch(error => cancel(error.message));
    });
    input.onFailure = error => cancel(`native-helper:${error.message}`);
    await input.ready;
    log('runtime', { versions: process.versions, platform: process.platform, arch: process.arch,
      interactive, pid: process.pid, displays: screen.getAllDisplays() });
    session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    session.defaultSession.setPermissionCheckHandler(() => false);
    session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
      const allowed = /^(file|data|devtools):/.test(details.url);
      if (!allowed) log('blocked-network', { url: details.url });
      callback({ cancel: !allowed });
    });
    const area = screen.getPrimaryDisplay().workArea;
    home = await shell('source', { x: area.x + 15, y: area.y + 30 });
    const other = await shell('target', { x: area.x + Math.min(700, Math.max(200, area.width - 680)), y: area.y + 80 });
    view = new WebContentsView({ webPreferences: preferences() }); guard(view.webContents);
    const projectContentsId = view.webContents.id;
    for (const [event, counter] of [['did-start-navigation', 'navigation'], ['destroyed', 'destroyed'],
      ['did-finish-load', 'load'], ['render-process-gone', 'gone']]) {
      view.webContents.on(event, (...args) => { counters[counter]++; log(`project:${event}`, {
        wcId: projectContentsId, detail: event === 'render-process-gone' ? args[1] : undefined }); });
    }
    owner = home; home.contentView.addChildView(view); layout();
    await view.webContents.loadFile(path.join(fixtures, 'project.html'));
    await focus(home); baseline = await inspect('baseline', false); baselineCounts = { ...counters };
    assert.equal(counters.bootstrap, 1); assert.equal(counters.load, 1); assert.equal(counters.destroyed, 0);
    assert.equal(baseline.snapshot.nodeSame, true); assert.equal(baseline.snapshot.fileSame, true);
    assert.ok(baseline.snapshot.selectionEnd > baseline.snapshot.selectionStart, 'noncollapsed caret fixture');
    if (interactive) { state('Native manual gesture pass required / Нужен ручной проход жестов');
      log('manual-ready', { note: 'No gesture claimed passed until trusted pointer trace and invariant snapshots are reviewed.' }); return; }
    for (let i = 0; i < 10; i++) {
      await transfer(other, `roundtrip-${i}-out`); await inspect(`roundtrip-${i}-out`);
      await transfer(home, `roundtrip-${i}-back`); await inspect(`roundtrip-${i}-back`);
    }
    assert.deepEqual(await owner.webContents.executeJavaScript("window.spike.action('detach')"), { ok: true });
    await inspect('command-detach');
    const detached = owner;
    assert.deepEqual(await owner.webContents.executeJavaScript("window.spike.action('return')"), { ok: true });
    detached.close(); await inspect('after-source-close');
    other.setSize(750, 600); await transfer(other, 'resized-target'); await inspect('resized-target');
    await transfer(home, 'return-after-resize');
    other.maximize(); await delay(180); await transfer(other, 'maximized-target'); await inspect('maximized-target');
    other.restore(); await delay(180); layout(); await inspect('restored-target'); await transfer(home, 'return-after-maximize');
    const secondary = screen.getAllDisplays().find(d => d.id !== screen.getPrimaryDisplay().id);
    if (secondary) {
      other.setPosition(secondary.workArea.x + 30, secondary.workArea.y + 30);
      await transfer(other, 'secondary-monitor'); await inspect('secondary-monitor');
      await transfer(home, 'return-from-secondary');
      other.setPosition(area.x + 700, area.y + 80);
    }
    await assert.rejects(transfer(other, 'attach-fault', true), /injected/); await inspect('after-rollback');
    const dying = await shell('destroy-during-attach');
    await assert.rejects(transfer(dying, 'target-destroy-during-attach', 'close-target'), /target closed during ACK/);
    await inspect('after-target-destroy-during-attach');
    const dead = await shell('closed-target'); dead.destroy();
    await assert.rejects(transfer(dead, 'closed-target'), /target unavailable/); await inspect('after-closed-target');
    // State-machine checks only, explicitly NOT evidence for GUI/OS gestures.
    const beforeCancel = windows.size;
    const cancelToken = await begin(owner, { x: 0, y: 0 }, 1, true); cancel('synthetic-Escape');
    await terminal({ token: cancelToken, kind: 'up', point: { x: -500, y: -500 }, provenance: 'synthetic-sample' });
    assert.equal(windows.size, beforeCancel); await inspect('synthetic-cancel-only');
    const terminalRegression = await require('./fixtures/native-transfer/terminal-regression.cjs').runTerminalRegression({
      input, screen, windows, shell, home, other, begin, terminal, transfer, inspect,
      owner: () => owner, focus, log, delay,
    });
    await focus(owner); const final = await inspect('final');
    fs.writeFileSync(path.join(run, 'result.json'), JSON.stringify({ nativeTransfer: 'passed',
      physicalGestures: 'NOT_TESTED', terminalRegression, focus: { owner: final.ownerFocused, contents: final.contentsFocused,
        document: final.snapshot.documentFocused }, baseline, final, counters }, null, 2));
    log('auto-passed', { nativeTransfer: true, physicalGestures: 'NOT_TESTED' });
    console.log(`Native transfer passed; physical gestures NOT TESTED. Evidence: ${run}`); quit();
  } catch (error) { log('fatal', { error: error.stack }); console.error(error); quit(1); }
}

if (process.versions.electron && process.env.NATIVE_SPIKE_WORKER === '1') nativeMain();
else launch().catch(error => { console.error(error); process.exitCode = 1; });
