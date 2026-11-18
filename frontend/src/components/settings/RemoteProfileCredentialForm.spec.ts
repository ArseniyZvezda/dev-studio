// @vitest-environment jsdom
/** Real local API + renderer-local Pinia + ModalDialog (studio-design §5.1/§10.1).
 * All fetches are mocked; no Remote, WebSocket, Verify or live network is used.
 */
import { createPinia, disposePinia } from 'pinia'
import { inspect } from 'node:util'
import { createApp, h, nextTick, shallowRef, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RemoteServerProfile } from '@/api/remote'
import { t } from '@/i18n'
import { useRemoteServersStore } from '@/stores/remote-servers'
import RemoteProfileCredentialForm from './RemoteProfileCredentialForm.vue'
import RemoteProfileMetadataForm, { remoteProfileMetadataState } from './RemoteProfileMetadataForm.vue'

const id = '11111111-1111-4111-8111-111111111111'
const otherId = '22222222-2222-4222-8222-222222222222'
const endpoint = `/api/remote/servers/${id}/credential`
const secret = 'PERSONAL-KEY-INPUT-AND-ERROR-CANARY'
const network = vi.fn<typeof fetch>()
// Gate L census (dev spec §16) is routed separately so credential intents keep exact call counts.
const consumersNetwork = vi.fn<typeof fetch>()
const consumersUrl = /^\/api\/remote\/servers\/([0-9a-f-]+)\/consumers(?:\/confirmation)?$/
function census(profileId = id, overrides: Record<string, unknown> = {}) {
  return { profile_id: profileId, url: 'https://remote.example/team', revision: 7, state: 'known',
    confirmed_at: '2026-11-18T10:00:00Z', bound_roots: [], active_roots: [], operations: [], ...overrides }
}
const router: typeof fetch = (url, init) => consumersUrl.test(String(url))
  ? consumersNetwork(url, init) : network(url, init)
const sockets = vi.fn()
const piniae: ReturnType<typeof createPinia>[] = []
const mounted: { app: App; host: HTMLDivElement; active: boolean }[] = []
const telemetry: unknown[] = []
const consoleCalls: unknown[] = []

function profile(overrides: Partial<RemoteServerProfile> = {}): RemoteServerProfile {
  return { profile_id: id, name: 'Original', url: 'https://remote.example/team', revision: 7,
    has_token: true, credential_source: 'stored', verification: { state: 'unavailable' }, ...overrides }
}
function response(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), { status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } })
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function harness(items = [profile()]) {
  const pinia = createPinia()
  piniae.push(pinia)
  const store = useRemoteServersStore(pinia)
  store.$onAction(({ name, args, after, onError }) => {
    telemetry.push({ name, args: JSON.stringify(args) })
    after(result => telemetry.push({ name, result: JSON.stringify(result) }))
    onError(cause => telemetry.push({ name, cause: String(cause) }))
  }, true)
  store.$subscribe((_mutation, state) => telemetry.push(JSON.stringify(state)), { flush: 'sync', detached: true })
  network.mockResolvedValueOnce(response({ items }))
  expect(await store.load()).toBe(true)
  return { pinia, store }
}
type Harness = Awaited<ReturnType<typeof harness>>
async function mountForm(context: Harness, initial = profile()) {
  const value = shallowRef(initial)
  const saved = vi.fn((...args: unknown[]) => { telemetry.push({ saved: args }) })
  const close = vi.fn((...args: unknown[]) => { telemetry.push({ close: args }) })
  const refreshRequired = vi.fn((...args: unknown[]) => { telemetry.push({ refreshRequired: args }) })
  const errors = vi.fn((...args: unknown[]) => { telemetry.push({ vueError: String(args[0]) }) })
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(RemoteProfileCredentialForm, {
    profile: value.value, onSaved: saved, onClose: close, onRefreshRequired: refreshRequired,
  }) })
  app.use(context.pinia)
  app.config.errorHandler = errors
  const view = { app, host, active: true, saved, close, refreshRequired, errors, value }
  mounted.push(view)
  app.mount(host)
  await flush() // The key intents stay gated until the census GET settles.
  return view
}
function unmount(view: typeof mounted[number]) {
  if (!view.active) return
  view.active = false
  view.app.unmount()
  view.host.remove()
}
async function flush() {
  // Real request() awaits fetch and Response.json() before the component continuation.
  for (let i = 0; i < 12; i += 1) { await Promise.resolve(); await nextTick() }
}
function form(): HTMLFormElement {
  const node = document.body.querySelector<HTMLFormElement>('[data-testid="remote-credential-form"]')
  expect(node).not.toBeNull()
  return node!
}
function field() { return form().querySelector<HTMLInputElement>('[data-testid="remote-credential-key"]')! }
function input(value = secret) {
  field().value = value
  field().dispatchEvent(new Event('input', { bubbles: true }))
}
function button(kind: 'submit' | 'cancel' | 'forget' | 'forget-confirm' | 'forget-cancel') {
  return form().querySelector<HTMLButtonElement>(`[data-testid="remote-credential-${kind}"]`)!
}
function submit(node = form()) { node.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) }
function forceClick(node: HTMLButtonElement) { node.dispatchEvent(new MouseEvent('click', { bubbles: true })) }
function alertText() { return form().querySelector('[role="alert"]')?.textContent }
function requests() { return network.mock.calls.filter(([, init]) => init?.method !== 'GET') }
async function act(intent: 'save' | 'forget') {
  if (intent === 'save') { input(); submit() }
  else { button('forget').click(); await flush(); button('forget-confirm').click() }
}
function safe(context: Harness, metadataRename = false) {
  expect(JSON.stringify(context.store.$state)).not.toContain(secret)
  expect(JSON.stringify(telemetry)).not.toContain(secret)
  expect(inspect(consoleCalls, { depth: 8 })).not.toContain(secret)
  expect(document.body.innerHTML).not.toContain(secret)
  expect(document.body.textContent).not.toContain(secret)
  expect(JSON.stringify(window.localStorage)).not.toContain(secret)
  expect(JSON.stringify(window.sessionStorage)).not.toContain(secret)
  expect(sockets).not.toHaveBeenCalled()
  for (const [url, init] of network.mock.calls) {
    // Only local metadata GET or this explicit local credential intent is allowed.
    expect(String(url)).toMatch(metadataRename
      ? /^\/api\/remote\/servers(?:\/[0-9a-f-]+(?:\/credential)?)?$/
      : /^\/api\/remote\/servers(?:\/[0-9a-f-]+\/credential)?$/)
    expect(init?.method).toMatch(metadataRename ? /^(GET|PUT|DELETE|PATCH)$/ : /^(GET|PUT|DELETE)$/)
    expect(String(url)).not.toContain(secret)
    expect(JSON.stringify(init?.headers)).not.toContain(secret)
  }
}

