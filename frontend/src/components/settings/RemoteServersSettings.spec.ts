// @vitest-environment jsdom
/** Saved metadata and explicit Verify only (remote-identity/studio-design §5.1/§10.1).
 * Exercise native DOM + real Pinia and ModalDialog; only the local API seam is mocked.
 * Deferred responses model other owners of this renderer's store, not live Remote traffic.
 */
import { createPinia, disposePinia } from 'pinia'
import { createApp, h, nextTick, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { createRemoteApiClient, createRemoteConsumersClient, RemoteApiError, type RemoteProfileConsumers,
  type RemoteServerProfile, type RemoteServers, type VerificationDTO } from '@/api/remote'
import { t } from '@/i18n'
import { rendererSession } from '@/session/project-session'
import { useRemoteServersStore } from '@/stores/remote-servers'
import RemoteServersSettings from './RemoteServersSettings.vue'

vi.mock('@/api/remote', async importOriginal => ({
  ...await importOriginal<typeof import('@/api/remote')>(), createRemoteApiClient: vi.fn(),
  createRemoteConsumersClient: vi.fn(),
}))

type Client = ReturnType<typeof createRemoteApiClient>
const id = '11111111-1111-4111-8111-111111111111'
const otherId = '22222222-2222-4222-8222-222222222222'
const secret = 'REMOTE-RAW-ERROR-AND-CREDENTIAL-CANARY'
const checkedAt = '2026-11-16T10:20:30.000Z'
const piniae: ReturnType<typeof createPinia>[] = []
const mounted: { app: App; host: HTMLDivElement; active: boolean }[] = []

function profile(overrides: Partial<RemoteServerProfile> = {}): RemoteServerProfile {
  return { profile_id: id, name: 'Team Remote', url: 'https://remote.example/team', revision: 7,
    has_token: true, credential_source: 'stored', verification: { state: 'unavailable' }, ...overrides }
}
function checked(overrides: Partial<VerificationDTO> = {}): VerificationDTO {
  return { state: 'valid', checked_at: checkedAt,
    user: { id: 'alice-id', display_name: 'Alice Remote', color: '#123456' },
    wire_protocol: 2, catalog_version: 1, error_code: null, ...overrides }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function makeClient() {
  return {
    listServers: vi.fn<Client['listServers']>().mockResolvedValue({ items: [] }),
    verify: vi.fn<Client['verify']>().mockResolvedValue(checked()),
    createProfile: vi.fn<Client['createProfile']>(),
    renameProfile: vi.fn<Client['renameProfile']>(),
    changeProfileUrl: vi.fn<Client['changeProfileUrl']>(),
    setCredential: vi.fn<Client['setCredential']>(),
    clearCredential: vi.fn<Client['clearCredential']>(),
    deleteProfile: vi.fn<Client['deleteProfile']>(),
    recoverProfile: vi.fn<Client['recoverProfile']>(),
    listProjects: vi.fn<Client['listProjects']>(),
    createProject: vi.fn<Client['createProject']>(),
    observe: vi.fn<Client['observe']>(),
  } satisfies Client
}
/** Gate L census (dev spec §16): by default the user already confirmed each profile's exact URL. */
let consumers = makeConsumers([])
function makeConsumers(items: RemoteServerProfile[]) {
  const census = (profileId: string): RemoteProfileConsumers => {
    const item = items.find(value => value.profile_id === profileId)!
    return { profile_id: profileId, url: item.url, revision: item.revision, state: 'known',
      confirmed_at: '2026-11-18T10:00:00Z', bound_roots: [], active_roots: [], operations: [] }
  }
  return {
    profileConsumers: vi.fn(async (profileId: string) => census(profileId)),
    confirmProfileConsumers: vi.fn(async (profileId: string) => census(profileId)),
    withdrawProfileConsumers: vi.fn(async (profileId: string) => ({ ...census(profileId), state: 'unknown' as const, confirmed_at: null })),
  } satisfies ReturnType<typeof createRemoteConsumersClient>
}
function harness(items: RemoteServerProfile[] = [profile()]) {
  consumers = makeConsumers(items)
  const pinia = createPinia()
  piniae.push(pinia)
  const api = makeClient()
  api.listServers.mockResolvedValue({ items })
  vi.mocked(createRemoteApiClient).mockReturnValueOnce(api)
  // Deliberately created by a different owner before the component mounts.
  const store = useRemoteServersStore(pinia)
  return { pinia, api, store }
}
type Harness = ReturnType<typeof harness>
function mountView(context: Harness) {
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(RemoteServersSettings) })
  app.use(context.pinia)
  const view = { app, host, active: true }
  mounted.push(view)
  app.mount(host)
  return view
}
function unmount(view: ReturnType<typeof mountView>) {
  if (!view.active) return
  view.active = false
  view.app.unmount()
  view.host.remove()
}
async function flush() {
  // No timers or sleeps: API promises and Vue's render/watch queues only.
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}
function button(root: ParentNode, testId: string): HTMLButtonElement {
  const element = root.querySelector<HTMLButtonElement>(`[data-testid="${testId}"]`)
  expect(element, testId).not.toBeNull()
  return element!
}
function row(root: ParentNode, profileId = id): HTMLElement {
  const element = root.querySelector<HTMLElement>(`[data-profile-id="${profileId}"]`)
  expect(element, profileId).not.toBeNull()
  return element!
}
function verifyButton(root: ParentNode, profileId = id) { return button(row(root, profileId), 'remote-verify') }
function refreshButton(root: ParentNode) { return button(root, 'remote-refresh') }
function dialog(): HTMLElement | null { return document.body.querySelector('[role="dialog"]') }
function forceClick(element: HTMLElement) { element.dispatchEvent(new MouseEvent('click', { bubbles: true })) }
function assertNoOtherCalls(api: ReturnType<typeof makeClient>) {
  for (const [name, method] of Object.entries(api)) {
    if (name !== 'listServers' && name !== 'verify') expect(method, name).not.toHaveBeenCalled()
  }
  expect(fetch).not.toHaveBeenCalled()
  expect(WebSocket).not.toHaveBeenCalled()
}
function mutate(context: Harness, next: RemoteServerProfile) {
  const ticket = context.store.beginMutation(next.profile_id)
  expect(ticket).not.toBeNull()
  expect(context.store.applyMutation(ticket!, next)).toBe(true)
}

