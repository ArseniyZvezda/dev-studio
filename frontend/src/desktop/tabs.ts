/** Shell-only adapter: no project store/session/socket/plugin imports (§5.3.4). */
import { ref, shallowRef, type Ref, type ShallowRef } from 'vue'
import type { DirectoryListing, PathInspection, PickerStatus, RecentProject } from '@/api/types'
import type { DesktopTabsBridge, DesktopTabsState, DesktopTabsTabRequest } from '@/desktop'
import { t } from '@/i18n'

export type DesktopShellState = Extract<DesktopTabsState, { role: 'shell' }>

/** Local pointer bookkeeping only (§5.3.4). Native main owns threshold, terminal
 * target, commit and rollback; pointerup/dragend must never request a transfer. */
export function createDesktopTabGestures(options: {
  bridge: DesktopTabsBridge | undefined
  state: () => DesktopShellState | null
  activate: (tabId: string) => void
  error: () => void
}) {
  type Point = { x: number; y: number }
  type Gesture = {
    request: DesktopTabsTabRequest; point: Point; moved: boolean; released: boolean; cancelled: boolean
    lease: Promise<{ token: string; expiresAt: number } | null>; cancellation?: Promise<boolean>
  }
  let gesture: Gesture | null = null, generation = 0, disposed = false, suppressedClick: string | null = null
  let disarming: Promise<boolean> | null = null
  function request(tabId: string): DesktopTabsTabRequest | null {
    const state = options.state(), tab = state?.tabs.find(item => item.tabId === tabId)
    return state && tab?.state === 'ready' && tab.ownerWindowId === state.window.windowId
      ? { windowId: state.window.windowId, windowRevision: state.window.revision, tabId, tabRevision: tab.revision } : null
  }
  function cancelLease(current: Gesture) {
    return current.cancellation ??= (async () => {
      const lease = await current.lease
      if (!lease) return true
      try { await options.bridge?.cancelDrag({ windowId: current.request.windowId,
        windowRevision: current.request.windowRevision, token: lease.token }); return true }
      catch {
        // A consumed token is an idempotent no-op in main. An IPC rejection is
        // not acknowledgement of disarm: do not activate/project-focus on failure.
        if (!disposed) options.error()
        return false
      }
    })()
  }
  function cancel() {
    generation += 1
    const current = gesture
    if (!current) return disarming ?? Promise.resolve(true)
    current.cancelled = true; gesture = null; suppressedClick = current.request.tabId
    const cancellation = cancelLease(current)
    disarming = cancellation
    void cancellation.then(() => { if (disarming === cancellation) disarming = null })
    return cancellation
  }
  function move(point: Point) {
    if (gesture && Math.hypot(point.x - gesture.point.x, point.y - gesture.point.y) >= 12) gesture.moved = true
  }
  return {
    get armed() { return gesture !== null || disarming !== null },
    begin(tabId: string, point: Point) {
      if (disposed || !options.bridge || (gesture && !gesture.released)) return false
      const source = request(tabId)
      if (!source) return false
      void cancel()
      suppressedClick = null
      const lease = options.bridge.beginDrag({ ...source, point }).catch(() => {
        if (!disposed) options.error()
        return null
      })
      gesture = { request: source, point, lease, moved: false, released: false, cancelled: false }
      return true
    },
    move,
    release(point: Point) { move(point); if (gesture) gesture.released = true },
    cancel,
    async click(tabId: string, pointer = true) {
      const current = gesture
      if (disposed) return
      if (!current) {
        if (pointer && suppressedClick === tabId) { suppressedClick = null; return }
        if (disarming) {
          const revision = generation
          if (await disarming && !disposed && revision === generation) options.activate(tabId)
          return
        }
        options.activate(tabId); return
      }
      if (!pointer) {
        const cancellation = cancel(), revision = generation
        if (await cancellation && !disposed && revision === generation) options.activate(tabId)
        return
      }
      if (current.request.tabId !== tabId || !current.released || current.moved || current.cancelled) return
      const revision = generation
      // Native click cancels; activating before disarm would mutate its lease revision
      // and move native focus out of the shell while the pointer is still captured.
      if (!await cancelLease(current)) return
      if (disposed || revision !== generation || gesture !== current) return
      gesture = null
      const latest = request(tabId)
      if (latest && Object.entries(current.request).every(([key, value]) => latest[key as keyof DesktopTabsTabRequest] === value)) {
        options.activate(tabId)
      }
    },
    sync() {
      if (!gesture) return false
      const latest = request(gesture.request.tabId)
      if (!latest || Object.entries(gesture.request).some(([key, value]) => latest[key as keyof DesktopTabsTabRequest] !== value)) {
        void cancel(); return true
      }
      return false
    },
    dispose() { disposed = true; void cancel() },
  }
}