beforeEach(() => {
  telemetry.length = 0
  consoleCalls.length = 0
  network.mockReset().mockRejectedValue(new Error('Unexpected mocked fetch'))
  sockets.mockReset()
  consumersNetwork.mockReset().mockImplementation(async url =>
    response(census(consumersUrl.exec(String(url))![1])))
  vi.stubGlobal('fetch', router)
  vi.stubGlobal('WebSocket', sockets)
  window.localStorage.clear()
  window.sessionStorage.clear()
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation((...args) => { telemetry.push({ storageWrite: args }) })
  for (const name of ['log', 'warn', 'error', 'info', 'debug', 'trace'] as const) {
    vi.spyOn(console, name).mockImplementation((...args) => { consoleCalls.push(args) })
  }
})
afterEach(() => {
  for (const view of mounted.splice(0)) unmount(view)
  for (const pinia of piniae.splice(0)) disposePinia(pinia)
  document.body.replaceChildren()
  vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})

describe('explicit personal credential intents', () => {
  it.each(['stored', 'environment', 'none'] as const)('opens empty/focused with effective %s source, without network or timers', async source => {
    vi.useFakeTimers()
    const context = await harness([profile({ credential_source: source, has_token: source !== 'none' })])
    const view = await mountForm(context, context.store.items[0])
    await flush()
    expect(document.body.querySelector('[role="dialog"]')?.getAttribute('aria-label'))
      .toBe(t('settings.remoteServers.credentials.title'))
    expect(field().type).toBe('password')
    expect(field().value).toBe('')
    expect(field().autocomplete).toBe('new-password')
    expect(document.activeElement).toBe(field())
    expect(form().querySelectorAll('input')).toHaveLength(1)
    expect(form().querySelector('[data-testid="remote-credential-source"]')?.textContent)
      .toBe(t(`settings.remoteServers.credential.${source === 'none' ? 'missing' : source}`))
    expect(button('submit').disabled).toBe(true)
    await vi.advanceTimersByTimeAsync(300_000)
    expect(vi.getTimerCount()).toBe(0)
    expect(network).toHaveBeenCalledTimes(1) // Initial harness metadata only.
    expect(view.saved).not.toHaveBeenCalled()
    safe(context)
  })

  it.each(['', ' ', '\t\r\n'])('empty/whitespace %j means keep even for forced submit; never acquires a ticket or DELETEs', async value => {
    const context = await harness()
    const begin = vi.spyOn(context.store, 'beginMutation')
    const view = await mountForm(context)
    input(value); submit(); submit()
    await flush()
    expect(begin).not.toHaveBeenCalled()
    expect(requests()).toHaveLength(0)
    expect(context.store.items).toEqual([profile()])
    expect(view.saved).not.toHaveBeenCalled()
    expect(view.close).not.toHaveBeenCalled()
    expect(view.refreshRequired).not.toHaveBeenCalled()
    expect(alertText()).toBeUndefined()
    safe(context)
  })

  it('takes a ticket before exact PUT dispatch, clears synchronously and publishes only confirmed metadata', async () => {
    const context = await harness()
    const pending = deferred<Response>()
    const begin = vi.spyOn(context.store, 'beginMutation')
    const apply = vi.spyOn(context.store, 'applyMutation')
    const view = await mountForm(context)
    network.mockImplementationOnce(async (url, init) => {
      expect(begin).toHaveBeenCalledExactlyOnceWith(id)
      expect(context.store.busy[id]).toBe('mutation')
      expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 1, refreshRequired: false })
      expect(url).toBe(endpoint)
      expect(init).toMatchObject({ method: 'PUT', cache: 'no-store', headers: { 'Content-Type': 'application/json' } })
      expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 7, token: `  ${secret}  ` })
      return pending.promise
    })
    input(`  ${secret}  `); submit()
    expect(field().value).toBe('') // Not merely after Vue's next render or HTTP settlement.
    expect(apply).not.toHaveBeenCalled()
    expect(view.saved).not.toHaveBeenCalled()
    expect(context.store.items).toEqual([profile()])
    safe(context)
    const confirmed = profile({ revision: 8, credential_source: 'environment', verification: { state: 'unchecked' } })
    pending.resolve(response(confirmed))
    await flush()
    expect(apply).toHaveBeenCalledTimes(1)
    expect(apply.mock.calls[0]![1]).toEqual(confirmed)
    expect(view.saved).toHaveBeenCalledExactlyOnceWith()
    expect(context.store.items).toEqual([confirmed])
    expect(context.store.busy).toEqual({})
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: false })
    expect(network).toHaveBeenCalledTimes(2)
    safe(context)
  })

  it.each(['none', 'environment'] as const)('Forget requires distinct confirmation; DELETE retains returned %s source without removing the profile', async source => {
    const context = await harness()
    const pending = deferred<Response>()
    const begin = vi.spyOn(context.store, 'beginMutation')
    const removal = vi.spyOn(context.store, 'applyRemoval')
    const view = await mountForm(context)
    input()
    button('forget').click()
    await flush()
    expect(field().value).toBe('')
    expect(form().querySelector('[data-testid="remote-credential-confirmation"]')).not.toBeNull()
    expect(form().textContent).toContain(t('settings.remoteServers.credentials.forgetWarning'))
    submit() // Enter/submit cannot stand in for consent.
    expect(begin).not.toHaveBeenCalled()
    expect(requests()).toHaveLength(0)
    network.mockImplementationOnce(async (url, init) => {
      expect(begin).toHaveBeenCalledExactlyOnceWith(id)
      expect(context.store.busy[id]).toBe('mutation')
      expect(url).toBe(endpoint)
      expect(init?.method).toBe('DELETE')
      expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 7 })
      expect(String(init?.body)).not.toContain(secret)
      return pending.promise
    })
    button('forget-confirm').click()
    expect(context.store.items).toEqual([profile()])
    const confirmed = profile({ revision: 8, credential_source: source, has_token: source !== 'none' })
    pending.resolve(response(confirmed))
    await flush()
    expect(context.store.items).toEqual([confirmed])
    expect(removal).not.toHaveBeenCalled()
    expect(view.saved).toHaveBeenCalledExactlyOnceWith()
    expect(requests()).toHaveLength(1)
    safe(context)
  })

  it.each(['forget-cancel', 'cancel', 'escape', 'unmount'] as const)('cancels Forget via %s without acquiring or dispatching', async action => {
    const context = await harness()
    const begin = vi.spyOn(context.store, 'beginMutation')
    const view = await mountForm(context)
    input(); button('forget').click()
    await flush()
    if (action === 'escape') form().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    else if (action === 'unmount') unmount(view)
    else button(action).click()
    await flush()
    expect(begin).not.toHaveBeenCalled()
    expect(requests()).toHaveLength(0)
    expect(view.saved).not.toHaveBeenCalled()
    expect(context.store.items).toEqual([profile()])
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: false })
    if (action === 'forget-cancel') {
      expect(form().querySelector('[data-testid="remote-credential-confirmation"]')).toBeNull()
      expect(field().value).toBe('')
      submit()
      expect(requests()).toHaveLength(0)
    }
    safe(context)
  })

  it.each(['cancel', 'escape', 'unmount'] as const)('erases unsent input on %s and never sends it', async action => {
    const context = await harness()
    const view = await mountForm(context)
    const node = field()
    input()
    if (action === 'cancel') button('cancel').click()
    if (action === 'escape') form().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    if (action === 'unmount') unmount(view)
    expect(node.value).toBe('')
    expect(requests()).toHaveLength(0)
    if (action !== 'unmount') {
      expect(view.close).toHaveBeenCalledExactlyOnceWith()
      input(); submit() // Even a synthetic event against the abandoned component is inert.
      expect(requests()).toHaveLength(0)
    }
    safe(context)
  })

  it.each(['save', 'forget'] as const)('serializes double %s synchronously and never reuses a successful snapshot', async intent => {
    const context = await harness()
    const pending = deferred<Response>()
    network.mockReturnValueOnce(pending.promise)
    const view = await mountForm(context)
    let confirm: HTMLButtonElement | undefined
    if (intent === 'save') { input(); submit(); submit() }
    else {
      button('forget').click(); await flush()
      confirm = button('forget-confirm')
      forceClick(confirm); forceClick(confirm)
    }
    await flush()
    expect(form().getAttribute('aria-busy')).toBe('true')
    expect(button('submit').disabled).toBe(true)
    expect(button('cancel').disabled).toBe(false)
    expect(form().textContent).toContain(t('settings.remoteServers.credentials.saving'))
    input(); submit()
    if (confirm) forceClick(confirm)
    expect(requests()).toHaveLength(1)
    pending.resolve(response(profile({ revision: 8 })))
    await flush()
    expect(field().value).toBe('')
    expect(view.saved).toHaveBeenCalledTimes(1)
    input(); submit(); forceClick(button('forget'))
    expect(requests()).toHaveLength(1)
    expect(remoteProfileMetadataState(context.store).pending).toBe(0)
    safe(context)
  })
})

