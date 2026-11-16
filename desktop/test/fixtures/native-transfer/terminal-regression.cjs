'use strict';
const assert = require('node:assert/strict');
const { classify } = require('./terminal-policy.cjs');

async function runTerminalRegression({ input, screen, windows, shell, home, other, begin, terminal,
  transfer, inspect, owner, focus, log, delay }) {
  const results = [];
  async function capture(target) {
    target.setAlwaysOnTop(true); target.show(); target.moveTop(); target.focus(); await delay(180);
    await focus(home); await delay(140);
    const c = target.getContentBounds(), p = screen.dipToScreenPoint({ x: c.x + 60, y: c.y + 20 });
    const token = await begin(home, { x: c.x - 100, y: c.y + 20 }, 99, true);
    const r = await input.request('sample', { token, x: p.x, y: p.y });
    const h = target.getNativeWindowHandle();
    assert.equal(r.terminal.target?.hwnd, h.length === 8 ? h.readBigUInt64LE().toString() : String(h.readUInt32LE()),
      'race fixture target must really be topmost at its sample point');
    return r.terminal;
  }
  async function rejected(record, label, mutate) {
    const size = windows.size;
    await mutate(); await delay(140);
    const native = await input.request('validate', { token: record.token });
    assert.equal(native.valid, false, `${label}: native invalidation required`);
    assert.equal(await terminal(record), false);
    assert.equal(owner(), home); assert.equal(windows.size, size - (label === 'destroyed-target' ? 1 : 0));
    await inspect(`race:${label}`); results.push({ label, valid: native.valid, reason: native.reason });
  }
  const stable = await capture(other);
  assert.equal((await input.request('validate', { token: stable.token })).valid, true);
  assert.equal(await terminal(stable), true); assert.equal(owner(), other);
  assert.equal(await terminal(stable), false, 'one-shot/replay');
  await transfer(home, 'regression-return-stable-strip'); await inspect('race:stable-strip');
  results.push({ label: 'stable-strip-and-replay', passed: true });

  const old = other.getBounds();
  await rejected(await capture(other), 'moved-target', () => other.setPosition(old.x + 90, old.y));
  other.setBounds(old); await delay(150);
  await rejected(await capture(other), 'move-away-and-back', async () => {
    other.setPosition(old.x + 60, old.y); await delay(70); other.setBounds(old);
  });
  await rejected(await capture(other), 'resized-target', () => other.setSize(old.width + 40, old.height));
  other.setBounds(old); await delay(150);

  const victim = await shell('terminal-race-victim', { x: 1450, y: 300 });
  const destroyed = await capture(victim);
  await rejected(destroyed, 'destroyed-target', () => victim.destroy());

  // A new topmost window replaces the captured target: never reinterpret as a new drop.
  const occluded = await capture(other);
  const count = windows.size;
  const occluder = await shell('terminal-race-occluder', { x: old.x, y: old.y }, false);
  occluder.setAlwaysOnTop(true); occluder.showInactive(); occluder.moveTop();
  await delay(140);
  const invalid = await input.request('validate', { token: occluded.token });
  assert.notEqual(invalid.reason, 'unknown-token', 'replacement must exercise saved native record, not blur cancellation');
  assert.notEqual(invalid.target?.hwnd, occluded.target.hwnd, 'actual topmost target replaced');
  assert.equal(invalid.valid, false); assert.equal(await terminal(occluded), false);
  assert.equal(owner(), home); assert.equal(windows.size, count + 1); occluder.close();
  await inspect('race:replaced-target'); results.push({ label: 'replaced-target', passed: true, reason: invalid.reason });

  for (const destroy of [false, true]) {
    const record = await capture(other), size = windows.size;
    const flash = await shell('flash-occluder', { x: old.x, y: old.y }, false);
    flash.setAlwaysOnTop(true); flash.showInactive(); flash.hide();
    if (destroy) flash.destroy();
    await delay(150);
    const check = await input.request('validate', { token: record.token });
    assert.equal(check.valid, false, 'queued SHOW must survive delivery-time HIDE/DESTROY');
    assert.equal(check.reason, 'window-topology-changed');
    assert.equal(await terminal(record), false); assert.equal(owner(), home);
    if (!flash.isDestroyed()) flash.close();
    await delay(100);
    assert.equal(windows.size, size);
    await inspect(`race:flash-${destroy ? 'destroy' : 'hide'}`);
    results.push({ label: `queued-show-then-${destroy ? 'destroy' : 'hide'}`, passed: true });
  }
  const finalRace = await capture(other);
  assert.equal((await input.request('validate', { token: finalRace.token })).valid, true);
  other.setPosition(old.x + 40, old.y); await delay(140);
  const finished = await input.request('finish', { token: finalRace.token });
  assert.equal(finished.valid, false); assert.equal(finished.consumed, true);
  assert.equal(await terminal(finalRace), false); assert.equal(owner(), home);
  other.setBounds(old); await delay(140); await inspect('race:finish-rechecks');
  results.push({ label: 'atomic-finish-rechecks-after-prior-validate', passed: true, reason: finished.reason });

  const prepareRace = await capture(other);
  const prepared = await shell('prepared-show-race', { x: old.x, y: old.y }, false);
  const ph = prepared.getNativeWindowHandle();
  await input.request('prepare', { token: prepareRace.token, hwnd: ph.length === 8 ? ph.readBigUInt64LE().toString() : String(ph.readUInt32LE()), pid: process.pid });
  prepared.setAlwaysOnTop(true); prepared.showInactive(); prepared.hide(); await delay(150);
  const forbiddenShow = await input.request('finish', { token: prepareRace.token });
  assert.equal(forbiddenShow.valid, false); assert.equal(forbiddenShow.reason, 'window-topology-changed');
  assert.equal(await terminal(prepareRace), false); assert.equal(owner(), home);
  prepared.close(); await delay(100); await inspect('race:prepared-later-shown');
  results.push({ label: 'prepared-then-show-hide-revokes-exemption', passed: true });

  // An unregistered ordinary window models the backdrop of a custom tear-out,
  // not a target that can import a tab. This is still synthetic terminal input.
  const backdrop = await shell('outside-backdrop-fixture', { x: 1450, y: 300 });
  windows.delete(backdrop.id);
  const outside = await capture(backdrop);
  const prior = windows.size;
  assert.equal(await terminal(outside), true);
  assert.equal(windows.size, prior + 1, 'one tear-out shell, not desktop-only');
  const detached = owner();
  await transfer(home, 'regression-return-outside'); detached.close();
  await inspect('race:ordinary-outside-detach');
  results.push({ label: 'ordinary-outside-native-backdrop', passed: true });

  const goneOutside = await capture(backdrop);
  backdrop.destroy(); await delay(140);
  const gone = await input.request('validate', { token: goneOutside.token });
  assert.equal(gone.valid, false);
  const beforeGone = windows.size;
  assert.equal(await terminal(goneOutside), false);
  assert.equal(windows.size, beforeGone); assert.equal(owner(), home);
  await inspect('race:outside-disappeared-no-fallback');
  results.push({ label: 'outside-disappeared-no-desktop-fallback', passed: true, reason: gone.reason });

  // Immutable policy: an ordinary outside application is valid tear-out backdrop,
  // not a desktop-only rule; taskbar/system menus and own content remain unsupported.
  const base = { kind: 'up', dipPoint: { x: 50, y: 90 }, target: { hwnd: 'foreign', className: 'NormalApp' } };
  assert.equal(classify(base, []).kind, 'detach');
  assert.equal(classify({ ...base, target: { ...base.target, className: 'WorkerW' } }, []).kind, 'detach');
  assert.equal(classify({ ...base, target: { ...base.target, className: 'Shell_TrayWnd' } }, []).kind, 'cancel');
  results.push({ label: 'outside-not-desktop-only-policy', passed: true, evidence: 'pure-policy' });
  other.setAlwaysOnTop(false);
  log('terminal-regression-passed', { results, physicalInput: 'NOT_TESTED', terminalSource: 'synthetic-sample/native-HWND' });
  return results;
}
module.exports = { runTerminalRegression };