/** Renderer identity is main-owned. A URL query alone never grants shell rights. */
export async function checkedDesktopBootstrap(
  bridge: DesktopTabsBridge | undefined, pathname: string,
): Promise<DesktopTabsState | null> {
  if (!bridge) return null // Legacy browser UX, including old URLs.
  const state = await bridge.bootstrap()
  if (state.role === 'shell') {
    if (pathname !== '/') throw new Error(t('desktop.tabs.error'))
  } else if (state.role !== 'project' || !/^[A-Za-z0-9_-]+$/.test(state.binding.project_id)
    || !(pathname === `/p/${state.binding.project_id}` || pathname.startsWith(`/p/${state.binding.project_id}/`))
    || state.tab.project.project_id !== state.binding.project_id
    || state.tab.project.epoch !== state.binding.epoch || !state.binding.epoch) {
    throw new Error(t('desktop.tabs.error'))
  }
  return state
}

export interface DesktopShellController {
  state: ShallowRef<DesktopShellState | null>
  pending: Ref<readonly string[]>
  error: Ref<string | null>
  menuOpen: Ref<boolean>
  recent: Ref<RecentProject[]>
  listing: Ref<DirectoryListing | null>
  selected: Ref<PathInspection | null>
  picker: Ref<PickerStatus | null>
  busy: Ref<boolean>
  start(): Promise<void>
  dispose(): void
  activate(tabId: string): Promise<void>
  setMenuOpen(open: boolean): Promise<void>
  openProject(root: string, create?: boolean): Promise<void>
  refreshRecent(): Promise<void>
  inspect(path: string): Promise<void>
  browse(path: string): Promise<void>
  pick(): Promise<void>
  forgetRecent(root: string): Promise<void>
}