describe('captured CAS and stale intent guards', () => {
  it.each(['save', 'forget'] as const)('does not dispatch %s for a null ticket or retire another owner’s real fence', async intent => {
    const context = await harness()
    const owner = context.store.beginMutation(id)!
    expect(owner).not.toBeNull()
    const end = vi.spyOn(context.store, 'endMutation')
    const view = await mountForm(context)
    await act(intent)
    await flush()
    expect(requests()).toHaveLength(0)
    expect(end).not.toHaveBeenCalled()
    expect(context.store.busy[id]).toBe('mutation')
    expect(view.refreshRequired).toHaveBeenCalledExactlyOnceWith()
    expect(view.saved).not.toHaveBeenCalled()
    expect(alertText()).toBe(t('settings.remoteServers.credentials.stale'))
    expect(context.store.endMutation(owner, false)).toBe(true)
    safe(context)
  })

  it('honours an explicitly returned null ticket before fetch, without adjusting shared pending', async () => {
    const context = await harness()
    vi.spyOn(context.store, 'beginMutation').mockReturnValue(null)
    const view = await mountForm(context)
    await act('save'); await flush()
    expect(requests()).toHaveLength(0)
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: true })
    expect(view.refreshRequired).toHaveBeenCalledTimes(1)
    safe(context)
  })

  for (const intent of ['save', 'forget'] as const) {
    it.each(['revision', 'url', 'deleted', 'prop-id', 'prop-revision', 'prop-url', 'loading', 'unloaded', 'load-error', 'shared-pending', 'shared-refresh'] as const)(
    `${intent}: rejects stale %s before pairing a new ticket with old expected_revision`, async change => {
      const context = await harness()
      const snapshot = profile()
      const view = await mountForm(context, snapshot)
      // Consent is given before a later binding/loading/latch change, never re-used blindly.
      if (intent === 'forget') { button('forget').click(); await flush() }
      if (change === 'revision') context.store.items[0]!.revision = 8
      if (change === 'url') context.store.items[0]!.url = 'https://new.example/team'
      if (change === 'deleted') context.store.items = []
      if (change === 'prop-id') view.value.value = profile({ profile_id: otherId })
      if (change === 'prop-revision') snapshot.revision = 8
      if (change === 'prop-url') snapshot.url = 'https://new.example/team'
      if (change === 'loading') context.store.loading = true
      if (change === 'unloaded') context.store.loaded = false
      if (change === 'load-error') context.store.error = { kind: 'request_failed', status: 503 }
      const state = remoteProfileMetadataState(context.store)
      if (change === 'shared-pending') state.pending = 1
      if (change === 'shared-refresh') state.refreshRequired = true
      await flush()
      const begin = vi.spyOn(context.store, 'beginMutation')
      if (intent === 'save') { input(); submit() }
      else forceClick(button('forget-confirm'))
      await flush()
      expect(begin).not.toHaveBeenCalled()
      expect(requests()).toHaveLength(0)
      expect(view.saved).not.toHaveBeenCalled()
      expect(view.refreshRequired).toHaveBeenCalledTimes(1)
      expect(alertText()).toBe(t('settings.remoteServers.credentials.stale'))
      expect(field().value).toBe('')
      expect(state.refreshRequired).toBe(true)
      expect(state.pending).toBe(change === 'shared-pending' ? 1 : 0)
      safe(context)
    },
    )
  }

  it.each(['prop', 'store', 'deleted'] as const)('does not apply a confirmed late DTO after the %s binding changed', async change => {
    const context = await harness()
    const pending = deferred<Response>()
    network.mockReturnValueOnce(pending.promise)
    const view = await mountForm(context)
    input(); submit()
    if (change === 'prop') view.value.value = profile({ profile_id: otherId })
    if (change === 'store') context.store.items = [profile({ revision: 9 })]
    if (change === 'deleted') context.store.items = []
    await flush()
    pending.resolve(response(profile({ revision: 8 })))
    await flush()
    expect(view.saved).not.toHaveBeenCalled()
    expect(view.refreshRequired).toHaveBeenCalledTimes(1)
    expect(context.store.items).toEqual(change === 'deleted' ? [] : [profile({ revision: change === 'store' ? 9 : 7 })])
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: true })
    safe(context)
  })

  it.each(['non-advancing', 'different-id', 'explicit-false'] as const)('treats %s applyMutation failure as stale, not saved or permission to retry', async kind => {
    const context = await harness()
    if (kind === 'explicit-false') vi.spyOn(context.store, 'applyMutation').mockReturnValue(false)
    network.mockResolvedValueOnce(response(profile({ revision: kind === 'non-advancing' ? 7 : 8,
      profile_id: kind === 'different-id' ? otherId : id })))
    const view = await mountForm(context)
    input(); submit(); await flush()
    expect(view.saved).not.toHaveBeenCalled()
    expect(view.refreshRequired).toHaveBeenCalledTimes(1)
    expect(context.store.items).toEqual([profile()])
    expect(context.store.busy).toEqual({})
    input(); submit()
    expect(requests()).toHaveLength(1)
    expect(alertText()).toBe(t('settings.remoteServers.credentials.stale'))
    safe(context)
  })
})

