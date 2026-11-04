/**
 * Whether a newer Studio exists, and installing it (§12.1).
 *
 * This store is about the **application**, not about the managed project — the one piece
 * of state here that survives a project switch in meaning, even though the backend rebuilds
 * its service with the new project's settings. Everything it shows comes from the Studio's
 * own git checkout: a release is a **tag**, so `origin/main` moving is not an update.
 *
 * The unusual part is the ending. An update finishes by restarting the server this page is
 * talking to, so the last thing the store does is watch for the backend to go away and come
 * back, and then reload — a page held across a rebuild is showing the previous SPA against
 * the new backend, which is exactly what `Cache-Control: no-cache` on `index.html` exists to
 * prevent (§2.5).
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError, api } from '@/api/client'
import type { UpdateStatus } from '@/api/types'
import { t } from '@/i18n'
import type { DesktopUpdateStatus } from '@/desktop'
import { onEvent } from '@/ws/connection'

/** How much of the transcript the dialog keeps. The whole of it is on the backend's disk. */
const MAX_LOG_LINES = 500

const POLL_MS = 1000

/**
 * How long to wait for the backend to disappear before assuming it already has. A restart
 * that is quick enough to be missed is not a reason to stop waiting for the one that
 * follows it.
 */
const DOWN_TIMEOUT_MS = 60_000

/** And how long to wait for it to come back. `npm ci` on a cold cache is minutes. */
const UP_TIMEOUT_MS = 15 * 60_000

export const useUpdatesStore = defineStore('updates', () => {
  const status = ref<UpdateStatus | null>(null)
  const log = ref<string[]>([])
  /** A refusal from the last apply — uncommitted changes, or work a restart would kill. */
  const refusal = ref<string | null>(null)
  const dialogOpen = ref(false)
  const busy = ref(false)
  /** Set once the backend is on its way out, until the page reloads into the new one. */
  const waiting = ref<string | null>(null)

  const desktop = ref(false)
  const downloaded = ref(false)
  const progress = ref(0)
  let desktopListening = false
  const desktopBridge = () => typeof window === 'undefined' ? undefined : window.voxariumUpdates
  function acceptDesktop(data: DesktopUpdateStatus): void {
    status.value = data
    downloaded.value = data.downloaded
    progress.value = data.progress
  }

  let listening = false

  const available = computed(() => status.value?.available === true)
  const state = computed(() => status.value?.state ?? 'idle')
  const running = computed(() => state.value === 'updating' || state.value === 'restarting')

  /** What the button says: `Update → v1.1`, composed from the tag and nothing else. */
  const label = computed(() =>
    status.value?.latest ? t('updates.label', { version: status.value.latest }) : '',
  )

  async function load(): Promise<void> {
    try {
      const bridge = desktopBridge()
      if (bridge) {
        const data = await bridge.getState()
        if (data) {
          desktop.value = true
          if (!desktopListening) {
            bridge.onStateChange(acceptDesktop)
            desktopListening = true
          }
          acceptDesktop(data)
          return
        }
      }
      listen()
      status.value = await api.updates()
      log.value = status.value.log ?? []
    } catch {
      /* The rest of the app does not depend on this; a Studio that cannot say is silent. */
    }
  }

  /** Fetch now instead of waiting for the backend's own schedule. */
  async function check(): Promise<void> {
    busy.value = true
    refusal.value = null
    try {
      if (desktop.value) acceptDesktop(await desktopBridge()!.check())
      else status.value = await api.checkForUpdates()
    } catch (cause) {
      refusal.value = cause instanceof Error ? cause.message : String(cause)
    } finally {
      busy.value = false
    }
  }

  /**
   * Pull, rebuild, restart.
   *
   * A 409 is the Studio refusing rather than failing — it is holding something back, and
   * `force` is how the user says they know. The message is kept for the dialog to show
   * beside that choice, so nothing is overruled without being read first.
   */
  async function apply(force = false): Promise<void> {
    busy.value = true
    refusal.value = null
    log.value = []
    try {
      if (desktop.value) acceptDesktop(await desktopBridge()!.download())
      else status.value = await api.applyUpdate(force)
    } catch (cause) {
      refusal.value = cause instanceof Error ? cause.message : String(cause)
    } finally {
      busy.value = false
    }
  }

  /** Start an update and show it happening — what the rail's button does in one click. */
  async function startFromButton(): Promise<void> {
    dialogOpen.value = true
    if (busy.value || running.value) return
    if (desktop.value && !available.value) await check()
    else if (!downloaded.value && !running.value && !busy.value) await apply(false)
  }

  async function installDesktop(): Promise<void> {
    if (busy.value) return
    busy.value = true
    try { acceptDesktop(await desktopBridge()!.install()) }
    catch (cause) { refusal.value = cause instanceof Error ? cause.message : String(cause) }
    finally { busy.value = false }
  }

  function listen(): void {
    if (listening) return
    listening = true
    onEvent('update.status', (data) => {
      status.value = data
      // The backend's own copy of the transcript is authoritative for a client that arrives
      // mid-update; a client that was here all along has the same lines already.
      if (data.log?.length && data.log.length > log.value.length) log.value = data.log
      if (data.state === 'restarting') void awaitRestart()
    })
    onEvent('update.log', (data) => {
      log.value.push(data.line)
      if (log.value.length > MAX_LOG_LINES) log.value.splice(0, log.value.length - MAX_LOG_LINES)
    })
  }

  /**
   * Wait out the restart, then reload into the Studio that came back.
   *
   * Both halves are needed. Reloading as soon as the backend answers would reload into the
   * one that is still shutting down — it answers until its socket closes — so the wait is
   * "gone, then back", and only the second half has a long deadline. Any HTTP answer at all
   * counts as back, a 401 included: the question is whether a server is listening, not
   * whether this tab may talk to it.
   */
  async function awaitRestart(): Promise<void> {
    if (waiting.value) return
    waiting.value = t('updates.waiting.stop')
    const wentDown = await pollUntil(() => reachable().then((up) => !up), DOWN_TIMEOUT_MS)
    waiting.value = wentDown ? t('updates.waiting.restarting') : t('updates.waiting.comeBack')
    const cameBack = await pollUntil(reachable, UP_TIMEOUT_MS)
    if (!cameBack) {
      waiting.value = t('updates.waiting.didNotComeBack')
      return
    }
    waiting.value = t('updates.waiting.reloading')
    window.location.reload()
  }

  return {
    desktop,
    downloaded,
    progress,
    installDesktop,
    status,
    log,
    refusal,
    dialogOpen,
    busy,
    waiting,
    available,
    state,
    running,
    label,
    load,
    check,
    apply,
    startFromButton,
  }
})

/** Is a backend answering at all? Only a transport failure means "no". */
async function reachable(): Promise<boolean> {
  try {
    await api.updates()
    return true
  } catch (cause) {
    return !(cause instanceof ApiError) || cause.status !== 0
  }
}

async function pollUntil(test: () => Promise<boolean>, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await test()) return true
    await new Promise((resolve) => setTimeout(resolve, POLL_MS))
  }
  return false
}
