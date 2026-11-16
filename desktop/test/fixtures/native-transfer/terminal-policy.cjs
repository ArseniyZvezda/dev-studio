'use strict';
const assert = require('node:assert/strict');

// Immutable event-time decision. Later observations may only INVALIDATE it.
function classify(terminal, shells) {
  if (terminal.kind !== 'up' || !terminal.target || terminal.target.hwnd === '0') return { kind: 'cancel' };
  const target = terminal.target;
  const shell = shells.find(w => w.hwnd === target.hwnd);
  if (shell) {
    const p = terminal.dipPoint, c = shell.content;
    return p.x >= c.x + 8 && p.x < c.x + c.width - 8 && p.y >= c.y + 8 && p.y < c.y + 42
      ? { kind: 'strip', windowId: shell.id, generation: shell.generation }
      : { kind: 'cancel', reason: 'unsupported shell surface' };
  }
  // Custom captured drag is not a file/tab drop into the foreign app. Any ordinary
  // outside window can be backdrop for tear-out; desktop-only is NOT the product rule.
  if (/^(Shell_TrayWnd|Shell_SecondaryTrayWnd|#32768|#32770)$/.test(target.className))
    return { kind: 'cancel', reason: 'unsupported system surface' };
  return { kind: 'detach' };
}
function assertLease(lease, drag, windows, revision) {
  assert.ok(!lease.consumed && drag && lease.token === drag.token && drag.revision === revision &&
    Date.now() <= drag.expires, 'stale/cancelled/consumed terminal');
  if (lease.decision.kind === 'strip') {
    const target = windows.get(lease.decision.windowId);
    assert.ok(target && !target.isDestroyed() && target.spikeGeneration === lease.decision.generation,
      'terminal target changed/disappeared');
  }
}
module.exports = { classify, assertLease };