describe('safe local errors without retry or implicit verification', () => {
  for (const intent of ['save', 'forget'] as const) {
    it.each([
      [409, 'profile_revision_conflict', 'conflict'], [409, 'credential_binding_conflict', 'conflict'],
      [409, 'profile_recovery_required', 'conflict'], [409, 'profile_in_use', 'inUse'],
      [409, 'profile_consumers_not_drained', 'notDrained'], [422, 'invalid_credential', 'invalid'],
      [503, 'credential_store_unavailable', 'unavailable'],
      [502, 'invalid_response', 'failed'], [401, 'studio_unauthorised', 'failed'],
      [403, 'studio_forbidden', 'failed'], [405, 'method_not_allowed', 'failed'],
    ] as const)(`${intent}: maps %s/%s to localized %s and never echoes details`, async (status, code, key) => {
      vi.useFakeTimers()
      const context = await harness()
      network.mockResolvedValueOnce(response({ code, details: { message: secret, root: secret } }, status))
      const view = await mountForm(context)
      const storage = vi.spyOn(Storage.prototype, 'setItem')
      await act(intent); await flush()
      expect(alertText()).toBe(t(`settings.remoteServers.credentials.${key}`))
      expect(view.saved).not.toHaveBeenCalled()
      expect(view.refreshRequired).toHaveBeenCalledTimes(status === 409 ? 1 : 0)
      expect(context.store.items).toEqual([profile()])
      expect(context.store.errors).toEqual({})
      expect(context.store.busy).toEqual({})
      expect(field().value).toBe('')
      expect(storage).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(300_000)
      submit()
      expect(requests()).toHaveLength(1)
      expect(network).toHaveBeenCalledTimes(2)
      if (status === 409) expect(remoteProfileMetadataState(context.store).refreshRequired).toBe(true)
      safe(context)
    })
  }

  it.each(['network', 'malformed-json', 'raw-code', 'raw-body'] as const)('sanitizes %s carrying a secret canary', async kind => {
    const context = await harness()
    if (kind === 'network') network.mockRejectedValueOnce(new Error(secret))
    if (kind === 'malformed-json') network.mockResolvedValueOnce(new Response(secret, { status: 503 }))
    if (kind === 'raw-code') network.mockResolvedValueOnce(response({ code: secret, details: { message: secret } }, 502))
    if (kind === 'raw-body') network.mockResolvedValueOnce(response({ message: secret, token: secret }, 502))
    const view = await mountForm(context)
    input(); submit(); await flush()
    expect(alertText()).toBe(t(`settings.remoteServers.credentials.${kind === 'malformed-json' ? 'unavailable' : 'failed'}`))
    expect(view.saved).not.toHaveBeenCalled()
    expect(view.errors).not.toHaveBeenCalled()
    expect(context.store.items).toEqual([profile()])
    expect(requests()).toHaveLength(1)
    safe(context)
  })

  it('clears immediately and safely handles a synchronously thrown fetch error', async () => {
    const context = await harness()
    network.mockImplementationOnce(() => { throw new Error(secret) })
    const view = await mountForm(context)
    input(); submit()
    expect(field().value).toBe('')
    await flush()
    expect(alertText()).toBe(t('settings.remoteServers.credentials.failed'))
    expect(view.saved).not.toHaveBeenCalled()
    expect(view.errors).not.toHaveBeenCalled()
    expect(context.store.busy).toEqual({})
    expect(remoteProfileMetadataState(context.store).pending).toBe(0)
    safe(context)
  })

  it.each(['unexpected-201', 'unexpected-204', 'token-field', 'missing-field', 'bad-source', 'bad-verification'] as const)(
    'does not apply an invalid or non-200 successful response (%s)', async kind => {
      const context = await harness()
      const apply = vi.spyOn(context.store, 'applyMutation')
      const body: Record<string, unknown> = { ...profile({ revision: 8 }) }
      if (kind === 'token-field') body.token = secret
      if (kind === 'missing-field') delete body.has_token
      if (kind === 'bad-source') body.credential_source = 'none' // has_token remains true.
      if (kind === 'bad-verification') body.verification = { state: 'unavailable', message: secret }
      network.mockResolvedValueOnce(response(body, kind === 'unexpected-201' ? 201 : kind === 'unexpected-204' ? 204 : 200))
      const view = await mountForm(context)
      await act('forget'); await flush()
      expect(apply).not.toHaveBeenCalled()
      expect(view.saved).not.toHaveBeenCalled()
      expect(alertText()).toBe(t('settings.remoteServers.credentials.failed'))
      expect(context.store.items).toEqual([profile()])
      expect(context.store.busy).toEqual({})
      safe(context)
    },
  )
})

