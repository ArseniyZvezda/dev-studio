// @vitest-environment jsdom
/** Direct metadata API boundary + real renderer-local Pinia and ModalDialog (§5.1/§10.1).
 * Deferred replies exercise abandonment without releasing in-flight mutation fences.
 */
import { createPinia, disposePinia } from 'pinia'
import { createApp, h, isReactive, nextTick, shallowRef, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { createRemoteApiClient, RemoteApiError, type RemoteServerProfile } from '@/api/remote'
import { t } from '@/i18n'
import { useRemoteServersStore } from '@/stores/remote-servers'
import RemoteProfileMetadataForm, { remoteProfileMetadataState } from './RemoteProfileMetadataForm.vue'

vi.mock('@/api/remote', async importOriginal => ({
  ...await importOriginal<typeof import('@/api/remote')>(), createRemoteApiClient: vi.fn(),
}))

type Client = ReturnType<typeof createRemoteApiClient>
const id = '11111111-1111-4111-8111-111111111111'
const otherId = '22222222-2222-4222-8222-222222222222'
const secret = 'RAW-REMOTE-DETAILS-CREDENTIAL-CANARY'
const piniae: ReturnType<typeof createPinia>[] = []
const mounted: { app: App; host: HTMLDivElement; active: boolean }[] = []

function profile(overrides: Partial<RemoteServerProfile> = {}): RemoteServerProfile {
  return { profile_id: id, name: 'Original', url: 'https://remote.example/team', revision: 7,
    has_token: true, credential_source: 'stored', verification: { state: 'unavailable' }, ...overrides }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function makeClient() {
  return {
    listServers: vi.fn<Client['listServers']>(), verify: vi.fn<Client['verify']>(),
    createProfile: vi.fn<Client['createProfile']>(), renameProfile: vi.fn<Client['renameProfile']>(),
    changeProfileUrl: vi.fn<Client['changeProfileUrl']>(), setCredential: vi.fn<Client['setCredential']>(),
    clearCredential: vi.fn<Client['clearCredential']>(), deleteProfile: vi.fn<Client['deleteProfile']>(),
    recoverProfile: vi.fn<Client['recoverProfile']>(), listProjects: vi.fn<Client['listProjects']>(),
    createProject: vi.fn<Client['createProject']>(), observe: vi.fn<Client['observe']>(),
  } satisfies Client
}
async function harness(items: RemoteServerProfile[] = []) {
  const pinia = createPinia()
  piniae.push(pinia)
  const api = makeClient()
  vi.mocked(createRemoteApiClient).mockReturnValue(api)
  const store = useRemoteServersStore(pinia)
  api.listServers.mockResolvedValue({ items })
  expect(await store.load()).toBe(true)
  api.listServers.mockClear()
  return { pinia, api, store }
}
type Harness = Awaited<ReturnType<typeof harness>>
function mountForm(context: Harness, initial?: RemoteServerProfile) {
  vi.mocked(createRemoteApiClient).mockReturnValue(context.api)
  const value = shallowRef(initial)
  const saved = vi.fn()
  const close = vi.fn()
  const refreshRequired = vi.fn()
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(RemoteProfileMetadataForm, {
    profile: value.value, onSaved: saved, onClose: close, onRefreshRequired: refreshRequired,
  }) })
  app.use(context.pinia)
  const view = { app, host, active: true, saved, close, refreshRequired, value }
  mounted.push(view)
  app.mount(host)
  return view
}
function unmount(view: typeof mounted[number]) {
  if (!view.active) return
  view.active = false
  view.app.unmount()
  view.host.remove()
}
async function flush() {
  await Promise.resolve(); await nextTick(); await Promise.resolve(); await nextTick()
}
function form(): HTMLFormElement {
  const node = document.body.querySelector<HTMLFormElement>('[data-testid="remote-metadata-form"]')
  expect(node).not.toBeNull()
  return node!
}
function input(kind: 'name' | 'url', value: string) {
  const node = form().querySelector<HTMLInputElement>(`[data-testid="remote-metadata-${kind}"]`)
  expect(node).not.toBeNull()
  node!.value = value
  node!.dispatchEvent(new Event('input', { bubbles: true }))
}
function button(kind: 'submit' | 'cancel') {
  return form().querySelector<HTMLButtonElement>(`[data-testid="remote-metadata-${kind}"]`)!
}
function submit(node = form()) { node.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) }
function alertText() { return form().querySelector('[role="alert"]')?.textContent }
function assertNoSideEffects(context: Harness) {
  for (const [name, method] of Object.entries(context.api)) {
    if (name !== 'createProfile' && name !== 'renameProfile') expect(method, name).not.toHaveBeenCalled()
  }
  expect(fetch).not.toHaveBeenCalled()
  expect(WebSocket).not.toHaveBeenCalled()
  expect(JSON.stringify(context.store.$state)).not.toContain(secret)
  expect(document.body.innerHTML).not.toContain(secret)
}
function mutate(context: Harness, value: RemoteServerProfile) {
  const ticket = context.store.beginMutation(value.profile_id)
  expect(ticket).not.toBeNull()
  expect(context.store.applyMutation(ticket!, value)).toBe(true)
}

