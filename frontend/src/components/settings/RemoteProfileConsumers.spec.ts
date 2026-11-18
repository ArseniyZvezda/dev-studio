// @vitest-environment jsdom
/** Gate L census panel (dev spec §16): the user, not the Studio, vouches for a URL's projects.
 * Only the consumers client seam is mocked; no fetch, Remote or WebSocket is reached.
 */
import { createApp, h, nextTick, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RemoteApiError, type createRemoteConsumersClient, type RemoteProfileConsumers as Census } from '@/api/remote'
import { setLocale, t } from '@/i18n'
import RemoteProfileConsumers from './RemoteProfileConsumers.vue'

const id = '11111111-1111-4111-8111-111111111111'
const url = 'https://remote.example/team'
const secret = 'CONSUMER-DETAILS-CANARY'
const binding = { profile_id: id, revision: 7, url }
const mounted: { app: App; host: HTMLDivElement }[] = []

function census(overrides: Partial<Census> = {}): Census {
  return { profile_id: id, url, revision: 7, state: 'unknown', confirmed_at: null,
    bound_roots: ['D:\\games\\alpha', 'D:\\games\\beta'], active_roots: [], operations: [], ...overrides }
}
function known(overrides: Partial<Census> = {}): Census {
  return census({ state: 'known', confirmed_at: '2026-11-18T10:00:00Z', ...overrides })
}
function makeApi(initial: Census = census()) {
  return {
    profileConsumers: vi.fn<ReturnType<typeof createRemoteConsumersClient>['profileConsumers']>().mockResolvedValue(initial),
    confirmProfileConsumers: vi.fn<ReturnType<typeof createRemoteConsumersClient>['confirmProfileConsumers']>(),
    withdrawProfileConsumers: vi.fn<ReturnType<typeof createRemoteConsumersClient>['withdrawProfileConsumers']>(),
  } satisfies ReturnType<typeof createRemoteConsumersClient>
}
async function flush() {
  for (let i = 0; i < 6; i += 1) { await Promise.resolve(); await nextTick() }
}
async function mount(api: ReturnType<typeof makeApi>, disabled = false) {
  const change = vi.fn<(value: Census | null) => void>()
  const refreshRequired = vi.fn()
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(RemoteProfileConsumers, {
    profile: binding, api, disabled, onChange: change, onRefreshRequired: refreshRequired,
  }) })
  mounted.push({ app, host })
  app.mount(host)
  await flush()
  return { host, change, refreshRequired }
}
function el<T extends HTMLElement = HTMLElement>(host: ParentNode, testId: string): T | null {
  return host.querySelector<T>(`[data-testid="remote-consumers-${testId}"]`)
}
function lastChange(view: { change: ReturnType<typeof vi.fn> }) {
  return view.change.mock.calls.at(-1)?.[0] as Census | null | undefined
}

beforeEach(async () => {
  await setLocale('en')
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected direct network')))
  vi.stubGlobal('WebSocket', vi.fn())
})
afterEach(async () => {
  for (const view of mounted.splice(0)) { view.app.unmount(); view.host.remove() }
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  await setLocale('en')
})

describe('unknown census', () => {
  it('shows the exact URL and projects, and confirms only after the checkbox and an explicit click', async () => {
    const api = makeApi()
    const view = await mount(api)
    expect(api.profileConsumers).toHaveBeenCalledExactlyOnceWith(id)
    expect(el(view.host, 'url')?.textContent).toBe(url)
    expect(el(view.host, 'roots')?.textContent).toContain('D:\\games\\alpha')
    expect(el(view.host, 'roots')?.textContent).toContain('D:\\games\\beta')
    expect(view.host.textContent).toContain(t('settings.remoteServers.consumers.unknownHint'))
    expect(view.host.textContent).toContain(t('settings.remoteServers.consumers.acknowledge', { url }))
    expect(lastChange(view)?.state).toBe('unknown')
    const confirm = el<HTMLButtonElement>(view.host, 'confirm')!
    expect(confirm.disabled).toBe(true)
    confirm.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect(api.confirmProfileConsumers).not.toHaveBeenCalled()

    api.confirmProfileConsumers.mockResolvedValueOnce(known())
    el<HTMLInputElement>(view.host, 'acknowledge')!.click()
    await flush()
    expect(confirm.disabled).toBe(false)
    expect(api.confirmProfileConsumers).not.toHaveBeenCalled()
    confirm.click()
    await flush()
    expect(api.confirmProfileConsumers).toHaveBeenCalledExactlyOnceWith(id, {
      expected_revision: 7, url, bound_roots: ['D:\\games\\alpha', 'D:\\games\\beta'],
    })
    expect(lastChange(view)?.state).toBe('known')
    expect(el(view.host, 'confirmed')).not.toBeNull()
    expect(el(view.host, 'acknowledge')).toBeNull()
  })

  it('states explicitly when no project is known and still requires confirmation', async () => {
    const api = makeApi(census({ bound_roots: [] }))
    const view = await mount(api)
    expect(el(view.host, 'empty')?.textContent?.trim()).toBe(t('settings.remoteServers.consumers.empty'))
    expect(el(view.host, 'roots')).toBeNull()
    expect(lastChange(view)?.state).toBe('unknown')
    expect(api.confirmProfileConsumers).not.toHaveBeenCalled()
  })

  it('reloads and asks again when the inventory changed, never re-confirming by itself', async () => {
    const api = makeApi()
    const view = await mount(api)
    api.confirmProfileConsumers.mockRejectedValueOnce(new RemoteApiError(409, 'consumer_inventory_changed', { root: secret }))
    api.profileConsumers.mockResolvedValueOnce(census({ bound_roots: ['D:\\games\\alpha', 'D:\\games\\gamma'] }))
    el<HTMLInputElement>(view.host, 'acknowledge')!.click()
    await flush()
    el<HTMLButtonElement>(view.host, 'confirm')!.click()
    await flush()
    expect(api.profileConsumers).toHaveBeenCalledTimes(2)
    expect(api.confirmProfileConsumers).toHaveBeenCalledTimes(1)
    expect(el(view.host, 'roots')?.textContent).toContain('D:\\games\\gamma')
    expect(el<HTMLInputElement>(view.host, 'acknowledge')!.checked).toBe(false)
    expect(el<HTMLButtonElement>(view.host, 'confirm')!.disabled).toBe(true)
    expect(el(view.host, 'error')?.textContent).toBe(t('settings.remoteServers.consumers.inventoryChanged'))
    expect(lastChange(view)?.state).toBe('unknown')
    expect(view.refreshRequired).not.toHaveBeenCalled()
    expect(view.host.textContent).not.toContain(secret)
  })

  it('asks for a profile refresh on a revision conflict and stays gated', async () => {
    const api = makeApi()
    const view = await mount(api)
    api.confirmProfileConsumers.mockRejectedValueOnce(new RemoteApiError(409, 'profile_revision_conflict'))
    el<HTMLInputElement>(view.host, 'acknowledge')!.click()
    await flush()
    el<HTMLButtonElement>(view.host, 'confirm')!.click()
    await flush()
    expect(view.refreshRequired).toHaveBeenCalledTimes(1)
    expect(lastChange(view)).toBeNull()
    expect(el(view.host, 'error')?.textContent).toBe(t('settings.remoteServers.consumers.conflict'))
  })

  it('does nothing while disabled by its owner', async () => {
    const api = makeApi()
    const view = await mount(api, true)
    expect(el<HTMLInputElement>(view.host, 'acknowledge')!.disabled).toBe(true)
    expect(el<HTMLButtonElement>(view.host, 'confirm')!.disabled).toBe(true)
  })
})

