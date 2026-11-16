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
import { useRunsStore } from './runs'
beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
  mocks.handlers.clear()
  mocks.scope.isLegacy = false
  mocks.scope.matches.mockReturnValue(true)
  mocks.api.nodeTypes.mockResolvedValue({ node_types: [], categories: {} })
  mocks.api.pipelines.mockResolvedValue([{ id: 'refreshed' }])
})
const detail = (id: string) => ({ run: { id, status: 'completed' }, nodes: [{ node_id: 'node', status: 'succeeded', outputs: {} }], paused_nodes: [] })
it('recovers the tracked snapshot and available logs with coalesced reads', async () => {
  const store = useRunsStore()
  store.subscribe()
  store.trackedRunId = 'one'
  mocks.api.run.mockResolvedValue(detail('one'))
  mocks.api.nodeLog.mockResolvedValue({ exists: true, text: 'recovered\n' })
  const gap = mocks.handlers.get('system.dropped')!
  await Promise.all([gap({ resync: true }, {}), gap({ resync: true }, {})])
  expect(mocks.api.run).toHaveBeenCalledTimes(1)
  expect(store.currentRun?.id).toBe('one')
  expect(store.logs.node).toEqual([{ stream: 'stdout', line: 'recovered' }])
})
it('cannot replace a newly selected run with a delayed snapshot', async () => {
  const store = useRunsStore()
  store.subscribe()
  store.trackedRunId = 'one'
  let resolve!: (value: unknown) => void
  mocks.api.run.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const recovery = mocks.handlers.get('system.dropped')!({ resync: true }, {})
  await vi.waitFor(() => expect(resolve).toBeTypeOf('function'))
  mocks.api.run.mockResolvedValueOnce(detail('two'))
  await store.openRun('two')
  resolve(detail('one'))
  await recovery
  expect(store.currentRun?.id).toBe('two')
  expect(mocks.api.nodeLog).not.toHaveBeenCalled()
})
it('cannot replace a new run or new live lines with delayed logs', async () => {
  const store = useRunsStore()
  store.subscribe()
  mocks.api.run.mockResolvedValue(detail('one'))
  await store.openRun('one')
  let resolve!: (value: unknown) => void
  mocks.api.nodeLog.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const pending = store.fetchNodeLog('node')
  mocks.handlers.get('node.log')!({ run_id: 'one', node_id: 'node', stream: 'stderr', line: 'live' })
  resolve({ exists: true, text: 'old' })
  await pending
  expect(store.logs.node).toEqual([{ stream: 'stdout', line: 'old' }, { stream: 'stderr', line: 'live' }])
  mocks.api.nodeLog.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const other = store.fetchNodeLog('node')
  store.reset()
  resolve({ exists: true, text: 'old' })
  await other
  expect(store.logs).toEqual({})
})
it('ignores legacy, foreign and non-resync notices', async () => {
  const store = useRunsStore()
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

it('preserves newer node WS state over a delayed recovery snapshot', async () => {
  const store = useRunsStore()
  store.subscribe()
  store.trackedRunId = 'one'
  let resolve!: (value: unknown) => void
  mocks.api.run.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  mocks.api.nodeLog.mockResolvedValue({ exists: false })
  const recovery = mocks.handlers.get('system.dropped')!({ resync: true }, {})
  await vi.waitFor(() => expect(resolve).toBeTypeOf('function'))
  mocks.handlers.get('node.status_changed')!({ run_id: 'one', node_id: 'node', status: 'completed', error: null, from_cache: false, outputs: { answer: 42 } })
  resolve({ ...detail('one'), nodes: [{ node_id: 'node', status: 'running', outputs: {} }] })
  await recovery
  expect(store.nodeStatus.node).toBe('completed')
  expect(store.nodeOutputs.node).toEqual({ answer: 42 })
})
it.each(['1\n2\n', '1\n2\n3\n'])('backfills missing lines while retaining the concurrent live suffix: %j', async (text) => {
  const store = useRunsStore()
  store.subscribe()
  mocks.api.run.mockResolvedValue(detail('one'))
  await store.openRun('one')
  store.logs = { node: [{ stream: 'stdout', line: '1' }] }
  let resolve!: (value: unknown) => void
  mocks.api.nodeLog.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const pending = store.fetchNodeLog('node')
  mocks.handlers.get('node.log')!({ run_id: 'one', node_id: 'node', stream: 'stdout', line: '3' })
  mocks.handlers.get('node.log')!({ run_id: 'one', node_id: 'node', stream: 'stdout', line: '4' })
  resolve({ exists: true, text })
  await pending
  expect(store.logs.node?.map((entry) => entry.line)).toEqual(['1', '2', '3', '4'])
})
it('repeats a snapshot when a gap arrives during the current recovery', async () => {
  const store = useRunsStore()
  store.subscribe()
  store.trackedRunId = 'one'
  let resolve!: (value: unknown) => void
  mocks.api.run.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  mocks.api.run.mockResolvedValue(detail('one'))
  mocks.api.nodeLog.mockResolvedValue({ exists: false })
  const gap = mocks.handlers.get('system.dropped')!
  const recovery = gap({ resync: true }, {})
  await vi.waitFor(() => expect(resolve).toBeTypeOf('function'))
  gap({ resync: true }, {})
  resolve(detail('one'))
  await recovery
  expect(mocks.api.run).toHaveBeenCalledTimes(2)
})