beforeEach(() => {
  vi.mocked(createRemoteApiClient).mockReset()
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected network')))
  vi.stubGlobal('WebSocket', vi.fn())
})
afterEach(() => {
  for (const view of mounted.splice(0)) unmount(view)
  for (const pinia of piniae.splice(0)) disposePinia(pinia)
  document.body.replaceChildren()
  vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})

describe('keyless Create and name-only Rename', () => {
  it('mounts an accessible, focused keyless modal without requests, timers, Verify or credential controls', async () => {
    vi.useFakeTimers()
    const context = await harness()
    mountForm(context)
    await flush()
    expect(document.body.querySelector('[role="dialog"]')?.getAttribute('aria-label'))
      .toBe(t('settings.remoteServers.metadata.addTitle'))
    expect(document.activeElement?.getAttribute('data-testid')).toBe('remote-metadata-name')
    expect(form().textContent).toContain(t('settings.remoteServers.metadata.keylessHint'))
    expect(form().querySelectorAll('input')).toHaveLength(2)
    expect(form().querySelector('input[type="password"]')).toBeNull()
    await vi.advanceTimersByTimeAsync(300_000)
    expect(vi.getTimerCount()).toBe(0)
    for (const method of Object.values(context.api)) expect(method).not.toHaveBeenCalled()
    assertNoSideEffects(context)
  })

  it('takes a create ticket BEFORE exact direct payload dispatch and applies only the confirmed DTO', async () => {
    const context = await harness()
    const pending = deferred<RemoteServerProfile>()
    context.api.createProfile.mockReturnValue(pending.promise)
    const begin = vi.spyOn(context.store, 'beginMutation')
    const apply = vi.spyOn(context.store, 'applyMutation')
    const view = mountForm(context)
    input('name', '  Team label  ')
    input('url', 'https://remote.example/team/')
    submit()
    expect(begin).toHaveBeenCalledExactlyOnceWith(undefined)
    expect(begin.mock.invocationCallOrder[0]).toBeLessThan(context.api.createProfile.mock.invocationCallOrder[0]!)
    expect(context.api.createProfile).toHaveBeenCalledExactlyOnceWith({
      name: '  Team label  ', url: 'https://remote.example/team/',
    })
    expect(context.store.items).toEqual([])
    expect(view.saved).not.toHaveBeenCalled()
    const confirmed = profile({ name: 'Team label', revision: 1, has_token: false, credential_source: 'none' })
    pending.resolve(confirmed)
    await flush()
    expect(apply).toHaveBeenCalledExactlyOnceWith(begin.mock.results[0]!.value, confirmed)
    expect(context.store.items).toEqual([confirmed])
    expect(view.saved).toHaveBeenCalledTimes(1)
    submit()
    expect(context.api.createProfile).toHaveBeenCalledTimes(1)
    expect(view.close).not.toHaveBeenCalled()
    unmount(view)
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: false })
    assertNoSideEffects(context)
  })

  it('renames using the captured revision and name alone, preserving credential and URL from the DTO', async () => {
    const context = await harness([profile()])
    const confirmed = profile({ name: 'Renamed', revision: 8 })
    context.api.renameProfile.mockResolvedValue(confirmed)
    const view = mountForm(context, context.store.items[0])
    expect(form().querySelector('[data-testid="remote-metadata-url"]')).toBeNull()
    expect(form().querySelectorAll('input')).toHaveLength(1)
    input('name', 'Renamed')
    submit()
    await flush()
    expect(context.api.renameProfile).toHaveBeenCalledExactlyOnceWith(id, { expected_revision: 7, name: 'Renamed' })
    expect(context.api.createProfile).not.toHaveBeenCalled()
    expect(context.store.items).toEqual([confirmed])
    expect(view.saved).toHaveBeenCalledTimes(1)
    assertNoSideEffects(context)
  })

  it.each([
    ['', 'https://remote.example'], ['  ', 'https://remote.example'],
    ['a'.repeat(121), 'https://remote.example'], ['bad\u0001name', 'https://remote.example'], ['Name', ''],
  ])('rejects invalid local fields without acquiring a ticket (%j, %j)', async (name, url) => {
    const context = await harness()
    const begin = vi.spyOn(context.store, 'beginMutation')
    mountForm(context)
    input('name', name); input('url', url); submit()
    await flush()
    expect(alertText()).toBe(t('settings.remoteServers.metadata.invalid'))
    expect(begin).not.toHaveBeenCalled()
    expect(context.api.createProfile).not.toHaveBeenCalled()
  })

  it('never dispatches when beginMutation returns null', async () => {
    const context = await harness()
    vi.spyOn(context.store, 'beginMutation').mockReturnValue(null)
    const view = mountForm(context)
    input('name', 'Name'); input('url', 'https://remote.example'); submit()
    await flush()
    expect(context.api.createProfile).not.toHaveBeenCalled()
    expect(alertText()).toBe(t('settings.remoteServers.metadata.stale'))
    expect(view.saved).not.toHaveBeenCalled()
    expect(view.refreshRequired).toHaveBeenCalledTimes(1)
    assertNoSideEffects(context)
  })

  it('honours another owner’s real profile ticket without retiring it', async () => {
    const context = await harness([profile()])
    const owner = context.store.beginMutation(id)!
    const end = vi.spyOn(context.store, 'endMutation')
    mountForm(context, profile())
    input('name', 'Blocked'); submit()
    await flush()
    expect(context.api.renameProfile).not.toHaveBeenCalled()
    expect(context.store.busy[id]).toBe('mutation')
    expect(end).not.toHaveBeenCalled()
    expect(context.store.endMutation(owner, false)).toBe(true)
  })

  it.each(['revision', 'url', 'deleted', 'prop'] as const)('rejects stale %s before acquiring a fresh ticket with old CAS', async change => {
    const context = await harness([profile()])
    const view = mountForm(context, context.store.items[0])
    if (change === 'revision') mutate(context, profile({ revision: 8 }))
    if (change === 'url') mutate(context, profile({ revision: 8, url: 'https://new.example' }))
    if (change === 'deleted') {
      const ticket = context.store.beginMutation(id)!
      expect(context.store.applyRemoval(ticket)).toBe(true)
    }
    if (change === 'prop') view.value.value = profile({ profile_id: otherId })
    await flush()
    const begin = vi.spyOn(context.store, 'beginMutation')
    input('name', 'Old CAS'); submit()
    await flush()
    expect(begin).not.toHaveBeenCalled()
    expect(context.api.renameProfile).not.toHaveBeenCalled()
    expect(alertText()).toBe(t('settings.remoteServers.metadata.stale'))
    expect(view.saved).not.toHaveBeenCalled()
  })

  it('does not treat applyMutation false as success or refresh automatically', async () => {
    const context = await harness([profile()])
    context.api.createProfile.mockResolvedValue(profile()) // Cannot replace an existing ID by Create.
    const view = mountForm(context)
    input('name', 'New'); input('url', 'https://new.example'); submit()
    await flush()
    expect(context.store.items).toEqual([profile()])
    expect(view.saved).not.toHaveBeenCalled()
    expect(alertText()).toBe(t('settings.remoteServers.metadata.stale'))
    expect(view.refreshRequired).toHaveBeenCalledTimes(1)
    submit()
    expect(context.api.createProfile).toHaveBeenCalledTimes(1)
    assertNoSideEffects(context)
  })

  it('prevents double submit synchronously, including forced submit events while the button is disabled', async () => {
    const context = await harness([profile()])
    const pending = deferred<RemoteServerProfile>()
    context.api.renameProfile.mockReturnValue(pending.promise)
    const view = mountForm(context, profile())
    const node = form()
    input('name', 'Once'); submit(node); submit(node)
    await flush()
    expect(button('submit').disabled).toBe(true)
    expect(button('cancel').disabled).toBe(false)
    expect(form().getAttribute('aria-busy')).toBe('true')
    expect(form().textContent).toContain(t('settings.remoteServers.metadata.saving'))
    submit(node)
    expect(context.api.renameProfile).toHaveBeenCalledTimes(1)
    pending.resolve(profile({ revision: 8, name: 'Once' }))
    await flush()
    expect(view.saved).toHaveBeenCalledTimes(1)
    expect(context.store.busy).toEqual({})
  })
})