beforeEach(() => {
  vi.mocked(createRemoteApiClient).mockReset()
  vi.mocked(createRemoteConsumersClient).mockReset().mockImplementation(() => consumers)
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected direct network')))
  vi.stubGlobal('WebSocket', vi.fn())
})
afterEach(() => {
  for (const view of mounted.splice(0)) unmount(view)
  for (const pinia of piniae.splice(0)) disposePinia(pinia)
  document.body.replaceChildren()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('personal key action integration', () => {
  async function openKey(context: Harness, host: HTMLElement) {
    vi.mocked(createRemoteApiClient).mockReturnValueOnce(context.api)
    button(row(host), 'remote-credential').click()
    await flush()
  }
  function enterKey() {
    const field = dialog()!.querySelector<HTMLInputElement>('input[type="password"]')!
    expect(field.value).toBe('')
    field.value = secret
    field.dispatchEvent(new Event('input', { bubbles: true }))
    return field
  }
  function saveKey() {
    dialog()!.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  }
  function closeKey() {
    dialog()!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  }
  function noRemote(context: Harness) {
    for (const method of ['verify', 'createProfile', 'renameProfile', 'changeProfileUrl', 'deleteProfile',
      'recoverProfile', 'listProjects', 'createProject', 'observe'] as const) {
      expect(context.api[method], method).not.toHaveBeenCalled()
    }
    expect(fetch).not.toHaveBeenCalled()
    expect(WebSocket).not.toHaveBeenCalled()
    expect(JSON.stringify(context.store.$state)).not.toContain(secret)
    expect(document.body.textContent).not.toContain(secret)
  }

  it('opens a separate empty password dialog, gates all other intents and cancels without a request', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    await openKey(context, view.host)
    expect(dialog()!.querySelector('input[type="password"]')).not.toBeNull()
    expect(dialog()!.querySelector('[data-testid="remote-metadata-name"]')).toBeNull()
    expect(button(view.host, 'remote-add').disabled).toBe(true)
    expect(refreshButton(view.host).disabled).toBe(true)
    expect(button(row(view.host), 'remote-rename').disabled).toBe(true)
    expect(verifyButton(view.host).disabled).toBe(true)
    forceClick(button(view.host, 'remote-add'))
    forceClick(button(row(view.host), 'remote-rename'))
    forceClick(verifyButton(view.host))
    enterKey()
    closeKey()
    await flush()
    expect(dialog()).toBeNull()
    expect(button(row(view.host), 'remote-credential').disabled).toBe(false)
    expect(context.api.setCredential).not.toHaveBeenCalled()
    expect(context.api.clearCredential).not.toHaveBeenCalled()
    await openKey(context, view.host)
    expect(dialog()!.querySelector<HTMLInputElement>('input[type="password"]')!.value).toBe('')
    noRemote(context)
  })

  it('applies only the confirmed PUT DTO and preserves subsequent explicit HTTP verification consent', async () => {
    const url = 'http://outside.example/team'
    const context = harness([profile({ url })])
    const view = mountView(context)
    await flush()
    await openKey(context, view.host)
    const field = enterKey()
    const pending = deferred<RemoteServerProfile>()
    context.api.setCredential.mockReturnValueOnce(pending.promise)
    saveKey(); saveKey()
    await flush()
    expect(field.value).toBe('')
    expect(context.api.setCredential).toHaveBeenCalledExactlyOnceWith(id, { expected_revision: 7, token: secret })
    expect(context.store.items).toEqual([profile({ url })])
    pending.resolve(profile({ url, revision: 8, credential_source: 'environment' }))
    await flush()
    expect(dialog()).toBeNull()
    expect(row(view.host).textContent).toContain(t('settings.remoteServers.credential.environment'))
    expect(context.store.items[0].revision).toBe(8)
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    noRemote(context)
    verifyButton(view.host).click()
    await flush()
    expect(dialog()!.textContent).toContain(t('settings.remoteServers.insecureWarning'))
    expect(context.api.verify).not.toHaveBeenCalled()
    button(document.body, 'remote-http-confirm').click()
    await flush()
    expect(context.api.verify).toHaveBeenCalledExactlyOnceWith(id, 8)
  })

  it('forgets only after confirmation and renders the returned effective ENV source, not a promised missing key', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    await openKey(context, view.host)
    enterKey()
    button(document.body, 'remote-credential-forget').click()
    await flush()
    expect(context.api.clearCredential).not.toHaveBeenCalled()
    expect(dialog()!.textContent).toContain(t('settings.remoteServers.credentials.forgetWarning'))
    saveKey() // Enter in a form cannot stand in for destructive confirmation.
    button(document.body, 'remote-credential-forget-cancel').click()
    await flush()
    expect(context.api.clearCredential).not.toHaveBeenCalled()
    button(document.body, 'remote-credential-forget').click()
    await flush()
    context.api.clearCredential.mockResolvedValueOnce(profile({ revision: 8, credential_source: 'environment' }))
    button(document.body, 'remote-credential-forget-confirm').click()
    await flush()
    expect(context.api.clearCredential).toHaveBeenCalledExactlyOnceWith(id, { expected_revision: 7 })
    expect(context.api.setCredential).not.toHaveBeenCalled()
    expect(dialog()).toBeNull()
    expect(row(view.host).textContent).toContain(t('settings.remoteServers.credential.environment'))
    expect(row(view.host).textContent).not.toContain(t('settings.remoteServers.credential.missing'))
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    noRemote(context)
  })

  it.each(['cancel', 'unmount'] as const)('shares the metadata busy/refresh fence after key %s and remount', async action => {
    const context = harness()
    const dispose = vi.spyOn(context.store, 'dispose')
    const first = mountView(context)
    await flush()
    await openKey(context, first.host)
    enterKey()
    const pending = deferred<RemoteServerProfile>()
    context.api.setCredential.mockReturnValueOnce(pending.promise)
    saveKey()
    if (action === 'cancel') closeKey()
    unmount(first)
    const second = mountView(context)
    await flush()
    expect(dialog()).toBeNull()
    for (const key of ['remote-add', 'remote-refresh', 'remote-credential', 'remote-rename', 'remote-verify']) {
      expect(button(second.host, key).disabled, key).toBe(true)
      forceClick(button(second.host, key))
    }
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    pending.resolve(profile({ revision: 8, credential_source: 'environment' }))
    await flush()
    expect(context.store.items).toEqual([profile()])
    expect(refreshButton(second.host).disabled).toBe(false)
    expect(button(second.host, 'remote-add').disabled).toBe(true)
    expect(button(second.host, 'remote-credential').disabled).toBe(true)
    context.api.listServers.mockRejectedValueOnce(new ApiError(503, secret))
    refreshButton(second.host).click()
    await flush()
    expect(button(second.host, 'remote-credential').disabled).toBe(true)
    context.api.listServers.mockResolvedValueOnce({ items: [profile({ revision: 8 })] })
    refreshButton(second.host).click()
    await flush()
    expect(button(second.host, 'remote-add').disabled).toBe(false)
    expect(button(second.host, 'remote-rename').disabled).toBe(false)
    expect(button(second.host, 'remote-credential').disabled).toBe(false)
    expect(dispose).not.toHaveBeenCalled()
    noRemote(context)
  })

  it('requires explicit successful Refresh after key conflict, blocking metadata edits too across remount', async () => {
    const context = harness()
    const first = mountView(context)
    await flush()
    await openKey(context, first.host)
    enterKey()
    context.api.setCredential.mockRejectedValueOnce(new RemoteApiError(409, 'profile_consumers_not_drained', { raw: secret }))
    saveKey()
    await flush()
    closeKey()
    unmount(first)
    const second = mountView(context)
    await flush()
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    for (const key of ['remote-add', 'remote-credential', 'remote-rename', 'remote-verify']) {
      expect(button(second.host, key).disabled).toBe(true)
    }
    context.api.listServers.mockResolvedValueOnce({ items: [profile({ revision: 8 })] })
    refreshButton(second.host).click()
    await flush()
    await openKey(context, second.host)
    enterKey()
    context.api.setCredential.mockResolvedValueOnce(profile({ revision: 9 }))
    saveKey()
    await flush()
    expect(context.api.setCredential).toHaveBeenLastCalledWith(id, { expected_revision: 8, token: secret })
    expect(context.api.setCredential).toHaveBeenCalledTimes(2)
    expect(dialog()).toBeNull()
    noRemote(context)
  })

  it('does not let a key action bypass an existing metadata conflict latch', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    vi.mocked(createRemoteApiClient).mockReturnValueOnce(context.api)
    button(row(view.host), 'remote-rename').click()
    await flush()
    const name = dialog()!.querySelector<HTMLInputElement>('input')!
    name.value = 'Conflict'
    name.dispatchEvent(new Event('input', { bubbles: true }))
    context.api.renameProfile.mockRejectedValueOnce(new RemoteApiError(409, 'profile_revision_conflict'))
    dialog()!.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    closeKey()
    await flush()
    expect(button(row(view.host), 'remote-credential').disabled).toBe(true)
    forceClick(button(row(view.host), 'remote-credential'))
    await flush()
    expect(dialog()).toBeNull()
    expect(context.api.setCredential).not.toHaveBeenCalled()
    expect(context.api.clearCredential).not.toHaveBeenCalled()
  })
})

