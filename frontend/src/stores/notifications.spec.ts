import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { onEvent } = vi.hoisted(() => ({ onEvent: vi.fn() }))
vi.mock('@/ws/connection', () => ({ onEvent }))
vi.mock('@/lib/automations', () => ({
  notificationText: (notification: { title: string; message: string }) => notification,
}))

import { DESKTOP_KEY, TOAST_LIFETIME_MS, useNotificationsStore } from './notifications'

const notification = {
  automation_id: 'notify', automation_name: 'Notify', title: 'Ready', message: 'Review',
  trigger: 'manual' as const, values: {}, route: null,
}
let pinia: Pinia
let storage: Map<string, string>
let storageListener: (event: Partial<StorageEvent>) => void
let browserNotification: ReturnType<typeof vi.fn>

function native(supported = true, enabled = true) {
  const bridge = {
    getState: vi.fn().mockResolvedValue({ supported, enabled }),
    setEnabled: vi.fn(async (value: boolean) => ({ supported, enabled: supported && value })),
  }
  Object.assign(window, { voxariumNotifications: bridge })
  return bridge
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  pinia = createPinia()
  setActivePinia(pinia)
  storage = new Map()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  })
  vi.stubGlobal('window', {
    focus: vi.fn(), removeEventListener: vi.fn(),
    addEventListener: (_type: string, listener: typeof storageListener) => { storageListener = listener },
  })
  vi.stubGlobal('document', { hidden: true })
  browserNotification = vi.fn(function () { return { close: vi.fn() } })
  Object.assign(browserNotification, { permission: 'granted', requestPermission: vi.fn() })
  vi.stubGlobal('Notification', browserNotification)
})
afterEach(() => {
  disposePinia(pinia)
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('desktop native notification ownership', () => {
  it('adopts main preference without publishing the missing renderer default', async () => {
    const bridge = native()
    const store = useNotificationsStore()
    store.subscribe()
    store.subscribe()
    await Promise.resolve()
    expect(bridge.getState).toHaveBeenCalledOnce()
    expect(bridge.setEnabled).not.toHaveBeenCalled()
    expect(store.desktop).toBe(true)
    expect(store.permission).toBe('granted')
    expect(storage.has(DESKTOP_KEY)).toBe(false)
    expect(onEvent).toHaveBeenCalledOnce()
  })

  it.each(['on', 'off'])('migrates explicit %s only', async (value) => {
    const bridge = native()
    storage.set(DESKTOP_KEY, value)
    const store = useNotificationsStore()
    store.subscribe()
    await Promise.resolve()
    expect(bridge.setEnabled).toHaveBeenCalledExactlyOnceWith(value === 'on')
    expect(bridge.getState).not.toHaveBeenCalled()
    expect(store.desktop).toBe(value === 'on')
  })

  it('keeps toasts but never raises a Web Notification in desktop', async () => {
    native()
    const store = useNotificationsStore()
    await store.enableDesktop()
    store.show(notification)
    expect(store.toasts).toHaveLength(1)
    expect(browserNotification).not.toHaveBeenCalled()
    vi.advanceTimersByTime(TOAST_LIFETIME_MS)
    expect(store.toasts).toHaveLength(0)
  })

  it('uses native support without browser permission, and persists explicit disable', async () => {
    const bridge = native()
    vi.stubGlobal('Notification', undefined)
    const store = useNotificationsStore()
    await store.enableDesktop()
    expect(store.permission).toBe('granted')
    store.setDesktop(false)
    await Promise.resolve()
    expect(bridge.setEnabled).toHaveBeenLastCalledWith(false)
    expect(storage.get(DESKTOP_KEY)).toBe('off')
    expect(store.desktop).toBe(false)
  })

  it('reflects unsupported native notifications without falling back to Web Notifications', async () => {
    native(false, false)
    const store = useNotificationsStore()
    await store.enableDesktop()
    expect(store.permission).toBe('unsupported')
    expect(store.desktop).toBe(false)
    store.show(notification)
    expect(browserNotification).not.toHaveBeenCalled()
  })

  it('does not let late bootstrap state overwrite a newer choice', async () => {
    const bridge = native()
    let resolve!: (state: { supported: boolean; enabled: boolean }) => void
    bridge.getState.mockReturnValue(new Promise((done) => { resolve = done }))
    const store = useNotificationsStore()
    store.subscribe()
    store.setDesktop(false)
    await Promise.resolve()
    resolve({ supported: true, enabled: true })
    await Promise.resolve()
    expect(store.desktop).toBe(false)
  })

  it('publishes another renderer storage change and removes the listener on disposal', async () => {
    const bridge = native()
    const store = useNotificationsStore()
    store.subscribe()
    await Promise.resolve()
    storage.set(DESKTOP_KEY, 'off')
    storageListener({ key: DESKTOP_KEY })
    await Promise.resolve()
    expect(bridge.setEnabled).toHaveBeenLastCalledWith(false)
    expect(store.desktop).toBe(false)
    disposePinia(pinia)
    expect(window.removeEventListener).toHaveBeenCalledWith('storage', storageListener)
  })

  it('retains browser banners and browser permission fallback', async () => {
    const store = useNotificationsStore()
    await store.enableDesktop()
    store.show(notification, 'event-1')
    expect(browserNotification).toHaveBeenCalledExactlyOnceWith('Ready', { body: 'Review', tag: 'event-1' })
    expect(store.toasts).toHaveLength(1)
    Object.assign(document, { hidden: false })
    store.show(notification)
    expect(browserNotification).toHaveBeenCalledOnce()
  })

  it('never falls back to browser banners when native IPC fails', async () => {
    const bridge = native()
    bridge.setEnabled.mockRejectedValue(new Error('offline'))
    const store = useNotificationsStore()
    await store.enableDesktop()
    store.show(notification)
    expect(browserNotification).not.toHaveBeenCalled()
    expect(store.toasts).toHaveLength(1)
  })

  it('separates browser tags by project and epoch while preserving legacy tags', async () => {
    const store = useNotificationsStore()
    await store.enableDesktop()
    store.subscribe()
    const receive = onEvent.mock.calls[0]![1]
    for (const scope of [
      { project_id: 'a', epoch: 'first' },
      { project_id: 'b', epoch: 'first' },
      { project_id: 'a', epoch: 'second' },
      {},
    ]) receive(notification, { ...scope, seq: 7 })
    expect(browserNotification.mock.calls.map((call) => call[1].tag)).toEqual([
      'voxarium_studio-["a","first",7]',
      'voxarium_studio-["b","first",7]',
      'voxarium_studio-["a","second",7]',
      'voxarium_studio-7',
    ])
  })
})