describe('sanitized explicit errors', () => {
  it.each([
    [409, 'profile_revision_conflict', 'conflict'], [409, 'credential_binding_conflict', 'conflict'],
    [422, 'invalid_server_url', 'invalid'], [503, 'application_store_unavailable', 'unavailable'],
    [502, 'invalid_response', 'failed'], [0, 'network', 'failed'],
  ] as const)('maps %s/%s to safe localized %s without automatic retry or load', async (status, code, key) => {
    vi.useFakeTimers()
    const context = await harness([profile()])
    context.api.renameProfile.mockRejectedValue(new RemoteApiError(status, code, { message: secret }))
    const view = mountForm(context, profile())
    input('name', 'Rename'); submit()
    await flush()
    expect(alertText()).toBe(t(`settings.remoteServers.metadata.${key}`))
    expect(view.saved).not.toHaveBeenCalled()
    expect(context.store.items).toEqual([profile()])
    expect(context.store.busy).toEqual({})
    await vi.advanceTimersByTimeAsync(300_000)
    if (status === 409) { submit(); expect(button('submit').disabled).toBe(true) }
    expect(view.refreshRequired).toHaveBeenCalledTimes(status === 409 ? 1 : 0)
    expect(context.api.renameProfile).toHaveBeenCalledTimes(1)
    assertNoSideEffects(context)
  })

  it('distinguishes URL collision but requires explicit Refresh and forbids resubmit like every conflict', async () => {
    const context = await harness()
    context.api.createProfile.mockRejectedValue(new RemoteApiError(409, 'profile_url_collision', { url: secret }))
    const view = mountForm(context)
    input('name', 'New'); input('url', 'https://remote.example'); submit()
    await flush()
    expect(alertText()).toBe(t('settings.remoteServers.metadata.collision'))
    expect(alertText()).not.toBe(t('settings.remoteServers.metadata.conflict'))
    expect(button('submit').disabled).toBe(true)
    submit()
    expect(context.api.createProfile).toHaveBeenCalledTimes(1)
    expect(view.saved).not.toHaveBeenCalled()
    expect(view.refreshRequired).toHaveBeenCalledTimes(1)
    expect(context.store.items).toEqual([])
    assertNoSideEffects(context)
  })

  it('sanitizes an ordinary thrown error rather than rendering its raw message', async () => {
    const context = await harness()
    context.api.createProfile.mockRejectedValue(new Error(secret))
    const view = mountForm(context)
    input('name', 'New'); input('url', 'https://remote.example'); submit()
    await flush()
    expect(alertText()).toBe(t('settings.remoteServers.metadata.failed'))
    expect(view.saved).not.toHaveBeenCalled()
    expect(context.store.items).toEqual([])
    assertNoSideEffects(context)
  })
})

