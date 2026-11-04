// Version recovery at desktop startup, including servers predating /runtime/recover (§2.7).
const path = require('node:path');

function compareVersions(left, right) {
  const parse = value => {
    const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/.exec(value || '');
    if (!match) throw new Error('unknownBackendVersion');
    const prerelease = match[4]?.split('.') || [];
    if (prerelease.some(part => /^0\d+$/.test(part))) throw new Error('unknownBackendVersion');
    return { core: match.slice(1, 4).map(BigInt), prerelease };
  };
  const a = parse(left), b = parse(right);
  for (let i = 0; i < 3; i++) if (a.core[i] !== b.core[i]) return a.core[i] < b.core[i] ? -1 : 1;
  // Release tags may include prereleases (§2.7); stable versions outrank those
  // with the same core, and numeric identifiers use numeric rather than text order.
  if (!a.prerelease.length || !b.prerelease.length) {
    return Number(!a.prerelease.length) - Number(!b.prerelease.length);
  }
  for (let i = 0; i < Math.max(a.prerelease.length, b.prerelease.length); i++) {
    const l = a.prerelease[i], r = b.prerelease[i];
    if (l === r) continue;
    if (l === undefined) return -1;
    if (r === undefined) return 1;
    const ln = /^\d+$/.test(l), rn = /^\d+$/.test(r);
    if (ln !== rn) return ln ? -1 : 1;
    return (ln ? BigInt(l) < BigInt(r) : l < r) ? -1 : 1;
  }
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
