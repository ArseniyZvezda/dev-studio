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