describe('known census', () => {
  it('shows the confirmation date and withdraws only on an explicit click', async () => {
    const api = makeApi(known({ bound_roots: [] }))
    const view = await mount(api)
    expect(el(view.host, 'confirmed')?.textContent).toContain(t('settings.remoteServers.consumers.confirmedAt', { time: '' }).trim())
    expect(el(view.host, 'confirmed')?.textContent).toContain('2026')
    expect(lastChange(view)?.state).toBe('known')
    expect(api.withdrawProfileConsumers).not.toHaveBeenCalled()
    api.withdrawProfileConsumers.mockResolvedValueOnce(census({ bound_roots: [] }))
    el<HTMLButtonElement>(view.host, 'withdraw')!.click()
    await flush()
    expect(api.withdrawProfileConsumers).toHaveBeenCalledExactlyOnceWith(id, { expected_revision: 7 })
    expect(lastChange(view)?.state).toBe('unknown')
    expect(el(view.host, 'acknowledge')!.getAttribute('type')).toBe('checkbox')
    expect(api.confirmProfileConsumers).not.toHaveBeenCalled()
  })

  it('marks syncing projects and warns that sync must be disabled there first', async () => {
    const api = makeApi(known({ active_roots: ['D:\\games\\beta'], operations: ['read', 'create'] }))
    const view = await mount(api)
    const active = view.host.querySelectorAll('[data-testid="remote-consumers-active"]')
    expect(active).toHaveLength(1)
    expect(active[0]!.closest('li')!.textContent).toContain('D:\\games\\beta')
    expect(el(view.host, 'active-warning')?.textContent?.trim()).toBe(t('settings.remoteServers.consumers.activeWarning'))
    expect(el(view.host, 'operations')?.textContent?.trim()).toBe(t('settings.remoteServers.consumers.operations', {
      operations: `${t('settings.remoteServers.consumers.operation.read')}, ${t('settings.remoteServers.consumers.operation.create')}`,
    }))
  })

  it('treats a census for another URL as stale, never as confirmed', async () => {
    const api = makeApi(known({ url: 'https://other.example' }))
    const view = await mount(api)
    expect(view.change.mock.calls.every(([value]) => value === null)).toBe(true)
    expect(view.refreshRequired).toHaveBeenCalledTimes(1)
  })
})

describe('localized errors', () => {
  it.each(['en', 'ru'] as const)('maps census and confirmation failures to %s text without details', async language => {
    await setLocale(language)
    const api = makeApi()
    api.profileConsumers.mockRejectedValueOnce(new RemoteApiError(503, 'consumer_lifecycle_unavailable', { root: secret }))
    const view = await mount(api)
    expect(el(view.host, 'error')?.textContent).toBe(t('settings.remoteServers.consumers.unavailable'))
    expect(lastChange(view)).toBeNull()
    el<HTMLButtonElement>(view.host, 'retry')!.click()
    await flush()
    expect(lastChange(view)?.state).toBe('unknown')
    api.confirmProfileConsumers.mockRejectedValueOnce(new RemoteApiError(503, 'consumer_confirmation_unavailable', { root: secret }))
    el<HTMLInputElement>(view.host, 'acknowledge')!.click()
    await flush()
    el<HTMLButtonElement>(view.host, 'confirm')!.click()
    await flush()
    const message = t('settings.remoteServers.consumers.confirmationUnavailable')
    expect(el(view.host, 'error')?.textContent).toBe(message)
    if (language === 'ru') expect(message).toMatch(/[а-я]/)
    expect(lastChange(view)?.state).toBe('unknown')
    expect(el<HTMLInputElement>(view.host, 'acknowledge')!.checked).toBe(false)
    expect(view.host.textContent).not.toContain(secret)
    expect(view.host.textContent).not.toContain('consumer_')
    expect(fetch).not.toHaveBeenCalled()
  })
})
