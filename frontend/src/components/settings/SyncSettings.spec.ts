// @vitest-environment jsdom
/** VS-176: real component + Pinia + project API transport, never a Remote server.
 * studio-design §5.2–5.3: the settings owner changes local identity only; DocsView
 * owns editor proof/ACK. HTTP responses below model that boundary, not backend interop.
 */
import { createPinia, disposePinia } from 'pinia'
import { createApp, h, nextTick, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectRemoteCommitReceipt, ProjectRemotePreparedChange, ProjectRemoteRecovery, ProjectRemoteStatus } from '@/api/types'
import { setLocale, t } from '@/i18n'
import { rendererSession } from '@/session/project-session'
import { useSyncStore } from '@/stores/sync'
import { ws } from '@/ws/connection'
import type { RemoteBindingChange } from '@/ws/events'
import SyncSettings from './SyncSettings.vue'

// The real singleton client captures this immutable renderer, just as bootstrap does.
vi.hoisted(() => { window.history.replaceState({}, '', '/p/local-A/settings') })
rendererSession.bind({ project_id: 'local-A', epoch: 'runtime-A' })

const generation = 7
// I5 fences oldG+1; I6 publishes a further safe, monotonic G in this epoch.
// No callback obtains its request precondition from latest mutable metadata.
const committedGeneration = generation + 2
const initialFingerprint = 'a'.repeat(64)
const committedFingerprint = 'b'.repeat(64)
const newId = 'next-game.v2_1'
const changeId = 'change-A'
const remotePath = '/api/projects/local-A/remote'
const mounted: { app: App; host: HTMLDivElement; active: boolean }[] = []
const piniae: ReturnType<typeof createPinia>[] = []
let phaseSeq = 100

function metadata(patch: Partial<ProjectRemoteStatus> = {}): ProjectRemoteStatus {
  return { project_id: 'local-A', epoch: 'runtime-A', backend_epoch: 'backend-A',
    persistent_id: 'old-game', identity_revision: 3, config_fingerprint: initialFingerprint,
    url: 'https://remote.example/team', enabled: true, has_token: true,
    binding_generation: generation, state: 'connected', active_rooms: 0, pending_writes: 0,
    ...patch }
}
function receipt(patch: Partial<ProjectRemoteCommitReceipt> = {}): ProjectRemoteCommitReceipt {
  return { ...metadata(), committed: true, change_id: changeId,
    binding_change: { change_id: changeId, state: 'committed' }, ...patch }
}
function prepared(patch: Partial<ProjectRemotePreparedChange> = {}): ProjectRemotePreparedChange {
  return { change_id: changeId, expires_at: Date.now() / 1000 + 120,
    from: { persistent_id: 'old-game', url: metadata().url },
    to: { persistent_id: newId, url: metadata().url, enabled: false }, required_editor_acks: [],
    plan: { operation: 'edit_id', enabled: false }, conflicts: [], ...patch }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
type Call = { method: string; path: string; body: unknown; url: URL }
type Handler = (call: Call) => Response | Promise<Response>
function refusal(code: string, status = 409) {
  return Response.json({ code, details: {} }, { status })
}
function harness(initial = metadata()) {
  const calls: Call[] = []
  const handlers = new Map<string, Handler>()
  let current = initial
  handlers.set('GET ', () => Response.json(current))
  handlers.set('POST /prepare', () => Response.json(prepared()))
  handlers.set(`POST /changes/${changeId}/quiesce`, () => Response.json({ state: 'ready', missing_acks: [] }))
  handlers.set(`POST /changes/${changeId}/commit`, () => {
    current = metadata({ persistent_id: newId, identity_revision: 4,
      config_fingerprint: committedFingerprint, binding_generation: committedGeneration,
      enabled: false, state: 'disabled' })
    return Response.json(receipt({ ...current, binding_change: { change_id: changeId, state: 'committed' } }))
  })
  handlers.set(`DELETE /changes/${changeId}`, () => new Response(null, { status: 204 }))
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), window.location.origin)
    const call: Call = { method: init?.method ?? 'GET', path: url.pathname,
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)), url }
    calls.push(call)
    const handler = handlers.get(`${call.method} ${call.path.startsWith(remotePath) ? call.path.slice(remotePath.length) : call.path}`)
    if (!handler) throw new Error(`Unexpected request: ${call.method} ${url}`)
    return handler(call)
  }))
  const pinia = createPinia()
  piniae.push(pinia)
  const store = useSyncStore(pinia)
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(SyncSettings) })
  app.use(pinia)
  const view = { app, host, active: true }
  mounted.push(view)
  app.mount(host)
  return { calls, handlers, store, view, setMetadata: (value: ProjectRemoteStatus) => { current = value } }
}
type Harness = ReturnType<typeof harness>
function node<T extends HTMLElement = HTMLElement>(host: ParentNode, name: string): T {
  const found = host.querySelector<T>(`[data-testid="remote-id-${name}"]`)
  expect(found, name).not.toBeNull()
  return found!
}
function button(h: Harness, name: string) { return node<HTMLButtonElement>(h.view.host, name) }
function input(h: Harness) { return node<HTMLInputElement>(h.view.host, 'input') }
function forceClick(element: HTMLElement) { element.dispatchEvent(new MouseEvent('click', { bubbles: true })) }
async function flush() {
  // Drain transport JSON promises and Vue watchers; no sleeps or wall-clock polling.
  for (let i = 0; i < 30; i++) { await Promise.resolve(); await nextTick() }
}
async function enter(h: Harness, value = newId) {
  input(h).value = value
  input(h).dispatchEvent(new Event('input', { bubbles: true }))
  await flush()
}
async function review(h: Harness, value = newId) {
  await enter(h, value)
  button(h, 'review').click()
  await flush()
}
async function confirm(h: Harness) { button(h, 'confirm').click(); await flush() }
function mutations(h: Harness) { return h.calls.filter(call => call.method !== 'GET') }
function count(h: Harness, suffix: string, method = 'POST') {
  return h.calls.filter(call => call.method === method && call.path === remotePath + suffix).length
}
function error(h: Harness) {
  const message = node(h.view.host, 'error').textContent?.trim()
  expect(message).toBeTruthy()
  expect(message).not.toMatch(/^sync\./)
  return message
}
function assertTransport(h: Harness) {
  for (const call of h.calls) {
    expect(call.path.startsWith(remotePath), call.path).toBe(true)
    expect(call.url.searchParams.get('project_id')).toBe('local-A')
    expect(call.url.searchParams.get('epoch')).toBe('runtime-A')
    expect(call.path).not.toMatch(/docs|editor-ack|editors|enable|migration|preview|servers|sync$/)
    expect(call.method).not.toBe('PUT')
  }
  expect(WebSocket).not.toHaveBeenCalled()
}
function unmount(h: Harness) {
  if (!h.view.active) return
  h.view.active = false
  h.view.app.unmount()
  h.view.host.remove()
}
function emitPhase(patch: Partial<RemoteBindingChange> = {}) {
  // Feed the real connection's parser without opening a socket. Production DocsView
  // receives this same scoped frame; these settings tests must never fabricate its ACK.
  const data: RemoteBindingChange = { change_id: changeId, expires_at: Date.now() / 1000 + 120,
    state: 'ready', binding_generation: generation + 1, expected_generation: generation,
    fenced: true, required_editor_acks: [], missing_acks: [], reason: null, ...patch }
  const connection = ws as unknown as { dispatch(raw: unknown): void }
  connection.dispatch(JSON.stringify({ type: 'remote.binding_change', data,
    project_id: 'local-A', epoch: 'runtime-A', seq: ++phaseSeq, ts: '' }))
}

