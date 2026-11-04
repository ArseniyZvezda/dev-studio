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
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

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
  return typeof Notification === 'undefined' ? 'unsupported' : Notification.permission
}

function readPreference(): boolean {
  try {
    return localStorage.getItem(DESKTOP_KEY) === 'on'
  } catch {
    return false
  }
}

export const useNotificationsStore = defineStore('notifications', () => {
  const toasts = ref<Toast[]>([])
  const desktop = ref(readPreference())
  const permission = ref<DesktopPermission>(readPermission())

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
    if (!desktop.value || readPermission() !== 'granted') return
    if (typeof document !== 'undefined' && !document.hidden) return
    const text = notificationText(notification)
    try {
      // The tag is the event's own sequence number: two tabs of one Studio hear the same
      // event, and the system collapses notifications that share a tag into one.
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
    if (typeof Notification === 'undefined') return
    if (Notification.permission === 'default') await Notification.requestPermission()
    permission.value = readPermission()
    setDesktop(permission.value === 'granted')
  }

  function setDesktop(on: boolean): void {
    desktop.value = on
    try {
      if (on) localStorage.setItem(DESKTOP_KEY, 'on')
      else localStorage.removeItem(DESKTOP_KEY)
    } catch {
      // Storage refusing costs the memory of the choice, never the choice on screen.
    }
  }

  let subscribed = false

  function subscribe(): void {
    if (subscribed) return
    subscribed = true
    onEvent('automation.notification', (payload, envelope) => {
      show(payload, typeof envelope?.seq === 'number' ? `voxarium_studio-${envelope.seq}` : undefined)
    })
  }

  return { toasts, desktop, permission, show, dismiss, hold, release, enableDesktop, setDesktop, subscribe }
})
