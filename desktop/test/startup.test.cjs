const { test } = require('node:test');
const assert = require('node:assert/strict');
const { registerStartup } = require('../startup.cjs');
const { ORIGIN } = require('../policy.cjs');
test('startup is opt-in, reads Windows state and rejects foreign frames', () => {
  let handler, settings, enabled = false;
  const window = { webContents: { mainFrame: { url: ORIGIN } }, once() {} };
  const event = { sender: window.webContents, senderFrame: window.webContents.mainFrame };
  const app = { isPackaged: true,
    getLoginItemSettings: () => ({openAtLogin:enabled, launchItems:[{name:'Voxarium Studio', enabled}]}),
    setLoginItemSettings: value => { settings = value; enabled = value.openAtLogin; } };
  registerStartup({app, window, platform:'win32', executable:'C:\\Program Files\\Voxarium Studio.exe', ipcMain:{handle: (_,fn) => { handler=fn; }}});
  assert.equal(settings, undefined);
  assert.equal(handler(event).enabled, false);
  assert.equal(handler(event,true).enabled, true);
  assert.equal(settings.path, 'C:\\Program Files\\Voxarium Studio.exe');
  assert.deepEqual(settings.args, []);
  assert.equal(handler(event,false).enabled, false);
  assert.throws(() => handler({...event, senderFrame:{url:ORIGIN}},true), /Untrusted/);
  assert.throws(() => handler(event,'true'), /Invalid/);
});

test('startup IPC is app-owned across windows, broadcasts changes and disposes explicitly', () => {
  const { EventEmitter } = require('node:events');
  const a = new EventEmitter(), b = new EventEmitter();
  const owners = new Map();
  for (const [window, role, url] of [[a, 'shell', 'data:text/html,shell'], [b, 'project', `${ORIGIN}/p/b`]]) {
    window.webContents = { mainFrame: { url }, isDestroyed: () => !!window.destroyed };
    owners.set(window.webContents, { window, role, contents: window.webContents });
    window.isDestroyed = () => !!window.destroyed;
  }
  let handler, installs = 0, removes = 0, enabled = false;
  const published = [];
  const ipcMain = { handle: (_name, fn) => { handler = fn; installs++; }, removeHandler: () => { removes++; } };
  const options = { ipcMain, app: { isPackaged: true,
    getLoginItemSettings: () => ({ openAtLogin: enabled }), setLoginItemSettings: value => { enabled = value.openAtLogin; } },
    platform: 'win32', authorize(event) {
      const owner = owners.get(event.sender);
      if (!owner) throw new Error('Untrusted sender');
      return owner;
    }, broadcast: (...args) => published.push(args) };
  const api = registerStartup(options);
  assert.equal(registerStartup(options), api);
  assert.equal(installs, 1);
  const event = window => ({ sender: window.webContents, senderFrame: window.webContents.mainFrame });
  assert.equal(handler(event(a), true).enabled, true);
  assert.deepEqual(published, [['studio:startup-state', { supported: true, enabled: true }]]);
  a.emit('closed'); a.destroyed = true; owners.delete(a.webContents);
  assert.equal(removes, 0);
  assert.equal(handler(event(b), false).enabled, false);
  assert.throws(() => handler(event(a)), /Untrusted/);
  assert.throws(() => handler({ ...event(b), senderFrame: { url: ORIGIN } }), /Untrusted/);
  b.webContents.mainFrame.url = 'https://example.com';
  assert.throws(() => handler(event(b)), /Untrusted/);
  b.webContents.mainFrame.url = `${ORIGIN}/p/b`;
  b.emit('closed'); b.destroyed = true;
  assert.equal(removes, 0);
  assert.throws(() => handler(event(b)), /Untrusted/);
  api.dispose(); api.dispose();
  assert.equal(removes, 1);
});

test('unsupported startup stays read-only, without changing OS settings', () => {
  let handler;
  const contents = { mainFrame: { url: ORIGIN } };
  const event = { sender: contents, senderFrame: contents.mainFrame };
  const api = registerStartup({ ipcMain: { handle: (_name, fn) => { handler = fn; }, removeHandler() {} },
    app: { isPackaged: true }, platform: 'linux', authorize: () => ({ role: 'project', contents }) });
  assert.deepEqual(handler(event), { supported: false, enabled: false });
  assert.throws(() => handler(event, true), /Invalid/);
  api.dispose();
  assert.throws(() => handler(event), /Untrusted/);
});