describe('abandoned continuations and renderer ownership', () => {
  it('shares only reactive pending/refresh UI state for the same store, not another Pinia root', async () => {
    const a = await harness()
    const b = await harness()
    const state = remoteProfileMetadataState(a.store)
    expect(isReactive(state)).toBe(true)
    expect(remoteProfileMetadataState(a.store)).toBe(state)
    expect(Object.keys(state).sort()).toEqual(['pending', 'refreshRequired'])
    state.pending = 1
    state.refreshRequired = true
    expect(remoteProfileMetadataState(b.store)).toEqual({ pending: 0, refreshRequired: false })
    expect(remoteProfileMetadataState(b.store)).not.toBe(state)
  })

  it.each(['cancel', 'escape', 'unmount'] as const)('prevents keyless Create redispatch across %s/remount until settlement AND explicit successful Refresh', async action => {
    const context = await harness()
    const state = remoteProfileMetadataState(context.store)
    const pending = deferred<RemoteServerProfile>()
    context.api.createProfile.mockReturnValueOnce(pending.promise)
    const first = mountForm(context)
    input('name', 'First'); input('url', 'https://first.example'); submit()
    expect(state).toEqual({ pending: 1, refreshRequired: false })
    expect(context.store.busy).toEqual({}) // Create has no profile-scoped busy entry.
    if (action === 'cancel') button('cancel').click()
    if (action === 'escape') form().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    unmount(first)
    expect(state).toEqual({ pending: 1, refreshRequired: true })
    const second = mountForm(context)
    input('name', 'Second'); input('url', 'https://second.example'); submit()
    await flush()
    expect(context.api.createProfile).toHaveBeenCalledTimes(1)
    expect(button('submit').disabled).toBe(true)
    expect(state.pending).toBe(1)
    pending.resolve(profile({ name: 'First', revision: 1, has_token: false, credential_source: 'none' }))
    await flush()
    expect(state).toEqual({ pending: 0, refreshRequired: true })
    expect(context.store.items).toEqual([])
    expect(first.saved).not.toHaveBeenCalled()
    expect(second.saved).not.toHaveBeenCalled()
    unmount(second)
    const third = mountForm(context)
    input('name', 'Third'); input('url', 'https://third.example'); submit()
    await flush()
    expect(context.api.createProfile).toHaveBeenCalledTimes(1)
    expect(state.refreshRequired).toBe(true)
    unmount(third)
    context.api.listServers.mockRejectedValueOnce(new ApiError(503, secret))
    expect(await context.store.load()).toBe(false)
    expect(state.refreshRequired).toBe(true)
    context.api.listServers.mockResolvedValueOnce({ items: [profile()] })
    expect(await context.store.load()).toBe(true)
    state.refreshRequired = false // Only parent's explicit successful Refresh clears the latch.
    context.api.listServers.mockClear()
    context.api.createProfile.mockResolvedValueOnce(profile({ profile_id: otherId, name: 'After refresh', revision: 1 }))
    const fourth = mountForm(context)
    input('name', 'After refresh'); input('url', 'https://second.example'); submit()
    await flush()
    expect(context.api.createProfile).toHaveBeenCalledTimes(2)
    expect(fourth.saved).toHaveBeenCalledTimes(1)
    expect(state).toEqual({ pending: 0, refreshRequired: false })
    assertNoSideEffects(context)
  })

  it('preserves a conflict latch when the form is closed and remounted for the same store', async () => {
    const context = await harness([profile()])
    context.api.renameProfile.mockRejectedValueOnce(new RemoteApiError(409, 'profile_revision_conflict'))
    const first = mountForm(context, profile())
    input('name', 'Conflict'); submit()
    await flush()
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: true })
    button('cancel').click(); unmount(first)
    const second = mountForm(context, profile())
    input('name', 'Cannot bypass'); submit()
    await flush()
    expect(context.api.renameProfile).toHaveBeenCalledTimes(1)
    expect(second.saved).not.toHaveBeenCalled()
    expect(second.refreshRequired).toHaveBeenCalledTimes(1)
    expect(remoteProfileMetadataState(context.store).refreshRequired).toBe(true)
    assertNoSideEffects(context)
  })

  it('does not latch refresh when successful saved synchronously unmounts the form', async () => {
    const context = await harness()
    context.api.createProfile.mockResolvedValueOnce(profile())
    const view = mountForm(context)
    view.saved.mockImplementation(() => unmount(view))
    input('name', 'Success'); input('url', 'https://remote.example'); submit()
    await flush()
    expect(view.saved).toHaveBeenCalledTimes(1)
    expect(view.active).toBe(false)
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: false })
    expect(context.store.items).toEqual([profile()])
    assertNoSideEffects(context)
  })

  it.each(['cancel', 'escape', 'unmount'] as const)('keeps the profile fence until settlement after %s and ignores the late DTO', async action => {
    const context = await harness([profile()])
    const pending = deferred<RemoteServerProfile>()
    context.api.renameProfile.mockReturnValue(pending.promise)
    const dispose = vi.spyOn(context.store, 'dispose')
    const end = vi.spyOn(context.store, 'endMutation')
    const view = mountForm(context, profile())
    input('name', 'Abandoned'); submit()
    if (action === 'cancel') button('cancel').click()
    if (action === 'escape') form().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    if (action === 'unmount') unmount(view)
    else { expect(view.close).toHaveBeenCalledTimes(1); submit() }
    await flush()
    expect(end).not.toHaveBeenCalled()
    expect(context.store.busy[id]).toBe('mutation')
    expect(context.store.beginMutation(id)).toBeNull()
    context.api.listServers.mockResolvedValueOnce({ items: [profile({ revision: 8 })] })
    expect(await context.store.load()).toBe(false)
    context.api.listServers.mockClear()
    pending.resolve(profile({ name: 'Abandoned', revision: 8 }))
    await flush()
    expect(context.store.items).toEqual([profile()])
    expect(view.saved).not.toHaveBeenCalled()
    expect(view.refreshRequired).not.toHaveBeenCalled()
    expect(end).toHaveBeenCalledTimes(1)
    expect(context.store.busy).toEqual({})
    expect(dispose).not.toHaveBeenCalled()
    expect(remoteProfileMetadataState(context.store)).toEqual({ pending: 0, refreshRequired: true })
    const ticket = context.store.beginMutation(id)
    expect(ticket).not.toBeNull()
    context.store.endMutation(ticket!, false)
    assertNoSideEffects(context)
  })

  it('keeps a keyless Create fence after unmount until its late success settles', async () => {
    const context = await harness()
    const pending = deferred<RemoteServerProfile>()
    context.api.createProfile.mockReturnValue(pending.promise)
    const view = mountForm(context)
    input('name', 'New'); input('url', 'https://remote.example'); submit()
    unmount(view)
    context.api.listServers.mockResolvedValue({ items: [profile()] })
    expect(await context.store.load()).toBe(false)
    pending.resolve(profile())
    await flush()
    expect(view.saved).not.toHaveBeenCalled()
    expect(context.store.items).toEqual([])
    expect(await context.store.load()).toBe(true) // Only the explicit later Refresh can publish it.
    expect(context.store.items).toEqual([profile()])
  })

  it('blocks an unmount/remount rename race until the first request settles, then requires a fresh form', async () => {
    const context = await harness([profile()])
    const pending = deferred<RemoteServerProfile>()
    context.api.renameProfile.mockReturnValue(pending.promise)
    const first = mountForm(context, profile())
    input('name', 'First'); submit(); unmount(first)
    const second = mountForm(context, profile())
    input('name', 'Second'); submit()
    await flush()
    expect(context.api.renameProfile).toHaveBeenCalledTimes(1)
    expect(context.store.busy[id]).toBe('mutation')
    pending.resolve(profile({ name: 'First', revision: 8 }))
    await flush()
    expect(context.store.items).toEqual([profile()])
    expect(first.saved).not.toHaveBeenCalled()
    expect(second.saved).not.toHaveBeenCalled()
    submit()
    expect(context.api.renameProfile).toHaveBeenCalledTimes(1)
    unmount(second)
    expect(await context.store.load()).toBe(true)
    remoteProfileMetadataState(context.store).refreshRequired = false // Parent's explicit successful Refresh.
    context.api.renameProfile.mockResolvedValue(profile({ name: 'Third', revision: 8 }))
    const third = mountForm(context, profile())
    input('name', 'Third'); submit()
    await flush()
    expect(context.api.renameProfile).toHaveBeenCalledTimes(2)
    expect(third.saved).toHaveBeenCalledTimes(1)
  })

  it('ignores late errors after close and retires its own ticket without setting store errors', async () => {
    const context = await harness([profile()])
    const pending = deferred<RemoteServerProfile>()
    context.api.renameProfile.mockReturnValue(pending.promise)
    const view = mountForm(context, profile())
    input('name', 'Abandoned'); submit(); button('cancel').click()
    pending.reject(new ApiError(503, secret))
    await flush()
    expect(context.store.items).toEqual([profile()])
    expect(context.store.errors).toEqual({})
    expect(context.store.busy).toEqual({})
    expect(view.saved).not.toHaveBeenCalled()
    expect(alertText()).toBeUndefined()
    assertNoSideEffects(context)
  })

  it('does not apply an old pending response to a form whose prop binding changed', async () => {
    const context = await harness([profile(), profile({ profile_id: otherId })])
    const pending = deferred<RemoteServerProfile>()
    context.api.renameProfile.mockReturnValue(pending.promise)
    const view = mountForm(context, profile())
    input('name', 'First'); submit()
    view.value.value = profile({ profile_id: otherId })
    pending.resolve(profile({ name: 'First', revision: 8 }))
    await flush()
    expect(context.store.items[0]).toEqual(profile())
    expect(context.store.busy).toEqual({})
    expect(view.saved).not.toHaveBeenCalled()
    expect(alertText()).toBe(t('settings.remoteServers.metadata.stale'))
  })

  it('keeps A and B Pinia renderers isolated when A settles after navigation to B', async () => {
    const a = await harness([profile()])
    const pending = deferred<RemoteServerProfile>()
    a.api.renameProfile.mockReturnValue(pending.promise)
    const first = mountForm(a, profile())
    input('name', 'Late A'); submit(); unmount(first)
    const b = await harness([profile({ name: 'B original' })])
    b.api.renameProfile.mockResolvedValue(profile({ revision: 8, name: 'B saved' }))
    const second = mountForm(b, b.store.items[0])
    input('name', 'B saved'); submit()
    await flush()
    pending.resolve(profile({ revision: 8, name: 'Late A' }))
    await flush()
    expect(first.saved).not.toHaveBeenCalled()
    expect(second.saved).toHaveBeenCalledTimes(1)
    expect(a.store.items).toEqual([profile()])
    expect(b.store.items).toEqual([profile({ revision: 8, name: 'B saved' })])
    expect(a.store.busy).toEqual({})
    expect(b.store.busy).toEqual({})
    expect(remoteProfileMetadataState(a.store)).toEqual({ pending: 0, refreshRequired: true })
    expect(remoteProfileMetadataState(b.store)).toEqual({ pending: 0, refreshRequired: false })
    assertNoSideEffects(a); assertNoSideEffects(b)
  })
})
