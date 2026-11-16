'use strict';
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const path = require('node:path');

// VS-52 / dev spec §5.3.4. Only the helper's native message-loop captures the
// release target. A transport failure poisons this instance; never retry a lease.
class NativeTerminal {
  constructor(log = () => {}, onTerminal = () => {}, { spawnProcess = spawn,
    startupTimeout = 15000, commandTimeout = 4000, stopTimeout = 2000 } = {}) {
    this.log = log; this.onTerminal = onTerminal; this.pending = new Map(); this.nextId = 0;
    this.commandTimeout = commandTimeout; this.stopTimeout = stopTimeout;
    this.ready = new Promise((resolve, reject) => { this.resolveReady = resolve; this.rejectReady = reject; });
    // Prewarming can fail before the first gesture; do not leak an unhandled rejection.
    this.ready.catch(() => {});
    // PowerShell cannot read ASAR virtual paths. electron-builder ships this
    // helper as extraResources/native-terminal.ps1, outside the archive.
    const helper = /\.asar(?:[\\/]|$)/.test(__dirname)
      ? path.join(process.resourcesPath, 'native-terminal.ps1') : path.join(__dirname, 'native-terminal.ps1');
    this.child = spawnProcess('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
      helper], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    this.readyTimer = setTimeout(() => this.fail(new Error('Native helper startup timeout')), startupTimeout);
    this.lines = createInterface({ input: this.child.stdout });
    this.lines.on('line', line => {
      if (this.failure || this.stopped) return;
      try {
        const record = JSON.parse(line.replace(/^\uFEFF/, ''));
        if (record.event === 'ready') {
          if (record.ready !== true || record.provenance !== 'native-hook') throw new Error('Invalid native helper readiness');
          clearTimeout(this.readyTimer); this.resolveReady(record);
        } else if (record.event === 'terminal') this.onTerminal(record);
        else if (record.id !== undefined) {
          const p = this.pending.get(String(record.id));
          if (p) {
            clearTimeout(p.timer); this.pending.delete(String(record.id));
            record.ok === true ? p.resolve(record) : p.reject(new Error(record.error || 'Native helper rejected command'));
          }
        } else throw new Error('Invalid native helper response');
      } catch (error) { this.fail(error); }
    });
    this.lines.on('error', error => this.fail(error));
    this.child.stderr.on('data', data => this.log('native-helper-stderr', { text: String(data).slice(0, 4096) }));
    this.child.stdin.on('error', error => this.fail(error));
    this.child.on('error', error => this.fail(error));
    this.child.on('exit', code => {
      this.exited = true;
      if (!this.stopping) this.fail(new Error(`Native helper exited ${code}`));
    });
  }
  fail(error) {
    if (this.failure || this.stopped) return;
    this.failure = error; clearTimeout(this.readyTimer); this.rejectReady(error);
    for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(error); }
    this.pending.clear();
    this.log('native-helper-error', { error: error.message });
    this.child.kill(); // no orphaned hooks after timeout/pipe/parse failure
    this.onFailure?.(error);
  }
  async request(op, payload = {}) {
    await this.ready;
    if (this.failure || this.stopped || (this.stopping && op !== 'quit')) throw this.failure || new Error('Native helper stopped');
    const id = String(++this.nextId);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail(new Error(`Native ${op} timeout`)), this.commandTimeout);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.child.stdin.write(JSON.stringify({ ...payload, id, op }) + '\n', error => { if (error) this.fail(error); });
      } catch (error) { this.fail(error); }
    });
  }
  stop() {
    if (this.stopPromise) return this.stopPromise;
    this.stopping = true;
    return this.stopPromise = (async () => {
      let timer;
      const exited = new Promise(resolve => {
        if (this.exited) resolve(); else this.child.once('exit', resolve);
      });
      try {
        await Promise.race([
          (async () => { if (!this.failure && !this.exited) await this.request('quit'); this.child.stdin.end(); await exited; })(),
          new Promise(resolve => { timer = setTimeout(resolve, this.stopTimeout); }),
        ]);
      } catch {} finally {
        clearTimeout(timer); clearTimeout(this.readyTimer);
        this.fail(new Error('Native helper stopped'));
        this.stopped = true; this.lines.close();
        if (!this.exited) this.child.kill();
      }
    })();
  }
}
module.exports = { NativeTerminal };