function memoryStorage(): Storage {
  const items = new Map<string, string>()
  return { get length() { return items.size }, clear: () => items.clear(),
    getItem: key => items.get(key) ?? null, key: index => [...items.keys()][index] ?? null,
    removeItem: key => { items.delete(key) }, setItem: (key, value) => { items.set(key, String(value)) } }
}
beforeEach(async () => {
  await setLocale('en')
  vi.stubGlobal('WebSocket', vi.fn())
  // Node ≥ 25 defines its own global localStorage (undefined without
  // --localstorage-file), shadowing jsdom's; the API client reads its token there.
  if (!window.localStorage) vi.stubGlobal('localStorage', memoryStorage())
})
afterEach(() => {
  for (const view of mounted.splice(0)) {
    if (view.active) view.app.unmount()
    view.host.remove()
  }
  piniae.splice(0).forEach(disposePinia)
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('local ID settings', () => {
  it('loads only scoped metadata and exposes no key, mutable URL or future Remote actions', async () => {
    const h = harness()
    await flush()
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    expect(h.view.host.textContent).toContain(metadata().url)
    expect(h.view.host.querySelector('input[type="password"], input[type="checkbox"], select')).toBeNull()
    for (const field of h.view.host.querySelectorAll<HTMLInputElement>('input')) {
      if (field !== input(h)) expect(field.readOnly || field.disabled).toBe(true)
    }
    expect(mutations(h)).toEqual([])
    assertTransport(h)
  })

  it.each(['en', 'ru'] as const)('requires review/confirmation and commits a disabled local identity (%s)', async language => {
    await setLocale(language)
    const h = harness()
    const scope = rendererSession.scope
    const bind = vi.spyOn(rendererSession, 'bind')
    const invalidate = vi.spyOn(rendererSession, 'invalidate')
    await flush()
    await review(h)
    expect(mutations(h)).toEqual([])
    expect(h.view.host.textContent).toContain(newId)
    await confirm(h)
    expect(mutations(h).map(call => call.path.slice(remotePath.length))).toEqual([
      '/prepare', `/changes/${changeId}/quiesce`, `/changes/${changeId}/commit`,
    ])
    expect(mutations(h)[0]!.body).toMatchObject({ backend_epoch: 'backend-A', operation: 'edit_id', expected_revision: 3,
      config_fingerprint: initialFingerprint, persistent_id: newId, url: metadata().url })
    expect(mutations(h)[1]!.body).toEqual({ expected_generation: generation })
    expect(mutations(h)[2]!.body).toEqual({ expected_revision: 3, expected_generation: generation + 1,
      config_fingerprint: initialFingerprint, confirmation: { url: metadata().url, persistent_id: newId } })
    expect(node(h.view.host, 'current').textContent).toContain(newId)
    expect(h.store.remote).toMatchObject({ persistent_id: newId, enabled: false, state: 'disabled' })
    expect(h.store.remote?.binding_generation).toBe(committedGeneration)
    expect(node(h.view.host, 'notice').textContent?.trim()).toBeTruthy()
    expect(rendererSession.scope).toBe(scope)
    expect(bind).not.toHaveBeenCalled()
    expect(invalidate).not.toHaveBeenCalled()
    assertTransport(h)
  })

  it.each(['', ' OLD', 'new ', 'New', '-new', '.new', '_new', 'new/id', 'new\\id',
    'другой', 'a'.repeat(65), 'new\tid'])('rejects noncanonical exact input %j without normalization or request', async value => {
    const h = harness()
    await flush()
    await enter(h, value)
    expect(input(h).value).toBe(value)
    expect(button(h, 'review').disabled).toBe(true)
    forceClick(button(h, 'review'))
    await flush()
    expect(h.view.host.querySelector('[data-testid="remote-id-confirm"]')).toBeNull()
    expect(mutations(h)).toEqual([])
    assertTransport(h)
  })

  it.each(['a', '0', 'a'.repeat(64), 'a._-9'])('accepts canonical boundary %j for review without requests', async value => {
    const h = harness()
    await flush()
    await enter(h, value)
    expect(button(h, 'review').disabled).toBe(false)
    button(h, 'review').click()
    await flush()
    expect(button(h, 'confirm').disabled).toBe(false)
    expect(mutations(h)).toEqual([])
    assertTransport(h)
  })

  it('does not offer a no-op edit of the current ID', async () => {
    const h = harness()
    await flush()
    await enter(h, 'old-game')
    expect(button(h, 'review').disabled).toBe(true)
    forceClick(button(h, 'review'))
    await flush()
    expect(mutations(h)).toEqual([])
  })

  it('cancels review before any mutation', async () => {
    const h = harness()
    await flush()
    await review(h)
    button(h, 'cancel').click()
    await flush()
    expect(mutations(h)).toEqual([])
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    assertTransport(h)
  })

  it('late-cancels the exact prepared change after Cancel during prepare', async () => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set('POST /prepare', () => pending.promise)
    await flush()
    await review(h)
    await confirm(h)
    button(h, 'cancel').click()
    await flush()
    pending.resolve(Response.json(prepared()))
    await flush()
    expect(count(h, '/prepare')).toBe(1)
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(1)
    expect(count(h, `/changes/${changeId}/quiesce`)).toBe(0)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    assertTransport(h)
  })

  it('cancels during quiesce and ignores a late ready result', async () => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/quiesce`, () => pending.promise)
    await flush()
    await review(h)
    await confirm(h)
    expect(count(h, `/changes/${changeId}/quiesce`)).toBe(1)
    button(h, 'cancel').click()
    await flush()
    pending.resolve(Response.json({ state: 'ready', missing_acks: [] }))
    await flush()
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(1)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    assertTransport(h)
  })

  it('shows DELETE cancellation failure without retry, commit, or claiming a successful cancellation', async () => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/quiesce`, () => pending.promise)
    h.handlers.set(`DELETE /changes/${changeId}`, () => { throw new Error('Synthetic DELETE unavailable') })
    await flush()
    await review(h)
    await confirm(h)
    button(h, 'cancel').click()
    await flush()
    expect(error(h)).toBe(t('sync.identity.cancelFailed'))
    expect(h.view.host.textContent).not.toContain(t('sync.identity.cancelled'))
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(1)
    pending.resolve(Response.json({ state: 'ready', missing_acks: [] }))
    await flush()
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(1)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    expect(error(h)).toBe(t('sync.identity.cancelFailed'))
    assertTransport(h)
  })

  it('shows a paused cancellation response honestly without resuming the fenced binding', async () => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/quiesce`, () => pending.promise)
    h.handlers.set(`DELETE /changes/${changeId}`, () => Response.json({
      change_id: changeId, state: 'paused', binding_generation: generation + 1,
      expected_generation: generation, fenced: true, reason: 'resume_required',
    }))
    await flush()
    await review(h)
    await confirm(h)
    button(h, 'cancel').click()
    await flush()
    expect(node(h.view.host, 'notice').textContent).toBe(t('sync.identity.paused'))
    expect(button(h, 'review').disabled).toBe(true)
    pending.resolve(Response.json({ state: 'ready', missing_acks: [] }))
    await flush()
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(1)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    assertTransport(h)
  })

  it('cannot cancel, duplicate-confirm or refresh an in-flight commit', async () => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/commit`, () => pending.promise)
    await flush()
    await review(h)
    await confirm(h)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(1)
    const cancel = h.view.host.querySelector<HTMLButtonElement>('[data-testid="remote-id-cancel"]')
    if (cancel) { expect(cancel.disabled).toBe(true); forceClick(cancel) }
    const confirmButton = h.view.host.querySelector<HTMLButtonElement>('[data-testid="remote-id-confirm"]')
    if (confirmButton) forceClick(confirmButton)
    forceClick(button(h, 'refresh'))
    await flush()
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(0)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(1)
    pending.resolve(Response.json(receipt({ persistent_id: newId, identity_revision: 4,
      config_fingerprint: committedFingerprint,
      binding_generation: committedGeneration, enabled: false, state: 'disabled' })))
    await flush()
    assertTransport(h)
  })

  it.each(['stale_binding', 'stale_binding_generation', 'stale_backend_epoch', 'pending_edits',
    'unknown_clients', 'reload_required', 'mutation_busy', 'recovery_required'])('surfaces prepare 409 %s without retry or legacy fallback', async code => {
    const h = harness()
    h.handlers.set('POST /prepare', () => refusal(code))
    await flush()
    await review(h)
    await confirm(h)
    error(h)
    await flush()
    expect(count(h, '/prepare')).toBe(1)
    expect(count(h, `/changes/${changeId}/quiesce`)).toBe(0)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    assertTransport(h)
  })

  it.each(['pending_edits', 'room_unresolved', 'unknown_clients', 'offline', 'timeout'])('preserves a blocked drain (%s), never acknowledges editors itself', async reason => {
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/quiesce`, () => Response.json({
      state: 'blocked', missing_acks: ['editor-dirty'], reason,
    }))
    await flush()
    await review(h)
    await confirm(h)
    error(h)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    expect(count(h, `/changes/${changeId}/quiesce`)).toBe(1)
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    assertTransport(h)
  })

  it.each(['prepare', 'quiesce', 'commit'] as const)('shows transport failure at %s without retry or success', async stage => {
    const h = harness()
    const path = stage === 'prepare' ? '/prepare' : `/changes/${changeId}/${stage}`
    h.handlers.set(`POST ${path}`, () => { throw new Error('Synthetic unavailable transport') })
    await flush()
    await review(h)
    await confirm(h)
    error(h)
    await flush()
    expect(count(h, path)).toBe(1)
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    expect(h.view.host.querySelector('[data-testid="remote-id-notice"]')).toBeNull()
    assertTransport(h)
  })

  it('never substitutes latest generation after the prepared response is delayed', async () => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set('POST /prepare', () => pending.promise)
    await flush()
    await review(h)
    await confirm(h)
    h.setMetadata(metadata({ binding_generation: 99, identity_revision: 9, config_fingerprint: 'foreign' }))
    expect(await h.store.loadRemote()).toBe(true)
    await flush()
    pending.resolve(Response.json(prepared()))
    await flush()
    expect(count(h, '/prepare')).toBe(1)
    expect(count(h, `/changes/${changeId}/quiesce`)).toBe(0)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    assertTransport(h)
  })

  it('does not adopt latest G after quiesce has started', async () => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/quiesce`, () => pending.promise)
    await flush()
    await review(h)
    await confirm(h)
    h.setMetadata(metadata({ binding_generation: 99 }))
    expect(await h.store.loadRemote()).toBe(true)
    await flush()
    pending.resolve(Response.json({ state: 'ready', missing_acks: [] }))
    await flush()
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    assertTransport(h)
  })

  it('unmount during prepare only cleans up the exact late change', async () => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set('POST /prepare', () => pending.promise)
    await flush()
    await review(h)
    await confirm(h)
    unmount(h)
    pending.resolve(Response.json(prepared()))
    await flush()
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(1)
    expect(count(h, `/changes/${changeId}/quiesce`)).toBe(0)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    assertTransport(h)
  })

  it('retains the reviewed target even if a disabled input receives a programmatic event', async () => {
    const h = harness()
    await flush()
    await review(h)
    expect(input(h).disabled).toBe(true)
    await enter(h, 'unreviewed-target')
    await confirm(h)
    expect(mutations(h)[0]!.body).toMatchObject({ persistent_id: newId })
    expect(mutations(h)[2]!.body).toMatchObject({ confirmation: { persistent_id: newId } })
    expect(node(h.view.host, 'current').textContent).toContain(newId)
    assertTransport(h)
  })

  it.each([
    { identity_revision: 4 }, { config_fingerprint: 'new-config' }, { binding_generation: generation + 1 },
  ])('rejects review snapshot drift before sending prepare: %j', async patch => {
    const h = harness()
    await flush()
    await review(h)
    h.setMetadata(metadata(patch))
    expect(await h.store.loadRemote()).toBe(true)
    await confirm(h)
    error(h)
    expect(mutations(h)).toEqual([])
    assertTransport(h)
  })

  it.each([
    { identity_revision: 4 }, { config_fingerprint: 'new-config' },
  ])('rejects same-G observation drift while prepare is pending: %j', async patch => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set('POST /prepare', () => pending.promise)
    await flush()
    await review(h)
    await confirm(h)
    h.setMetadata(metadata(patch))
    expect(await h.store.loadRemote()).toBe(true)
    pending.resolve(Response.json(prepared()))
    await flush()
    error(h)
    expect(count(h, `/changes/${changeId}/quiesce`)).toBe(0)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    assertTransport(h)
  })

  it('allows only its own exact G+1 ready fence, not an automatic latest-G retry', async () => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/quiesce`, () => pending.promise)
    await flush()
    await review(h)
    await confirm(h)
    h.setMetadata(metadata({ binding_generation: generation + 1, state: 'quiescing' }))
    emitPhase()
    await flush()
    expect(h.store.remote?.binding_generation).toBe(generation + 1)
    pending.resolve(Response.json({ state: 'ready', missing_acks: [] }))
    await flush()
    expect(count(h, `/changes/${changeId}/commit`)).toBe(1)
    expect(mutations(h).find(call => call.path.endsWith('/commit'))!.body).toMatchObject({
      expected_generation: generation + 1, expected_revision: 3, config_fingerprint: initialFingerprint,
    })
    expect(node(h.view.host, 'current').textContent).toContain(newId)
    assertTransport(h)
  })

  it.each([
    { change_id: 'foreign-change' }, { state: 'paused' as const },
    { state: 'cancelled' as const }, { state: 'expired' as const },
    { binding_generation: generation + 2 },
  ])('does not commit after a foreign/stopped fence: %j', async phase => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/quiesce`, () => pending.promise)
    await flush()
    await review(h)
    await confirm(h)
    h.setMetadata(metadata({ binding_generation: phase.binding_generation ?? generation + 1, state: 'quiescing' }))
    emitPhase(phase)
    await flush()
    pending.resolve(Response.json({ state: 'ready', missing_acks: [] }))
    await flush()
    error(h)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    assertTransport(h)
  })

  it.each(['stale_binding', 'stale_binding_generation', 'pending_edits'])('does not retry commit 409 %s or cancel an unknown commit outcome', async code => {
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/commit`, () => refusal(code))
    await flush()
    await review(h)
    await confirm(h)
    error(h)
    await flush()
    expect(count(h, `/changes/${changeId}/commit`)).toBe(1)
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(0)
    expect(button(h, 'review').disabled).toBe(true)
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    assertTransport(h)
  })

  it('ignores a delayed commit response superseded by a foreign binding change', async () => {
    const pending = deferred<Response>()
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/commit`, () => pending.promise)
    await flush()
    await review(h)
    await confirm(h)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(1)
    h.setMetadata(metadata({ persistent_id: 'foreign-game', identity_revision: 10,
      binding_generation: committedGeneration + 100, config_fingerprint: 'foreign-config',
      enabled: false, state: 'disabled' }))
    emitPhase({ change_id: 'foreign-change', binding_generation: committedGeneration + 100 })
    await flush()
    expect(h.store.remote?.persistent_id).toBe('foreign-game')
    pending.resolve(Response.json(receipt({ persistent_id: newId, identity_revision: 4,
      config_fingerprint: committedFingerprint,
      binding_generation: committedGeneration, enabled: false, state: 'disabled' })))
    await flush()
    expect(error(h)).toBe(t('sync.identity.commitUnknown'))
    expect(node(h.view.host, 'current').textContent).toContain('foreign-game')
    expect(h.view.host.querySelector('[data-testid="remote-id-notice"]')).toBeNull()
    expect(count(h, `/changes/${changeId}/commit`)).toBe(1)
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(0)
    assertTransport(h)
  })

  it('fails closed for a legacy backend-range committed G that JavaScript cannot represent safely', async () => {
    const h = harness()
    // A legacy/malformed backend's 63-bit result is rounded by JSON.parse.
    // Preserve the guard even after I6 adopts safe monotonic integers.
    const backendJsonGeneration = '9223372036854775807'
    const result = metadata({ persistent_id: newId, identity_revision: 4,
      config_fingerprint: committedFingerprint, enabled: false, state: 'disabled' })
    const raw = JSON.stringify(receipt({ ...result, binding_change: { change_id: changeId, state: 'committed' } })).replace('"binding_generation":7', `"binding_generation":${backendJsonGeneration}`)
    const unsafe = JSON.parse(raw) as ProjectRemoteStatus
    expect(Number.isSafeInteger(unsafe.binding_generation)).toBe(false)
    h.handlers.set(`POST /changes/${changeId}/commit`, () => {
      h.setMetadata(unsafe)
      return new Response(raw, { headers: { 'Content-Type': 'application/json' } })
    })
    await flush()
    await review(h)
    await confirm(h)
    expect(error(h)).toBe(t('sync.identity.commitUnknown'))
    expect(h.view.host.querySelector('[data-testid="remote-id-notice"]')).toBeNull()
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    expect(h.store.remote?.binding_generation).toBe(generation)
    // Explicit refresh still cannot admit rounded G or secretly repair/retry it.
    button(h, 'refresh').click()
    await flush()
    expect(error(h)).toBe(t('sync.identity.unavailable'))
    expect(h.store.remote?.binding_generation).toBe(generation)
    expect(button(h, 'review').disabled).toBe(true)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(1)
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(0)
    assertTransport(h)
  })

  it('fails closed when a safe final G regresses below the observed G', async () => {
    const h = harness(metadata({ binding_generation: 900 }))
    h.handlers.set(`POST /changes/${changeId}/commit`, () => {
      const result = metadata({ persistent_id: newId, identity_revision: 4,
        config_fingerprint: committedFingerprint,
        binding_generation: 5, enabled: false, state: 'disabled' })
      h.setMetadata(result)
      return Response.json(receipt({ ...result, binding_change: { change_id: changeId, state: 'committed' } }))
    })
    await flush()
    await review(h)
    await confirm(h)
    // A regressed receipt remains inadmissible even if an old backend emits it.
    // The monotonic observation contract is not relaxed by a successful HTTP status;
    // neither the response nor a refresh authorizes guessed/latest G or retry.
    expect(error(h)).toBe(t('sync.identity.commitUnknown'))
    expect(h.view.host.querySelector('[data-testid="remote-id-notice"]')).toBeNull()
    button(h, 'refresh').click()
    await flush()
    expect(error(h)).toBe(t('sync.identity.unavailable'))
    expect(h.store.remote?.binding_generation).toBe(900)
    expect(button(h, 'review').disabled).toBe(true)
    expect(mutations(h).find(call => call.path.endsWith('/quiesce'))!.body).toEqual({ expected_generation: 900 })
    expect(mutations(h).find(call => call.path.endsWith('/commit'))!.body).toMatchObject({ expected_generation: 901 })
    expect(count(h, `/changes/${changeId}/commit`)).toBe(1)
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(0)
    assertTransport(h)
  })

  it.each([
    { persistent_id: 'foreign-game' }, { identity_revision: 99 },
    { url: 'https://other.example' }, { enabled: true }, { state: 'connected' as const },
    { project_id: 'local-B' }, { epoch: 'runtime-B' }, { backend_epoch: 'backend-B' },
    { binding_generation: undefined as unknown as number },
    { config_fingerprint: undefined as unknown as string },
    { binding_generation: -1 }, { binding_generation: 1.5 },
    { binding_generation: '900' as unknown as number },
    { config_fingerprint: initialFingerprint }, { config_fingerprint: 'not-a-fingerprint' },
    { config_fingerprint: 'b'.repeat(63) },
  ])('does not advertise success for a mismatched commit result: %j', async patch => {
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/commit`, () => Response.json(receipt({
      persistent_id: newId, identity_revision: 4, binding_generation: committedGeneration,
      config_fingerprint: committedFingerprint, enabled: false, state: 'disabled', ...patch,
    })))
    await flush()
    await review(h)
    await confirm(h)
    error(h)
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    expect(h.view.host.querySelector('[data-testid="remote-id-notice"]')).toBeNull()
    expect(count(h, `/changes/${changeId}/commit`)).toBe(1)
    expect(count(h, `/changes/${changeId}`, 'DELETE')).toBe(0)
    assertTransport(h)
  })

  it('clears a saved notice when the follow-up metadata contradicts the commit receipt', async () => {
    const h = harness()
    // A valid receipt followed by the old observation must not advertise the
    // old ID as the successfully saved destination, even on a 200 response.
    h.handlers.set(`POST /changes/${changeId}/commit`, () => Response.json(receipt({
      persistent_id: newId, identity_revision: 4, binding_generation: committedGeneration,
      config_fingerprint: committedFingerprint, enabled: false, state: 'disabled',
    })))
    await flush()
    await review(h)
    await confirm(h)
    expect(error(h)).toBe(t('sync.identity.commitUnknown'))
    expect(h.view.host.querySelector('[data-testid="remote-id-notice"]')).toBeNull()
    expect(node(h.view.host, 'current').textContent).toContain('old-game')
    expect(button(h, 'review').disabled).toBe(true)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(1)
    assertTransport(h)
  })

  it('requires explicit refresh after a refused mutation; refresh alone cannot retry it', async () => {
    const h = harness()
    h.handlers.set('POST /prepare', () => refusal('stale_binding'))
    await flush()
    await review(h)
    await confirm(h)
    error(h)
    expect(button(h, 'review').disabled).toBe(true)
    forceClick(button(h, 'review'))
    await flush()
    expect(count(h, '/prepare')).toBe(1)
    button(h, 'refresh').click()
    await flush()
    expect(count(h, '/prepare')).toBe(1)
    expect(h.view.host.querySelector('[data-testid="remote-id-error"]')).toBeNull()
    assertTransport(h)
  })

  it('keeps a failed refresh unavailable instead of guessing revision or G', async () => {
    const h = harness()
    await flush()
    h.handlers.set('GET ', () => refusal('reload_required'))
    button(h, 'refresh').click()
    await flush()
    error(h)
    await enter(h)
    forceClick(button(h, 'review'))
    await flush()
    expect(button(h, 'review').disabled).toBe(true)
    expect(mutations(h)).toEqual([])
    assertTransport(h)
  })
})


const profileId = '11111111-1111-4111-8111-111111111111'
const storage = 'c'.repeat(32)
const globalServersPath = '/api/remote/servers'
const projectsPath = `${globalServersPath}/${profileId}/projects`
function projectNode<T extends HTMLElement = HTMLElement>(h: Harness, name: string) {
  const element = h.view.host.querySelector<T>(`[data-testid="remote-project-${name}"]`)
  expect(element, name).not.toBeNull()
  return element!
}
function projectButton(h: Harness, name: string) { return projectNode<HTMLButtonElement>(h, name) }
async function projectInput(h: Harness, name: string, value: string) {
  const element = projectNode<HTMLInputElement>(h, name)
  element.value = value
  element.dispatchEvent(new Event('input', { bubbles: true }))
  await flush()
}
async function catalogHarness(initial = metadata()) {
  const h = harness(initial)
  const profile = { profile_id: profileId, name: 'Same label', url: metadata().url, revision: 7,
    has_token: true, credential_source: 'stored', verification: { state: 'unavailable' } }
  const project = { id: newId, display_name: 'Same label', storage_epoch: storage,
    metadata_revision: 1, created_at: '2026-11-16T00:00:00Z' }
  h.handlers.set(`GET ${globalServersPath}`, () => Response.json({ items: [profile] }))
  h.handlers.set(`GET ${projectsPath}`, () => Response.json({ items: [project], next_cursor: null }))
  await flush()
  projectButton(h, 'servers').click()
  await flush()
  const select = projectNode<HTMLSelectElement>(h, 'profile')
  select.value = profileId
  select.dispatchEvent(new Event('change', { bubbles: true }))
  await flush()
  projectButton(h, 'catalog').click()
  await flush()
  return { ...h, profile, project }
}

describe('explicit gated project Remote actions', () => {
  it.each(['en', 'ru'] as const)('keeps network gates honest on metadata load (%s)', async language => {
    await setLocale(language)
    const h = harness()
    await flush()
    expect(h.calls).toHaveLength(1)
    for (const name of ['catalog', 'create', 'preview', 'enable']) expect(projectButton(h, name).disabled).toBe(true)
    forceClick(projectButton(h, 'enable'))
    await flush()
    expect(h.calls).toHaveLength(1)
    expect(h.view.host.textContent).toContain(t('sync.project.enableGate'))
    expect(h.view.host.querySelector('input[type="password"]')).toBeNull()
  })

  it('loads metadata and catalog only explicitly; identical labels still show exact ID and URL', async () => {
    const h = await catalogHarness()
    expect(h.calls.map(call => call.method + ' ' + call.path)).toEqual([
      `GET ${remotePath}`, `GET ${globalServersPath}`, `GET ${projectsPath}`,
    ])
    expect(h.view.host.textContent).toContain(newId)
    expect(h.view.host.textContent).toContain(h.profile.url)
    expect(h.calls.filter(call => call.path.startsWith(globalServersPath)).every(call => !call.url.searchParams.has('epoch'))).toBe(true)
    expect(mutations(h)).toEqual([])
    expect(WebSocket).not.toHaveBeenCalled()
  })

  it('shows backend catalog gate refusal, with no Create/Select/fallback despite valid metadata', async () => {
    const h = await catalogHarness()
    h.handlers.set(`GET ${projectsPath}`, () => refusal('catalog_unavailable', 503))
    projectButton(h, 'catalog').click()
    await flush()
    expect(projectNode(h, 'error').textContent).toBe(t('sync.project.unavailable'))
    expect(h.view.host.querySelector('[data-testid="remote-project-select"]')).toBeNull()
    expect(projectButton(h, 'create').disabled).toBe(true)
    expect(mutations(h)).toEqual([])
  })

  it.each([
    { id: 'New' }, { storage_epoch: 'invented' }, { metadata_revision: 2 ** 60 },
    { created_at: null }, { display_name: null },
  ])('rejects malformed HTTP-200 catalog row %j', async patch => {
    const h = await catalogHarness()
    h.handlers.set(`GET ${projectsPath}`, () => Response.json({ items: [{ ...h.project, ...patch }], next_cursor: null }))
    projectButton(h, 'catalog').click()
    await flush()
    expect(projectNode(h, 'error').textContent).toBeTruthy()
    expect(h.view.host.querySelector('[data-testid="remote-project-select"]')).toBeNull()
    expect(projectButton(h, 'create').disabled).toBe(true)
  })

  it('requires distinct Select review and commits disabled without Create, Preview or Enable', async () => {
    const h = await catalogHarness()
    h.handlers.set('POST /prepare', call => {
      expect(call.body).toMatchObject({ operation: 'select', profile_id: profileId,
        catalog_id: newId, catalog_instance_id: storage, url: h.profile.url })
      return Response.json(prepared())
    })
    h.handlers.set(`POST /changes/${changeId}/commit`, () => {
      const result = receipt({ persistent_id: newId, identity_revision: 4, binding_generation: committedGeneration,
        config_fingerprint: committedFingerprint, enabled: false, state: 'disabled',
        profile_id: profileId, space_instance_id: storage })
      h.setMetadata(result)
      return Response.json(result)
    })
    projectButton(h, 'select').click()
    await flush()
    expect(mutations(h)).toEqual([])
    expect(h.view.host.textContent).toContain(t('sync.project.selectTitle'))
    await confirm(h)
    expect(node(h.view.host, 'notice').textContent).toBe(t('sync.project.selected'))
    expect(h.store.remote).toMatchObject({ persistent_id: newId, enabled: false, profile_id: profileId })
    expect(mutations(h).map(call => call.path)).toEqual([
      `${remotePath}/prepare`, `${remotePath}/changes/${changeId}/quiesce`, `${remotePath}/changes/${changeId}/commit`,
    ])
    expect(projectButton(h, 'enable').disabled).toBe(true)
  })

  it('cancels Select review with no barrier or editor mutation', async () => {
    const h = await catalogHarness()
    projectButton(h, 'select').click()
    await flush()
    button(h, 'cancel').click()
    await flush()
    expect(mutations(h)).toEqual([])
    expect(h.store.remote?.persistent_id).toBe('old-game')
  })

  it.each(['created', 'conflict', 'unknown', 'rejected'] as const)('Create outcome %s is not Select/Enable/retry', async state => {
    const h = await catalogHarness()
    h.handlers.set(`POST ${projectsPath}`, () => Response.json({ state,
      project: state === 'created' ? h.project : null,
      error_code: state === 'created' ? null : state === 'conflict' ? 'project_exists'
        : state === 'unknown' ? 'create_unknown' : 'invalid_display_name' }, { status: state === 'created' ? 201 : 200 }))
    await projectInput(h, 'create-id', newId)
    await projectInput(h, 'create-name', 'Same label')
    projectButton(h, 'create').click()
    await flush()
    expect(projectNode(h, 'create-result').textContent).toBe(t(`sync.project.outcome.${state}`))
    expect(projectButton(h, 'create').disabled).toBe(true)
    forceClick(projectButton(h, 'create'))
    await flush()
    expect(mutations(h)).toHaveLength(1)
    expect(mutations(h)[0]!.path).toBe(projectsPath)
    expect(mutations(h)[0]!.body).toMatchObject({ id: newId, display_name: 'Same label', expected_revision: 7 })
    expect(h.store.remote?.persistent_id).toBe('old-game')
    expect(h.store.remote?.enabled).toBe(true) // Create never mutates local project
    expect(WebSocket).not.toHaveBeenCalled()
  })

  it('observes only the retained original operation after unknown, without a second POST Create', async () => {
    const h = await catalogHarness()
    let operation = ''
    h.handlers.set(`POST ${projectsPath}`, call => {
      operation = (call.body as { operation_id: string }).operation_id
      return Response.json({ state: 'unknown', project: null, error_code: 'create_unknown' })
    })
    await projectInput(h, 'create-id', newId)
    await projectInput(h, 'create-name', 'Original name')
    projectButton(h, 'create').click()
    await flush()
    h.handlers.set(`POST ${projectsPath}/operations/${operation}/observe`, () => Response.json({ state: 'found', project: h.project, error_code: null }))
    await projectInput(h, 'create-id', 'different-id') // disabled event cannot rebind original operation
    projectButton(h, 'observe').click()
    await flush()
    expect(projectNode(h, 'create-result').textContent).toBe(t('sync.project.outcome.found'))
    expect(mutations(h).map(call => call.path)).toEqual([projectsPath, `${projectsPath}/operations/${operation}/observe`])
    expect(h.store.remote?.persistent_id).toBe('old-game')
  })

  it('retains an exceptional Create intent and does not reinterpret a 503 as unknown/success', async () => {
    const h = await catalogHarness()
    h.handlers.set(`POST ${projectsPath}`, () => refusal('catalog_unavailable', 503))
    await projectInput(h, 'create-id', newId)
    projectButton(h, 'create').click()
    await flush()
    expect(projectNode(h, 'error').textContent).toBe(t('sync.project.unavailable'))
    expect(projectButton(h, 'create').disabled).toBe(true)
    expect(h.view.host.querySelector('[data-testid="remote-project-create-result"]')).toBeNull()
    expect(mutations(h)).toHaveLength(1)
  })

  it('discards a catalog callback after unmount, without selecting a result', async () => {
    const h = await catalogHarness()
    const pending = deferred<Response>()
    h.handlers.set(`GET ${projectsPath}`, () => pending.promise)
    projectButton(h, 'catalog').click()
    await flush()
    unmount(h)
    pending.resolve(Response.json({ items: [h.project], next_cursor: null }))
    await flush()
    expect(mutations(h)).toEqual([])
  })

  it.each(['unavailable', 'malformed'] as const)('read-only Preview %s never releases Enable', async variant => {
    const h = harness(metadata({ enabled: false, state: 'disabled', profile_id: profileId, space_instance_id: storage }))
    // VS-186: a malformed HTTP-200 preview (non-hex id, object conflicts) is not Enable authority.
    h.handlers.set('POST /reconcile-preview', () => variant === 'malformed'
      ? Response.json({ preview_id: 'preview-A', fingerprint: 'f'.repeat(64), counts: { conflicts: 1 },
        conflicts: [{}], expires_at: Date.now() / 1000 + 60 })
      : refusal('preview_unavailable', 503))
    await flush()
    projectButton(h, 'preview').click()
    await flush()
    expect(h.view.host.querySelector('[data-testid="remote-project-preview-result"]')).toBeNull()
    if (variant === 'unavailable') expect(projectNode(h, 'error').textContent).toBe(t('sync.project.unavailable'))
    else expect(projectNode(h, 'error').textContent).toBe(t('sync.identity.failed'))
    expect(projectButton(h, 'enable').disabled).toBe(true)
    expect(mutations(h).map(call => call.path)).toEqual([`${remotePath}/reconcile-preview`])
    expect(WebSocket).not.toHaveBeenCalled()
  })

  it('does not display a late Preview after a same-G drain event', async () => {
    const h = harness(metadata({ enabled: false, state: 'disabled', profile_id: profileId, space_instance_id: storage }))
    const pending = deferred<Response>()
    h.handlers.set('POST /reconcile-preview', () => pending.promise)
    await flush()
    projectButton(h, 'preview').click()
    await flush()
    emitPhase({ binding_generation: generation, state: 'draining', fenced: false })
    await flush()
    pending.resolve(Response.json(previewDto()))
    await flush()
    expect(h.view.host.querySelector('[data-testid="remote-project-preview-result"]')).toBeNull()
    expect(projectButton(h, 'enable').disabled).toBe(true)
  })
})

describe('ID delivery receipt and restart regressions', () => {
  it.each([
    { committed: false }, { change_id: 'another-change' }, { binding_change: { change_id: 'another', state: 'committed' } },
    { binding_change: null }, { binding_generation: generation + 1 }, { active_rooms: 1 },
    { pending_writes: 1 }, { profile_id: profileId },
  ])('refuses an incomplete/inconsistent commit receipt %j', async patch => {
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/commit`, () => Response.json(receipt({ persistent_id: newId,
      identity_revision: 4, config_fingerprint: committedFingerprint, binding_generation: committedGeneration,
      enabled: false, state: 'disabled', ...patch } as unknown as Partial<ProjectRemoteCommitReceipt>)))
    await flush(); await review(h); await confirm(h)
    expect(error(h)).toBe(t('sync.identity.commitUnknown'))
    expect(h.view.host.querySelector('[data-testid="remote-id-notice"]')).toBeNull()
    expect(h.store.remote?.persistent_id).toBe('old-game')
    assertTransport(h)
  })

  it('does not announce a saved ID when the follow-up store read is unavailable', async () => {
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/commit`, () => {
      h.handlers.set('GET ', () => refusal('metadata_unavailable', 503))
      return Response.json(receipt({ persistent_id: newId, identity_revision: 4,
        config_fingerprint: committedFingerprint, binding_generation: committedGeneration,
        enabled: false, state: 'disabled' }))
    })
    await flush(); await review(h); await confirm(h)
    expect(error(h)).toBe(t('sync.identity.commitUnknown'))
    expect(h.view.host.querySelector('[data-testid="remote-id-notice"]')).toBeNull()
    expect(h.store.remote?.persistent_id).toBe('old-game')
    expect(count(h, `/changes/${changeId}/commit`)).toBe(1)
  })

  it('cannot commit a late old-epoch prepared reply after backend restart observation', async () => {
    const h = harness()
    const pending = deferred<Response>()
    h.handlers.set('POST /prepare', () => pending.promise)
    await flush(); await review(h); await confirm(h)
    h.setMetadata(metadata({ backend_epoch: 'backend-B', binding_generation: 0 }))
    expect(await h.store.loadRemote()).toBe(false)
    expect(h.store.remotePending).toBe(true)
    pending.resolve(Response.json(prepared()))
    await flush()
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    expect(h.view.host.querySelector('[data-testid="remote-id-notice"]')).toBeNull()
    expect(h.store.remote?.persistent_id).toBe('old-game')
  })
})


describe('ID epoch and numeric delivery boundaries', () => {
  it('delivers the last safe monotonic commit G without rounding or latest-G substitution', async () => {
    const old = Number.MAX_SAFE_INTEGER - 2
    const h = harness(metadata({ binding_generation: old }))
    h.handlers.set(`POST /changes/${changeId}/commit`, () => {
      const result = receipt({ persistent_id: newId, identity_revision: 4, config_fingerprint: committedFingerprint,
        binding_generation: Number.MAX_SAFE_INTEGER, enabled: false, state: 'disabled' })
      h.setMetadata(result)
      return Response.json(result)
    })
    await flush(); await review(h); await confirm(h)
    expect(node(h.view.host, 'notice').textContent).toBe(t('sync.identity.saved'))
    expect(h.store.remote?.binding_generation).toBe(Number.MAX_SAFE_INTEGER)
    expect(mutations(h).find(call => call.path.endsWith('/quiesce'))!.body).toEqual({ expected_generation: old })
    expect(mutations(h).find(call => call.path.endsWith('/commit'))!.body).toMatchObject({ expected_generation: old + 1 })
    expect(button(h, 'review').disabled).toBe(true) // no safe room for another fence+commit
    assertTransport(h)
  })

  it.each(['quiesce', 'commit'] as const)('fences a late %s callback after restart, retaining the old observation', async stage => {
    const h = harness()
    const pending = deferred<Response>()
    h.handlers.set(`POST /changes/${changeId}/${stage}`, () => pending.promise)
    await flush(); await review(h); await confirm(h)
    h.setMetadata(metadata({ backend_epoch: 'backend-B', binding_generation: 0 }))
    expect(await h.store.loadRemote()).toBe(false)
    pending.resolve(stage === 'quiesce' ? Response.json({ state: 'ready', missing_acks: [] })
      : Response.json(receipt({ persistent_id: newId, identity_revision: 4,
        config_fingerprint: committedFingerprint, binding_generation: committedGeneration, enabled: false, state: 'disabled' })))
    await flush()
    error(h)
    expect(h.view.host.querySelector('[data-testid="remote-id-notice"]')).toBeNull()
    expect(count(h, `/changes/${changeId}/commit`)).toBe(stage === 'commit' ? 1 : 0)
    expect(h.store.reloadRequired).toBe(true)
    expect(h.store.remote?.backend_epoch).toBe('backend-A')
    assertTransport(h)
  })
})


describe('no contradictory barrier admission', () => {
  it('does not commit a ready response which still lists missing editor ACKs', async () => {
    const h = harness()
    h.handlers.set(`POST /changes/${changeId}/quiesce`, () => Response.json({ state: 'ready', missing_acks: ['unsaved-editor'] }))
    await flush(); await review(h); await confirm(h)
    expect(error(h)).toBe(t('sync.identity.pendingEdits'))
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    expect(h.store.remote?.persistent_id).toBe('old-game')
    assertTransport(h)
  })
})


function freshBlocked(patch: Partial<ProjectRemoteStatus> = {}): ProjectRemoteStatus {
  // New renderer/store, first observation in the new backend lifetime. This is
  // not an old renderer adopting another epoch or restoring a v2 target.
  return metadata({ backend_epoch: 'backend-after-restart', binding_generation: 0,
    persistent_id: 'saved-before-restart', enabled: false, state: 'blocked',
    active_rooms: 0, pending_writes: 0, binding_change: null, reason: null, ...patch })
}

describe('fresh disabled/idle network-blocked local ID attempts', () => {
  it.each(['en', 'ru'] as const)('allows explicit offline ID review and the real local barrier flow (%s)', async language => {
    await setLocale(language)
    const h = harness(freshBlocked())
    h.handlers.set('POST /prepare', call => {
      expect(call.body).toEqual({ backend_epoch: 'backend-after-restart', operation: 'edit_id', persistent_id: newId, url: metadata().url,
        expected_revision: 3, config_fingerprint: initialFingerprint })
      return Response.json(prepared({ from: { persistent_id: 'saved-before-restart', url: metadata().url } }))
    })
    h.handlers.set(`POST /changes/${changeId}/commit`, () => {
      const result = receipt({ backend_epoch: 'backend-after-restart', binding_generation: 2,
        persistent_id: newId, identity_revision: 4, config_fingerprint: committedFingerprint,
        enabled: false, state: 'disabled' })
      h.setMetadata(result)
      return Response.json(result)
    })
    await flush()
    expect(h.store.reloadRequired).toBe(false)
    expect(node(h.view.host, 'offline-attempt').textContent).toBe(t('sync.identity.offlineAttempt'))
    expect(h.view.host.textContent).toContain(t('sync.identity.blocked')) // not a ready/connected claim
    expect(mutations(h)).toEqual([])
    await enter(h)
    expect(button(h, 'review').disabled).toBe(false)
    button(h, 'review').click()
    await flush()
    expect(mutations(h)).toEqual([]) // explicit confirmation still mandatory
    await confirm(h)
    expect(mutations(h).map(call => call.path.slice(remotePath.length))).toEqual([
      '/prepare', `/changes/${changeId}/quiesce`, `/changes/${changeId}/commit`,
    ])
    expect(mutations(h)[1]!.body).toEqual({ expected_generation: 0 })
    expect(mutations(h)[2]!.body).toMatchObject({ expected_generation: 1, expected_revision: 3,
      config_fingerprint: initialFingerprint, confirmation: { url: metadata().url, persistent_id: newId } })
    expect(node(h.view.host, 'notice').textContent).toBe(t('sync.identity.saved'))
    expect(h.store.remote).toMatchObject({ backend_epoch: 'backend-after-restart', binding_generation: 2,
      persistent_id: newId, enabled: false, state: 'disabled' })
    expect(projectButton(h, 'enable').disabled).toBe(true)
    assertTransport(h)
  })

  it.each([
    { enabled: true }, { active_rooms: 1 }, { pending_writes: 1 },
    { binding_change: { change_id: 'pending-change', state: 'blocked' } },
    { binding_change: undefined },
    { reason: 'recovery_required' }, { reason: 'pending_edits' }, { reason: 'resume_required' },
    { state: 'migration_required' as const }, { state: 'quiescing' as const },
  ])('does not grant a local attempt from non-idle/unknown/recovery observation %j', async patch => {
    const h = harness(freshBlocked(patch))
    await flush(); await enter(h)
    expect(button(h, 'review').disabled).toBe(true)
    forceClick(button(h, 'review'))
    await flush()
    expect(h.view.host.querySelector('[data-testid="remote-id-confirm"]')).toBeNull()
    expect(mutations(h)).toEqual([])
    expect(h.store.remote?.persistent_id).toBe('saved-before-restart')
    assertTransport(h)
  })

  it('does not treat an observed phase event as an idle blocked binding', async () => {
    const h = harness(freshBlocked())
    await flush()
    emitPhase({ state: 'blocked', binding_generation: 0, fenced: false, reason: 'pending_edits' })
    await flush(); await enter(h)
    expect(h.store.bindingChange?.state).toBe('blocked')
    expect(button(h, 'review').disabled).toBe(true)
    forceClick(button(h, 'review'))
    await flush()
    expect(mutations(h)).toEqual([])
  })

  it('does not extend the local-ID exception to Select or Enable', async () => {
    const h = await catalogHarness(freshBlocked())
    await enter(h)
    expect(button(h, 'review').disabled).toBe(false)
    expect(projectButton(h, 'select').disabled).toBe(true)
    expect(projectButton(h, 'enable').disabled).toBe(true)
    forceClick(projectButton(h, 'select'))
    forceClick(projectButton(h, 'enable'))
    await flush()
    expect(mutations(h)).toEqual([])
  })

  it.each([
    ['recovery_required', 'sync.identity.recoveryRequired'],
    ['pending_edits', 'sync.identity.pendingEdits'],
  ] as const)('leaves backend prepare authoritative and shows typed %s without retry', async (code, key) => {
    const h = harness(freshBlocked())
    h.handlers.set('POST /prepare', () => refusal(code))
    await flush(); await review(h); await confirm(h)
    expect(error(h)).toBe(t(key))
    expect(input(h).value).toBe(newId)
    expect(h.store.remote?.persistent_id).toBe('saved-before-restart')
    expect(count(h, '/prepare')).toBe(1)
    expect(count(h, `/changes/${changeId}/quiesce`)).toBe(0)
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    expect(button(h, 'review').disabled).toBe(true)
    assertTransport(h)
  })

  it('preserves an authoritative pending-editor drain refusal after the local attempt', async () => {
    const h = harness(freshBlocked())
    h.handlers.set(`POST /changes/${changeId}/quiesce`, () => Response.json({
      state: 'blocked', missing_acks: ['dirty-editor'], reason: 'pending_edits',
    }))
    await flush(); await review(h); await confirm(h)
    expect(error(h)).toBe(t('sync.identity.pendingEdits'))
    expect(count(h, `/changes/${changeId}/commit`)).toBe(0)
    expect(input(h).value).toBe(newId)
    expect(h.store.remote?.persistent_id).toBe('saved-before-restart')
    assertTransport(h) // no Markdown write/editor ACK/force-discard from the form
  })

  it('still requires reload for the old renderer observing this same new blocked epoch', async () => {
    const h = harness()
    await flush()
    h.setMetadata(freshBlocked())
    expect(await h.store.loadRemote()).toBe(false)
    expect(h.store.reloadRequired).toBe(true)
    expect(h.store.remote?.backend_epoch).toBe('backend-A')
    await enter(h)
    expect(button(h, 'review').disabled).toBe(true)
    expect(h.view.host.querySelector('[data-testid="remote-id-offline-attempt"]')).toBeNull()
    forceClick(button(h, 'review'))
    await flush()
    expect(mutations(h)).toEqual([])
    expect(await h.store.loadRemote()).toBe(false)
    expect(h.store.remote?.backend_epoch).toBe('backend-A')
    assertTransport(h)
  })
})

describe('two safe increments before any ID barrier', () => {
  it.each([
    ['disabled', Number.MAX_SAFE_INTEGER - 1], ['disabled', Number.MAX_SAFE_INTEGER],
    ['blocked', Number.MAX_SAFE_INTEGER - 1], ['blocked', Number.MAX_SAFE_INTEGER],
  ] as const)('refuses %s G=%s even when oldG+1 alone would fit', async (state, old) => {
    const h = harness(freshBlocked({ state, binding_generation: old }))
    await flush(); await enter(h)
    expect(button(h, 'review').disabled).toBe(true)
    forceClick(button(h, 'review'))
    await flush()
    expect(mutations(h)).toEqual([])
    expect(h.store.remote?.binding_generation).toBe(old)
    expect(h.view.host.querySelector('[data-testid="remote-id-confirm"]')).toBeNull()
    assertTransport(h)
  })
})

// ------------------------------------------------ VS-186 I13: Preview → explicit Enable / Disable
const previewId = 'd'.repeat(32)
const previewFingerprint = 'e'.repeat(64)
function previewDto(patch: Record<string, unknown> = {}) {
  return { preview_id: previewId, fingerprint: previewFingerprint,
    counts: { unchanged: 2, upload: 1, conflict: 1 }, conflicts: ['notes/plan.md'],
    expires_at: Date.now() / 1000 + 60, base: 'matched', incarnation: { space_instance_id: storage, seq: 5 }, ...patch }
}
function selectedMetadata(patch: Partial<ProjectRemoteStatus> = {}) {
  return metadata({ enabled: false, state: 'disabled', profile_id: profileId, space_instance_id: storage, ...patch })
}
function enableHarness(initial = selectedMetadata()) {
  const h = harness(initial)
  h.handlers.set('POST /reconcile-preview', () => Response.json(previewDto()))
  h.handlers.set('POST /enable', () => {
    const enabled = selectedMetadata({ enabled: true, state: 'connecting', binding_generation: generation + 1 })
    h.setMetadata(enabled)
    return Response.json(enabled)
  })
  return h
}
async function previewNow(h: Harness) {
  await flush()
  projectButton(h, 'preview').click()
  await flush()
}
function enableCalls(h: Harness) { return count(h, '/enable') }

describe('I13 Preview and explicit Enable', () => {
  it('shows profile ID and instance read-only next to ID and URL', async () => {
    const h = harness(selectedMetadata())
    await flush()
    expect(projectNode(h, 'profile-id').textContent).toBe(profileId)
    expect(projectNode(h, 'instance').textContent).toBe(storage)
    expect(node(h.view.host, 'current').textContent).toBe('old-game')
    expect(h.view.host.textContent).toContain(metadata().url)
    const bare = harness(metadata({ profile_id: null, space_instance_id: null }))
    await flush()
    expect(projectNode(bare, 'profile-id').textContent).toBe(t('sync.project.none'))
    expect(projectNode(bare, 'instance').textContent).toBe(t('sync.project.none'))
  })

  it('sends the captured CAS body with Preview and shows counts without enabling', async () => {
    const h = enableHarness()
    expect(projectButton(h, 'enable').disabled).toBe(true)
    await previewNow(h)
    expect(mutations(h).map(call => call.path)).toEqual([`${remotePath}/reconcile-preview`])
    expect(mutations(h)[0]!.body).toEqual({ expected_revision: 3, config_fingerprint: initialFingerprint, backend_epoch: 'backend-A' })
    const result = projectNode(h, 'preview-result').textContent
    expect(result).toContain(t('sync.project.previewResult', { conflicts: 1 }))
    expect(result).toContain(`${t('sync.project.count.upload')}: 1`)
    expect(result).toContain(t('sync.project.base.matched'))
    expect(projectButton(h, 'enable').disabled).toBe(false)
    expect(enableCalls(h)).toBe(0)
    expect(h.store.remote?.enabled).toBe(false)
    expect(WebSocket).not.toHaveBeenCalled()
  })

  it.each(['en', 'ru'] as const)('Enable requires an explicit confirmation of the exact ID and URL, then reloads status (%s)', async language => {
    await setLocale(language)
    const h = enableHarness()
    await previewNow(h)
    projectButton(h, 'enable').click()
    await flush()
    expect(enableCalls(h)).toBe(0)
    expect(projectNode(h, 'enable-review').textContent).toContain(t('sync.project.enableTitle'))
    expect(projectNode(h, 'enable-id').textContent).toBe('old-game')
    expect(projectNode(h, 'enable-url').textContent).toBe(metadata().url)
    expect(projectNode(h, 'enable-conflicts').textContent).toContain('notes/plan.md')
    const reads = h.calls.filter(call => call.method === 'GET').length
    projectButton(h, 'enable-confirm').click()
    await flush()
    expect(enableCalls(h)).toBe(1)
    expect(mutations(h).find(call => call.path === `${remotePath}/enable`)!.body).toEqual({
      expected_revision: 3, expected_generation: generation, preview_id: previewId, fingerprint: previewFingerprint,
      confirmation: { url: metadata().url, persistent_id: 'old-game', action: 'reconcile' },
      config_fingerprint: initialFingerprint, backend_epoch: 'backend-A',
    })
    expect(h.calls.filter(call => call.method === 'GET').length).toBeGreaterThan(reads)
    expect(h.store.remote?.enabled).toBe(true)
    expect(projectNode(h, 'notice').textContent).toBe(t('sync.project.enabled'))
    expect(h.view.host.querySelector('[data-testid="remote-project-preview-result"]')).toBeNull()
    expect(h.view.host.querySelector('[data-testid="remote-project-error"]')).toBeNull()
  })

  it.each(['preview_stale', 'preview_expired', 'instance_mismatch', 'cursor_rollback', 'stale_binding',
    'confirmation_required', 'duplicate_local_binding', 'mystery_code'])('Enable refusal %s enables nothing, drops the preview and never retries', async code => {
    const h = enableHarness()
    h.handlers.set('POST /enable', () => refusal(code))
    await previewNow(h)
    projectButton(h, 'enable').click()
    await flush()
    projectButton(h, 'enable-confirm').click()
    await flush()
    expect(enableCalls(h)).toBe(1)
    const shown = projectNode(h, 'error').textContent?.trim()
    expect(shown).toBeTruthy()
    expect(shown).not.toMatch(/^sync\.|_/)
    expect(shown).not.toContain(code)
    expect(h.store.remote?.enabled).toBe(false)
    expect(h.view.host.querySelector('[data-testid="remote-project-preview-result"]')).toBeNull()
    expect(h.view.host.querySelector('[data-testid="remote-project-enable-review"]')).toBeNull()
    expect(projectButton(h, 'enable').disabled).toBe(true)
    forceClick(projectButton(h, 'enable'))
    await flush()
    expect(enableCalls(h)).toBe(1)
  })

  it('reports an unknown Enable outcome after a transport failure, without retry', async () => {
    const h = enableHarness()
    h.handlers.set('POST /enable', () => { throw new TypeError('network down') })
    await previewNow(h)
    projectButton(h, 'enable').click()
    await flush()
    projectButton(h, 'enable-confirm').click()
    await flush()
    expect(projectNode(h, 'error').textContent).toBe(t('sync.project.enableUnknown'))
    expect(enableCalls(h)).toBe(1)
    expect(projectButton(h, 'preview').disabled).toBe(true) // explicit refresh first
  })

  it('cancel during confirmation sends no Enable request', async () => {
    const h = enableHarness()
    await previewNow(h)
    projectButton(h, 'enable').click()
    await flush()
    projectButton(h, 'enable-cancel').click()
    await flush()
    expect(h.view.host.querySelector('[data-testid="remote-project-enable-review"]')).toBeNull()
    expect(enableCalls(h)).toBe(0)
    expect(h.store.remote?.enabled).toBe(false)
  })

  it('keeps Enable unavailable without a preview or for an expired preview response', async () => {
    const h = enableHarness()
    await flush()
    forceClick(projectButton(h, 'enable'))
    await flush()
    expect(h.view.host.querySelector('[data-testid="remote-project-enable-review"]')).toBeNull()
    h.handlers.set('POST /reconcile-preview', () => Response.json(previewDto({ expires_at: Date.now() / 1000 - 1 })))
    await previewNow(h)
    expect(h.view.host.querySelector('[data-testid="remote-project-preview-result"]')).toBeNull()
    expect(projectButton(h, 'enable').disabled).toBe(true)
    expect(enableCalls(h)).toBe(0)
  })

  it('drops a preview when it expires, even with the confirmation open', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    const h = enableHarness()
    await previewNow(h)
    projectButton(h, 'enable').click()
    await flush()
    vi.advanceTimersByTime(61_000)
    await flush()
    expect(h.view.host.querySelector('[data-testid="remote-project-preview-result"]')).toBeNull()
    expect(h.view.host.querySelector('[data-testid="remote-project-enable-review"]')).toBeNull()
    expect(projectNode(h, 'error').textContent).toBe(t('sync.project.previewExpired'))
    expect(projectButton(h, 'enable').disabled).toBe(true)
    expect(enableCalls(h)).toBe(0)
  })

  it.each([
    { config_fingerprint: 'f'.repeat(64) }, { identity_revision: 4 }, { binding_generation: generation + 1 },
    { space_instance_id: 'b'.repeat(32) }, { url: 'https://other.example/team' },
  ])('drops the preview after snapshot change %j', async patch => {
    const h = enableHarness()
    await previewNow(h)
    projectButton(h, 'enable').click()
    await flush()
    h.setMetadata(selectedMetadata(patch))
    button(h, 'refresh').click()
    await flush()
    expect(h.view.host.querySelector('[data-testid="remote-project-preview-result"]')).toBeNull()
    expect(h.view.host.querySelector('[data-testid="remote-project-enable-confirm"]')).toBeNull()
    expect(projectButton(h, 'enable').disabled).toBe(true)
    forceClick(projectButton(h, 'enable'))
    await flush()
    expect(enableCalls(h)).toBe(0)
  })

  it('drops the preview on a binding phase event', async () => {
    const h = enableHarness()
    await previewNow(h)
    emitPhase({ binding_generation: generation, state: 'draining', fenced: false })
    await flush()
    expect(h.view.host.querySelector('[data-testid="remote-project-preview-result"]')).toBeNull()
    expect(projectButton(h, 'enable').disabled).toBe(true)
  })

  it('rejects a preview of another server project instance', async () => {
    const h = enableHarness()
    h.handlers.set('POST /reconcile-preview', () => Response.json(previewDto({ incarnation: { space_instance_id: 'b'.repeat(32), seq: 1 } })))
    await previewNow(h)
    expect(projectNode(h, 'error').textContent).toBe(t('sync.project.instanceMismatch'))
    expect(projectButton(h, 'enable').disabled).toBe(true)
  })

  it('Select and Create never enable on their own', async () => {
    const h = await catalogHarness(selectedMetadata())
    await projectInput(h, 'create-id', 'another-id')
    h.handlers.set(`POST ${projectsPath}`, () => Response.json({ state: 'created', project: { ...h.project, id: 'another-id' }, error_code: null }, { status: 201 }))
    projectButton(h, 'create').click()
    await flush()
    expect(mutations(h).map(call => call.path)).toEqual([projectsPath])
    expect(projectButton(h, 'enable').disabled).toBe(true)
    expect(h.store.remote?.enabled).toBe(false)
  })
})

describe('I13 explicit Disable', () => {
  function disableHarness() {
    const h = harness(selectedMetadata({ enabled: true, state: 'connected' }))
    h.handlers.set('POST /prepare', () => Response.json(prepared({
      to: { persistent_id: 'old-game', url: metadata().url, enabled: false }, plan: { operation: 'disable', enabled: false } })))
    h.handlers.set(`POST /changes/${changeId}/commit`, () => {
      const result = receipt({ binding_change: { change_id: changeId, state: 'committed' } as const, ...selectedMetadata({ identity_revision: 4, config_fingerprint: committedFingerprint,
        binding_generation: committedGeneration }) } as Partial<ProjectRemoteCommitReceipt>)
      h.setMetadata(selectedMetadata({ identity_revision: 4, config_fingerprint: committedFingerprint,
        binding_generation: committedGeneration }))
      return Response.json(result)
    })
    return h
  }

  it.each(['en', 'ru'] as const)('uses the prepare/quiesce/commit barrier and ends disabled (%s)', async language => {
    await setLocale(language)
    const h = disableHarness()
    await flush()
    projectButton(h, 'disable').click()
    await flush()
    expect(mutations(h)).toEqual([])
    expect(h.view.host.textContent).toContain(t('sync.project.disableTitle'))
    await confirm(h)
    expect(mutations(h).map(call => call.path.slice(remotePath.length))).toEqual([
      '/prepare', `/changes/${changeId}/quiesce`, `/changes/${changeId}/commit`,
    ])
    expect(mutations(h)[0]!.body).toEqual({ backend_epoch: 'backend-A', operation: 'disable', persistent_id: 'old-game',
      url: metadata().url, profile_id: profileId, expected_revision: 3, config_fingerprint: initialFingerprint })
    expect(mutations(h)[2]!.body).toEqual({ expected_revision: 3, expected_generation: generation + 1,
      config_fingerprint: initialFingerprint, confirmation: { url: metadata().url, persistent_id: 'old-game' } })
    expect(node(h.view.host, 'notice').textContent).toBe(t('sync.project.disabled'))
    expect(h.store.remote).toMatchObject({ enabled: false, state: 'disabled', profile_id: profileId })
    expect(h.view.host.querySelector('[data-testid="remote-project-disable"]')).toBeNull()
    expect(count(h, '/enable')).toBe(0)
  })

  it('cancels Disable review with no mutation', async () => {
    const h = disableHarness()
    await flush()
    projectButton(h, 'disable').click()
    await flush()
    button(h, 'cancel').click()
    await flush()
    expect(mutations(h)).toEqual([])
    expect(h.store.remote?.enabled).toBe(true)
  })
})

// ------------------------------------------------ VS-186 I15: explicit legacy migration
const planId = 'f'.repeat(64)
const catalogId = 'legacy-game'
const legacyInstance = '0123456789abcdef0123456789abcdef'
function legacyMetadata() {
  return metadata({ persistent_id: null, identity_revision: null, enabled: false, state: 'migration_required',
    profile_id: null, space_instance_id: null })
}
function planDto(patch: Record<string, unknown> = {}) {
  return { migration_plan_id: planId, config_fingerprint: initialFingerprint, persistent_id: null, identity_revision: null,
    legacy_url: metadata().url, legacy_selector: 'Legacy Game', has_legacy_key: true, state: 'needs_profile',
    reasons: ['legacy_resolution_required'], ...patch }
}
function migrationHarness() {
  const h = harness(legacyMetadata())
  const profile = { profile_id: profileId, name: 'Same label', url: metadata().url, revision: 7,
    has_token: true, credential_source: 'stored', verification: { state: 'unavailable' } }
  h.handlers.set(`GET ${globalServersPath}`, () => Response.json({ items: [profile,
    { ...profile, profile_id: '22222222-2222-4222-8222-222222222222', url: 'https://remote.example/other' }] }))
  h.handlers.set('GET /migration', () => Response.json(planDto()))
  h.handlers.set(`POST /migration/${planId}/profile`, () => Response.json({ migration_plan_id: planId, profile_id: profileId,
    profile_revision: 8, url: metadata().url, credential: 'stored' }))
  h.handlers.set(`POST /migration/${planId}/resolve`, () => Response.json({ migration_plan_id: planId,
    legacy_selector: 'Legacy Game', catalog_id: catalogId, space_instance_id: legacyInstance, changes: {}, conflicts: [] }))
  h.handlers.set('POST /prepare', () => Response.json(prepared({
    to: { persistent_id: catalogId, url: metadata().url, enabled: false }, plan: { operation: 'migrate', enabled: false } })))
  h.handlers.set(`POST /changes/${changeId}/commit`, () => {
    const after = metadata({ persistent_id: catalogId, identity_revision: 1, config_fingerprint: committedFingerprint,
      binding_generation: committedGeneration, enabled: false, state: 'disabled', profile_id: profileId,
      space_instance_id: legacyInstance })
    h.setMetadata(after)
    return Response.json(receipt({ ...after, binding_change: { change_id: changeId, state: 'committed' } }))
  })
  return h
}
function migrationNode<T extends HTMLElement = HTMLElement>(h: Harness, name: string) {
  const element = h.view.host.querySelector<T>(`[data-testid="remote-migration-${name}"]`)
  expect(element, name).not.toBeNull()
  return element!
}
function migrationButton(h: Harness, name: string) { return migrationNode<HTMLButtonElement>(h, name) }
async function click(element: HTMLElement) { element.click(); await flush() }
async function loadPlan(h: Harness) { await flush(); await click(migrationButton(h, 'load')) }
async function chooseExistingProfile(h: Harness) {
  await click(migrationButton(h, 'servers'))
  const select = migrationNode<HTMLSelectElement>(h, 'profile')
  select.value = profileId
  select.dispatchEvent(new Event('change', { bubbles: true }))
  await flush()
}

describe('I15 legacy migration panel', () => {
  it('is hidden for a project that does not require migration', async () => {
    const h = harness()
    await flush()
    expect(h.view.host.querySelector('[data-testid="remote-migration"]')).toBeNull()
  })

  it.each(['en', 'ru'] as const)('walks plan → profile → resolve → apply, each by an explicit click, and ends disabled (%s)', async language => {
    await setLocale(language)
    const h = migrationHarness()
    await flush()
    expect(h.calls.map(call => call.method + ' ' + call.path)).toEqual([`GET ${remotePath}`])
    await loadPlan(h)
    expect(migrationNode(h, 'url').textContent).toBe(metadata().url)
    expect(migrationNode(h, 'selector').textContent).toBe('Legacy Game')
    expect(migrationNode(h, 'state').textContent).toBe(t('sync.migration.state.needs_profile'))
    expect(mutations(h)).toEqual([])
    await chooseExistingProfile(h)
    // Only profiles whose URL equals the legacy URL exactly are offered.
    expect([...migrationNode<HTMLSelectElement>(h, 'profile').options].map(option => option.value)).toEqual(['', profileId])
    expect(migrationButton(h, 'profile-submit').disabled).toBe(true) // URL not yet confirmed
    await click(migrationNode(h, 'url-confirm'))
    await click(migrationButton(h, 'profile-submit'))
    expect(mutations(h).map(call => call.path)).toEqual([`${remotePath}/migration/${planId}/profile`])
    expect(mutations(h)[0]!.body).toEqual({ config_fingerprint: initialFingerprint, profile_id: profileId,
      import_legacy_credential: false, confirmation: { url: metadata().url } })
    expect(migrationNode(h, 'profile-result').textContent).toContain(t('sync.migration.credential.stored'))
    expect(mutations(h)).toHaveLength(1)
    await click(migrationButton(h, 'resolve'))
    expect(mutations(h)[1]!.body).toEqual({ profile_id: profileId, expected_profile_revision: 8, config_fingerprint: initialFingerprint })
    const mapping = migrationNode(h, 'resolution').textContent
    expect(mapping).toContain('Legacy Game')
    expect(mapping).toContain(catalogId)
    expect(mapping).toContain(legacyInstance)
    expect(mutations(h)).toHaveLength(2)
    await click(migrationButton(h, 'apply'))
    expect(mutations(h)).toHaveLength(2)
    expect(h.view.host.textContent).toContain(t('sync.migration.applyTitle'))
    await confirm(h)
    expect(mutations(h).slice(2).map(call => call.path.slice(remotePath.length))).toEqual([
      '/prepare', `/changes/${changeId}/quiesce`, `/changes/${changeId}/commit`,
    ])
    expect(mutations(h)[2]!.body).toEqual({ backend_epoch: 'backend-A', operation: 'migrate', persistent_id: catalogId,
      url: metadata().url, migration_plan_id: planId, profile_id: profileId, catalog_id: catalogId,
      catalog_instance_id: legacyInstance, expected_revision: 0, config_fingerprint: initialFingerprint })
    expect(mutations(h)[4]!.body).toEqual({ expected_revision: 0, expected_generation: generation + 1,
      config_fingerprint: initialFingerprint, confirmation: { url: metadata().url, persistent_id: catalogId } })
    expect(node(h.view.host, 'notice').textContent).toBe(t('sync.migration.applied'))
    expect(h.store.remote).toMatchObject({ persistent_id: catalogId, enabled: false, state: 'disabled',
      profile_id: profileId, space_instance_id: legacyInstance })
    expect(h.view.host.querySelector('[data-testid="remote-migration"]')).toBeNull()
    expect(count(h, '/enable')).toBe(0)
    expect(WebSocket).not.toHaveBeenCalled()
  })

  it('creates a new profile with explicit key import and URL confirmation', async () => {
    const h = migrationHarness()
    await loadPlan(h)
    const name = migrationNode<HTMLInputElement>(h, 'name')
    name.value = 'Team server'
    name.dispatchEvent(new Event('input', { bubbles: true }))
    await click(migrationNode(h, 'import'))
    expect(migrationButton(h, 'profile-submit').disabled).toBe(true)
    await click(migrationNode(h, 'url-confirm'))
    await click(migrationButton(h, 'profile-submit'))
    expect(mutations(h)[0]!.body).toEqual({ config_fingerprint: initialFingerprint, name: 'Team server',
      import_legacy_credential: true, confirmation: { url: metadata().url } })
  })

  it('cannot import a key the plan does not have', async () => {
    const h = migrationHarness()
    h.handlers.set('GET /migration', () => Response.json(planDto({ has_legacy_key: false })))
    await loadPlan(h)
    expect(migrationNode<HTMLInputElement>(h, 'import').disabled).toBe(true)
  })

  it.each([
    ['profile', 'credential_conflict', 'sync.migration.error.credentialConflict'],
    ['profile', 'profile_url_collision', 'sync.migration.error.urlCollision'],
    ['profile', 'legacy_completeness_unproven', 'sync.migration.error.gate'],
    ['resolve', 'legacy_space_not_found', 'sync.migration.error.notFound'],
    ['resolve', 'profile_revision_conflict', 'sync.migration.error.stale'],
    ['resolve', 'catalog_unavailable', 'sync.migration.error.unavailable'],
  ] as const)('shows a %s refusal %s localized and stops there', async (step, code, key) => {
    const h = migrationHarness()
    h.handlers.set(`POST /migration/${planId}/${step}`, () => refusal(code, code === 'catalog_unavailable' ? 503 : 409))
    await loadPlan(h)
    await chooseExistingProfile(h)
    await click(migrationNode(h, 'url-confirm'))
    await click(migrationButton(h, 'profile-submit'))
    if (step === 'resolve') await click(migrationButton(h, 'resolve'))
    expect(migrationNode(h, 'error').textContent).toBe(t(key))
    expect(migrationNode(h, 'error').textContent).not.toContain(code)
    expect(h.view.host.querySelector('[data-testid="remote-migration-apply"]')).toBeNull()
    expect(count(h, '/prepare')).toBe(0)
    expect(mutations(h)).toHaveLength(step === 'resolve' ? 2 : 1)
  })

  it('blocks apply when resolution reports conflicts', async () => {
    const h = migrationHarness()
    h.handlers.set(`POST /migration/${planId}/resolve`, () => Response.json({ migration_plan_id: planId,
      legacy_selector: 'Legacy Game', catalog_id: catalogId, space_instance_id: legacyInstance, changes: {}, conflicts: ['x'] }))
    await loadPlan(h)
    await chooseExistingProfile(h)
    await click(migrationNode(h, 'url-confirm'))
    await click(migrationButton(h, 'profile-submit'))
    await click(migrationButton(h, 'resolve'))
    expect(migrationNode(h, 'conflicts').textContent).toBe(t('sync.migration.resolveConflicts', { count: 1 }))
    expect(migrationButton(h, 'apply').disabled).toBe(true)
    forceClick(migrationButton(h, 'apply'))
    await flush()
    expect(count(h, '/prepare')).toBe(0)
  })

  it('offers no profile step for a conflicting or invalid plan and shows reasons localized', async () => {
    const h = migrationHarness()
    h.handlers.set('GET /migration', () => Response.json(planDto({ state: 'migration_conflict',
      reasons: ['identity_selector_conflict', 'something_new'] })))
    await loadPlan(h)
    expect(migrationNode(h, 'state').textContent).toBe(t('sync.migration.state.migration_conflict'))
    expect(h.view.host.textContent).toContain(t('sync.migration.reason.identity_selector_conflict'))
    expect(h.view.host.textContent).toContain(t('sync.migration.reason.unrecognised'))
    expect(h.view.host.textContent).not.toContain('something_new')
    expect(h.view.host.querySelector('[data-testid="remote-migration-profile-submit"]')).toBeNull()
  })

  it('discards a migration apply cancelled at review without any request', async () => {
    const h = migrationHarness()
    await loadPlan(h)
    await chooseExistingProfile(h)
    await click(migrationNode(h, 'url-confirm'))
    await click(migrationButton(h, 'profile-submit'))
    await click(migrationButton(h, 'resolve'))
    await click(migrationButton(h, 'apply'))
    button(h, 'cancel').click()
    await flush()
    expect(count(h, '/prepare')).toBe(0)
    expect(h.store.remote?.state).toBe('migration_required')
  })
})

// ------------------------------------------------ VS-188: explicit recovery and checkpoint archive (§16.10)
const recoveryChange = 'f'.repeat(32)
function recoveryDto(patch: Partial<ProjectRemoteRecovery> = {}): ProjectRemoteRecovery {
  return { change_id: recoveryChange, operation: 'select', phase: 'fenced', outcome: 'previous',
    target: { persistent_id: 'old-game', url: metadata().url, identity_revision: 3, enabled: false },
    available: true, code: null, ...patch }
}
function recoveryMetadata(recovery: ProjectRemoteRecovery | null = recoveryDto(), patch: Partial<ProjectRemoteStatus> = {}) {
  return selectedMetadata({ state: 'blocked', reason: 'recovery_required', recovery, ...patch })
}
function recoveryHarness(initial = recoveryMetadata()) {
  const h = harness(initial)
  h.handlers.set('POST /recover', () => {
    const target = initial.recovery!.target
    const after = selectedMetadata({ persistent_id: target.persistent_id, url: target.url,
      identity_revision: initial.identity_revision, binding_generation: generation + 1,
      config_fingerprint: committedFingerprint, recovery: null })
    h.setMetadata(after)
    return Response.json({ project_id: 'local-A', epoch: 'runtime-A', backend_epoch: 'backend-A',
      change_id: initial.recovery!.change_id, outcome: initial.recovery!.outcome, recovered: true,
      persistent_id: target.persistent_id, identity_revision: initial.identity_revision, url: target.url,
      enabled: false, config_fingerprint: committedFingerprint, binding_generation: generation + 1,
      profile_id: profileId, space_instance_id: storage, state: 'disabled', reason: null })
  })
  return h
}
function recoveryNode<T extends HTMLElement = HTMLElement>(h: Harness, name: string) {
  const element = h.view.host.querySelector<T>(`[data-testid="remote-recovery-${name}"]`)
  expect(element, name).not.toBeNull()
  return element!
}
function recoveryButton(h: Harness, name: string) { return recoveryNode<HTMLButtonElement>(h, name) }
function missing(h: Harness, testId: string) { return h.view.host.querySelector(`[data-testid="${testId}"]`) === null }
function recoverCalls(h: Harness) { return count(h, '/recover') }
const expectedRecoverBody = {
  change_id: recoveryChange, expected_revision: 3, config_fingerprint: initialFingerprint,
  confirmation: { url: metadata().url, persistent_id: 'old-game', action: 'recover' }, backend_epoch: 'backend-A',
}
async function startRecovery(h: Harness) {
  await flush()
  recoveryButton(h, 'start').click()
  await flush()
}

describe('VS-188 explicit recovery of an interrupted binding change', () => {
  it.each([undefined, null])('shows no recovery panel when recovery is %s', async value => {
    const h = harness(selectedMetadata(value === undefined ? {} : { recovery: value }))
    await flush()
    expect(missing(h, 'remote-recovery')).toBe(true)
    expect(missing(h, 'remote-recovery-start')).toBe(true)
    expect(mutations(h)).toEqual([])
  })

  it.each([
    ['select', 'previous'], ['enable', 'commit'], ['edit_id', 'previous'], ['disable', 'commit'],
    ['unbind', 'commit'], ['migrate', 'previous'], [null, 'previous'],
  ] as const)('explains interrupted %s with outcome %s and the exact target', async (operation, outcome) => {
    const h = recoveryHarness(recoveryMetadata(recoveryDto({ operation, outcome })))
    await flush()
    const opKey = operation ? `sync.project.recovery.operation.${operation}` as const : 'sync.project.recovery.operation.unknown' as const
    expect(recoveryNode(h, 'reason').textContent).toBe(t('sync.project.recovery.reason', { operation: t(opKey) }))
    expect(recoveryNode(h, 'outcome').textContent).toBe(t(`sync.project.recovery.outcome.${outcome}`))
    expect(recoveryNode(h, 'id').textContent).toBe('old-game')
    expect(recoveryNode(h, 'url').textContent).toBe(metadata().url)
    expect(recoveryButton(h, 'start').disabled).toBe(false)
    expect(mutations(h)).toEqual([]) // nothing recovers on load
  })

  it('shows a legacy root without an ID and an unbound target without a server', async () => {
    const h = recoveryHarness(recoveryMetadata(recoveryDto({ operation: 'unbind', outcome: 'commit',
      target: { persistent_id: null, url: '', identity_revision: 3, enabled: false } })))
    await flush()
    expect(recoveryNode(h, 'id').textContent).toBe(t('sync.project.recovery.legacyId'))
    expect(recoveryNode(h, 'url').textContent).toBe(t('sync.identity.noServer'))
  })

  it.each([
    ['recovery_conflict', 'sync.project.recovery.error.recovery_conflict'],
    ['recovery_ambiguous', 'sync.project.recovery.error.recovery_ambiguous'],
    ['binding_journal_unavailable', 'sync.project.recovery.error.binding_journal_unavailable'],
    [null, 'sync.project.recovery.unavailable'],
  ] as const)('offers no command when unavailable (%s)', async (code, key) => {
    const h = recoveryHarness(recoveryMetadata(recoveryDto({ available: false, code })))
    await flush()
    expect(missing(h, 'remote-recovery-start')).toBe(true)
    expect(recoveryNode(h, 'unavailable').textContent).toBe(t(key))
    expect(recoveryNode(h, 'id').textContent).toBe('old-game')
    expect(mutations(h)).toEqual([])
  })

  it('refuses a malformed recovery record without offering the command', async () => {
    const h = recoveryHarness(recoveryMetadata(recoveryDto({ change_id: 'not-hex' })))
    await flush()
    expect(missing(h, 'remote-recovery-start')).toBe(true)
    expect(recoveryNode(h, 'unavailable').textContent).toBe(t('sync.project.recovery.error.invalid'))
  })

  it('reviews the exact target, then sends the captured body once and reloads the status', async () => {
    const h = recoveryHarness()
    const before = deferred<Response>()
    h.handlers.set('POST /recover', (handler => async (call: Call) => { await before.promise; return handler(call) })(h.handlers.get('POST /recover')!))
    await startRecovery(h)
    expect(recoverCalls(h)).toBe(0)
    expect(recoveryNode(h, 'review').textContent).toContain(t('sync.project.recovery.reviewHint'))
    expect(recoveryNode(h, 'review-id').textContent).toBe('old-game')
    expect(recoveryNode(h, 'review-url').textContent).toBe(metadata().url)
    recoveryButton(h, 'confirm').click()
    await flush()
    expect(recoverCalls(h)).toBe(1)
    expect(recoveryButton(h, 'confirm').disabled).toBe(true)
    forceClick(recoveryButton(h, 'confirm'))
    forceClick(button(h, 'refresh'))
    await flush()
    expect(recoverCalls(h)).toBe(1)
    const reads = h.calls.filter(call => call.method === 'GET').length
    before.resolve(new Response())
    await flush()
    expect(mutations(h).map(call => call.path)).toEqual([`${remotePath}/recover`])
    expect(mutations(h)[0]!.body).toEqual(expectedRecoverBody)
    expect(h.calls.filter(call => call.method === 'GET').length).toBeGreaterThan(reads)
    expect(recoveryNode(h, 'notice').textContent).toBe(t('sync.project.recovery.done'))
    expect(missing(h, 'remote-recovery')).toBe(true)
    expect(missing(h, 'remote-recovery-error')).toBe(true)
    expect(h.store.remote?.enabled).toBe(false)
    expect(h.store.remote?.recovery).toBeNull()
  })

  it('sends revision 0 and a null ID for a legacy root without identity', async () => {
    const legacy = recoveryMetadata(recoveryDto({ operation: 'migrate',
      target: { persistent_id: null, url: metadata().url, identity_revision: 0, enabled: false } }),
    { identity_revision: null, persistent_id: null })
    const h = recoveryHarness(legacy)
    await startRecovery(h)
    expect(recoveryNode(h, 'review-id').textContent).toBe(t('sync.project.recovery.legacyId'))
    recoveryButton(h, 'confirm').click()
    await flush()
    expect(mutations(h)[0]!.body).toEqual({ ...expectedRecoverBody, expected_revision: 0,
      confirmation: { ...expectedRecoverBody.confirmation, persistent_id: null } })
    expect(recoveryNode(h, 'notice').textContent).toBe(t('sync.project.recovery.done'))
  })

  it('cancel at review sends nothing', async () => {
    const h = recoveryHarness()
    await startRecovery(h)
    recoveryButton(h, 'cancel').click()
    await flush()
    expect(missing(h, 'remote-recovery-review')).toBe(true)
    expect(recoveryButton(h, 'start').disabled).toBe(false)
    expect(mutations(h)).toEqual([])
  })

  it('drops the review when a refresh observes another snapshot', async () => {
    const h = recoveryHarness()
    await startRecovery(h)
    h.setMetadata(recoveryMetadata(recoveryDto(), { config_fingerprint: 'f'.repeat(64) }))
    button(h, 'refresh').click()
    await flush()
    expect(missing(h, 'remote-recovery-review')).toBe(true)
    expect(mutations(h)).toEqual([])
  })

  it.each([
    ['confirmation_required', 409, 'sync.project.recovery.error.confirmation_required'],
    ['stale_binding', 409, 'sync.project.recovery.error.stale_binding'],
    ['recovery_conflict', 409, 'sync.project.recovery.error.recovery_conflict'],
    ['recovery_ambiguous', 409, 'sync.project.recovery.error.recovery_ambiguous'],
    ['unknown_change', 409, 'sync.project.recovery.error.unknown_change'],
    ['change_in_progress', 409, 'sync.project.recovery.error.change_in_progress'],
    ['stale_backend_epoch', 409, 'sync.project.recovery.error.stale_backend_epoch'],
    ['reload_required', 409, 'sync.project.recovery.error.reload_required'],
    ['binding_journal_unavailable', 503, 'sync.project.recovery.error.binding_journal_unavailable'],
    ['consumer_lifecycle_unavailable', 503, 'sync.project.recovery.error.consumer_lifecycle_unavailable'],
    ['config_store_unavailable', 503, 'sync.project.recovery.error.config_store_unavailable'],
    ['binding_store_unavailable', 503, 'sync.project.recovery.error.binding_store_unavailable'],
    ['recovery_failed', 503, 'sync.project.recovery.error.recovery_failed'],
    ['mystery_code', 409, 'sync.project.recovery.error.refused'],
  ] as const)('refusal %s (%s) is shown localized and never retried', async (code, status, key) => {
    const h = recoveryHarness()
    h.handlers.set('POST /recover', () => refusal(code, status))
    await startRecovery(h)
    recoveryButton(h, 'confirm').click()
    await flush()
    expect(recoverCalls(h)).toBe(1)
    expect(recoveryNode(h, 'error').textContent).toBe(t(key))
    expect(missing(h, 'remote-recovery-review')).toBe(true)
    expect(missing(h, 'remote-recovery-notice')).toBe(true)
    // An explicit refresh is required before another explicit attempt.
    expect(recoveryButton(h, 'start').disabled).toBe(true)
    forceClick(recoveryButton(h, 'start'))
    await flush()
    expect(recoverCalls(h)).toBe(1)
  })

  it.each([
    ['a transport failure', () => { throw new TypeError('network down') }],
    ['a gateway error without a code', () => new Response('bad gateway', { status: 502 })],
    ['a contradictory receipt', () => Response.json({ recovered: true, change_id: 'a'.repeat(32), enabled: false })],
  ] as const)('reports an unknown outcome after %s and requires refresh', async (_label, reply) => {
    const h = recoveryHarness()
    h.handlers.set('POST /recover', reply)
    await startRecovery(h)
    recoveryButton(h, 'confirm').click()
    await flush()
    expect(recoveryNode(h, 'error').textContent).toBe(t('sync.project.recovery.error.unknown'))
    expect(recoverCalls(h)).toBe(1)
    expect(recoveryButton(h, 'start').disabled).toBe(true)
    button(h, 'refresh').click()
    await flush()
    expect(recoveryButton(h, 'start').disabled).toBe(false) // explicit, not automatic
    expect(recoverCalls(h)).toBe(1)
  })

  it('disables every other binding command while recovery is pending', async () => {
    const h = await catalogHarness(recoveryMetadata())
    h.handlers.set('POST /reconcile-preview', () => Response.json(previewDto()))
    await enter(h)
    expect(button(h, 'review').disabled).toBe(true)
    expect(projectButton(h, 'select').disabled).toBe(true)
    expect(projectButton(h, 'preview').disabled).toBe(true)
    expect(projectButton(h, 'enable').disabled).toBe(true)
    for (const element of [button(h, 'review'), projectButton(h, 'select'), projectButton(h, 'preview'), projectButton(h, 'enable')]) {
      forceClick(element)
    }
    await flush()
    expect(mutations(h)).toEqual([])
    expect(missing(h, 'remote-id-confirm')).toBe(true)
    expect(recoveryButton(h, 'start').disabled).toBe(false)
  })

  it('disables Disable while an enabled project reports a recovery', async () => {
    const h = recoveryHarness(recoveryMetadata(recoveryDto(), { enabled: true, state: 'blocked' }))
    await flush()
    expect(projectButton(h, 'disable').disabled).toBe(true)
  })

  it('renders the recovery panel in Russian', async () => {
    await setLocale('ru')
    const h = recoveryHarness()
    await startRecovery(h)
    const reason = recoveryNode(h, 'reason').textContent!
    expect(reason).toBe(t('sync.project.recovery.reason', { operation: t('sync.project.recovery.operation.select') }))
    expect(reason).toMatch(/[а-яё]/i)
    expect(recoveryButton(h, 'confirm').textContent).toBe(t('sync.project.recovery.confirm'))
    expect(recoveryButton(h, 'confirm').textContent).toMatch(/[а-яё]/i)
  })
})

function archiveCalls(h: Harness) { return count(h, '/checkpoint/archive') }
function archiveHarness(base: string) {
  const h = enableHarness()
  h.handlers.set('POST /reconcile-preview', () => Response.json(previewDto({ base })))
  h.handlers.set('POST /checkpoint/archive', () => Response.json({ project_id: 'local-A', epoch: 'runtime-A',
    backend_epoch: 'backend-A', archived: true, base, preview_required: true }))
  return h
}
const expectedArchiveBody = {
  expected_revision: 3, expected_generation: generation, preview_id: previewId, fingerprint: previewFingerprint,
  confirmation: { url: metadata().url, persistent_id: 'old-game', action: 'archive_checkpoint' },
  config_fingerprint: initialFingerprint, backend_epoch: 'backend-A',
}

describe('VS-188 archive of a checkpoint from another Remote incarnation', () => {
  it.each(['none', 'matched'])('offers no archive for base %s', async base => {
    const h = archiveHarness(base)
    await previewNow(h)
    expect(missing(h, 'remote-project-archive-panel')).toBe(true)
    expect(projectButton(h, 'enable').disabled).toBe(false)
  })

  it.each(['epoch_mismatch', 'cursor_rollback'] as const)('offers archive instead of Enable for base %s', async base => {
    const h = archiveHarness(base)
    await previewNow(h)
    expect(projectNode(h, 'archive-reason').textContent).toBe(t(`sync.project.archive.${base}`))
    expect(projectButton(h, 'enable').disabled).toBe(true)
    forceClick(projectButton(h, 'enable'))
    await flush()
    expect(missing(h, 'remote-project-enable-review')).toBe(true)
    projectButton(h, 'archive').click()
    await flush()
    expect(archiveCalls(h)).toBe(0)
    expect(projectNode(h, 'archive-id').textContent).toBe('old-game')
    expect(projectNode(h, 'archive-url').textContent).toBe(metadata().url)
    projectButton(h, 'archive-confirm').click()
    await flush()
    expect(mutations(h).map(call => call.path)).toEqual([`${remotePath}/reconcile-preview`, `${remotePath}/checkpoint/archive`])
    expect(mutations(h)[1]!.body).toEqual(expectedArchiveBody)
    expect(projectNode(h, 'notice').textContent).toBe(t('sync.project.archive.done'))
    expect(missing(h, 'remote-project-preview-result')).toBe(true)
    expect(missing(h, 'remote-project-archive-review')).toBe(true)
    expect(projectButton(h, 'enable').disabled).toBe(true)
    expect(count(h, '/enable')).toBe(0)
    expect(h.store.remote?.enabled).toBe(false)
  })

  it('cancel at review sends nothing and keeps the preview', async () => {
    const h = archiveHarness('epoch_mismatch')
    await previewNow(h)
    projectButton(h, 'archive').click()
    await flush()
    projectButton(h, 'archive-cancel').click()
    await flush()
    expect(missing(h, 'remote-project-archive-review')).toBe(true)
    expect(archiveCalls(h)).toBe(0)
    expect(missing(h, 'remote-project-preview-result')).toBe(false)
  })

  it.each([
    ['confirmation_required', 409, 'sync.project.archive.error.confirmation_required'],
    ['preview_not_found', 409, 'sync.project.archive.error.preview_stale'],
    ['preview_stale', 409, 'sync.project.archive.error.preview_stale'],
    ['preview_expired', 409, 'sync.project.archive.error.preview_expired'],
    ['checkpoint_current', 409, 'sync.project.archive.error.checkpoint_current'],
    ['already_enabled', 409, 'sync.project.alreadyEnabled'],
    ['recovery_required', 409, 'sync.identity.recoveryRequired'],
    ['change_in_progress', 409, 'sync.project.archive.error.change_in_progress'],
    ['stale_binding', 409, 'sync.project.archive.error.stale_binding'],
    ['checkpoint_unavailable', 503, 'sync.project.archive.error.checkpoint_unavailable'],
    ['mystery_code', 409, 'sync.project.archive.error.refused'],
  ] as const)('archive refusal %s drops the preview and is never retried', async (code, status, key) => {
    const h = archiveHarness('cursor_rollback')
    h.handlers.set('POST /checkpoint/archive', () => refusal(code, status))
    await previewNow(h)
    projectButton(h, 'archive').click()
    await flush()
    projectButton(h, 'archive-confirm').click()
    await flush()
    expect(archiveCalls(h)).toBe(1)
    expect(projectNode(h, 'error').textContent).toBe(t(key))
    expect(missing(h, 'remote-project-preview-result')).toBe(true)
    expect(missing(h, 'remote-project-archive-panel')).toBe(true)
    expect(missing(h, 'remote-project-notice')).toBe(true)
  })

  it('reports an unknown archive outcome after a transport failure and requires refresh', async () => {
    const h = archiveHarness('epoch_mismatch')
    h.handlers.set('POST /checkpoint/archive', () => { throw new TypeError('network down') })
    await previewNow(h)
    projectButton(h, 'archive').click()
    await flush()
    projectButton(h, 'archive-confirm').click()
    await flush()
    expect(projectNode(h, 'error').textContent).toBe(t('sync.project.archive.unknown'))
    expect(archiveCalls(h)).toBe(1)
    expect(missing(h, 'remote-project-preview-result')).toBe(true)
    expect(projectButton(h, 'preview').disabled).toBe(true)
  })

  it('renders the archive explanation in Russian', async () => {
    await setLocale('ru')
    const h = archiveHarness('epoch_mismatch')
    await previewNow(h)
    const reason = projectNode(h, 'archive-reason').textContent!
    expect(reason).toBe(t('sync.project.archive.epoch_mismatch'))
    expect(reason).toMatch(/[а-яё]/i)
    expect(projectButton(h, 'archive').textContent).toBe(t('sync.project.archive.archive'))
  })
})
