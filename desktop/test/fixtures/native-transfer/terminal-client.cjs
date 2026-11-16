'use strict';
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const path = require('node:path');

// Prewarmed helper: the hook, NOT this transport, captures the terminal target.
class NativeTerminal {
  constructor(log, onTerminal) {
    this.log = log; this.pending = new Map(); this.nextId = 0;
    this.child = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-File',
      path.join(__dirname, 'terminal-input.ps1')], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    this.ready = new Promise((resolve, reject) => {
      this.resolveReady = resolve; this.rejectReady = reject;
      this.readyTimer = setTimeout(() => reject(new Error('native helper startup timeout')), 15_000);
    });
    createInterface({ input: this.child.stdout }).on('line', line => {
      try {
        const record = JSON.parse(line.replace(/^\uFEFF/, ''));
        log('native-helper', { record });
        if (record.event === 'ready') { clearTimeout(this.readyTimer); this.resolveReady(record); }
        else if (record.event === 'terminal') onTerminal(record);
        else if (record.id !== undefined) {
          const p = this.pending.get(String(record.id));
          if (p) { clearTimeout(p.timer); this.pending.delete(String(record.id));
            record.ok ? p.resolve(record) : p.reject(new Error(record.error || 'native helper rejected command')); }
        }
      } catch (error) { this.fail(error); }
    });
    this.child.stderr.on('data', data => log('native-helper-stderr', { text: String(data) }));
    this.child.stdin.on('error', error => { if (!this.stopping) this.fail(error); });
    this.child.on('error', error => this.fail(error));
    this.child.on('exit', code => { if (!this.stopping) this.fail(new Error(`native helper exited ${code}`)); });
  }
  fail(error) {
    clearTimeout(this.readyTimer); this.rejectReady(error);
    for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(error); }
    this.pending.clear(); this.log('native-helper-error', { error: error.message });
    this.onFailure?.(error);
  }
  async request(op, payload = {}) {
    await this.ready;
    const id = String(++this.nextId);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`native ${op} timeout`)); }, 4000);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(JSON.stringify({ id, op, ...payload }) + '\n', error => {
        if (error) { clearTimeout(timer); this.pending.delete(id); reject(error); }
      });
    });
  }
  async stop() {
    this.stopping = true;
    const exited = new Promise(resolve => this.child.once('exit', resolve));
    let timer;
    try {
      await this.request('quit'); this.child.stdin.end();
      await Promise.race([exited, new Promise(resolve => { timer = setTimeout(() => { this.child.kill(); resolve(); }, 2000); })]);
    } finally { clearTimeout(timer); if (this.child.exitCode === null) this.child.kill(); }
  }
}
module.exports = { NativeTerminal };