describe('metadata-only mounting and display', () => {
  it('mounts with one metadata GET, no Verify, timers, automatic retries or unrelated operations', async () => {
    vi.useFakeTimers()
    const context = harness()
    const pending = deferred<RemoteServers>()
    context.api.listServers.mockReturnValueOnce(pending.promise)
    const view = mountView(context)
    await flush()
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    expect(view.host.textContent).toContain(t('settings.remoteServers.loading'))
    expect(refreshButton(view.host).disabled).toBe(true)
    expect(view.host.textContent).not.toContain(t('settings.remoteServers.empty'))
    expect(vi.getTimerCount()).toBe(0)
    pending.resolve({ items: [profile()] })
    await flush()
    expect(row(view.host).textContent).toContain('Team Remote')
    expect(row(view.host).textContent).toContain('https://remote.example/team')
    await vi.advanceTimersByTimeAsync(300_000)
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    expect(context.api.verify).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
    assertNoOtherCalls(context.api)
    expect(view.host.querySelector('input, select, textarea')).toBeNull()
  })

  it('renders a genuine empty success separately from initial load failure and explicit Refresh recovery', async () => {
    vi.useFakeTimers()
    const context = harness([])
    context.api.listServers.mockRejectedValueOnce(new ApiError(503, secret))
    const view = mountView(context)
    await flush()
    expect(view.host.querySelector('[role="alert"]')?.textContent).toContain(t('settings.remoteServers.loadError'))
    expect(view.host.textContent).not.toContain(t('settings.remoteServers.empty'))
    expect(document.body.innerHTML).not.toContain(secret)
    expect(refreshButton(view.host).disabled).toBe(false)
    await vi.advanceTimersByTimeAsync(300_000)
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    refreshButton(view.host).click()
    await flush()
    expect(context.api.listServers).toHaveBeenCalledTimes(2)
    expect(view.host.textContent).toContain(t('settings.remoteServers.empty'))
    expect(view.host.querySelector('[role="alert"]')).toBeNull()
    expect(context.api.verify).not.toHaveBeenCalled()
    assertNoOtherCalls(context.api)
  })

  it.each(['stored', 'environment', 'none'] as const)('shows credential source %s without inventing a checked status', async source => {
    const context = harness([profile({ credential_source: source, has_token: source !== 'none' })])
    const view = mountView(context)
    await flush()
    const key = source === 'none' ? 'missing' : source
    expect(row(view.host).textContent).toContain(t(`settings.remoteServers.credential.${key}`))
    expect(row(view.host).textContent).toContain(t('settings.remoteServers.status.unavailable'))
    expect(row(view.host).textContent).not.toContain(t('settings.remoteServers.status.unchecked'))
    expect(row(view.host).textContent).not.toContain(t('settings.remoteServers.status.valid'))
    expect(row(view.host).querySelector('time')).toBeNull()
    expect(row(view.host).textContent).toContain(t('settings.remoteServers.noChecked'))
    expect(context.api.verify).not.toHaveBeenCalled()
  })

  it.each(['unchecked', 'checking'] as const)('preserves metadata-only %s without manufacturing time or user', async state => {
    const view = mountView(harness([profile({ verification: { state } })]))
    await flush()
    expect(row(view.host).textContent).toContain(t(`settings.remoteServers.status.${state}`))
    expect(row(view.host).querySelector('time')).toBeNull()
    expect(row(view.host).textContent).not.toContain('Alice Remote')
  })

  it.each(['valid', 'unauthorised', 'offline', 'incompatible', 'insecure_blocked', 'error'] as const)(
    'shows last checked %s, exact timestamp and user, not an eternal connection claim', async state => {
      const context = harness([profile({ verification: checked({ state }) })])
      const view = mountView(context)
      await flush()
      expect(row(view.host).textContent).toContain(t(`settings.remoteServers.status.${state}`))
      expect(row(view.host).textContent).toContain(t('settings.remoteServers.lastResult'))
      expect(row(view.host).querySelector('time')?.dateTime).toBe(checkedAt)
      expect(row(view.host).querySelector('time')?.textContent?.trim()).toBeTruthy()
      expect(row(view.host).textContent).toContain('Alice Remote')
      expect(context.api.verify).not.toHaveBeenCalled()
    },
  )

  it('shows unknown user for a checked result without a user', async () => {
    const view = mountView(harness([profile({ verification: checked({ user: null }) })]))
    await flush()
    expect(row(view.host).textContent).toContain(t('settings.remoteServers.unknownUser'))
    expect(row(view.host).querySelector('time')?.dateTime).toBe(checkedAt)
  })

  it('keeps previous checked history separate from unavailable metadata', async () => {
    const context = harness([profile({ verification: checked() })])
    const view = mountView(context)
    await flush()
    context.api.listServers.mockResolvedValueOnce({ items: [profile()] })
    refreshButton(view.host).click()
    await flush()
    expect(row(view.host).textContent).toContain(t('settings.remoteServers.status.unavailable'))
    expect(row(view.host).textContent).toContain(t('settings.remoteServers.lastResult'))
    expect(row(view.host).textContent).toContain('Alice Remote')
    expect(row(view.host).querySelector('time')?.dateTime).toBe(checkedAt)
  })
})

describe('explicit Verify and busy/error states', () => {
  it.each(['valid', 'unauthorised', 'offline', 'incompatible', 'insecure_blocked', 'error'] as const)(
    'applies explicit Verify result %s using only saved profile ID and revision', async state => {
      const context = harness()
      context.api.verify.mockResolvedValueOnce(checked({ state }))
      const view = mountView(context)
      await flush()
      verifyButton(view.host).click()
      await flush()
      expect(context.api.verify).toHaveBeenCalledExactlyOnceWith(id, 7)
      expect(row(view.host).textContent).toContain(t(`settings.remoteServers.status.${state}`))
      expect(row(view.host).querySelector('time')?.dateTime).toBe(checkedAt)
      expect(context.api.listServers).toHaveBeenCalledTimes(1)
      expect(dialog()).toBeNull()
      assertNoOtherCalls(context.api)
    },
  )

  it('reports generic Verify failure, retains last result, and never retries or renders raw canary', async () => {
    vi.useFakeTimers()
    const context = harness([profile({ verification: checked() })])
    context.api.verify.mockRejectedValueOnce(new ApiError(401, secret))
    const view = mountView(context)
    await flush()
    verifyButton(view.host).click()
    await flush()
    expect(row(view.host).querySelector('[role="alert"]')?.textContent).toContain(t('settings.remoteServers.verifyError'))
    expect(row(view.host).querySelector('time')?.dateTime).toBe(checkedAt)
    expect(row(view.host).textContent).toContain('Alice Remote')
    expect(document.body.innerHTML).not.toContain(secret)
    expect(JSON.stringify(context.store.$state)).not.toContain(secret)
    expect(verifyButton(view.host).disabled).toBe(false)
    expect(refreshButton(view.host).disabled).toBe(false)
    await vi.advanceTimersByTimeAsync(300_000)
    expect(context.api.verify).toHaveBeenCalledTimes(1)
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
  })

  it('guards same-turn double click and refresh while busy, but leaves another profile independently actionable', async () => {
    const context = harness([profile(), profile({ profile_id: otherId, name: 'Other' })])
    const pending = deferred<VerificationDTO>()
    context.api.verify.mockReturnValueOnce(pending.promise)
    const view = mountView(context)
    await flush()
    const verify = verifyButton(view.host)
    forceClick(verify)
    forceClick(verify)
    forceClick(refreshButton(view.host))
    expect(context.api.verify).toHaveBeenCalledExactlyOnceWith(id, 7)
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    await flush()
    expect(verifyButton(view.host).disabled).toBe(true)
    expect(refreshButton(view.host).disabled).toBe(true)
    expect(verifyButton(view.host, otherId).disabled).toBe(false)
    expect(row(view.host).getAttribute('aria-busy')).toBe('true')
    expect(row(view.host).textContent).toContain(t('settings.remoteServers.verifying'))
    pending.resolve(checked())
    await flush()
    expect(verifyButton(view.host).disabled).toBe(false)
    expect(refreshButton(view.host).disabled).toBe(false)
    expect(row(view.host).getAttribute('aria-busy')).toBe('false')
  })

  it('disables Verify during metadata loading and failed Refresh without hiding existing rows as empty', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    const pending = deferred<RemoteServers>()
    context.api.listServers.mockReturnValueOnce(pending.promise)
    refreshButton(view.host).click()
    forceClick(verifyButton(view.host))
    await flush()
    expect(verifyButton(view.host).disabled).toBe(true)
    expect(refreshButton(view.host).disabled).toBe(true)
    expect(view.host.textContent).toContain(t('settings.remoteServers.loading'))
    pending.reject(new Error(secret))
    await flush()
    expect(row(view.host).textContent).toContain('Team Remote')
    expect(view.host.textContent).toContain(t('settings.remoteServers.loadError'))
    expect(view.host.textContent).not.toContain(t('settings.remoteServers.empty'))
    expect(verifyButton(view.host).disabled).toBe(true)
    forceClick(verifyButton(view.host))
    expect(context.api.verify).not.toHaveBeenCalled()
    expect(refreshButton(view.host).disabled).toBe(false)
    expect(document.body.innerHTML).not.toContain(secret)
    refreshButton(view.host).click()
    await flush()
    expect(verifyButton(view.host).disabled).toBe(false)
  })

  it('reflects another owner\'s mutation busy state without issuing a request', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    const ticket = context.store.beginMutation(id)!
    forceClick(verifyButton(view.host))
    forceClick(refreshButton(view.host))
    await flush()
    expect(verifyButton(view.host).disabled).toBe(true)
    expect(refreshButton(view.host).disabled).toBe(true)
    expect(row(view.host).textContent).toContain(t('settings.remoteServers.busy'))
    expect(context.api.verify).not.toHaveBeenCalled()
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    expect(context.store.endMutation(ticket, false)).toBe(true)
    await flush()
    expect(verifyButton(view.host).disabled).toBe(false)
  })
})

