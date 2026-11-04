const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { once } = require('node:events');

test('installer checks and stops only the selected installation, protecting its own launcher',
  { skip: process.platform !== 'win32', timeout: 60_000 }, async () => {
    const scratch = path.resolve(__dirname, '../../.scratch');
    fs.mkdirSync(scratch, { recursive: true });
    const root = fs.mkdtempSync(path.join(scratch, 'installer-process-test-'));
    const installation = path.join(root, "Studio's folder");
    const sibling = installation + '-other';
    const helper = path.resolve(__dirname, '../scripts/check-installation.ps1');
    const workers = [];
    let powershell = 'powershell.exe';
    function check(directory, ...args) {
      const result = spawnSync(powershell, ['-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', helper, '-Directory', directory, ...args],
      { windowsHide: true, encoding: 'utf8', timeout: 20_000 });
      assert.ifError(result.error);
      return result.status;
    }
    async function start(directory, source = 'setInterval(() => {}, 1000); console.log("ready")') {
      const child = spawn(path.join(directory, 'worker.exe'), ['-e', source],
        { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
      const exited = once(child, 'exit');
      workers.push({ child, exited });
      await once(child.stdout, 'data');
      return child;
    }
    try {
      for (const directory of [installation, sibling]) {
        fs.mkdirSync(directory);
        fs.copyFileSync(process.execPath, path.join(directory, 'worker.exe'));
      }
      assert.equal(check(installation), 0);
      const first = await start(installation);
      const second = await start(installation);
      const other = await start(sibling);
      assert.equal(check(installation.toUpperCase()), 1, 'Read-only check detects busy installations');
      assert.doesNotThrow(() => process.kill(first.pid, 0), 'Checking must not terminate a worker');
      assert.equal(check(path.parse(installation).root, '-Terminate'), 2, 'Never target an entire drive');
      assert.equal(check(installation + '\\', '-Terminate', '-WaitSeconds', '5'), 0);
      assert.throws(() => process.kill(first.pid, 0));
      assert.throws(() => process.kill(second.pid, 0));
      assert.doesNotThrow(() => process.kill(other.pid, 0), 'A sibling folder with the same prefix is unrelated');

      const nativeWorker = await start(installation);
      powershell = path.join(process.env.WINDIR, 'SysWOW64/WindowsPowerShell/v1.0/powershell.exe');
      assert.equal(check(installation), 1, 'NSIS uses 32-bit PowerShell to detect 64-bit workers');
      assert.equal(check(installation, '-Terminate', '-WaitSeconds', '5'), 0);
      assert.throws(() => process.kill(nativeWorker.pid, 0), '32-bit PowerShell must stop the 64-bit worker');
      assert.doesNotThrow(() => process.kill(other.pid, 0));
      powershell = 'powershell.exe';

      // Simulate setup running from inside its own target directory.
      const launcher = await start(installation, `
        console.log('ready');
        const r = require('node:child_process').spawnSync('powershell.exe',
          ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', ${JSON.stringify(helper)},
           '-Directory', ${JSON.stringify(installation)}, '-InstallerProcessId', String(process.pid),
           '-Terminate', '-WaitSeconds', '2'], { windowsHide: true });
        process.exit(r.status ?? 99);
      `);
      assert.equal((await workers.find(w => w.child === launcher).exited)[0], 0,
        'The helper must neither report nor terminate the installer itself');

      await start(installation, 'setTimeout(() => {}, 1800); console.log("ready")');
      assert.equal(check(installation, '-WaitSeconds', '5'), 0, 'Normal updater shutdown gets time to finish');
    } finally {
      for (const { child, exited } of workers) {
        if (child.exitCode === null && child.signalCode === null) child.kill();
        await exited;
      }
      // mkdtemp above confines cleanup to this test's own scratch directory.
      assert.equal(path.dirname(path.resolve(root)), scratch);
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
