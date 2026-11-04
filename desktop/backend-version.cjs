// Version recovery at desktop startup, including servers predating /runtime/recover (§2.7).
const path = require('node:path');

function compareVersions(left, right) {
  const parse = value => {
    const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(value || '');
    if (!match) throw new Error('unknownBackendVersion');
    return match.slice(1).map(Number);
  };
  const a = parse(left), b = parse(right);
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return Math.sign(a[i] - b[i]);
  return 0;
}

async function backendStartPlan(existing, expectedVersion, readProject) {
  if (!existing) return { reuse: false, replacement: false, args: ['-NoOpen', '-Desktop'] };
  if (existing.service !== 'voxarium_studio' || existing.status !== 'ok') throw new Error('portBusy');
  if (existing.version === expectedVersion) return { reuse: true };
  // Never replace a newer backend with the older desktop's bundled runtime.
  if (compareVersions(existing.version, expectedVersion) > 0) throw new Error('desktopTooOld');
  const project = await readProject();
  if (!project?.root || !path.isAbsolute(project.root)) throw new Error('restartProjectUnknown');
  return { reuse: false, replacement: true, args: ['-NoOpen', '-Project', project.root] };
}

module.exports = { backendStartPlan, compareVersions };
