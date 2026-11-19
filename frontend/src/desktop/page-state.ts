/** Project-relative page state and router restoration, dev spec §5.3.6.
 * This is not a draft snapshot or a transfer handler. Main requests restore only
 * for an explicitly resolved project; the existing router owns the navigation.
 */
import { nextTick, onScopeDispose, shallowRef, watch } from 'vue'
import { isNavigationFailure, NavigationFailureType, type Router } from 'vue-router'
import type { DesktopPageDescriptor, DesktopPageRestoreStatus, DesktopTabsBridge, DesktopTabsState } from '@/desktop'
import { rendererSession } from '@/session/project-session'

type Selector = 'path' | 'task' | 'chat' | 'assign'
const selectors: Selector[] = ['path', 'task', 'chat', 'assign']
const keysFor = (path: string): Selector[] => path === '/tasks' ? ['task', 'assign']
  : ['/explorer', '/documents'].includes(path) ? ['path']
    : ['/chats', '/producer'].includes(path) ? ['chat'] : []

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && [Object.prototype, null].includes(Object.getPrototypeOf(value))
}
function safePath(value: unknown, route: boolean): value is string {
  if (typeof value !== 'string' || !value || value.length > (route ? 2048 : 4096)) return false
  try {
    const decoded = decodeURIComponent(value)
    return !/[\\?#%:\u0000-\u001f\u007f\ud800-\udfff]/u.test(decoded)
      && !decoded.includes('//') && !decoded.split('/').some(part => part === '.' || part === '..')
      && (route ? decoded.startsWith('/') && !['p', 'api', 'ws'].includes(decoded.split('/')[1])
        : !decoded.startsWith('/'))
  } catch { return false }
}
function safeSelector(key: Selector, value: unknown): value is string {
  // Selectors are literal project paths, not URI components. Router query
  // encoding handles filenames containing '#' and '%' (same manifest schema).
  return key === 'path' ? typeof value === 'string' && value.length <= 4096
    && !/[\\?:\u0000-\u001f\u007f\ud800-\udfff]/u.test(value)
    && !value.startsWith('/') && !value.includes('//')
    && !value.split('/').some(part => part === '.' || part === '..')
    : typeof value === 'string' && value.length <= 256 && /^[A-Za-z0-9_.:-]+$/.test(value)
}

/** Strict on ingress; never silently turn malformed restore data into Explorer. */
export function checkedPageDescriptor(value: unknown): DesktopPageDescriptor | null {
  if (!object(value) || !safePath(value.path, true)
    || Object.keys(value).some(key => key !== 'path' && key !== 'query')) return null
  const query: DesktopPageDescriptor['query'] = {}
  if (Object.hasOwn(value, 'query')) {
    if (!object(value.query)) return null
    for (const [key, item] of Object.entries(value.query)) {
      if (!selectors.includes(key as Selector) || !keysFor(value.path).includes(key as Selector)
        || !safeSelector(key as Selector, item)) return null
      query[key as Selector] = item
    }
  }
  return { path: value.path, ...(Object.keys(query).length ? { query } : {}) }
}

export interface PageRestoreOptions {
  checkTarget(page: DesktopPageDescriptor): Promise<boolean>
  assertCurrent?(): void
  applyTarget?(page: DesktopPageDescriptor): Promise<boolean>
}

class StalePageRestoreError extends Error {}

type PageTarget = { path: () => string; apply: (page: DesktopPageDescriptor, assertCurrent: () => void) => Promise<boolean> }
const targets = new Set<PageTarget>()
const targetRevision = shallowRef(0)
/** An awaitable seam in the existing page, not a second navigation or editor.
 * Registration lives with the mounted view; transfer never touches its scope.
 */
export function useDesktopPageTarget(path: string | (() => string), apply: PageTarget['apply']): void {
  if (rendererSession.isLegacy) return
  const target = { path: typeof path === 'string' ? () => path : path, apply }
  targets.add(target); targetRevision.value += 1
  onScopeDispose(() => { targets.delete(target); targetRevision.value += 1 })
}

async function applyPageTarget(router: Router, page: DesktopPageDescriptor, assertCurrent: () => void): Promise<boolean> {
  // A fresh runtime's router can be ready before AppShell/session loading is.
  // Wait for that page's real loader rather than ACKing a preflight REST read.
  const guard = () => {
    assertCurrent()
    const route = router.currentRoute.value
    if (route.path !== page.path || keysFor(page.path).some(key =>
      typeof route.query[key] === 'string' && route.query[key] !== page.query?.[key])) throw new StalePageRestoreError()
  }
  const target = await new Promise<PageTarget>((resolve, reject) => {
    const check = () => {
      try {
        guard()
        const found = [...targets].find(item => item.path() === page.path)
        if (found) { cleanup(); resolve(found) }
      } catch (error) { cleanup(); reject(error) }
    }
    const stop = watch([targetRevision, () => router.currentRoute.value.path], check, { flush: 'post' })
    const timer = setTimeout(() => { cleanup(); reject(new Error('Page did not become ready')) }, 15000)
    const cleanup = () => { stop(); clearTimeout(timer) }
    check()
  })
  guard()
  return target.apply(page, guard)
}

/** Small per-renderer tracker. Only consumed selectors survive query scrubbing;
 * navigating to another page resets them. Hooks also track UI selection/close.
 */
export function createPageStateTracker(router: Router, changed: () => void = () => {}) {
  let path = '', retained: Partial<Record<Selector, string | null>> = {}, restoring = false, navigation = 0
  function snapshot(): DesktopPageDescriptor | null {
    const route = router.currentRoute.value
    if (route.path !== path) { path = route.path; retained = {} }
    const query: DesktopPageDescriptor['query'] = {}
    for (const key of keysFor(path)) {
      const value = route.query[key]
      if (safeSelector(key, value)) query[key] = value
    }
    for (const key of keysFor(path)) {
      if (retained[key] === null) delete query[key]
      else if (retained[key] !== undefined) query[key] = retained[key]!
    }
    return checkedPageDescriptor({ path, ...(Object.keys(query).length ? { query } : {}) })
  }
  const stop = router.afterEach((to, from, failure) => {
    if (!failure) {
      if (to.fullPath !== from.fullPath) navigation += 1
      // A new deep link wins over an old selection. Removing a consumed query
      // does not; selection hooks explicitly clear it when the UI closes.
      if (to.path === path) for (const key of keysFor(path)) {
        if (to.query[key] !== undefined && to.query[key] !== from.query[key]) delete retained[key]
      }
      snapshot(); if (!restoring) changed()
    }
  })
  return {
    snapshot,
    setSelector(page: string, key: Selector, value: string | null) {
      if (router.currentRoute.value.path !== page || !keysFor(page).includes(key)) return
      snapshot()
      if (value === null || safeSelector(key, value)) retained[key] = value
      if (!restoring) changed()
    },
    async restore(value: unknown, options: PageRestoreOptions): Promise<DesktopPageRestoreStatus> {
      const page = checkedPageDescriptor(value)
      if (!page) return 'invalid'
      const startedAt = navigation
      try {
        options.assertCurrent?.()
        if (!await options.checkTarget(page)) return 'unavailable'
        options.assertCurrent?.()
        if (navigation !== startedAt) return 'stale'
        const target = router.resolve(page)
        if (!target.matched.length || target.name === 'not-found' || target.matched.some(record => record.redirect)) return 'unavailable'
        restoring = true
        const result = await router.replace(page)
        options.assertCurrent?.()
        if (result && !isNavigationFailure(result, NavigationFailureType.duplicated)) return 'failed'
        await nextTick()
        if (router.currentRoute.value.path !== page.path) return 'stale'
        if (options.applyTarget && !await options.applyTarget(page)) return 'unavailable'
        options.assertCurrent?.()
        if (router.currentRoute.value.path !== page.path) return 'stale'
        snapshot()
        return 'restored'
      } catch (error) { return error instanceof StalePageRestoreError ? 'stale' : 'failed' }
      finally { restoring = false; changed() }
    },
    dispose: stop,
  }
}

let currentTracker: ReturnType<typeof createPageStateTracker> | undefined
/** No-op in a browser: legacy consume-query/reload behaviour stays unchanged. */
export function setDesktopPageSelector(path: string, key: Selector, value: string | null): void {
  currentTracker?.setSelector(path, key, value)
}

/** Installed only by the project router, never imported into the shell graph. */
export function connectDesktopPageState(router: Router, bridge: DesktopTabsBridge | undefined): void {
  if (rendererSession.isLegacy || !bridge?.reportPage || !bridge.onRestorePage || !bridge.acknowledgePageRestore) return
  let state: Extract<DesktopTabsState, { role: 'project' }> | undefined
  // A page report is the renderer's attestation of a SETTLED page (§5.3.6): main
  // releases its restore gate on the first one. Reporting during the initial
  // navigation would attest the transient '/' (the redirect record, never a real
  // page) and the settling navigation would then race the restore as "stale".
  let revision = 0, reporting = false, reportAgain = false, routerReady = false
  const tracker = createPageStateTracker(router, () => { void report() })
  currentTracker = tracker
  function accept(value: DesktopTabsState) {
    if (value.role !== 'project' || value.binding.project_id !== rendererSession.projectId) return
    if (state && value.tab.revision < state.tab.revision) return
    state = value
    // Transfer merely refreshes the IPC identity. It never calls restore/router.
    void report()
  }
  async function report() {
    reportAgain = true
    if (reporting || !state || !routerReady) return
    reporting = true
    try {
      while (reportAgain && state) {
        reportAgain = false
        const page = tracker.snapshot(), tab = state.tab
        if (page) await bridge!.reportPage!({ tabId: tab.tabId, tabRevision: tab.revision,
          transferGeneration: tab.transferGeneration, page })
      }
    } catch { /* A stale generation is retried by the next main state publication. */ }
    finally {
      reporting = false
      // A transfer/selection may overtake an invoke rejected for its old tuple.
      // Do not strand the newer snapshot waiting for an unrelated future event.
      if (reportAgain) void report()
    }
  }
  bridge.onStateChange(accept)
  bridge.onRestorePage(request => {
    void (async () => {
      const own = ++revision
      const matches = () => state?.tab.state === 'ready' && state.tab.tabId === request.tabId && state.tab.revision === request.tabRevision
        && state.tab.transferGeneration === request.transferGeneration && own === revision
      let status: DesktopPageRestoreStatus = 'stale'
      try {
        if (matches()) {
          const assertSession = rendererSession.capture()
          status = await restoreTarget(router, tracker, request.page, () => {
            assertSession()
            if (!matches()) throw new StalePageRestoreError()
          })
          if (!matches()) status = 'stale'
        }
      } catch { status = matches() ? 'failed' : 'stale' }
      await bridge.acknowledgePageRestore!({ requestId: request.requestId, tabId: request.tabId,
        tabRevision: request.tabRevision, transferGeneration: request.transferGeneration, status }).catch(() => {})
    })()
  })
  void router.isReady().then(async () => {
    // The initial navigation has settled: reports may now attest a real page.
    routerReady = true
    accept(await bridge.bootstrap())
    // Chats has no URL-driven selection of its own. Keep only the selected
    // durable conversation, never the popup state, draft or attached Files.
    const [{ useChatStore }, { useDocsStore }] = await Promise.all([import('@/stores/chat'), import('@/stores/docs')])
    const chat = useChatStore()
    watch([() => router.currentRoute.value.path, () => chat.view, () => chat.activeChatId], () => {
      const route = router.currentRoute.value, path = route.path
      // Do not replace an incoming target with yesterday's popup selection while
      // its transcript is still loading. Restore consumes the Chats query below.
      if (route.query.chat && route.query.chat !== chat.activeChatId) return
      if (path === '/chats' || path === '/producer') tracker.setSelector(path, 'chat', chat.view === 'chat' ? chat.activeChatId : null)
    }, { immediate: true })
    const docs = useDocsStore()
    watch([() => router.currentRoute.value.path, () => docs.open?.path], () => {
      // Returning to Documents may show its resident selection without a query.
      if (!router.currentRoute.value.query.path) tracker.setSelector('/documents', 'path', docs.open?.path ?? null)
    }, { immediate: true })
  }).catch(() => {})
}

async function restoreTarget(router: Router, tracker: ReturnType<typeof createPageStateTracker>,
  value: unknown, assertCurrent: () => void): Promise<DesktopPageRestoreStatus> {
  const { api, ApiError } = await import('@/api/client')
  let teamId: string | undefined
  return tracker.restore(value, {
    assertCurrent,
    async checkTarget(page) {
      // Bootstrap/check this immutable scoped runtime, never the legacy selection.
      await api.project()
      assertCurrent()
      const query = page.query ?? {}
      try {
        if (page.path === '/documents' && query.path) await api.doc(query.path)
        if (page.path === '/explorer' && query.path) {
          const parent = query.path.split('/').slice(0, -1).join('/')
          if (!(await api.tree(parent)).entries.some(entry => entry.path === query.path)) return false
        }
        if (page.path.startsWith('/pipelines/')) await api.pipeline(decodeURIComponent(page.path.slice('/pipelines/'.length)))
        if (query.chat) {
          const chat = await api.chat(query.chat)
          if (page.path === '/producer' && !(await api.teams()).some(team => team.members.some(member =>
            member.id === chat.member_id && member.builtin === 'producer'))) return false
        }
        if (page.path === '/tasks' && (query.task || query.assign)) {
          const teams = await api.teams()
          if (query.task) {
            for (const team of teams) {
              if ((await api.tasks(team.id)).some(task => task.id === query.task)) { teamId = team.id; break }
            }
            if (!teamId) return false
          }
          if (query.assign) {
            const owner = teams.find(team => team.members.some(member => member.id === query.assign))
            if (!owner || (teamId && teamId !== owner.id)) return false
            teamId = owner.id
          }
          const { useTeamsStore } = await import('@/stores/teams')
          assertCurrent()
          const store = useTeamsStore()
          await store.load()
          assertCurrent()
          store.activeTeamId = teamId!
          await store.refreshTeam()
        }
        // Plugin registration is authoritative, and may not have finished on a
        // fresh renderer. Missing/disabled pages return unavailable, not Explorer.
        if (router.resolve(page).name === 'not-found') {
          const { pluginLoader } = await import('@/plugin-runtime/loader')
          await pluginLoader().loadPages()
        }
        return true
      } catch (error) {
        if (error instanceof ApiError && [400, 404, 410].includes(error.status)) return false
        throw error
      }
    },
    async applyTarget(page) {
      if (['/explorer', '/tasks', '/documents', '/producer', '/chats'].includes(page.path) || page.path.startsWith('/pipelines/')) {
        return applyPageTarget(router, page, assertCurrent)
      }
      return true
    },
  })
}
