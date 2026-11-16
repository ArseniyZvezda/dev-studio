/**
 * Notifications an automation sends (§6.5) — the *notify* action, as this window shows it.
 *
 * Two surfaces, one event. A toast in the corner of the Studio, always; and the system's own
 * notification when the reader asked for one **and this window is not the one being looked
 * at** — "a task needs review" is for somebody who went to do something else while an agent
 * worked, and a system banner over a page already showing the toast is the same news twice.
 *
 * Whether to raise system notifications is the reader's, not the project's, so it lives in
 * `localStorage` (`voxarium_studio.notifications.desktop`) beside the language and the transcript
 * verbosity — and beside the browser's own permission, which is per browser anyway.
 * In desktop, main owns system banners and the durable native preference (§5.3.6);
 * this store migrates explicit local choices and otherwise adopts main's state.
 */

import { defineStore } from 'pinia'
import { onScopeDispose, ref } from 'vue'

import type { AutomationNotification } from '@/api/types'
import { notificationText } from '@/lib/automations'
import { onEvent } from '@/ws/connection'

export const DESKTOP_KEY = 'voxarium_studio.notifications.desktop'

/** Long enough to read two lines; a toast under the pointer stays until the pointer leaves. */
export const TOAST_LIFETIME_MS = 8000
export const MAX_TOASTS = 4

export interface Toast {
  id: number
  notification: AutomationNotification
}

export type DesktopPermission = NotificationPermission | 'unsupported'

function readPermission(): DesktopPermission {
  if (nativeBridge()) return 'default'
  return typeof Notification === 'undefined' ? 'unsupported' : Notification.permission
}

function nativeBridge() {
  return typeof window === 'undefined' ? undefined : window.voxariumNotifications
}

function readPreference(): boolean | undefined {
  try {
    const value = localStorage.getItem(DESKTOP_KEY)
    return value === null ? undefined : value === 'on'
  } catch {
    return undefined
  }
}

export const useNotificationsStore = defineStore('notifications', () => {
  const toasts = ref<Toast[]>([])
  const desktop = ref(readPreference() ?? false)
  const permission = ref<DesktopPermission>(readPermission())

  let preferenceRevision = 0
  async function syncNative(enabled?: boolean): Promise<void> {
    const bridge = nativeBridge()
    if (!bridge) return
    const revision = ++preferenceRevision
    try {
      const state = await (enabled === undefined ? bridge.getState() : bridge.setEnabled(enabled))
      if (revision !== preferenceRevision) return
      permission.value = state.supported ? 'granted' : 'unsupported'
      desktop.value = state.enabled
    } catch {
      // Main remains the only native owner even if IPC temporarily fails.
    }
  }

  let nextId = 1
  const timers = new Map<number, ReturnType<typeof setTimeout>>()

  function dismiss(id: number): void {
    const timer = timers.get(id)
    if (timer) clearTimeout(timer)
    timers.delete(id)
    toasts.value = toasts.value.filter((toast) => toast.id !== id)
  }

  /** Stop the clock while the pointer is on a toast, and start it over when it leaves. */
  function hold(id: number): void {
    const timer = timers.get(id)
    if (timer) clearTimeout(timer)
    timers.delete(id)
  }

  function release(id: number): void {
    if (!toasts.value.some((toast) => toast.id === id)) return
    hold(id)
    timers.set(id, setTimeout(() => dismiss(id), TOAST_LIFETIME_MS))
  }

  function show(notification: AutomationNotification, tag?: string): void {
    const toast: Toast = { id: nextId++, notification }
    const kept = [...toasts.value, toast]
    for (const dropped of kept.slice(0, Math.max(0, kept.length - MAX_TOASTS))) dismiss(dropped.id)
    toasts.value = [...toasts.value, toast]
    release(toast.id)
    raiseDesktop(notification, tag)
  }

  function raiseDesktop(notification: AutomationNotification, tag?: string): void {
    // Desktop app stream owns banners, including while no renderer exists (§5.3.6).
    if (nativeBridge()) return
    if (!desktop.value || readPermission() !== 'granted') return
    if (typeof document !== 'undefined' && !document.hidden) return
    const text = notificationText(notification)
    try {
      // The tag identifies the scoped event: two tabs hear the same event, while
      // independent projects/epochs may reuse its sequence number (§5.3.2).
      const raised = new Notification(text.title, { body: text.message, tag })
      raised.onclick = () => {
        window.focus()
        raised.close()
      }
    } catch {
      // Some browsers only allow notifications from a service worker; the toast still showed.
    }
  }

  /** Turn system notifications on — asking the browser first, if it has not been asked. */
  async function enableDesktop(): Promise<void> {
    if (nativeBridge()) {
      persistPreference(true)
      await syncNative(true)
      return
    }
    if (typeof Notification === 'undefined') return
    if (Notification.permission === 'default') await Notification.requestPermission()
    permission.value = readPermission()
    setDesktop(permission.value === 'granted')
  }

  function setDesktop(on: boolean): void {
    persistPreference(on)
    void syncNative(on)
  }

  function persistPreference(on: boolean): void {
    desktop.value = on
    try {
      // Explicit off must survive migration; absence means adopt main's saved choice.
      localStorage.setItem(DESKTOP_KEY, on ? 'on' : 'off')
    } catch {
      // Storage refusing costs the memory of the choice, never the choice on screen.
    }
  }

  let subscribed = false

  function storageChanged(event: StorageEvent): void {
    if (event.key !== DESKTOP_KEY && event.key !== null) return
    const preference = readPreference()
    if (preference !== undefined) desktop.value = preference
    else if (!nativeBridge()) desktop.value = false
    void syncNative(preference)
  }
  onScopeDispose(() => {
    if (typeof window !== 'undefined') window.removeEventListener('storage', storageChanged)
    for (const timer of timers.values()) clearTimeout(timer)
  })

  function subscribe(): void {
    if (subscribed) return
    subscribed = true
    // Only migrate an explicit stored choice, never the renderer's default false.
    void syncNative(readPreference())
    if (typeof window !== 'undefined') window.addEventListener('storage', storageChanged)
    onEvent('automation.notification', (payload, envelope) => {
      const identity = envelope?.project_id && envelope.epoch
        ? JSON.stringify([envelope.project_id, envelope.epoch, envelope.seq])
        : envelope?.seq
      show(payload, typeof envelope?.seq === 'number' ? `voxarium_studio-${identity}` : undefined)
    })
  }

  return { toasts, desktop, permission, show, dismiss, hold, release, enableDesktop, setDesktop, subscribe }
})
