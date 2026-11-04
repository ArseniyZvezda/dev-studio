/**
 * Project-level state: which project the backend has open, its config, and whether we
 * are talking to it successfully. Loaded once at startup, before the router renders
 * anything, so views can assume the project is known.
 *
 * Opening a different project is also here, because it is the one action that invalidates
 * every other store at once — see `openProject` for why that ends in a page reload
 * rather than a fan-out of refreshes.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError, api, recoverRuntime } from '@/api/client'
import type { ProjectInfo, ProjectTaskCounts, RecentProject, StatusInfo } from '@/api/types'
import { t } from '@/i18n'
import { FRONTEND_VERSION, waitForBackendVersion } from '@/lib/runtime-version'
import { onEvent, ws } from '@/ws/connection'
import { useDocsStore } from './docs'
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

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const info = await api.project()
      project.value = info
      needsToken.value = false
      acceptVersion(info)
      listenForProjectSwitch()
      ws.connect()
      if (versionMismatch.value) return
      useThemeStore().hydrate(info.config?.theme)
      const docs = useDocsStore()
      docs.hydrate(info.config?.docs)
      // The folds are this browser's, not the config's — but which project's, only this knows.
      docs.restoreFolds(info.root)
      void refreshTaskCounts()
    } catch (cause) {
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

  let checkingVersion = false
  async function refreshVersion(): Promise<void> {
    if (checkingVersion) return
    checkingVersion = true
    try {
      acceptVersion(await api.project())
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

  async function refreshStatus(): Promise<void> {
    try {
      status.value = await api.status()
    } catch {
      /* Status is diagnostic only — a failure here must not break the UI. */
    }
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
    switching.value = true
    try {
      await api.openProject(path, create)
      window.location.reload()
    } catch (cause) {
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
    onEvent('project.opened', (data) => {
      if (project.value && data.root === project.value.root) return
      switching.value = true
      window.location.reload()
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