describe('HTTP consent security boundary', () => {
  it('teleports a real dialog with exact pending URL; cancel sends nothing and confirm sends once', async () => {
    const url = 'http://remote.example:8787/exact/Deployment'
    const context = harness([profile({ url })])
    const view = mountView(context)
    await flush()
    verifyButton(view.host).click()
    await flush()
    const confirmation = dialog()!
    expect(confirmation).not.toBeNull()
    expect(view.host.contains(confirmation)).toBe(false)
    expect(confirmation.getAttribute('aria-modal')).toBe('true')
    expect(confirmation.textContent).toContain(t('settings.remoteServers.insecureWarning'))
    expect(confirmation.querySelector('bdi')?.textContent).toBe(url)
    expect(context.api.verify).not.toHaveBeenCalled()
    expect(refreshButton(view.host).disabled).toBe(true)
    expect(verifyButton(view.host).disabled).toBe(true)
    forceClick(refreshButton(view.host))
    forceClick(verifyButton(view.host))
    button(document.body, 'remote-http-cancel').click()
    await flush()
    expect(dialog()).toBeNull()
    expect(context.api.verify).not.toHaveBeenCalled()
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    verifyButton(view.host).click()
    await flush()
    const confirm = button(document.body, 'remote-http-confirm')
    forceClick(confirm)
    forceClick(confirm)
    await flush()
    expect(context.api.verify).toHaveBeenCalledExactlyOnceWith(id, 7)
    expect(dialog()).toBeNull()
    assertNoOtherCalls(context.api)
  })

  it.each(['http://localhost:8787/team', 'http://127.0.0.1:8787/team', 'http://[::1]:8787/team',
    'https://outside.example/team'])('does not request HTTP consent for allowed parsed URL %s', async url => {
    const context = harness([profile({ url })])
    const view = mountView(context)
    await flush()
    verifyButton(view.host).click()
    await flush()
    expect(dialog()).toBeNull()
    expect(context.api.verify).toHaveBeenCalledExactlyOnceWith(id, 7)
  })

  it.each(['http://localhost.evil.example/team', 'http://evil.localhost/team',
    'http://127.0.0.1.evil.example/team', 'http://127.0.0.2/team', 'http://[::ffff:127.0.0.1]/team',
    'http://localhost./team', 'http://192.168.1.2/team',
    // Python urlsplit (backend is_insecure) does NOT rewrite these to its literal
    // loopback allowlist. WHATWG URL does: do not widen consent exceptions in UI.
    'http://127.1/team', 'http://2130706433/team', 'http://0x7f000001/team',
    'http://0177.0.0.1/team', 'http://%6cocalhost/team',
    'http://[0:0:0:0:0:0:0:1]/team'])('never trusts suffixes or non-allowlisted host %s', async url => {
    const context = harness([profile({ url })])
    const view = mountView(context)
    await flush()
    verifyButton(view.host).click()
    await flush()
    expect(dialog()?.querySelector('bdi')?.textContent).toBe(url)
    expect(context.api.verify).not.toHaveBeenCalled()
    button(document.body, 'remote-http-cancel').click()
    await flush()
    expect(context.api.verify).not.toHaveBeenCalled()
  })

  it('Escape closes the actual ModalDialog without dispatching Verify', async () => {
    const context = harness([profile({ url: 'http://outside.example/team' })])
    const view = mountView(context)
    await flush()
    verifyButton(view.host).click()
    await flush()
    dialog()!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
    expect(dialog()).toBeNull()
    expect(context.api.verify).not.toHaveBeenCalled()
  })

  it.each(['mutation', 'load', 'removal'] as const)('invalidates pending consent when an external %s changes binding', async change => {
    const url = 'http://outside.example/old'
    const next = profile({ url: 'http://new.example/new', revision: 8 })
    const context = harness([profile({ url })])
    const view = mountView(context)
    await flush()
    verifyButton(view.host).click()
    await flush()
    const staleConfirm = button(document.body, 'remote-http-confirm')
    if (change === 'mutation') mutate(context, next)
    else if (change === 'removal') {
      expect(context.store.applyRemoval(context.store.beginMutation(id)!)).toBe(true)
    } else {
      context.api.listServers.mockResolvedValueOnce({ items: [next] })
      expect(await context.store.load()).toBe(true)
    }
    // A click arriving before the render/watch flush must also fail closed.
    forceClick(staleConfirm)
    await flush()
    expect(context.api.verify).not.toHaveBeenCalled()
    expect(dialog()).toBeNull()
    if (change !== 'removal') {
      verifyButton(view.host).click()
      await flush()
      expect(dialog()?.querySelector('bdi')?.textContent).toBe(next.url)
      button(document.body, 'remote-http-confirm').click()
      await flush()
      expect(context.api.verify).toHaveBeenCalledExactlyOnceWith(id, 8)
    }
  })
})

