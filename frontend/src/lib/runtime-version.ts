import { version } from '../../package.json'

/** Compiled into this bundle; never copied from the server's reply (§2.5). */
export const FRONTEND_VERSION = version

export async function waitForBackendVersion(version: string): Promise<void> {
  const deadline = Date.now() + 120_000
  while (Date.now() < deadline) {
    try {
      const response = await fetch('/healthz', { cache: 'no-store', signal: AbortSignal.timeout(1500) })
      if (response.ok) {
        const health = await response.json()
        if (health.service === 'voxarium_studio' && health.status === 'ok' && health.version === version) return
      }
    } catch { /* Expected while the old process releases the port. */ }
    await new Promise(resolve => setTimeout(resolve, 500))
  }
  throw new Error('restart-timeout')
}