export function createDesktopShellController(bridge = window.voxariumTabs): DesktopShellController {
  const state = shallowRef<DesktopShellState | null>(null)
  const pending = ref<readonly string[]>([])
  const error = ref<string | null>(null)
  const menuOpen = ref(false)
  const recent = ref<RecentProject[]>([])
  const listing = ref<DirectoryListing | null>(null)
  const selected = ref<PathInspection | null>(null)
  const picker = ref<PickerStatus | null>(null)
  const busy = ref(false)
  let disposed = false, unsubscribe: (() => void) | undefined
  let selectionRevision = 0, operations = 0

  function accept(next: DesktopTabsState) {
    if (disposed) return
    if (next.role !== 'shell') throw new Error(t('desktop.tabs.error'))
    // Events may overtake an invoke reply; do not regress native ownership.
    if (state.value && (next.window.windowId !== state.value.window.windowId
      || next.window.revision < state.value.window.revision)) return
    const previousNativeError = state.value?.error
    state.value = next
    if (next.error) error.value = next.error
    else if (previousNativeError && error.value === previousNativeError) error.value = null
  }
  function client(): DesktopTabsBridge {
    if (!bridge || disposed) throw new Error(t('desktop.tabs.error'))
    return bridge
  }
  function request() {
    const current = state.value
    if (!current) throw new Error(t('desktop.tabs.error'))
    return { windowId: current.window.windowId, windowRevision: current.window.revision }
  }
  async function run(work: () => Promise<void>) {
    if (disposed) return
    operations += 1; busy.value = true; error.value = null
    try { await work() }
    catch (cause) {
      if (!disposed) {
        error.value = cause instanceof Error ? cause.message : String(cause)
        // Refresh revision after a stale assertion, without rebinding any project.
        try { if (state.value) accept(await client().getState()) } catch { /* keep original error */ }
      }
    } finally { operations -= 1; if (!disposed) busy.value = operations > 0 }
  }
  async function refreshRecent() {
    const result = await client().recentProjects()
    if (!disposed) recent.value = result
  }
  async function setMenuOpen(open: boolean) {
    await run(async () => {
      // Main reserves hit zones first. Never paint a menu underneath a native view.
      await client().setMenuOpen({ ...request(), open })
      if (!disposed) menuOpen.value = open
      accept(await client().getState())
      if (open) await refreshRecent()
    })
  }
  async function openProject(root: string, create = false) {
    if (pending.value.includes(root)) return
    await run(async () => {
      if (create && (!selected.value?.is_dir || selected.value.is_project || selected.value.path !== root)) {
        throw new Error(t('desktop.tabs.error'))
      }
      pending.value = [...pending.value, root]
      try {
        await client().openProject({ ...request(), root, create })
        accept(await client().getState())
        // Also on cross-window deduplication: hide this window's menu and reveal
        // its unchanged incumbent view, without stealing focus from the target.
        if (menuOpen.value) {
          await client().setMenuOpen({ ...request(), open: false })
          if (!disposed) menuOpen.value = false
          accept(await client().getState())
        }
      } finally { if (!disposed) pending.value = pending.value.filter(item => item !== root) }
    })
  }
  return {
    state, pending, error, menuOpen, recent, listing, selected, picker, busy,
    async start() {
      await run(async () => {
        if (!unsubscribe) unsubscribe = client().onStateChange(next => {
          try { accept(next) } catch (cause) { error.value = String(cause) }
        })
        accept(await client().bootstrap())
        await Promise.all([refreshRecent(), (async () => {
          const result = await client().pickerStatus()
          if (!disposed) picker.value = result
        })()])
      })
    },
    dispose() { disposed = true; selectionRevision += 1; unsubscribe?.(); unsubscribe = undefined },
    async activate(tabId) {
      await run(async () => {
        const tab = state.value?.tabs.find(item => item.tabId === tabId)
        if (!tab) return
        await client().activateTab({ ...request(), tabId, tabRevision: tab.revision })
        accept(await client().getState())
      })
    },
    setMenuOpen, openProject,
    refreshRecent: () => run(refreshRecent),
    async inspect(path) {
      const revision = ++selectionRevision
      selected.value = null
      await run(async () => {
        const result = await client().inspectPath(path)
        if (!disposed && revision === selectionRevision) selected.value = result
      })
    },
    async browse(path) {
      const revision = ++selectionRevision
      selected.value = null
      await run(async () => {
        const result = await client().browseDirectory(path)
        if (disposed || revision !== selectionRevision) return
        listing.value = result
        selected.value = result.path ? { path: result.path, name: result.name,
          exists: true, is_dir: true, is_project: result.is_project } : null
      })
    },
    async pick() {
      const revision = ++selectionRevision
      await run(async () => {
        const result = await client().pickDirectory(selected.value?.path)
        if (disposed || revision !== selectionRevision || result.cancelled) return
        if (!result.available) throw new Error(result.reason || t('project.open.dialogUnavailable'))
        selected.value = result.inspection ?? null
        if (selected.value?.is_project) await openProject(selected.value.path)
      })
    },
    forgetRecent: root => run(async () => { await client().forgetRecent(root); await refreshRecent() }),
  }
}