describe('renderer ownership, abandonment and shared metadata latch', () => {
  for (const intent of ['save', 'forget'] as const) {
    it.each(['cancel', 'escape', 'unmount'] as const)(`${intent}: keeps the fence across %s/remount until settlement and explicit Refresh`, async action => {
      const context = await harness()
      const state = remoteProfileMetadataState(context.store)
      const pending = deferred<Response>()
      network.mockReturnValueOnce(pending.promise)
      const end = vi.spyOn(context.store, 'endMutation')
      const apply = vi.spyOn(context.store, 'applyMutation')
      const dispose = vi.spyOn(context.store, 'dispose')
      const first = await mountForm(context)
      await act(intent)
      expect(state).toEqual({ pending: 1, refreshRequired: false })
      if (action === 'cancel') button('cancel').click()
      if (action === 'escape') form().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      unmount(first)
      expect(state).toEqual({ pending: 1, refreshRequired: true })
      expect(end).not.toHaveBeenCalled()
      expect(context.store.busy[id]).toBe('mutation')
      expect(context.store.beginMutation(id)).toBeNull()
      expect(await context.store.verify(id)).toBe(false) // No request may bypass the fence.
      network.mockResolvedValueOnce(response({ items: [profile({ revision: 8 })] }))
      expect(await context.store.load()).toBe(false) // Pending GET must not publish a snapshot.
      const second = await mountForm(context)
      input(); submit()
      await flush()
      expect(button('submit').disabled).toBe(true)
      expect(requests()).toHaveLength(1)
      pending.resolve(response(profile({ revision: 8 })))
      await flush()
      expect(first.saved).not.toHaveBeenCalled()
      expect(first.refreshRequired).not.toHaveBeenCalled()
      expect(second.saved).not.toHaveBeenCalled()
      expect(apply).not.toHaveBeenCalled()
      expect(end).toHaveBeenCalledTimes(1)
      expect(dispose).not.toHaveBeenCalled()
      expect(context.store.items).toEqual([profile()])
      expect(context.store.busy).toEqual({})
      expect(state).toEqual({ pending: 0, refreshRequired: true })
      unmount(second)
      const third = await mountForm(context)
      input(); submit(); await flush()
      expect(requests()).toHaveLength(1)
      unmount(third)
      network.mockResolvedValueOnce(response({ code: 'metadata_unavailable', details: {} }, 503))
      expect(await context.store.load()).toBe(false)
      expect(state.refreshRequired).toBe(true)
      network.mockResolvedValueOnce(response({ items: [profile({ revision: 8 })] }))
      expect(await context.store.load()).toBe(true)
      expect(state.refreshRequired).toBe(true) // The form/store never owns the parent's refresh latch.
      state.refreshRequired = false // Parent's explicit successful Refresh.
      const fourth = await mountForm(context, context.store.items[0])
      network.mockResolvedValueOnce(response(profile({ revision: 9 })))
      input(); submit(); await flush()
      expect(JSON.parse(String(requests()[1]![1]?.body))).toEqual({ expected_revision: 8, token: secret })
      expect(fourth.saved).toHaveBeenCalledTimes(1)
      expect(state).toEqual({ pending: 0, refreshRequired: false })
      safe(context)
    })
  }

  it('keeps a conflict latch across modal remount and ordinary metadata load until the parent explicitly clears it', async () => {
    const context = await harness()
    network.mockResolvedValueOnce(response({ code: 'profile_revision_conflict', details: {} }, 409))
    const first = await mountForm(context)
    input(); submit(); await flush()
    button('cancel').click(); unmount(first)
    network.mockResolvedValueOnce(response({ items: [profile({ revision: 8 })] }))
    expect(await context.store.load()).toBe(true)
    const second = await mountForm(context, context.store.items[0])
    input(); submit(); await flush()
    expect(requests()).toHaveLength(1)
    expect(second.saved).not.toHaveBeenCalled()
    expect(second.refreshRequired).toHaveBeenCalledTimes(1)
    expect(remoteProfileMetadataState(context.store).refreshRequired).toBe(true)
    safe(context)
  })

  it.each(['save', 'forget'] as const)('does not interpret synchronous saved→unmount as abandoned %s', async intent => {
    const context = await harness()
    network.mockResolvedValueOnce(response(profile({ revision: 8 })))
    const view = await mountForm(context)
    view.saved.mockImplementation(() => unmount(view))
    await act(intent); await flush()
    expect(view.saved).toHaveBeenCalledExactlyOnceWith()
    expect(view.active).toBe(false)
    expect(context.store.items).toEqual([profile({ revision: 8 })])
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: false })
    safe(context)
  })

  for (const intent of ['save', 'forget'] as const) {
    it.each(['close', 'unmount'] as const)(`${intent}: ignores a late error after %s while retiring only its own ticket`, async action => {
    const context = await harness()
    const pending = deferred<Response>()
    network.mockReturnValueOnce(pending.promise)
    const view = await mountForm(context)
    await act(intent)
    if (action === 'close') button('cancel').click()
    else unmount(view)
    pending.reject(new Error(secret))
    await flush()
    expect(view.saved).not.toHaveBeenCalled()
    expect(view.refreshRequired).not.toHaveBeenCalled()
    expect(view.errors).not.toHaveBeenCalled()
    expect(context.store.items).toEqual([profile()])
    expect(context.store.errors).toEqual({})
    expect(context.store.busy).toEqual({})
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: true })
    if (action === 'close') expect(alertText()).toBeUndefined()
    safe(context)
    })
  }

  it('shares a credential request fence with the metadata form, without a credential-local latch', async () => {
    const context = await harness()
    const pending = deferred<Response>()
    network.mockReturnValueOnce(pending.promise)
    const credentialView = await mountForm(context)
    input(); submit(); unmount(credentialView)
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp({ render: () => h(RemoteProfileMetadataForm, { profile: profile() }) })
    app.use(context.pinia)
    mounted.push({ app, host, active: true })
    app.mount(host)
    const metadataForm = document.body.querySelector<HTMLFormElement>('[data-testid="remote-metadata-form"]')!
    const name = metadataForm.querySelector<HTMLInputElement>('[data-testid="remote-metadata-name"]')!
    name.value = 'No bypass'
    name.dispatchEvent(new Event('input', { bubbles: true }))
    metadataForm.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    expect(metadataForm.querySelector<HTMLButtonElement>('[data-testid="remote-metadata-submit"]')!.disabled).toBe(true)
    expect(requests()).toHaveLength(1)
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 1, refreshRequired: true })
    pending.resolve(response(profile({ revision: 8 })))
    await flush()
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: true })
    safe(context)
  })

  it('honours a metadata form’s actual pending request before taking a credential ticket', async () => {
    const context = await harness()
    const pending = deferred<Response>()
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp({ render: () => h(RemoteProfileMetadataForm, { profile: profile() }) })
    app.use(context.pinia)
    mounted.push({ app, host, active: true })
    app.mount(host)
    const metadataForm = document.body.querySelector<HTMLFormElement>('[data-testid="remote-metadata-form"]')!
    const name = metadataForm.querySelector<HTMLInputElement>('[data-testid="remote-metadata-name"]')!
    name.value = 'Pending rename'
    name.dispatchEvent(new Event('input', { bubbles: true }))
    network.mockReturnValueOnce(pending.promise)
    metadataForm.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    expect(requests()).toHaveLength(1)
    expect(requests()[0]![1]?.method).toBe('PATCH')
    expect(remoteProfileMetadataState(context.store).pending).toBe(1)
    const begin = vi.spyOn(context.store, 'beginMutation')
    const view = await mountForm(context)
    input(); submit(); await flush()
    expect(begin).not.toHaveBeenCalled()
    expect(requests()).toHaveLength(1)
    expect(view.saved).not.toHaveBeenCalled()
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 1, refreshRequired: true })
    pending.resolve(response(profile({ revision: 8, name: 'Pending rename' })))
    await flush()
    expect(context.store.items).toEqual([profile({ revision: 8, name: 'Pending rename' })])
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: true })
    safe(context, true)
  })

  it('isolates A/B Pinia roots when A settles after navigation and B successfully changes its own key', async () => {
    const a = await harness()
    const pending = deferred<Response>()
    network.mockReturnValueOnce(pending.promise)
    const first = await mountForm(a)
    input(); submit(); unmount(first)
    const b = await harness([profile({ name: 'B profile' })])
    expect(remoteProfileMetadataState(a.store)).not.toBe(remoteProfileMetadataState(b.store))
    expect(remoteProfileMetadataState(b.store)).toEqual({ pending: 0, refreshRequired: false })
    const second = await mountForm(b, b.store.items[0])
    network.mockResolvedValueOnce(response(profile({ revision: 8, name: 'B confirmed', credential_source: 'environment' })))
    input(); submit(); await flush()
    pending.resolve(response(profile({ revision: 8, name: 'Late A' })))
    await flush()
    expect(first.saved).not.toHaveBeenCalled()
    expect(second.saved).toHaveBeenCalledTimes(1)
    expect(a.store.items).toEqual([profile()])
    expect(b.store.items).toEqual([profile({ revision: 8, name: 'B confirmed', credential_source: 'environment' })])
    expect(a.store.busy).toEqual({})
    expect(b.store.busy).toEqual({})
    expect(remoteProfileMetadataState(a.store)).toEqual({ pending: 0, refreshRequired: true })
    expect(remoteProfileMetadataState(b.store)).toEqual({ pending: 0, refreshRequired: false })
    expect(requests()).toHaveLength(2)
    safe(a); safe(b)
  })
})