describe('late responses, shared-store lifetime and renderer isolation', () => {
  it.each(['resolve', 'reject'] as const)('fences a late load %s after a confirmed external mutation', async outcome => {
    const context = harness()
    const view = mountView(context)
    await flush()
    const pending = deferred<RemoteServers>()
    context.api.listServers.mockReturnValueOnce(pending.promise)
    refreshButton(view.host).click()
    mutate(context, profile({ name: 'New binding', revision: 8, url: 'https://new.example' }))
    if (outcome === 'resolve') pending.resolve({ items: [profile({ name: 'Stale row', verification: checked() })] })
    else pending.reject(new Error(secret))
    await flush()
    expect(row(view.host).textContent).toContain('New binding')
    expect(view.host.textContent).not.toContain('Stale row')
    expect(view.host.textContent).not.toContain(t('settings.remoteServers.loadError'))
    expect(row(view.host).querySelector('time')).toBeNull()
    expect(document.body.innerHTML).not.toContain(secret)
  })

  it.each(['mutation-resolve', 'mutation-reject', 'load-resolve', 'load-reject'] as const)(
    'never displays stale Verify history/errors on replacement binding (%s)', async scenario => {
      const context = harness()
      const view = mountView(context)
      await flush()
      const pending = deferred<VerificationDTO>()
      context.api.verify.mockReturnValueOnce(pending.promise)
      verifyButton(view.host).click()
      const next = profile({ name: 'Replacement', revision: 8, url: 'https://new.example' })
      if (scenario.startsWith('mutation')) mutate(context, next)
      else {
        context.api.listServers.mockResolvedValueOnce({ items: [next] })
        expect(await context.store.load()).toBe(true)
      }
      if (scenario.endsWith('resolve')) pending.resolve(checked())
      else pending.reject(new ApiError(503, secret))
      await flush()
      expect(row(view.host).textContent).toContain('Replacement')
      expect(row(view.host).textContent).toContain(t('settings.remoteServers.status.unavailable'))
      expect(row(view.host).textContent).not.toContain('Alice Remote')
      expect(row(view.host).querySelector('time')).toBeNull()
      expect(row(view.host).querySelector('[role="alert"]')).toBeNull()
      expect(verifyButton(view.host).disabled).toBe(false)
      expect(document.body.innerHTML).not.toContain(secret)
      expect(context.api.verify).toHaveBeenCalledTimes(1)
    },
  )

  it('removing a profile while Verify is pending never resurrects its row', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    const pending = deferred<VerificationDTO>()
    context.api.verify.mockReturnValueOnce(pending.promise)
    verifyButton(view.host).click()
    expect(context.store.applyRemoval(context.store.beginMutation(id)!)).toBe(true)
    pending.resolve(checked())
    await flush()
    expect(view.host.querySelector('[data-profile-id]')).toBeNull()
    expect(view.host.textContent).toContain(t('settings.remoteServers.empty'))
    expect(context.store.lastChecked).toEqual({})
  })

  it('unmount/remount during Verify preserves a foreign store and its request without an extra GET', async () => {
    const context = harness()
    const dispose = vi.spyOn(context.store, 'dispose')
    const piniaDispose = vi.spyOn(context.store, '$dispose')
    const first = mountView(context)
    await flush()
    const pending = deferred<VerificationDTO>()
    context.api.verify.mockReturnValueOnce(pending.promise)
    verifyButton(first.host).click()
    unmount(first)
    expect(context.store.busy[id]).toBe('verify')
    const second = mountView(context)
    await flush()
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    expect(verifyButton(second.host).disabled).toBe(true)
    pending.resolve(checked())
    await flush()
    expect(row(second.host).textContent).toContain('Alice Remote')
    expect(row(second.host).querySelector('time')?.dateTime).toBe(checkedAt)
    expect(verifyButton(second.host).disabled).toBe(false)
    expect(dispose).not.toHaveBeenCalled()
    expect(piniaDispose).not.toHaveBeenCalled()
    expect(useRemoteServersStore(context.pinia)).toBe(context.store)
    unmount(second)
    const third = mountView(context)
    await flush()
    expect(context.api.listServers).toHaveBeenCalledTimes(2)
    expect(row(third.host).querySelector('time')?.dateTime).toBe(checkedAt)
    expect(context.api.verify).toHaveBeenCalledTimes(1)
  })

  it('unmount/remount while metadata GET is pending reuses the foreign request and applies its late result', async () => {
    const context = harness()
    const pending = deferred<RemoteServers>()
    context.api.listServers.mockReturnValueOnce(pending.promise)
    const dispose = vi.spyOn(context.store, 'dispose')
    const first = mountView(context)
    unmount(first)
    const second = mountView(context)
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    pending.resolve({ items: [profile()] })
    await flush()
    expect(row(second.host).textContent).toContain('Team Remote')
    expect(dispose).not.toHaveBeenCalled()
    expect(context.api.verify).not.toHaveBeenCalled()
  })

  it('drops local consent on unmount, never dispatches a detached confirm, and demands fresh consent on remount', async () => {
    const context = harness([profile({ url: 'http://outside.example/team' })])
    const first = mountView(context)
    await flush()
    verifyButton(first.host).click()
    await flush()
    const detachedConfirm = button(document.body, 'remote-http-confirm')
    unmount(first)
    forceClick(detachedConfirm)
    await flush()
    expect(dialog()).toBeNull()
    expect(context.api.verify).not.toHaveBeenCalled()
    const second = mountView(context)
    await flush()
    expect(dialog()).toBeNull()
    expect(context.api.verify).not.toHaveBeenCalled()
    verifyButton(second.host).click()
    await flush()
    expect(dialog()).not.toBeNull()
    expect(context.api.verify).not.toHaveBeenCalled()
  })

  it('isolates simultaneous A/B Pinia roots with identical profile IDs, deferred results, errors and unmounts', async () => {
    const bind = vi.spyOn(rendererSession, 'bind')
    const invalidate = vi.spyOn(rendererSession, 'invalidate')
    const a = harness([profile({ name: 'Renderer A' })])
    const b = harness([profile({ name: 'Renderer B', url: 'https://b.example', revision: 20 })])
    const viewA = mountView(a)
    const viewB = mountView(b)
    await flush()
    expect(a.store).not.toBe(b.store)
    expect(row(viewA.host).textContent).toContain('Renderer A')
    expect(row(viewB.host).textContent).toContain('Renderer B')
    const pending = deferred<VerificationDTO>()
    a.api.verify.mockReturnValueOnce(pending.promise)
    b.api.verify.mockRejectedValueOnce(new Error(secret))
    verifyButton(viewA.host).click()
    verifyButton(viewB.host).click()
    await flush()
    expect(row(viewB.host).textContent).toContain(t('settings.remoteServers.verifyError'))
    expect(row(viewA.host).querySelector('[role="alert"]')).toBeNull()
    expect(refreshButton(viewB.host).disabled).toBe(false)
    unmount(viewA)
    pending.resolve(checked())
    await flush()
    expect(b.store.lastChecked).toEqual({})
    expect(row(viewB.host).querySelector('time')).toBeNull()
    b.api.verify.mockResolvedValueOnce(checked({ user: null }))
    verifyButton(viewB.host).click()
    await flush()
    expect(b.api.verify).toHaveBeenLastCalledWith(id, 20)
    expect(a.api.verify).toHaveBeenCalledExactlyOnceWith(id, 7)
    expect(row(viewB.host).textContent).toContain(t('settings.remoteServers.unknownUser'))
    expect(row(viewB.host).textContent).not.toContain('Alice Remote')
    expect(bind).not.toHaveBeenCalled()
    expect(invalidate).not.toHaveBeenCalled()
    expect(document.body.innerHTML).not.toContain(secret)
    expect(createRemoteApiClient).toHaveBeenCalledTimes(2)
    assertNoOtherCalls(a.api)
    assertNoOtherCalls(b.api)
  })
})

