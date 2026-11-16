/**
 * Project-level state: the renderer's project, its config, and whether we
 * are talking to it successfully. Loaded once at startup, before the router renders
 * anything, so views can assume the project is known.
 *
 * Scoped renderers keep their immutable ProjectSession (§5.3). Only the legacy browser
 * adapter can open a different selection and reload every store/plugin route at once.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError, api, recoverRuntime } from '@/api/client'
import type { ProjectInfo, ProjectTaskCounts, RecentProject, StatusInfo } from '@/api/types'
import { setLocale, t } from '@/i18n'
import { FRONTEND_VERSION, waitForBackendVersion } from '@/lib/runtime-version'
import { rendererSession } from '@/session/project-session'
import { onEvent, ws } from '@/ws/connection'
import { useDocsStore } from './docs'
import { useSyncStore } from './sync'
import { useThemeStore } from './theme'

export const useSessionStore = defineStore('session', () => {
  const project = ref<ProjectInfo | null>(null)
  const status = ref<StatusInfo | null>(null)
  const recent = ref<RecentProject[]>([])
  const taskCounts = ref<ProjectTaskCounts | null>(null)
  const loading = ref(true)
  const error = ref<string | null>(null)
  const needsToken = ref(false)
  const frontendVersion = FRONTEND_VERSION
  const backendVersion = ref<string | null>(null)
  const versionChecked = ref(false)
  const versionMismatch = computed(() => versionChecked.value && backendVersion.value !== frontendVersion)
  const recoveryState = ref<'idle' | 'checking' | 'restarting' | 'reloading' | 'failed'>('idle')
  const recoveryError = ref<string | null>(null)
  const attempted = new Set<string>()
  /** Set while the backend tears one project down and brings the next one up. */
  const switching = ref(false)

  const projectName = computed(() => project.value?.name ?? t('app.name'))
  const connection = ws.state

  let listening = false
  let reloadScheduled = false

  /** Retire the legacy session before any late HTTP/socket continuation can apply. */
  function reloadLegacyProject(): void {
    if (reloadScheduled) return
    reloadScheduled = true
    switching.value = true
    rendererSession.invalidate()
    ws.disconnect()
    window.location.reload()
  }

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const current = rendererSession.capture()
      const info = await api.project()
      current()
      // The scoped API bootstraps the immutable identity before returning (§5.3.2).
      // Never hydrate a renderer with legacy or foreign project metadata.
      assertProjectBinding(info)
      project.value = info
      needsToken.value = false
      acceptVersion(info)
      listenForProjectSwitch()
      ws.connect()
      if (versionMismatch.value) return
      // The browser cache is only the startup fallback; the user's config wins (§6.4).
      if (info.config?.language) await setLocale(info.config.language)
      current()
      useThemeStore().hydrate(info.config?.theme)
      const docs = useDocsStore()
      docs.hydrate(info.config?.docs)
      // The folds are this browser's, not the config's — but which project's, only this knows.
      docs.restoreFolds(info.root)
      // Known before any view renders, so a document is opened in its room the first time
      // rather than alone and then again (dev spec §16.4).
      const sync = useSyncStore()
      sync.subscribe()
      await sync.load()
      current()
      void refreshTaskCounts()
    } catch (cause) {
      if (reloadScheduled) return
      if (cause instanceof ApiError && cause.isAuthFailure) {
        needsToken.value = true
        error.value = t('project.session.tokenRequired')
      } else {
        error.value = cause instanceof Error ? cause.message : String(cause)
      }
    } finally {
      loading.value = false
    }
  }

  function acceptVersion(info: ProjectInfo): void {
    backendVersion.value = typeof info.version?.version === 'string' ? info.version.version : null
    versionChecked.value = true
  }

  function assertProjectBinding(info: ProjectInfo): void {
    if (!rendererSession.isLegacy && !rendererSession.matches(info)) {
      throw new Error('Project response does not match the renderer binding')
    }
  }

  let checkingVersion = false
  async function refreshVersion(): Promise<void> {
    if (checkingVersion) return
    checkingVersion = true
    try {
      const info = await api.project()
      assertProjectBinding(info)
      acceptVersion(info)
    } catch { /* Transport/auth errors do not erase a confirmed version mismatch. */ }
    finally { checkingVersion = false }
  }

  async function recoverVersionMismatch(force = false): Promise<void> {
    if (!versionMismatch.value || ['checking', 'restarting', 'reloading'].includes(recoveryState.value)) return
    const key = `voxarium_studio.version-recovery:${frontendVersion}:${backendVersion.value}`
    let previous = attempted.has(key)
    try { previous ||= sessionStorage.getItem(key) === 'attempted' } catch { /* Storage may be unavailable. */ }
    if (previous && !force) {
      recoveryState.value = 'failed'
      recoveryError.value = t('app.versionMismatch.repeated')
      return
    }
    recoveryError.value = null
    recoveryState.value = 'checking'
    attempted.add(key)
    try { sessionStorage.setItem(key, 'attempted') } catch { /* The in-memory guard still applies. */ }
    try {
      if (!backendVersion.value) throw new Error(t('app.versionMismatch.unknownBackend'))
      const result = await recoverRuntime(frontendVersion, backendVersion.value)
      if (!result.version || !['restart', 'reload'].includes(result.action)) throw new Error(t('app.versionMismatch.unsupported'))
      if (result.action === 'restart') {
        recoveryState.value = 'restarting'
        await waitForBackendVersion(result.version)
      }
      recoveryState.value = 'reloading'
      window.location.reload()
    } catch (cause) {
      recoveryState.value = 'failed'
      recoveryError.value = cause instanceof ApiError && [404, 405].includes(cause.status)
        ? t('app.versionMismatch.unsupported')
        : cause instanceof Error && cause.message === 'restart-timeout'
          ? t('app.versionMismatch.timeout')
          : cause instanceof Error ? cause.message : String(cause)
    }
  }

  let statusRequest: Promise<void> | null = null
  function refreshStatus(): Promise<void> {
    if (statusRequest) return statusRequest
    statusRequest = (async () => {
      try {
        status.value = await api.status()
      } catch {
        // §6.1: a failed check must not leave stale green backend indicators behind.
        status.value = null
      }
    })().finally(() => { statusRequest = null })
    return statusRequest
  }

  async function refreshRecent(): Promise<void> {
    try {
      recent.value = await api.recentProjects()
    } catch {
      /* The recent list is a convenience; the dialog works without it. */
    }
  }

  // Serialise reads, but repeat if a task changed during the request. Dropping that
  // invalidation would leave the footer showing the snapshot from before the change.
  let countsLoading = false
  let countsDirty = false
  async function refreshTaskCounts(): Promise<void> {
    countsDirty = true
    if (countsLoading) return
    countsLoading = true
    try {
      do {
        countsDirty = false
        try {
          taskCounts.value = await api.projectTaskCounts()
        } catch {
          taskCounts.value = null
        }
      } while (countsDirty)
    } finally {
      countsLoading = false
    }
  }

  async function forgetRecent(path: string): Promise<void> {
    recent.value = await api.forgetRecentProject(path)
  }

  let resyncRequest: Promise<void> | null = null
  let resyncPending = false
  function resyncProject(): Promise<void> {
    resyncPending = true
    if (resyncRequest) return resyncRequest
    const refreshMetadata = async () => {
      const current = rendererSession.capture()
      const info = await api.project()
      current()
      assertProjectBinding(info)
      project.value = info
      acceptVersion(info)
    }
    // A gap is not a project switch (§5.3.2). Refetch only this session's metadata;
    // subscribed domain stores own their draft-preserving recovery from the same event.
    resyncRequest = Promise.resolve().then(async () => {
      do {
        resyncPending = false
        await Promise.allSettled([
          refreshMetadata(), refreshStatus(), refreshTaskCounts(), useSyncStore().load(),
        ])
      } while (resyncPending && !reloadScheduled)
    }).finally(() => { resyncRequest = null })
    return resyncRequest
  }

  /**
   * Point the backend at another project and start over against it.
   *
   * The reload is the point, not a shortcut: every store, the plugin-contributed routes
   * and the WebSocket resume position all describe the project we are leaving. Resetting
   * them one by one is a list nobody can keep complete — a plugin page registered by the
   * old project's plugins would quietly survive it. A reload cannot get that wrong, and
   * the token lives in `localStorage`, so it costs the user nothing.
   */
  async function openProject(path: string, create = true): Promise<void> {
    // Desktop opening belongs to the future shell, never the legacy selection (§5.3.3).
    if (!rendererSession.isLegacy) throw new Error('A project renderer cannot change its project binding')
    if (reloadScheduled) return
    switching.value = true
    try {
      await api.openProject(path, create)
      reloadLegacyProject()
    } catch (cause) {
      // project.opened can arrive before the open HTTP response, invalidating it.
      // The reload is already committed: neither surface the stale rejection nor undo it.
      if (reloadScheduled) return
      switching.value = false
      throw cause
    }
  }

  /** A project opened from *another* tab invalidates this one just as thoroughly. */
  function listenForProjectSwitch(): void {
    if (listening) return
    listening = true
    onEvent('team.task.created', () => void refreshTaskCounts())
    onEvent('team.task.status_changed', () => void refreshTaskCounts())
    onEvent('team.task.deleted', () => void refreshTaskCounts())
    onEvent('system.hello', () => {
      void refreshTaskCounts()
      void refreshVersion()
    })
    onEvent('system.dropped', (data, envelope) => {
      if (rendererSession.isLegacy || reloadScheduled || !data.resync) return
      if (!rendererSession.matches(envelope)) return
      void resyncProject()
    })
    onEvent('project.opened', (data) => {
      // This event describes only legacy browser selection, not desktop activation.
      if (!rendererSession.isLegacy) return
      if (project.value && data.root === project.value.root) return
      reloadLegacyProject()
    })
  }

  return {
    project,
    status,
    recent,
    taskCounts,
    loading,
    error,
    needsToken,
    frontendVersion,
    backendVersion,
    versionMismatch,
    recoveryState,
    recoveryError,
    refreshVersion,
    recoverVersionMismatch,
    switching,
    projectName,
    connection,
    load,
    refreshStatus,
    refreshRecent,
    refreshTaskCounts,
    forgetRecent,
    openProject,
  }
})