describe('Gate L: key intents wait for the user-confirmed consumer census (dev spec §16)', () => {
  function gate() { return form().querySelector('[data-testid="remote-credential-gate"]') }
  function panel(testId: string) { return form().querySelector<HTMLElement>(`[data-testid="remote-consumers-${testId}"]`) }
  function postedConfirmations() {
    return consumersNetwork.mock.calls.filter(([, init]) => init?.method === 'POST')
  }

  it('keeps Save and Forget disabled while the census is unknown and sends nothing on forced submit', async () => {
    const context = await harness()
    consumersNetwork.mockImplementation(async () => response(census(id, {
      state: 'unknown', confirmed_at: null, bound_roots: ['D:\games\alpha'],
    })))
    const begin = vi.spyOn(context.store, 'beginMutation')
    await mountForm(context)
    input()
    await flush()
    expect(button('submit').disabled).toBe(true)
    expect(button('forget').disabled).toBe(true)
    expect(gate()?.textContent).toBe(t('settings.remoteServers.consumers.required'))
    submit(); forceClick(button('forget'))
    await flush()
    expect(begin).not.toHaveBeenCalled()
    expect(requests()).toHaveLength(0)
    expect(postedConfirmations()).toHaveLength(0)
    expect(field().value).toBe(secret)
    input('')
  })

  it('enables Save only after the user confirms exactly the shown URL and projects, then PUTs the key', async () => {
    const context = await harness()
    const roots = ['D:\games\alpha', 'D:\games\beta']
    consumersNetwork.mockImplementation(async (_url, init) => response(census(id, init?.method === 'POST'
      ? { bound_roots: roots } : { state: 'unknown', confirmed_at: null, bound_roots: roots })))
    const view = await mountForm(context)
    input()
    expect(button('submit').disabled).toBe(true)
    panel('acknowledge')!.click()
    await flush()
    panel('confirm')!.click()
    await flush()
    expect(postedConfirmations()).toHaveLength(1)
    const [url, init] = postedConfirmations()[0]!
    expect(url).toBe(`/api/remote/servers/${id}/consumers/confirmation`)
    expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 7, url: 'https://remote.example/team', bound_roots: roots })
    expect(String(init?.body)).not.toContain(secret)
    expect(gate()).toBeNull()
    expect(button('submit').disabled).toBe(false)
    network.mockResolvedValueOnce(response(profile({ revision: 8 })))
    submit(); await flush()
    expect(requests()).toHaveLength(1)
    expect(view.saved).toHaveBeenCalledExactlyOnceWith()
    safe(context)
  })

  it('warns about syncing projects without stopping them', async () => {
    const context = await harness()
    consumersNetwork.mockImplementation(async () => response(census(id, {
      bound_roots: ['D:\games\alpha'], active_roots: ['D:\games\alpha'],
    })))
    await mountForm(context)
    expect(panel('active-warning')?.textContent?.trim()).toBe(t('settings.remoteServers.consumers.activeWarning'))
    expect(panel('active')?.textContent?.trim()).toBe(t('settings.remoteServers.consumers.syncing'))
    expect(consumersNetwork.mock.calls.every(([, init]) => init?.method === 'GET')).toBe(true)
  })

  it('stays gated when the census cannot be read', async () => {
    const context = await harness()
    consumersNetwork.mockImplementation(async () => response({ code: 'consumer_lifecycle_unavailable', details: { root: secret } }, 503))
    await mountForm(context)
    input()
    expect(button('submit').disabled).toBe(true)
    expect(panel('error')?.textContent).toBe(t('settings.remoteServers.consumers.unavailable'))
    submit(); await flush()
    expect(requests()).toHaveLength(0)
    input('')
    safe(context)
  })

  it.each(['save', 'forget'] as const)('%s: maps 503 consumer_lifecycle_unavailable to a localized hint and re-reads the census, never re-confirming', async intent => {
    const context = await harness()
    network.mockResolvedValueOnce(response({ code: 'consumer_lifecycle_unavailable', details: { message: secret } }, 503))
    const view = await mountForm(context)
    consumersNetwork.mockImplementation(async () => response(census(id, { state: 'unknown', confirmed_at: null })))
    await act(intent); await flush()
    expect(alertText()).toBe(t('settings.remoteServers.consumers.notConfirmed'))
    expect(view.refreshRequired).not.toHaveBeenCalled()
    expect(consumersNetwork).toHaveBeenCalledTimes(2)
    expect(postedConfirmations()).toHaveLength(0)
    expect(button('submit').disabled).toBe(true)
    expect(requests()).toHaveLength(1)
    safe(context)
  })
})