describe('explicit server metadata actions', () => {
  function open(context: Harness, host: HTMLElement, action: 'remote-add' | 'remote-rename') {
    vi.mocked(createRemoteApiClient).mockReturnValueOnce(context.api)
    button(host, action).click()
  }
  function input(testId: string, value: string) {
    const field = document.body.querySelector<HTMLInputElement>(`[data-testid="${testId}"]`)
    expect(field).not.toBeNull()
    field!.value = value
    field!.dispatchEvent(new Event('input', { bubbles: true }))
  }
  function submit() {
    const form = document.body.querySelector('[data-testid="remote-metadata-form"]')!
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  }
  function noRemote(context: Harness) {
    expect(context.api.verify).not.toHaveBeenCalled()
    for (const method of ['changeProfileUrl', 'setCredential', 'clearCredential', 'deleteProfile',
      'recoverProfile', 'listProjects', 'createProject', 'observe'] as const) {
      expect(context.api[method], method).not.toHaveBeenCalled()
    }
    expect(fetch).not.toHaveBeenCalled()
    expect(WebSocket).not.toHaveBeenCalled()
  }

  it('opens explicit keyless Create, cancels without writes, then applies only confirmed metadata without refetch or Verify', async () => {
    const context = harness([])
    const view = mountView(context)
    await flush()
    open(context, view.host, 'remote-add')
    await flush()
    expect(dialog()?.textContent).toContain(t('settings.remoteServers.metadata.addTitle'))
    expect(button(view.host, 'remote-add').disabled).toBe(true)
    expect(refreshButton(view.host).disabled).toBe(true)
    expect(dialog()?.querySelector('input[type="password"]')).toBeNull()
    input('remote-metadata-name', 'New server')
    input('remote-metadata-url', 'https://new.example')
    button(document.body, 'remote-metadata-cancel').click()
    await flush()
    expect(dialog()).toBeNull()
    expect(context.api.createProfile).not.toHaveBeenCalled()
    open(context, view.host, 'remote-add')
    await flush()
    input('remote-metadata-name', 'New server')
    input('remote-metadata-url', 'https://new.example')
    const pending = deferred<RemoteServerProfile>()
    context.api.createProfile.mockReturnValueOnce(pending.promise)
    submit()
    submit()
    await flush()
    expect(context.api.createProfile).toHaveBeenCalledExactlyOnceWith({ name: 'New server', url: 'https://new.example' })
    expect(context.store.items).toEqual([])
    expect(button(document.body, 'remote-metadata-submit').disabled).toBe(true)
    pending.resolve(profile({ name: 'New server', url: 'https://new.example', has_token: false, credential_source: 'none' }))
    await flush()
    expect(dialog()).toBeNull()
    expect(row(view.host).textContent).toContain('New server')
    expect(row(view.host).textContent).toContain(t('settings.remoteServers.credential.missing'))
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    noRemote(context)
  })

  it('renames only the captured name/revision and preserves explicit HTTP consent afterward', async () => {
    const url = 'http://outside.example/team'
    const context = harness([profile({ url })])
    const view = mountView(context)
    await flush()
    open(context, view.host, 'remote-rename')
    await flush()
    expect(dialog()?.textContent).toContain(t('settings.remoteServers.metadata.renameTitle'))
    expect(dialog()?.querySelector('[data-testid="remote-metadata-url"]')).toBeNull()
    expect(verifyButton(view.host).disabled).toBe(true)
    input('remote-metadata-name', 'Renamed')
    context.api.renameProfile.mockResolvedValueOnce(profile({ name: 'Renamed', revision: 8, url }))
    submit()
    submit()
    await flush()
    expect(context.api.renameProfile).toHaveBeenCalledExactlyOnceWith(id, { name: 'Renamed', expected_revision: 7 })
    expect(dialog()).toBeNull()
    expect(row(view.host).textContent).toContain('Renamed')
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    noRemote(context)
    verifyButton(view.host).click()
    await flush()
    expect(dialog()?.querySelector('bdi')?.textContent).toBe(url)
    expect(context.api.verify).not.toHaveBeenCalled()
    button(document.body, 'remote-http-confirm').click()
    await flush()
    expect(context.api.verify).toHaveBeenCalledExactlyOnceWith(id, 8)
  })

  it('blocks actions when loading, unavailable or another modal/operation owns the row', async () => {
    const context = harness([profile({ url: 'http://outside.example' })])
    const pending = deferred<RemoteServers>()
    context.api.listServers.mockReturnValueOnce(pending.promise)
    const view = mountView(context)
    expect(button(view.host, 'remote-add').disabled).toBe(true)
    pending.resolve({ items: [profile({ url: 'http://outside.example' })] })
    await flush()
    verifyButton(view.host).click()
    await flush()
    expect(button(view.host, 'remote-add').disabled).toBe(true)
    expect(button(row(view.host), 'remote-rename').disabled).toBe(true)
    forceClick(button(view.host, 'remote-add'))
    forceClick(button(row(view.host), 'remote-rename'))
    expect(context.api.createProfile).not.toHaveBeenCalled()
    expect(context.api.renameProfile).not.toHaveBeenCalled()
    button(document.body, 'remote-http-cancel').click()
    await flush()
    const ticket = context.store.beginMutation(id)!
    await flush()
    expect(button(view.host, 'remote-add').disabled).toBe(true)
    expect(button(row(view.host), 'remote-rename').disabled).toBe(true)
    context.store.endMutation(ticket, false)
    await flush()
    context.api.listServers.mockRejectedValueOnce(new ApiError(503, secret))
    refreshButton(view.host).click()
    await flush()
    expect(button(view.host, 'remote-add').disabled).toBe(true)
    expect(button(row(view.host), 'remote-rename').disabled).toBe(true)
    noRemote(context)
  })

  it('drops editor state and detached actions on navigation without disposing the shared store', async () => {
    const context = harness()
    const dispose = vi.spyOn(context.store, 'dispose')
    const first = mountView(context)
    await flush()
    open(context, first.host, 'remote-rename')
    await flush()
    const detached = button(document.body, 'remote-metadata-submit')
    unmount(first)
    forceClick(detached)
    const second = mountView(context)
    await flush()
    expect(dialog()).toBeNull()
    expect(context.api.renameProfile).not.toHaveBeenCalled()
    expect(dispose).not.toHaveBeenCalled()
    expect(row(second.host).textContent).toContain('Team Remote')
    noRemote(context)
  })

  it.each([
    [409, 'profile_url_collision', 'collision'],
    [422, 'invalid_server_url', 'invalid'],
    [503, 'application_store_unavailable', 'unavailable'],
  ] as const)('keeps Create failure %s visible without a row, success, implicit refresh or Verify', async (status, code, key) => {
    const context = harness([])
    const view = mountView(context)
    await flush()
    open(context, view.host, 'remote-add')
    await flush()
    input('remote-metadata-name', 'New server')
    input('remote-metadata-url', 'https://new.example')
    context.api.createProfile.mockRejectedValueOnce(new RemoteApiError(status, code, { message: secret }))
    submit()
    await flush()
    expect(dialog()?.querySelector('[role="alert"]')?.textContent).toBe(t(`settings.remoteServers.metadata.${key}`))
    expect(context.store.items).toEqual([])
    expect(context.api.createProfile).toHaveBeenCalledTimes(1)
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    expect(document.body.innerHTML).not.toContain(secret)
    button(document.body, 'remote-metadata-cancel').click()
    await flush()
    expect(dialog()).toBeNull()
    expect(view.host.querySelector('[data-profile-id]')).toBeNull()
    noRemote(context)
  })

  it.each(['cancel', 'unmount'] as const)('fences keyless Create across %s and remount until settlement plus explicit Refresh', async action => {
    const context = harness([])
    const first = mountView(context)
    await flush()
    open(context, first.host, 'remote-add')
    await flush()
    input('remote-metadata-name', 'Pending server')
    input('remote-metadata-url', 'https://pending.example')
    const pending = deferred<RemoteServerProfile>()
    context.api.createProfile.mockReturnValueOnce(pending.promise)
    submit()
    if (action === 'cancel') {
      button(document.body, 'remote-metadata-cancel').click()
      await flush()
      expect(button(first.host, 'remote-add').disabled).toBe(true)
      forceClick(button(first.host, 'remote-add'))
      expect(dialog()).toBeNull()
    }
    unmount(first)
    const second = mountView(context)
    await flush()
    expect(button(second.host, 'remote-add').disabled).toBe(true)
    expect(refreshButton(second.host).disabled).toBe(true)
    forceClick(button(second.host, 'remote-add'))
    expect(dialog()).toBeNull()
    expect(context.api.createProfile).toHaveBeenCalledTimes(1)
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    const committed = profile({ name: 'Pending server', url: 'https://pending.example', has_token: false, credential_source: 'none' })
    pending.resolve(committed)
    await flush()
    expect(context.store.items).toEqual([])
    expect(button(second.host, 'remote-add').disabled).toBe(true)
    expect(refreshButton(second.host).disabled).toBe(false)
    context.api.listServers.mockRejectedValueOnce(new ApiError(503, secret))
    refreshButton(second.host).click()
    await flush()
    expect(button(second.host, 'remote-add').disabled).toBe(true)
    context.api.listServers.mockResolvedValueOnce({ items: [committed] })
    refreshButton(second.host).click()
    await flush()
    expect(button(second.host, 'remote-add').disabled).toBe(false)
    expect(row(second.host).textContent).toContain('Pending server')
    expect(context.api.createProfile).toHaveBeenCalledTimes(1)
    noRemote(context)
  })

  it('requires an explicit successful Refresh after a conflict, even across navigation and remount', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    open(context, view.host, 'remote-rename')
    await flush()
    input('remote-metadata-name', 'Conflicting rename')
    context.api.renameProfile.mockRejectedValueOnce(new RemoteApiError(409, 'profile_revision_conflict', { raw: secret }))
    submit()
    await flush()
    expect(dialog()?.textContent).toContain(t('settings.remoteServers.metadata.conflict'))
    submit()
    expect(context.api.renameProfile).toHaveBeenCalledTimes(1)
    button(document.body, 'remote-metadata-cancel').click()
    await flush()
    expect(button(row(view.host), 'remote-rename').disabled).toBe(true)
    expect(button(view.host, 'remote-add').disabled).toBe(true)
    forceClick(button(row(view.host), 'remote-rename'))
    expect(dialog()).toBeNull()
    unmount(view)
    const remounted = mountView(context)
    await flush()
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    expect(button(row(remounted.host), 'remote-rename').disabled).toBe(true)
    context.api.listServers.mockRejectedValueOnce(new ApiError(503, secret))
    refreshButton(remounted.host).click()
    await flush()
    expect(button(row(remounted.host), 'remote-rename').disabled).toBe(true)
    context.api.listServers.mockResolvedValueOnce({ items: [profile({ revision: 8 })] })
    refreshButton(remounted.host).click()
    await flush()
    expect(button(row(remounted.host), 'remote-rename').disabled).toBe(false)
    open(context, remounted.host, 'remote-rename')
    await flush()
    input('remote-metadata-name', 'Fresh rename')
    context.api.renameProfile.mockResolvedValueOnce(profile({ name: 'Fresh rename', revision: 9 }))
    submit()
    await flush()
    expect(context.api.renameProfile).toHaveBeenLastCalledWith(id, { name: 'Fresh rename', expected_revision: 8 })
    expect(context.api.renameProfile).toHaveBeenCalledTimes(2)
    expect(dialog()).toBeNull()
    expect(document.body.innerHTML).not.toContain(secret)
    noRemote(context)
  })
})


describe('global scope release gate explanation', () => {
  it('keeps key/URL safety prerequisites explicit without treating Verify as project Enable', async () => {
    const h = harness()
    const view = mountView(h)
    await flush()
    const gate = view.host.querySelector('[data-testid="remote-mutation-gate"]')
    expect(gate?.textContent).toBe(t('settings.remoteServers.mutationGate'))
    expect(gate?.textContent).not.toContain('settings.remoteServers.')
    assertNoOtherCalls(h.api)
  })
})

