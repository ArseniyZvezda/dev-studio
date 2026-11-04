import { afterEach, expect, it, vi } from 'vitest'
import { waitForBackendVersion } from './runtime-version'

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

it('waits through the old listener and a closed port until the expected Studio version answers', async () => {
  vi.useFakeTimers()
  const response = (version: string) => ({ ok: true, json: async () => ({ service: 'voxarium_studio', status: 'ok', version }) })
  const fetcher = vi.fn().mockResolvedValueOnce(response('4.5.0')).mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce(response('4.6.0'))
  vi.stubGlobal('fetch', fetcher)
  const waiting = waitForBackendVersion('4.6.0')
  await vi.advanceTimersByTimeAsync(1000)
  await waiting
  expect(fetcher).toHaveBeenCalledTimes(3)
})

it('times out if a different service or version keeps holding the port', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ service: 'other', status: 'ok', version: '4.6.0' }) }))
  const waiting = expect(waitForBackendVersion('4.6.0')).rejects.toThrow('restart-timeout')
  await vi.advanceTimersByTimeAsync(120_000)
  await waiting
})
