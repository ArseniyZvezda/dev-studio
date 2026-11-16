import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
const mocks = vi.hoisted(() => ({
  api: { nodeTypes: vi.fn(), pipelines: vi.fn(), pipeline: vi.fn(), run: vi.fn(), nodeLog: vi.fn() },
  scope: { isLegacy: false, matches: vi.fn(() => true) },
  handlers: new Map<string, (payload: any, envelope?: any) => any>(),
}))
vi.mock('@/api/client', () => ({ api: mocks.api }))
vi.mock('@/session/project-session', () => ({ rendererSession: mocks.scope }))
vi.mock('@/ws/connection', () => ({ onEvent: (type: string, handler: any) => mocks.handlers.set(type, handler) }))
import { usePipelinesStore } from './pipelines'
beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
  mocks.handlers.clear()
  mocks.scope.isLegacy = false
  mocks.scope.matches.mockReturnValue(true)
  mocks.api.nodeTypes.mockResolvedValue({ node_types: [], categories: {} })
  mocks.api.pipelines.mockResolvedValue([{ id: 'refreshed' }])
})
it('refreshes only the server registry/list, coalescing gaps and preserving draft issues', async () => {
  const store = usePipelinesStore()
  store.subscribe()
  store.issues = [{ message: 'draft' }] as any
  const gap = mocks.handlers.get('system.dropped')!
  await Promise.all([gap({ resync: true }, {}), gap({ resync: true }, {})])
  expect(mocks.api.pipelines).toHaveBeenCalledTimes(1)
  expect(mocks.api.nodeTypes).toHaveBeenCalledTimes(1)
  expect(mocks.api.pipeline).not.toHaveBeenCalled()
  expect(store.issues).toEqual([{ message: 'draft' }])
  expect(store.summaries).toEqual([{ id: 'refreshed' }])
})
it('ignores legacy, foreign and non-resync notices', async () => {
  const store = usePipelinesStore()
  store.subscribe()
  const gap = mocks.handlers.get('system.dropped')!
  await gap({ resync: false }, {})
  mocks.scope.matches.mockReturnValue(false)
  await gap({ resync: true }, {})
  mocks.scope.isLegacy = true
  mocks.scope.matches.mockReturnValue(true)
  await gap({ resync: true }, {})
  expect(mocks.api.pipelines).not.toHaveBeenCalled()
  expect(mocks.api.run).not.toHaveBeenCalled()
})

it('schedules a final list refresh for a gap arriving during a read', async () => {
  const store = usePipelinesStore()
  store.subscribe()
  let resolve!: (value: unknown) => void
  mocks.api.pipelines.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const gap = mocks.handlers.get('system.dropped')!
  const recovery = gap({ resync: true }, {})
  await vi.waitFor(() => expect(resolve).toBeTypeOf('function'))
  gap({ resync: true }, {})
  resolve([{ id: 'old' }])
  await recovery
  expect(mocks.api.pipelines).toHaveBeenCalledTimes(2)
  expect(store.summaries).toEqual([{ id: 'refreshed' }])
})