describe('Gate L consumer confirmation in the key dialog (dev spec §16)', () => {
  it('shows the census, blocks Save until the user confirms and never confirms by itself', async () => {
    const context = harness()
    const unknown: RemoteProfileConsumers = { profile_id: id, url: profile().url, revision: 7, state: 'unknown',
      confirmed_at: null, bound_roots: ['D:\games\alpha'], active_roots: [], operations: ['verify'] }
    consumers.profileConsumers.mockResolvedValueOnce(unknown)
    const view = mountView(context)
    await flush()
    vi.mocked(createRemoteApiClient).mockReturnValueOnce(context.api)
    button(row(view.host), 'remote-credential').click()
    await flush(); await flush()
    const panel = dialog()!.querySelector('[data-testid="remote-consumers"]')!
    expect(panel.textContent).toContain('D:\games\alpha')
    expect(panel.textContent).toContain(t('settings.remoteServers.consumers.operations', {
      operations: t('settings.remoteServers.consumers.operation.verify') }))
    const field = dialog()!.querySelector<HTMLInputElement>('input[type="password"]')!
    field.value = secret
    field.dispatchEvent(new Event('input', { bubbles: true }))
    await flush()
    expect(button(document.body, 'remote-credential-submit').disabled).toBe(true)
    dialog()!.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    expect(context.api.setCredential).not.toHaveBeenCalled()
    expect(consumers.confirmProfileConsumers).not.toHaveBeenCalled()
    button(document.body, 'remote-consumers-acknowledge').click()
    await flush()
    button(document.body, 'remote-consumers-confirm').click()
    await flush(); await flush()
    expect(consumers.confirmProfileConsumers).toHaveBeenCalledExactlyOnceWith(id,
      { expected_revision: 7, url: profile().url, bound_roots: ['D:\games\alpha'] })
    expect(button(document.body, 'remote-credential-submit').disabled).toBe(false)
    context.api.setCredential.mockResolvedValueOnce(profile({ revision: 8 }))
    dialog()!.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    expect(context.api.setCredential).toHaveBeenCalledExactlyOnceWith(id, { expected_revision: 7, token: secret })
    expect(dialog()).toBeNull()
  })

  it('localizes a not-drained refusal instead of showing its code or details', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    vi.mocked(createRemoteApiClient).mockReturnValueOnce(context.api)
    button(row(view.host), 'remote-credential').click()
    await flush(); await flush()
    const field = dialog()!.querySelector<HTMLInputElement>('input[type="password"]')!
    field.value = secret
    field.dispatchEvent(new Event('input', { bubbles: true }))
    context.api.setCredential.mockRejectedValueOnce(new RemoteApiError(409, 'profile_consumers_not_drained', { root: secret }))
    dialog()!.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    expect(dialog()!.querySelector('[role="alert"]')?.textContent).toBe(t('settings.remoteServers.credentials.notDrained'))
    expect(document.body.textContent).not.toContain(secret)
    expect(document.body.textContent).not.toContain('profile_consumers_not_drained')
  })
})

describe('C7 profile deletion and URL change (dev spec §16.11)', () => {
  const newUrl = 'https://moved.example/team'
  async function openAction(context: Harness, host: HTMLElement, action: 'remote-delete' | 'remote-change-url') {
    vi.mocked(createRemoteApiClient).mockReturnValueOnce(context.api)
    button(row(host), action).click()
    await flush(); await flush()
  }
  function typeUrl(value: string) {
    const field = document.body.querySelector<HTMLInputElement>('[data-testid="remote-metadata-new-url"]')
    expect(field).not.toBeNull()
    field!.value = value
    field!.dispatchEvent(new Event('input', { bubbles: true }))
  }
  async function reviewUrl(value = newUrl) {
    typeUrl(value)
    await flush()
    button(document.body, 'remote-url-review').click()
    await flush()
  }
  function text(testId: string): string | null | undefined {
    return document.body.querySelector(`[data-testid="${testId}"]`)?.textContent
  }
  function alertText(): string | null | undefined {
    return dialog()?.querySelector('[role="alert"]')?.textContent?.trim()
  }
  function noWrites(context: Harness) {
    for (const method of ['verify', 'createProfile', 'renameProfile', 'changeProfileUrl', 'setCredential',
      'clearCredential', 'deleteProfile', 'recoverProfile', 'listProjects', 'createProject', 'observe'] as const) {
      expect(context.api[method], method).not.toHaveBeenCalled()
    }
    expect(consumers.confirmProfileConsumers).not.toHaveBeenCalled()
    expect(consumers.withdrawProfileConsumers).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
  }
  const refusals = [
    ['profile_in_use', 409, 'settings.remoteServers.lifecycle.inUse'],
    ['profile_consumers_not_drained', 409, 'settings.remoteServers.lifecycle.notDrained'],
    ['consumer_lifecycle_unavailable', 503, 'settings.remoteServers.lifecycle.notConfirmed'],
  ] as const

  it('deletes only after review of the exact name/URL and explicit confirmation, once, without reload', async () => {
    const other = profile({ profile_id: otherId, name: 'Other', url: 'https://other.example' })
    const context = harness([profile(), other])
    const view = mountView(context)
    await flush()
    await openAction(context, view.host, 'remote-delete')
    expect(text('remote-delete-name')).toBe('Team Remote')
    expect(text('remote-delete-url')).toBe(profile().url)
    expect(document.body.querySelector('[data-testid="remote-delete-confirm"]')).toBeNull()
    expect(verifyButton(view.host, otherId).disabled).toBe(true)
    button(document.body, 'remote-delete-review').click()
    await flush()
    expect(context.api.deleteProfile).not.toHaveBeenCalled()
    expect(text('remote-delete-confirmation')).toContain(t('settings.remoteServers.delete.confirmWarning',
      { name: 'Team Remote', url: profile().url }))
    const pending = deferred<void>()
    context.api.deleteProfile.mockReturnValueOnce(pending.promise)
    const confirm = button(document.body, 'remote-delete-confirm')
    confirm.click()
    forceClick(confirm)
    await flush()
    expect(context.api.deleteProfile).toHaveBeenCalledExactlyOnceWith(id, { expected_revision: 7 })
    expect(confirm.disabled).toBe(true)
    pending.resolve()
    await flush()
    expect(dialog()).toBeNull()
    expect(view.host.querySelector(`[data-profile-id="${id}"]`)).toBeNull()
    expect(row(view.host, otherId).textContent).toContain('Other')
    expect(context.store.items.map(item => item.profile_id)).toEqual([otherId])
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    expect(verifyButton(view.host, otherId).disabled).toBe(false)
  })

  it('cancelling deletion at review, at confirmation or with Escape sends nothing', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    await openAction(context, view.host, 'remote-delete')
    button(document.body, 'remote-delete-cancel').click()
    await flush()
    expect(dialog()).toBeNull()
    await openAction(context, view.host, 'remote-delete')
    button(document.body, 'remote-delete-review').click()
    await flush()
    button(document.body, 'remote-delete-back').click()
    await flush()
    expect(document.body.querySelector('[data-testid="remote-delete-confirmation"]')).toBeNull()
    button(document.body, 'remote-delete-review').click()
    await flush()
    button(document.body, 'remote-delete-cancel').click()
    await flush()
    expect(dialog()).toBeNull()
    await openAction(context, view.host, 'remote-delete')
    button(document.body, 'remote-delete-review').click()
    await flush()
    dialog()!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
    expect(dialog()).toBeNull()
    expect(row(view.host).textContent).toContain('Team Remote')
    expect(button(row(view.host), 'remote-delete').disabled).toBe(false)
    noWrites(context)
  })

  it('keeps deletion blocked until the projects using this URL are confirmed', async () => {
    const context = harness()
    consumers.profileConsumers.mockResolvedValueOnce({ profile_id: id, url: profile().url, revision: 7,
      state: 'unknown', confirmed_at: null, bound_roots: [], active_roots: [], operations: [] })
    const view = mountView(context)
    await flush()
    await openAction(context, view.host, 'remote-delete')
    button(document.body, 'remote-delete-review').click()
    await flush()
    expect(text('remote-delete-gate')).toBe(t('settings.remoteServers.lifecycle.required'))
    const confirm = button(document.body, 'remote-delete-confirm')
    expect(confirm.disabled).toBe(true)
    forceClick(confirm)
    await flush()
    expect(context.api.deleteProfile).not.toHaveBeenCalled()
  })

  it.each(refusals)('shows a %s deletion refusal as text and never retries', async (code, status, key) => {
    const context = harness()
    const view = mountView(context)
    await flush()
    await openAction(context, view.host, 'remote-delete')
    const bound: RemoteProfileConsumers = { profile_id: id, url: profile().url, revision: 7, state: 'known',
      confirmed_at: '2026-11-18T10:00:00Z', bound_roots: ['D:\\games\\alpha'], active_roots: [], operations: [] }
    consumers.profileConsumers.mockResolvedValueOnce(bound)
    button(document.body, 'remote-delete-review').click()
    await flush()
    context.api.deleteProfile.mockRejectedValueOnce(new RemoteApiError(status, code, { root: secret }))
    button(document.body, 'remote-delete-confirm').click()
    await flush(); await flush()
    expect(alertText()).toBe(t(key))
    expect(consumers.profileConsumers).toHaveBeenCalledTimes(2)
    expect(text('remote-consumers-roots')).toContain('D:\\games\\alpha')
    expect(context.api.deleteProfile).toHaveBeenCalledTimes(1)
    expect(row(view.host).textContent).toContain('Team Remote')
    expect(context.store.busy[id]).toBeUndefined()
    expect(document.body.textContent).not.toContain(secret)
    expect(document.body.textContent).not.toContain(code)
    // No automatic retry: only another explicit Continue → Delete would send again.
    await flush()
    expect(context.api.deleteProfile).toHaveBeenCalledTimes(1)
    expect(document.body.querySelector('[data-testid="remote-delete-confirmation"]')).toBeNull()
  })

  it('a stale revision on deletion blocks the dialog and requires an explicit Refresh', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    await openAction(context, view.host, 'remote-delete')
    button(document.body, 'remote-delete-review').click()
    await flush()
    context.api.deleteProfile.mockRejectedValueOnce(new RemoteApiError(409, 'profile_revision_conflict', { raw: secret }))
    button(document.body, 'remote-delete-confirm').click()
    await flush()
    expect(alertText()).toBe(t('settings.remoteServers.lifecycle.stale'))
    expect(document.body.querySelector('[data-testid="remote-delete-confirm"]')).toBeNull()
    expect(button(document.body, 'remote-delete-review').disabled).toBe(true)
    button(document.body, 'remote-delete-cancel').click()
    await flush()
    expect(view.host.textContent).toContain(t('settings.remoteServers.metadata.stale'))
    expect(button(row(view.host), 'remote-delete').disabled).toBe(true)
    expect(context.api.deleteProfile).toHaveBeenCalledTimes(1)
    expect(document.body.textContent).not.toContain(secret)
  })

  it('never sends a deletion whose reviewed revision another owner already replaced', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    await openAction(context, view.host, 'remote-delete')
    button(document.body, 'remote-delete-review').click()
    await flush()
    mutate(context, profile({ name: 'Renamed elsewhere', revision: 8 }))
    await flush()
    forceClick(button(document.body, 'remote-delete-confirm'))
    await flush()
    expect(context.api.deleteProfile).not.toHaveBeenCalled()
    expect(alertText()).toBe(t('settings.remoteServers.lifecycle.stale'))
  })

  it('changes the URL only after confirming the exact old and new URL, clearing the saved key, once', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    await openAction(context, view.host, 'remote-change-url')
    expect(dialog()?.textContent).toContain(t('settings.remoteServers.changeUrl.title'))
    expect(text('remote-url-current')).toBe(profile().url)
    expect(text('remote-url-key-warning')).toBe(t('settings.remoteServers.changeUrl.keyWarning'))
    expect(document.body.querySelector('[data-testid="remote-metadata-name"]')).toBeNull()
    await reviewUrl(`  ${newUrl} `)
    expect(context.api.changeProfileUrl).not.toHaveBeenCalled()
    expect(text('remote-url-confirm-old')).toBe(profile().url)
    expect(text('remote-url-confirm-new')).toBe(newUrl)
    // Submitting the form (Enter) never confirms a reviewed URL change.
    document.body.querySelector('[data-testid="remote-metadata-form"]')!
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    expect(context.api.changeProfileUrl).not.toHaveBeenCalled()
    const pending = deferred<RemoteServerProfile>()
    context.api.changeProfileUrl.mockReturnValueOnce(pending.promise)
    const confirm = button(document.body, 'remote-url-confirm')
    confirm.click()
    forceClick(confirm)
    await flush()
    expect(context.api.changeProfileUrl).toHaveBeenCalledExactlyOnceWith(id,
      { expected_revision: 7, url: newUrl, clear_credential: true })
    pending.resolve(profile({ url: newUrl, revision: 8, has_token: false, credential_source: 'none' }))
    await flush()
    expect(dialog()).toBeNull()
    expect(row(view.host).textContent).toContain(newUrl)
    expect(row(view.host).textContent).toContain(t('settings.remoteServers.credential.missing'))
    expect(context.api.listServers).toHaveBeenCalledTimes(1)
    expect(context.api.verify).not.toHaveBeenCalled()
  })

  it('cancelling or going back from a URL change sends nothing and rejects an unchanged URL', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    await openAction(context, view.host, 'remote-change-url')
    await reviewUrl(profile().url)
    expect(alertText()).toBe(t('settings.remoteServers.changeUrl.unchanged'))
    expect(document.body.querySelector('[data-testid="remote-url-confirmation"]')).toBeNull()
    await reviewUrl()
    button(document.body, 'remote-url-back').click()
    await flush()
    expect(document.body.querySelector('[data-testid="remote-url-confirmation"]')).toBeNull()
    await reviewUrl()
    button(document.body, 'remote-metadata-cancel').click()
    await flush()
    expect(dialog()).toBeNull()
    expect(row(view.host).textContent).toContain(profile().url)
    noWrites(context)
  })

  it.each(refusals)('shows a %s URL-change refusal as text and never retries', async (code, status, key) => {
    const context = harness()
    const view = mountView(context)
    await flush()
    await openAction(context, view.host, 'remote-change-url')
    await reviewUrl()
    context.api.changeProfileUrl.mockRejectedValueOnce(new RemoteApiError(status, code, { root: secret }))
    button(document.body, 'remote-url-confirm').click()
    await flush(); await flush()
    expect(alertText()).toBe(t(key))
    expect(consumers.profileConsumers).toHaveBeenCalledTimes(2)
    await flush()
    expect(context.api.changeProfileUrl).toHaveBeenCalledTimes(1)
    expect(row(view.host).textContent).toContain(profile().url)
    expect(context.store.items[0]!.revision).toBe(7)
    expect(document.body.textContent).not.toContain(secret)
    expect(document.body.textContent).not.toContain(code)
  })

  it('a stale revision on URL change blocks the form and requires an explicit Refresh', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    await openAction(context, view.host, 'remote-change-url')
    await reviewUrl()
    context.api.changeProfileUrl.mockRejectedValueOnce(new RemoteApiError(409, 'profile_revision_conflict', { raw: secret }))
    button(document.body, 'remote-url-confirm').click()
    await flush()
    expect(alertText()).toBe(t('settings.remoteServers.lifecycle.stale'))
    expect(document.body.querySelector('[data-testid="remote-url-confirm"]')).toBeNull()
    expect(button(document.body, 'remote-url-review').disabled).toBe(true)
    button(document.body, 'remote-metadata-cancel').click()
    await flush()
    expect(button(row(view.host), 'remote-change-url').disabled).toBe(true)
    expect(context.api.changeProfileUrl).toHaveBeenCalledTimes(1)
    expect(document.body.textContent).not.toContain(secret)
  })

  it('never sends a URL change whose reviewed revision another owner already replaced', async () => {
    const context = harness()
    const view = mountView(context)
    await flush()
    await openAction(context, view.host, 'remote-change-url')
    await reviewUrl()
    mutate(context, profile({ name: 'Renamed elsewhere', revision: 8 }))
    await flush()
    forceClick(button(document.body, 'remote-url-confirm'))
    await flush()
    expect(context.api.changeProfileUrl).not.toHaveBeenCalled()
    expect(alertText()).toBe(t('settings.remoteServers.metadata.stale'))
  })
})
