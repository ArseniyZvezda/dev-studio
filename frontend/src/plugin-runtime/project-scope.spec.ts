// @vitest-environment jsdom
/** Real loader/runtime regressions for immutable project renderers (§5.3.2, §9.3). */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia } from 'pinia'
import { computed, createApp, defineComponent, h, nextTick, onBeforeUnmount, shallowRef, type Component } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'

import { setToken } from '@/api/auth'
import { api } from '@/api/client'
import type { LoadedPlugin, PluginPage, SyntaxGrammar } from '@/api/types'
import { t } from '@/i18n'
import { ProjectSession } from '@/session/project-session'
import { useSessionStore } from '@/stores/session'
import { WsConnection } from '@/ws/connection'
import { createPluginHost, createRuntime, pluginResourceUrl, rendererPluginHost, type PluginHost, type PluginRuntime } from './index'
import { pluginComponent, pluginLoader, PluginLoader, type PluginModule } from './loader'
import { provideNodeParamWriter } from './node-params'

const PLUGIN = 'same-plugin'
const VERSION = 'same-version'
const hosts: PluginHost[] = []
const mountedRenderers: (() => void)[] = []
const requests: { url: URL; init: RequestInit }[] = []
const pages = new Map<string, PluginPage[]>()
let legacyProject = 'A'
let respond: (url: URL, init: RequestInit) => unknown

vi.mock('@/stores/search', () => ({ useSearchStore: () => ({ available: false, open: false }) }))
vi.mock('@/components/chat/ChatWidget.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/layout/SideNav.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/layout/StudioStatusBar.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/layout/SearchPalette.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/layout/NotificationToasts.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/layout/DesktopTitleBar.vue', () => ({ default: { render: () => null } }))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function host(id: string, epoch = `epoch-${id}`): PluginHost {
  const session = new ProjectSession(id)
  session.bind({ project_id: id, epoch })
  const router = createRouter({ history: createMemoryHistory(`/p/${id}`), routes: [
    { path: '/', name: 'home', component: { render: () => null } },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: { render: () => null } },
  ] })
  const result = createPluginHost(session, { router })
  hosts.push(result)
  return result
}

function moduleUrl(id: string, source = 'page.js', plugin = PLUGIN): string {
  return `/api/projects/${id}/plugins/${plugin}/assets/${source}?v=${VERSION}&project_id=${id}&epoch=epoch-${id}`
}

function page(id: string, route = '/same-page', source = 'page.js'): PluginPage {
  return { id: 'same-page', plugin_id: PLUGIN, title: `Page ${id}`, route, icon: null, component_url: moduleUrl(id, source) }
}

// Exercise Vue's actual defineAsyncComponent loader, without mounting a layout or
// pretending that a mocked network import proves which runtime the factory receives.
function load(component: Component): Promise<Component> {
  return (component as { __asyncLoader: () => Promise<Component> }).__asyncLoader()
}

function factory(label: string, capture: (runtime: PluginRuntime) => void = () => {}): PluginModule {
  return { default: (runtime: PluginRuntime) => { capture(runtime); return { name: label, render: () => label } } }
}

function assertScoped(raw: string, id: string, path: string, token = false): URL {
  const url = new URL(raw, window.location.origin)
  expect(url.pathname).toBe(`/api/projects/${id}${path}`)
  expect(url.searchParams.get('project_id')).toBe(id)
  expect(url.searchParams.get('epoch')).toBe(`epoch-${id}`)
  expect(url.searchParams.get('token')).toBe(token ? 'secret /&' : null)
  return url
}

class Socket {
  static OPEN = 1
  static all: Socket[] = []
  readyState = 0
  onmessage: ((event: { data: string }) => void) | null = null
  constructor(readonly url: string) { Socket.all.push(this) }
  close() { this.readyState = 3 }
  deliver(session: ProjectSession, seq: number, extra: object = {}) {
    this.onmessage?.({ data: JSON.stringify({ type: 'team.doc.changed', data: { path: 'same.md' }, seq, ...session.scope, ...extra }) })
  }
}

/** Mount the real RouterView and acquire the same loader lifetime as App.vue. */
function mountRenderer(owner: PluginHost, loader: PluginLoader, view: Component = RouterView) {
  const container = document.createElement('div')
  document.body.append(container)
  const app = createApp(defineComponent({
    setup() {
      onBeforeUnmount(loader.start())
      return () => h('div', [
        h('output', { class: 'loader-state' }, loader.state.loading ? 'loading' : (loader.state.error ?? 'ready')),
        h(view),
      ])
    },
  })).use(owner.router)
  app.mount(container)
  let mounted = true
  const unmount = () => {
    if (!mounted) return
    mounted = false
    app.unmount()
    container.remove()
  }
  mountedRenderers.push(unmount)
  return { container, unmount }
}

function loadedPlugin(enabled = true): LoadedPlugin {
  return { id: PLUGIN, name: 'Same plugin', version: VERSION, author: '', description: '', scope: 'project',
    enabled, status: enabled ? 'loaded' : 'disabled', error: null, node_types: [], pages: [], previewers: [],
    grammars: [], hooks: [], actions: [], search_providers: [], automation_actions: [], has_backend: false,
    has_venv: false, env: null, root: 'D:\\plugin' }
}

function registryEvent(socket: Socket, owner: PluginHost, seq: number, type: 'plugin.changed' | 'system.dropped', plugins = [loadedPlugin()]) {
  socket.deliver(owner.session, seq, { type, data: type === 'plugin.changed'
    ? { plugins, node_type_count: 0 }
    : { count: 1, resync: true, reason: 'expired' } })
}

/** Real App + singleton host; only session bootstrapping is shortened, not its socket. */
async function mountApplication(view: Component, duringLoad?: (owner: PluginHost) => void) {
  const { default: App } = await import('@/App.vue')
  const pinia = createPinia()
  const session = useSessionStore(pinia)
  const owner = rendererPluginHost
  hosts.push(owner)
  session.loading = false
  const loadSession = vi.spyOn(session, 'load').mockImplementation(async () => {
    owner.events.connect()
    duringLoad?.(owner)
    session.loading = false
  })
  const removeRoute = owner.router.addRoute({ path: '/__vs40-mounted', name: Symbol('vs40-mounted'), component: view })
  await owner.router.push('/__vs40-mounted')
  const container = document.createElement('div')
  document.body.append(container)
  const app = createApp(App).use(pinia).use(owner.router)
  app.mount(container)
  let mounted = true
  const unmount = () => {
    if (!mounted) return
    mounted = false
    app.unmount()
    removeRoute()
    container.remove()
  }
  mountedRenderers.push(unmount)
  return { container, unmount, owner, loader: pluginLoader(owner), loadSession }
}

beforeEach(() => {
  pages.clear()
  requests.length = 0
  Socket.all = []
  legacyProject = 'A'
  setToken('secret /&')
  respond = (url, init) => {
    if (url.pathname === '/api/project/open') {
      legacyProject = 'B'
      return { path: 'D:\\B' }
    }
    const id = /^\/api\/projects\/([^/]+)\//.exec(url.pathname)?.[1] ?? legacyProject
    if (url.pathname.endsWith('/plugins')) return [loadedPlugin()]
    if (url.pathname.endsWith('/plugins/pages')) return pages.get(id) ?? []
    if (url.pathname.endsWith('/settings')) return { id: PLUGIN, settings: { project: id, method: init.method } }
    if (url.pathname.endsWith('/explorer/raw')) return new Response(new Uint8Array([id === 'A' ? 65 : 66]))
    return { project: id }
  }
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init: RequestInit = {}) => {
    const url = new URL(String(input), window.location.origin)
    requests.push({ url, init })
    const result = await respond(url, init)
    return result instanceof Response ? result : new Response(JSON.stringify(result), { headers: { 'Content-Type': 'application/json' } })
  }))
  vi.stubGlobal('WebSocket', Socket)
})

afterEach(() => {
  mountedRenderers.splice(0).forEach(unmount => unmount())
  hosts.splice(0).forEach(value => value.events.disconnect())
  setToken(null)
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('project-bound plugin module factories', () => {
  it('finishes A after B with the same IDs/version, keeping every retained operation in A after legacy open B', async () => {
    const a = host('A'); const b = host('B')
    const pendingA = deferred<PluginModule>()
    let runtimeA!: PluginRuntime; let runtimeB!: PluginRuntime
    const importA = vi.fn((_url: string) => pendingA.promise)
    const importB = vi.fn(async (_url: string) => factory('CodeFromB', runtime => { runtimeB = runtime }))
    const loaderA = new PluginLoader(a, importA); const loaderB = new PluginLoader(b, importB)
    pages.set('A', [page('A')]); pages.set('B', [page('B')])
    await Promise.all([loaderA.loadPages(), loaderB.loadPages()])
    const completionA = load(loaderA.component(moduleUrl('A'), PLUGIN))
    expect((await load(loaderB.component(moduleUrl('B'), PLUGIN))) as object).toMatchObject({ name: 'CodeFromB' })
    await api.openProject('D:\\B')
    expect(legacyProject).toBe('B')
    pendingA.resolve(factory('CodeFromA', runtime => { runtimeA = runtime }))
    expect(await completionA).toMatchObject({ name: 'CodeFromA' })
    assertScoped(importA.mock.calls[0]![0]!, 'A', `/plugins/${PLUGIN}/assets/page.js`, true)
    assertScoped(importB.mock.calls[0]![0]!, 'B', `/plugins/${PLUGIN}/assets/page.js`, true)
    expect(runtimeA.router.currentRoute).toBe(a.router.currentRoute)
    expect(runtimeB.router.currentRoute).toBe(b.router.currentRoute)
    expect(runtimeA.router.resolve('/same-page').href).toBe('/p/A/same-page')
    expect(runtimeB.router.resolve('/same-page').href).toBe('/p/B/same-page')

    expect(await runtimeA.settings.get()).toMatchObject({ project: 'A' })
    await runtimeA.settings.save({ colour: 'red' })
    expect(await runtimeA.action('same-action', { same: 'payload' })).toEqual({ project: 'A' })
    expect(await runtimeA.api.pluginSettings(PLUGIN)).toMatchObject({ settings: { project: 'A' } })
    expect([...new Uint8Array(await runtimeA.rawFileBytes('same.bin'))]).toEqual([65])
    const raw = assertScoped(runtimeA.rawFileUrl('folder/same.bin'), 'A', '/explorer/raw', true)
    expect(raw.searchParams.get('path')).toBe('folder/same.bin')
    assertScoped(runtimeA.apiUrl(`/plugins/${PLUGIN}/settings`, { answer: 42 }), 'A', `/plugins/${PLUGIN}/settings`)
    const scopedCalls = requests.filter(({ url }) => url.pathname !== '/api/project/open')
    expect(scopedCalls.every(({ url }) => url.pathname.startsWith('/api/projects/A/') || url.pathname.startsWith('/api/projects/B/'))).toBe(true)
    const saved = requests.find(({ init }) => init.method === 'PUT')!
    assertScoped(saved.url.href, 'A', `/plugins/${PLUGIN}/settings`)
    expect(JSON.parse(saved.init.body as string)).toEqual({ settings: { colour: 'red' } })
    expect(new Headers(saved.init.headers).get('Authorization')).toBe('Bearer secret /&')
    expect(new Headers(requests.find(({ url }) => url.pathname.endsWith('/explorer/raw'))!.init.headers).get('Authorization')).toBe('Bearer secret /&')
  })

  it('caches only the same owner, source and host; nodes and pages may share one module', async () => {
    const a = host('A'); const b = host('B')
    const importer = vi.fn(async (url: string) => factory(url))
    const la = new PluginLoader(a, importer); const lb = new PluginLoader(b, importer)
    const first = la.component(moduleUrl('A'), PLUGIN)
    expect(la.component(moduleUrl('A'), PLUGIN, 'Custom node same-node')).toBe(first)
    const changed = la.component(moduleUrl('A', 'replacement.js'), PLUGIN)
    const versioned = la.component(moduleUrl('A').replace(VERSION, 'next-version'), PLUGIN)
    const otherOwner = la.component(moduleUrl('A', 'page.js', 'other-plugin'), 'other-plugin')
    const otherProject = lb.component(moduleUrl('B'), PLUGIN)
    expect(new Set([first, changed, versioned, otherOwner, otherProject]).size).toBe(5)
    await Promise.all([first, changed, versioned, otherOwner, otherProject].map(load))
    expect(importer).toHaveBeenCalledTimes(5)
    la.reset()
    const replacement = la.component(moduleUrl('A'), PLUGIN)
    expect(replacement).not.toBe(first)
    await load(replacement)
    expect(importer).toHaveBeenCalledTimes(6)
    // Reset must defeat the browser's native module cache too, not just Vue's map,
    // even if the backend source stamp is unchanged.
    const initialUrl = new URL(importer.mock.calls[0]![0], window.location.origin)
    const replacementUrl = new URL(importer.mock.calls[5]![0], window.location.origin)
    expect(replacementUrl.pathname).toBe(initialUrl.pathname)
    expect(replacementUrl.searchParams.get('v')).toBe(initialUrl.searchParams.get('v'))
    expect(replacementUrl.href).not.toBe(initialUrl.href)
    expect(lb.component(moduleUrl('B'), PLUGIN)).toBe(otherProject)
  })

  it.each(['reset', 'invalidate'] as const)('does not invoke a late import factory after %s', async operation => {
    const a = host('A'); const pending = deferred<PluginModule>()
    const importer = vi.fn(() => pending.promise)
    const loader = new PluginLoader(a, importer)
    const completion = load(loader.component(moduleUrl('A'), PLUGIN))
    expect(importer).toHaveBeenCalledTimes(1)
    if (operation === 'reset') loader.reset()
    else a.session.invalidate()
    const setup = vi.fn(() => ({ name: 'MustNeverRun' }))
    const rejected = expect(completion).rejects.toThrow(operation === 'reset' ? /replaced/ : /no longer current/)
    pending.resolve({ setup })
    await rejected
    expect(setup).not.toHaveBeenCalled()
  })

  it.each(['reset', 'invalidate'] as const)('rejects retained mutation callbacks after %s before sending or writing', async operation => {
    const a = host('A'); const b = host('B')
    let old!: PluginRuntime
    const writeA = vi.fn(); const writeB = vi.fn()
    const withdrawA = provideNodeParamWriter(writeA, a.session)
    const withdrawB = provideNodeParamWriter(writeB, b.session)
    const loader = new PluginLoader(a, async () => factory('Old', runtime => { old = runtime }))
    await load(loader.component(moduleUrl('A'), PLUGIN))
    old.setNodeParams('same-node', { before: true })
    expect(writeA).toHaveBeenCalledTimes(1)
    if (operation === 'reset') loader.reset()
    else a.session.invalidate()
    const before = requests.length
    expect(() => old.setNodeParams('same-node', { after: true })).toThrow()
    expect(() => old.api.savePluginSettings(PLUGIN, { after: true })).toThrow()
    expect(() => old.action('write', {})).toThrow()
    await expect(old.settings.save({ after: true })).rejects.toThrow()
    expect(() => old.rawFileUrl('same.bin')).toThrow()
    expect(() => old.assetUrl('same.css')).toThrow()
    expect(() => old.router.push('/same-page')).toThrow()
    expect(() => old.router.replace('/same-page')).toThrow()
    expect(requests).toHaveLength(before)
    expect(writeA).toHaveBeenCalledTimes(1); expect(writeB).not.toHaveBeenCalled()
    withdrawA(); withdrawB()
    await expect(createRuntime(PLUGIN, b).settings.save({ still: 'active' })).resolves.toBeUndefined()
  })
})

describe('page registry ownership', () => {
  it('updates, removes and navigates A without touching same-id routes, nav or components in B', async () => {
    const a = host('A'); const b = host('B')
    const la = new PluginLoader(a, async () => factory('A'))
    const lb = new PluginLoader(b, async () => factory('B'))
    pages.set('A', [page('A')]); pages.set('B', [page('B')])
    await Promise.all([la.loadPages(), lb.loadPages()])
    await Promise.all([a.router.push('/same-page'), b.router.push('/same-page')])
    const routeB = b.router.getRoutes().find(route => route.meta.pluginId === PLUGIN)!
    const componentB = lb.component(moduleUrl('B'), PLUGIN)
    pages.set('A', [page('A', '/updated-page', 'updated.js')])
    await la.loadPages()
    expect(a.router.resolve('/same-page').name).toBe('not-found')
    expect(a.router.resolve('/updated-page').meta.pluginId).toBe(PLUGIN)
    expect(la.navItems.map(item => item.route)).toEqual(['/updated-page'])
    pages.set('A', [])
    await la.loadPages()
    expect(a.router.resolve('/updated-page').name).toBe('not-found')
    expect(la.navItems).toEqual([])
    expect(b.router.getRoutes().find(route => route.meta.pluginId === PLUGIN)).toBe(routeB)
    expect(b.router.currentRoute.value.fullPath).toBe('/same-page')
    expect(lb.navItems).toEqual([page('B')])
    expect(lb.component(moduleUrl('B'), PLUGIN)).toBe(componentB)
  })

  it('does not revive pages from a registry request overtaken by reset or a newer list', async () => {
    const a = host('A'); const loader = new PluginLoader(a)
    const pending = deferred<unknown>()
    respond = url => url.pathname.endsWith('/plugins') ? [loadedPlugin()] : pending.promise
    const first = loader.loadPages()
    loader.reset()
    respond = url => url.pathname.endsWith('/plugins') ? [loadedPlugin()] : [page('A', '/new-page')]
    await loader.loadPages()
    pending.resolve([page('A', '/old-page')])
    await first
    expect(loader.navItems.map(item => item.route)).toEqual(['/new-page'])
    expect(a.router.resolve('/old-page').name).toBe('not-found')
  })

  it('refuses an unscoped page list without withdrawing an existing valid registration', async () => {
    const a = host('A'); const loader = new PluginLoader(a)
    pages.set('A', [page('A')]); await loader.loadPages()
    const route = a.router.getRoutes().find(item => item.meta.pluginId === PLUGIN)
    pages.set('A', [{ ...page('A'), component_url: `/api/plugins/${PLUGIN}/assets/page.js?v=${VERSION}` }])
    await expect(loader.loadPages()).rejects.toThrow(/Unscoped legacy or foreign/)
    expect(loader.state.loading).toBe(false)
    expect(loader.state.error).toMatch(/Unscoped legacy or foreign/)
    expect(a.router.getRoutes().find(item => item.meta.pluginId === PLUGIN)).toBe(route)
    expect(loader.navItems).toEqual([page('A')])
  })
})

describe('scoped event-driven plugin lifecycle in mounted renderers', () => {
  it('forwards plugin props, attrs, listeners and slots without reviving a retained tree after reset', async () => {
    const a = host('A')
    const path = shallowRef('first.bin'); const changed = vi.fn(); const unmounted = vi.fn()
    const loader = new PluginLoader(a, async () => ({ default: (runtime: PluginRuntime) => defineComponent({
      props: { path: { type: String, required: true } },
      emits: ['change'],
      setup(props, { emit, slots }) {
        onBeforeUnmount(unmounted)
        return () => h('button', {
          'data-url': runtime.rawFileUrl(props.path),
          onClick: () => emit('change', props.path),
        }, slots.default?.())
      },
    }) }))
    const retained = loader.component(moduleUrl('A', 'previewer.js'), PLUGIN)
    await a.router.push('/')
    const view = mountRenderer(a, loader, defineComponent({ setup: () => () =>
      h(retained, { path: path.value, class: 'forwarded-preview', onChange: changed },
        { default: () => `slot:${path.value}` }),
    }))
    await loader.loadPages()
    await vi.waitFor(() => expect(view.container.querySelector('button')?.textContent).toBe('slot:first.bin'))
    const button = view.container.querySelector('button')!
    expect(button.className).toBe('forwarded-preview')
    path.value = 'second.bin'
    await nextTick()
    expect(view.container.querySelector('button')).toBe(button)
    expect(button.textContent).toBe('slot:second.bin')
    expect(assertScoped(button.dataset.url!, 'A', '/explorer/raw', true).searchParams.get('path')).toBe('second.bin')
    button.click()
    expect(changed).toHaveBeenCalledExactlyOnceWith('second.bin')
    // reset() withdraws and re-enables registration in one synchronous turn. An
    // invalidated-only gate would mistakenly render this retained old runtime.
    path.value = 'queued-before-reset.bin'
    loader.reset()
    await nextTick()
    expect(view.container.querySelector('button')).toBeNull()
    expect(unmounted).toHaveBeenCalledTimes(1)
    await loader.loadPages()
    path.value = 'after-recovery.bin'
    await nextTick()
    expect(view.container.querySelector('button')).toBeNull()
    expect(unmounted).toHaveBeenCalledTimes(1)
  })

  it.each(['reload', 'held inventory', 'repeated gap', 'failed refresh/retry', 'disable'] as const)(
    'withdraws a mounted URL-helper page without render errors during %s', async scenario => {
      // Observe, but never swallow, Vue's render warnings/errors. Async-component
      // rejections and unhandled scheduler failures must still fail this regression.
      const errors = vi.spyOn(console, 'error'); const warnings = vi.spyOn(console, 'warn')
      const noRenderErrors = () => {
        expect(errors).not.toHaveBeenCalled()
        // Vue Router diagnoses defineAsyncComponent route records in development;
        // that pre-existing advisory is not a render failure and remains visible.
        expect(warnings.mock.calls.filter(([message]) => !String(message).includes('[VUE_ROUTER_R0029]'))).toEqual([])
      }
      const a = host('A'); const b = host('B')
      const writeA = vi.fn(); const writeB = vi.fn()
      const stopA = provideNodeParamWriter(writeA, a.session)
      const stopB = provideNodeParamWriter(writeB, b.session)
      const pulse = shallowRef(0)
      const runtimes = { A: [] as PluginRuntime[], B: [] as PluginRuntime[] }
      const nodes = new Map<string, PluginRuntime>()
      let revision = 'old'
      const importer = vi.fn(async (url: string): Promise<PluginModule> => {
        const id = url.includes('/projects/A/') ? 'A' : 'B'
        const label = `${id}-${id === 'A' ? revision : 'stable'}`
        return { default: (runtime: PluginRuntime) => {
          if (url.includes('node.js')) {
            nodes.set(id, runtime)
            return { render: () => null }
          }
          runtimes[id].push(runtime)
          return defineComponent({ setup() {
            return () => h('section', { class: 'url-helper-page', 'data-pulse': pulse.value }, [
              h('span', label),
              // Real plugin pages build image/download URLs DURING render, not
              // once in setup. A revoked page must not get another unsafe render.
              h('img', { src: runtime.assetUrl('icon.svg') }),
              h('a', { href: runtime.rawFileUrl('same.bin') }, 'download'),
            ])
          } })
        } }
      })
      const la = new PluginLoader(a, importer); const lb = new PluginLoader(b, importer)
      pages.set('A', [page('A')]); pages.set('B', [page('B')])
      await Promise.all([a.router.push('/same-page'), b.router.push('/same-page')])
      const va = mountRenderer(a, la); const vb = mountRenderer(b, lb)
      await Promise.all([la.loadPages(), lb.loadPages()])
      await vi.waitFor(() => {
        expect(va.container.textContent).toContain('A-old')
        expect(vb.container.textContent).toContain('B-stable')
      })
      await Promise.all([
        load(la.component(moduleUrl('A', 'node.js'), PLUGIN)),
        load(lb.component(moduleUrl('B', 'node.js'), PLUGIN)),
      ])
      const old = runtimes.A[0]!; const stable = runtimes.B[0]!
      const nodeA = nodes.get('A')!; const nodeB = nodes.get('B')!
      nodeA.setNodeParams('same-node', { before: true })
      expect(writeA).toHaveBeenCalledExactlyOnceWith('same-node', { before: true })
      writeA.mockClear()
      const seenA = vi.fn(); const seenB = vi.fn()
      old.onEvent('team.doc.changed', seenA); stable.onEvent('team.doc.changed', seenB)
      const routeB = b.router.getRoutes().find(route => route.meta.pluginId === PLUGIN)
      const componentB = lb.component(moduleUrl('B'), PLUGIN)
      const elementB = vb.container.querySelector('.url-helper-page')
      const routeBeforeB = b.router.currentRoute.value
      const readsB = requests.filter(({ url }) => url.pathname.startsWith('/api/projects/B/')).length
      a.events.connect(); b.events.connect()
      const [sa, sb] = Socket.all as [Socket, Socket]
      const inventories = [deferred<unknown>(), deferred<unknown>()]
      const lists = [deferred<unknown>(), deferred<unknown>()]
      let inventoryReads = 0; let pageReads = 0
      const fallback = respond
      respond = (url, init) => {
        if (url.pathname === '/api/projects/A/plugins') return inventories[inventoryReads++]!.promise
        if (url.pathname === '/api/projects/A/plugins/pages') return lists[pageReads++]!.promise
        return fallback(url, init)
      }
      const noStaleWrites = async () => {
        const writes = () => requests.filter(({ init }) => !['GET', 'HEAD'].includes(init.method ?? 'GET'))
        const count = writes().length
        expect(() => old.api.savePluginSettings(PLUGIN, { stale: true })).toThrow(/replaced/)
        expect(() => old.action('write', { stale: true })).toThrow(/replaced/)
        expect(() => nodeA.setNodeParams('same-node', { stale: true })).toThrow(/replaced/)
        expect(() => old.onEvent('team.doc.changed', seenA)).toThrow(/replaced/)
        await expect(old.settings.save({ stale: true })).rejects.toThrow(/replaced/)
        // The event's legitimate inventory GETs may start during this await.
        expect(writes()).toHaveLength(count)
        expect(writeA).not.toHaveBeenCalled(); expect(writeB).not.toHaveBeenCalled()
      }
      const withdrawn = async () => {
        // Also queue a normal reactive plugin render while REST is held. This
        // exposes a stale RouterView record even when removeRoute() already ran.
        pulse.value += 1
        await nextTick()
        expect(va.container.querySelector('.url-helper-page')).toBeNull()
        expect(la.navItems).toEqual([])
        expect(a.router.resolve('/same-page').name).toBe('not-found')
        noRenderErrors()
      }
      revision = 'fresh'
      const changed = scenario === 'reload' || scenario === 'disable'
      registryEvent(sa, a, 1, changed ? 'plugin.changed' : 'system.dropped')
      // These checks run before REST or a Vue tick: retained capabilities must
      // already be revoked, not merely hidden behind a pending route transition.
      const immediateChecks = noStaleWrites()
      sa.deliver(a.session, 2); sb.deliver(b.session, 1)
      expect(seenA).not.toHaveBeenCalled(); expect(seenB).toHaveBeenCalledTimes(1)
      await immediateChecks
      await vi.waitFor(() => expect(pageReads).toBe(1))
      expect(inventoryReads).toBe(changed ? 0 : 1)
      await withdrawn()
      // A direct attempt to reopen the withdrawn route cannot revive its old
      // component while either authoritative inventory endpoint is pending.
      await a.router.push('/same-page?during=refresh')
      await withdrawn()
      let attempt = 0
      if (scenario === 'repeated gap') {
        registryEvent(sa, a, 3, 'system.dropped')
        inventories[0]!.resolve([loadedPlugin()])
        lists[0]!.resolve([page('A', '/overtaken-page', 'overtaken.js')])
        await vi.waitFor(() => { expect(pageReads).toBe(2); expect(inventoryReads).toBe(2) })
        await noStaleWrites()
        await withdrawn()
        expect(a.router.resolve('/overtaken-page').name).toBe('not-found')
        expect(importer.mock.calls.some(([url]) => url.includes('overtaken.js'))).toBe(false)
        attempt = 1
      } else if (scenario === 'disable') {
        // The enabled response began before disable. It must not resurrect the
        // page when it finally arrives, even though its module stamp is equal.
        registryEvent(sa, a, 3, 'plugin.changed', [loadedPlugin(false)])
        lists[0]!.resolve([page('A')])
        await vi.waitFor(() => expect(pageReads).toBe(2))
        expect(inventoryReads).toBe(0)
        await noStaleWrites()
        await withdrawn()
        expect(runtimes.A).toHaveLength(1)
        attempt = 1
      } else if (scenario === 'failed refresh/retry') {
        const rejected = expect(la.waitForRefresh()).rejects.toThrow()
        inventories[0]!.resolve([loadedPlugin()])
        lists[0]!.resolve(new Response(JSON.stringify({ detail: 'held inventory failed' }), { status: 503 }))
        await rejected
        expect(la.state.loading).toBe(false)
        expect(la.state.error).toBeTruthy()
        await noStaleWrites()
        await withdrawn()
        expect(va.container.querySelector('.loader-state')?.textContent).toBe(la.state.error)
        // Public explicit retry, without another event or private state reset.
        const retry = la.loadPages()
        await vi.waitFor(() => { expect(pageReads).toBe(2); expect(inventoryReads).toBe(2) })
        await withdrawn()
        inventories[1]!.resolve([loadedPlugin()]); lists[1]!.resolve([page('A')])
        await retry
        attempt = 1
      }
      if (scenario !== 'failed refresh/retry') {
        inventories[attempt]!.resolve([loadedPlugin()])
        lists[attempt]!.resolve(scenario === 'disable' ? [] : [page('A')])
        await la.waitForRefresh()
      }
      await vi.waitFor(() => {
        expect(la.state).toMatchObject({ loading: false, error: null })
        if (scenario === 'disable') {
          expect(va.container.querySelector('.url-helper-page')).toBeNull()
          expect(a.router.currentRoute.value.name).toBe('not-found')
          expect(la.navItems).toEqual([])
        } else {
          expect(va.container.textContent).toContain('A-fresh')
          expect(runtimes.A).toHaveLength(2)
          assertScoped(va.container.querySelector('img')!.getAttribute('src')!, 'A', `/plugins/${PLUGIN}/assets/icon.svg`, true)
          assertScoped(va.container.querySelector('a')!.getAttribute('href')!, 'A', '/explorer/raw', true)
        }
      })
      expect(va.container.textContent).not.toContain('A-old')
      await noStaleWrites()
      expect(b.router.currentRoute.value).toBe(routeBeforeB)
      expect(b.router.getRoutes().find(route => route.meta.pluginId === PLUGIN)).toBe(routeB)
      expect(lb.component(moduleUrl('B'), PLUGIN)).toBe(componentB)
      expect(lb.navItems).toEqual([page('B')])
      expect(vb.container.querySelector('.url-helper-page')).toBe(elementB)
      expect(runtimes.B).toEqual([stable])
      expect(requests.filter(({ url }) => url.pathname.startsWith('/api/projects/B/'))).toHaveLength(readsB)
      await expect(stable.settings.save({ still: 'B' })).resolves.toBeUndefined()
      nodeB.setNodeParams('same-node', { still: 'B' })
      expect(writeB).toHaveBeenCalledExactlyOnceWith('same-node', { still: 'B' })
      noRenderErrors()
      stopA(); stopB()
    },
  )

  it.each(['plugin.changed', 'system.dropped'] as const)('reloads and disables only mounted A through %s with equal IDs/stamps', async eventType => {
    const { highlightFile } = await import('@/lib/highlight/highlighter')
    const { pluginGrammars, pluginGrammarGeneration } = await import('@/lib/highlight/grammars')
    const a = host('A'); const b = host('B')
    let revisionA = 'old'; let enabledA = true
    const writeA = vi.fn(); const writeB = vi.fn()
    const withdrawA = provideNodeParamWriter(writeA, a.session)
    const withdrawB = provideNodeParamWriter(writeB, b.session)
    const runtimes = { A: [] as PluginRuntime[], B: [] as PluginRuntime[] }
    const mounts = { A: 0, B: 0 }; const unmounts = { A: 0, B: 0 }
    const importer = vi.fn(async (url: string): Promise<PluginModule> => {
      const id = url.includes('/projects/A/') ? 'A' : 'B'
      const label = `${id}-${id === 'A' ? revisionA : 'stable'}`
      return { default: (runtime: PluginRuntime) => {
        runtimes[id].push(runtime)
        return defineComponent({ setup() {
          mounts[id] += 1
          onBeforeUnmount(() => { unmounts[id] += 1 })
          return () => h('div', { class: 'plugin-page' }, label)
        } })
      } }
    })
    const la = new PluginLoader(a, importer); const lb = new PluginLoader(b, importer)
    const fallback = respond
    respond = (url, init) => {
      const id = url.pathname.includes('/projects/A/') ? 'A' : 'B'
      if (url.pathname.endsWith('/plugins')) return id === 'A' && !enabledA ? [] : [loadedPlugin()]
      if (url.pathname.endsWith('/plugins/grammars')) return id === 'A' && !enabledA ? [] : [{
        id: 'event-language', plugin_id: PLUGIN, name: 'Same language', extensions: ['.event-grammar'],
        aliases: [], scope_name: 'source.event', grammar_url: moduleUrl(id, 'event.tmLanguage.json'),
      } satisfies SyntaxGrammar]
      if (url.pathname.endsWith('/event.tmLanguage.json')) return {
        scopeName: 'source.event', patterns: [{ match: 'SAME',
          name: id === 'A' && revisionA === 'old' ? 'keyword.control' : 'string.quoted.double' }],
      }
      return fallback(url, init)
    }
    pages.set('A', [page('A')]); pages.set('B', [page('B')])
    await Promise.all([a.router.push('/same-page'), b.router.push('/same-page')])
    const va = mountRenderer(a, la); const vb = mountRenderer(b, lb)
    await Promise.all([la.loadPages(), lb.loadPages()])
    await vi.waitFor(() => {
      expect(va.container.textContent).toContain('A-old')
      expect(vb.container.textContent).toContain('B-stable')
    })
    const highlight = (owner: PluginHost) => highlightFile('same.event-grammar', 'SAME', 'dark', owner.session)
    const oldHighlight = await highlight(a); const highlightB = await highlight(b)
    const grammarB = await pluginGrammars(b.session)
    const generationA = pluginGrammarGeneration(a.session); const generationB = pluginGrammarGeneration(b.session)
    const routeB = b.router.getRoutes().find(route => route.meta.pluginId === PLUGIN)
    const componentB = lb.component(moduleUrl('B'), PLUGIN)
    const runtimeA = runtimes.A[0]!; const runtimeB = runtimes.B[0]!
    // Pages have no editor owner. Keep a separate retained node runtime to prove
    // that A's invalidation still cannot revoke B's same-id editor capability.
    const nodeRuntimeB = createRuntime(PLUGIN, b)
    // Mutating APIs are retained by plugin handlers, not recaptured after the event.
    const seenA = vi.fn(); const seenB = vi.fn()
    runtimeA.onEvent('team.doc.changed', seenA); runtimeB.onEvent('team.doc.changed', seenB)
    a.events.connect(); b.events.connect()
    const [sa, sb] = Socket.all as [Socket, Socket]
    const pagesBefore = requests.filter(({ url }) => url.pathname.endsWith('/plugins/pages')).length
    revisionA = 'fresh'
    // Foreign project/epoch envelopes must not invalidate even before the real event.
    sa.deliver(b.session, 90, { type: eventType, data: { count: 1, resync: true } })
    sa.deliver(a.session, 91, { type: eventType, epoch: 'wrong', data: { count: 1, resync: true } })
    expect(pluginGrammarGeneration(a.session)).toBe(generationA)
    registryEvent(sa, a, 1, eventType)
    registryEvent(sa, a, 2, eventType)
    registryEvent(sa, a, 3, eventType)
    const beforeStaleCalls = requests.length
    expect(() => runtimeA.action('write', { stale: true })).toThrow(/replaced/)
    expect(() => runtimeA.api.savePluginSettings(PLUGIN, { stale: true })).toThrow(/replaced/)
    expect(() => runtimeA.setNodeParams('same-node', { stale: true })).toThrow(/replaced/)
    expect(() => runtimeA.router.push('/same-page')).toThrow(/replaced/)
    expect(requests).toHaveLength(beforeStaleCalls)
    expect(writeA).not.toHaveBeenCalled(); expect(writeB).not.toHaveBeenCalled()
    expect(pluginGrammarGeneration(a.session)).toBe(generationA + 1)
    sa.deliver(a.session, 4); sb.deliver(b.session, 1)
    expect(seenA).not.toHaveBeenCalled(); expect(seenB).toHaveBeenCalledTimes(1)
    await vi.waitFor(() => expect(va.container.textContent).toContain('A-fresh'))
    expect(requests.filter(({ url }) => url.pathname.endsWith('/plugins/pages'))).toHaveLength(pagesBefore + 1)
    expect(mounts).toEqual({ A: 2, B: 1 }); expect(unmounts).toEqual({ A: 1, B: 0 })
    const freshHighlight = await highlight(a)
    expect(oldHighlight?.lines[0]?.[0]?.color).toBeTruthy()
    expect(freshHighlight?.lines[0]?.[0]?.color).toBe(highlightB?.lines[0]?.[0]?.color)
    expect(freshHighlight?.lines[0]?.[0]?.color).not.toBe(oldHighlight?.lines[0]?.[0]?.color)

    enabledA = false; pages.set('A', [])
    registryEvent(sa, a, 5, eventType, [])
    await vi.waitFor(() => {
      expect(la.state.loading).toBe(false)
      expect(va.container.querySelector('.plugin-page')).toBeNull()
      expect(a.router.currentRoute.value.name).toBe('not-found')
    })
    expect(la.navItems).toEqual([])
    expect(await highlight(a)).toBeNull()
    expect(b.router.getRoutes().find(route => route.meta.pluginId === PLUGIN)).toBe(routeB)
    expect(lb.component(moduleUrl('B'), PLUGIN)).toBe(componentB)
    expect(runtimes.B).toEqual([runtimeB])
    expect(await pluginGrammars(b.session)).toBe(grammarB)
    expect(pluginGrammarGeneration(b.session)).toBe(generationB)
    expect(await highlight(b)).toEqual(highlightB)
    expect(vb.container.textContent).toContain('B-stable')
    await expect(runtimeB.settings.save({ still: 'B' })).resolves.toBeUndefined()
    nodeRuntimeB.setNodeParams('same-node', { still: 'B' })
    expect(writeB).toHaveBeenCalledWith('same-node', { still: 'B' })
    expect(requests.filter(({ url }) => url.pathname === '/api/projects/B/plugins/grammars')).toHaveLength(1)
    expect(requests.filter(({ url }) => url.pathname === '/api/projects/B/plugins/pages')).toHaveLength(1)
    withdrawA(); withdrawB()
  })

  it('rejects coalesced ordinary refresh failures, exposes reactive error, and retries without replacing unchanged mounted registrations', async () => {
    const { pluginGrammars, pluginGrammarGeneration } = await import('@/lib/highlight/grammars')
    const a = host('A')
    const mounted = vi.fn()
    const importer = vi.fn(async (_url: string): Promise<PluginModule> => ({
      default: () => defineComponent({ setup() {
        mounted()
        return () => h('div', { class: 'stable-page' }, 'MountedStable')
      } }),
    }))
    const loader = new PluginLoader(a, importer)
    pages.set('A', [page('A')])
    await a.router.push('/same-page')
    const view = mountRenderer(a, loader)
    await loader.loadPages()
    await vi.waitFor(() => expect(view.container.textContent).toContain('MountedStable'))
    const mountedPage = view.container.querySelector('.stable-page')
    const component = loader.component(moduleUrl('A'), PLUGIN)
    const route = a.router.getRoutes().find(item => item.meta.pluginId === PLUGIN)
    const fallback = respond
    const grammar: SyntaxGrammar = { id: 'unchanged-language', plugin_id: PLUGIN, name: 'Unchanged language',
      extensions: ['.unchanged-language'], aliases: [], scope_name: 'source.unchanged',
      grammar_url: moduleUrl('A', 'unchanged.tmLanguage.json') }
    respond = (url, init) => url.pathname.endsWith('/plugins/grammars') ? [grammar] : fallback(url, init)
    const grammars = await pluginGrammars(a.session)
    const registeredGrammar = grammars.byId.get(grammar.id)
    expect(registeredGrammar).toBeTruthy()
    const generation = pluginGrammarGeneration(a.session)
    const failing = deferred<unknown>()
    respond = url => url.pathname.endsWith('/plugins/pages') ? failing.promise : []
    const count = requests.length
    const refreshes = [loader.loadPages(), loader.loadPages(), loader.loadPages()]
    const rejections = refreshes.map(refresh => expect(refresh).rejects.toThrow())
    await vi.waitFor(() => expect(view.container.querySelector('.loader-state')?.textContent).toBe('loading'))
    expect(requests).toHaveLength(count + 1)
    failing.resolve(new Response(JSON.stringify({ detail: 'registry temporarily unavailable' }), { status: 503 }))
    await Promise.all(rejections)
    expect(loader.state.loading).toBe(false)
    expect(loader.state.error).toBeTruthy()
    await nextTick()
    expect(view.container.querySelector('.loader-state')?.textContent).toBe(loader.state.error)
    expect(view.container.textContent).toContain('MountedStable')
    expect(a.router.getRoutes().find(item => item.meta.pluginId === PLUGIN)).toBe(route)
    expect(loader.component(moduleUrl('A'), PLUGIN)).toBe(component)
    respond = (url, init) => url.pathname.endsWith('/plugins/grammars') ? [grammar] : fallback(url, init)
    await loader.loadPages()
    expect(loader.state).toMatchObject({ loading: false, error: null })
    expect(loader.component(moduleUrl('A'), PLUGIN)).toBe(component)
    expect(a.router.getRoutes().find(item => item.meta.pluginId === PLUGIN)).toBe(route)
    expect(await pluginGrammars(a.session)).toBe(grammars)
    expect((await pluginGrammars(a.session)).byId.get(grammar.id)).toBe(registeredGrammar)
    expect(pluginGrammarGeneration(a.session)).toBe(generation)
    expect(importer).toHaveBeenCalledTimes(1)
    expect(mounted).toHaveBeenCalledTimes(1)
    expect(view.container.querySelector('.stable-page')).toBe(mountedPage)
  })

  it.each([false, true])('refetches after a second gap during REST await and never commits its overtaken inventory (old request rejects=%s)', async rejects => {
    const a = host('A')
    const importer = vi.fn(async (_url: string) => factory('FreshOnly'))
    const loader = new PluginLoader(a, importer)
    pages.set('A', [page('A')])
    await a.router.push('/same-page')
    const view = mountRenderer(a, loader)
    await loader.loadPages()
    await vi.waitFor(() => expect(view.container.textContent).toContain('FreshOnly'))
    a.events.connect()
    const socket = Socket.all[0]!
    const first = deferred<unknown>(); const second = deferred<unknown>()
    let fetches = 0
    respond = url => url.pathname.endsWith('/plugins') ? [loadedPlugin()] : (++fetches === 1 ? first.promise : second.promise)
    registryEvent(socket, a, 1, 'system.dropped')
    await vi.waitFor(() => expect(fetches).toBe(1))
    registryEvent(socket, a, 2, 'system.dropped')
    if (rejects) first.reject(new Error('Superseded REST request failed'))
    else first.resolve([page('A', '/overtaken-page', 'overtaken.js')])
    await vi.waitFor(() => expect(fetches).toBe(2))
    expect(loader.navItems).toEqual([])
    expect(a.router.resolve('/overtaken-page').name).toBe('not-found')
    expect(importer.mock.calls.some(([url]) => String(url).includes('overtaken.js'))).toBe(false)
    second.resolve([page('A')])
    await vi.waitFor(() => {
      expect(view.container.textContent).toContain('FreshOnly')
      expect(loader.state.loading).toBe(false)
    })
    expect(loader.navItems).toEqual([page('A')])
    expect(fetches).toBe(2)
  })

  it('makes callers joining an event refresh reject together and recovers on the next scoped gap', async () => {
    const a = host('A')
    let revision = 'before-error'
    const importer = vi.fn(async (_url: string) => factory(revision))
    const loader = new PluginLoader(a, importer)
    pages.set('A', [page('A')])
    await a.router.push('/same-page')
    const view = mountRenderer(a, loader)
    await loader.loadPages()
    await vi.waitFor(() => expect(view.container.textContent).toContain('before-error'))
    a.events.connect()
    const socket = Socket.all[0]!
    const beforeIdleJoin = requests.length
    await loader.waitForRefresh()
    expect(requests).toHaveLength(beforeIdleJoin)
    const failure = deferred<unknown>()
    respond = url => url.pathname.endsWith('/plugins') ? [loadedPlugin()] : failure.promise
    const pageReads = () => requests.filter(({ url }) => url.pathname.endsWith('/plugins/pages')).length
    const before = pageReads()
    registryEvent(socket, a, 1, 'plugin.changed')
    const joined = [loader.waitForRefresh(), loader.waitForRefresh()]
    const rejected = joined.map(promise => expect(promise).rejects.toThrow())
    await vi.waitFor(() => expect(pageReads()).toBe(before + 1))
    failure.resolve(new Response(JSON.stringify({ detail: 'event refresh failed' }), { status: 503 }))
    await Promise.all(rejected)
    expect(loader.state.loading).toBe(false)
    expect(loader.state.error).toBeTruthy()
    const beforeFailedJoin = requests.length
    await expect(loader.waitForRefresh()).rejects.toThrow(loader.state.error!)
    expect(requests).toHaveLength(beforeFailedJoin)
    expect(loader.navItems).toEqual([])
    expect(a.router.resolve('/same-page').name).toBe('not-found')
    await nextTick()
    expect(view.container.querySelector('.loader-state')?.textContent).toBe(loader.state.error)
    revision = 'recovered-after-error'
    respond = url => url.pathname.endsWith('/plugins') ? [loadedPlugin()] : [page('A')]
    registryEvent(socket, a, 2, 'system.dropped')
    await vi.waitFor(() => {
      expect(view.container.textContent).toContain('recovered-after-error')
      expect(loader.state).toMatchObject({ loading: false, error: null })
    })
    expect(importer).toHaveBeenCalledTimes(2)
    expect(pageReads()).toBe(before + 2)
    await loader.waitForRefresh()
    expect(pageReads()).toBe(before + 2)
  })

  it('does not execute a late mounted import factory after a scoped event replaces it', async () => {
    const a = host('A'); const pending = deferred<PluginModule>()
    let imports = 0
    const importer = vi.fn(() => ++imports === 1 ? pending.promise : Promise.resolve(factory('ReplacementMounted')))
    const loader = new PluginLoader(a, importer)
    pages.set('A', [page('A')])
    await a.router.push('/same-page')
    const view = mountRenderer(a, loader)
    await loader.loadPages()
    await vi.waitFor(() => expect(importer).toHaveBeenCalledTimes(1))
    a.events.connect()
    registryEvent(Socket.all[0]!, a, 1, 'plugin.changed')
    await vi.waitFor(() => expect(view.container.textContent).toContain('ReplacementMounted'))
    const lateSetup = vi.fn(() => ({ render: () => 'StaleMustNeverMount' }))
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => {})
    pending.resolve({ setup: lateSetup })
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
    expect(lateSetup).not.toHaveBeenCalled()
    expect(view.container.textContent).toContain('ReplacementMounted')
    expect(view.container.textContent).not.toContain('StaleMustNeverMount')
    expect(importer).toHaveBeenCalledTimes(2)
    error.mockRestore(); warning.mockRestore()
  })

  it('keeps an unrelated mounted pipeline editor and its writer alive across ordinary refresh and plugin events', async () => {
    const { default: AppShell } = await import('@/components/layout/AppShell.vue')
    const { pluginGrammarGeneration } = await import('@/lib/highlight/grammars')
    const a = host('A'); const loader = new PluginLoader(a)
    const mounted = vi.fn(); const unmounted = vi.fn(); const writes = vi.fn()
    let runtime!: PluginRuntime
    const editor = defineComponent({ setup() {
      mounted()
      const withdraw = provideNodeParamWriter(writes, a.session)
      runtime = createRuntime(PLUGIN, a)
      onBeforeUnmount(() => { withdraw(); unmounted() })
      return () => h('textarea', { value: 'unsaved local draft' })
    } })
    a.router.addRoute({ path: '/pipelines/:id', name: 'pipeline-editor', component: editor })
    pages.set('A', [page('A')])
    await a.router.push('/pipelines/same-id')
    const view = mountRenderer(a, loader, AppShell)
    await loader.loadPages()
    const textarea = view.container.querySelector('textarea')!
    textarea.value = 'user draft must survive'
    const generation = pluginGrammarGeneration(a.session)
    await loader.loadPages()
    expect(pluginGrammarGeneration(a.session)).toBe(generation)
    a.events.connect()
    registryEvent(Socket.all[0]!, a, 1, 'plugin.changed')
    await vi.waitFor(() => expect(loader.state.loading).toBe(false))
    await nextTick()
    expect(view.container.querySelector('textarea')).toBe(textarea)
    expect(textarea.value).toBe('user draft must survive')
    expect(mounted).toHaveBeenCalledTimes(1); expect(unmounted).not.toHaveBeenCalled()
    runtime.setNodeParams('same-node', { retained: true })
    expect(writes).toHaveBeenCalledWith('same-node', { retained: true })
  })

  it('refcounts mounted ownership, unsubscribes the final owner and suppresses late REST/import completions', async () => {
    const a = host('A'); const pendingImport = deferred<PluginModule>()
    const realOn = a.events.on.bind(a.events)
    const subscriptions: { type: string; stop: ReturnType<typeof vi.fn> }[] = []
    vi.spyOn(a.events, 'on').mockImplementation((type, handler) => {
      const stop = vi.fn(realOn(type, handler))
      subscriptions.push({ type, stop })
      return stop
    })
    let runtime!: PluginRuntime
    const loader = new PluginLoader(a, async (url: string) => url.includes('late.js')
      ? pendingImport.promise : factory('Owned', value => { runtime = value }))
    pages.set('A', [page('A')])
    await a.router.push('/same-page')
    const view = mountRenderer(a, loader)
    const stopOther = loader.start()
    await loader.loadPages()
    await vi.waitFor(() => expect(view.container.textContent).toContain('Owned'))
    a.events.connect()
    const socket = Socket.all[0]!
    const late = load(loader.component(moduleUrl('A', 'late.js'), PLUGIN))
    const rejectLate = expect(late).rejects.toThrow(/replaced/)
    const seen = vi.fn()
    runtime.onEvent('team.doc.changed', seen)
    view.unmount()
    const lifecycleStops = subscriptions.filter(({ type }) => type === 'plugin.changed' || type === 'system.dropped')
    expect(lifecycleStops).toHaveLength(2)
    lifecycleStops.forEach(({ stop }) => expect(stop).not.toHaveBeenCalled())
    // Another consumer still owns the subscription; the first stop must not reset it.
    await expect(runtime.settings.save({ owner: 'still-active' })).resolves.toBeUndefined()
    socket.deliver(a.session, 1)
    expect(seen).toHaveBeenCalledTimes(1)
    const pendingList = deferred<unknown>(); let fetches = 0
    respond = url => {
      if (url.pathname.endsWith('/plugins')) return [loadedPlugin()]
      fetches += 1
      return pendingList.promise
    }
    registryEvent(socket, a, 2, 'plugin.changed')
    await vi.waitFor(() => expect(fetches).toBe(1))
    stopOther(); stopOther()
    lifecycleStops.forEach(({ stop }) => expect(stop).toHaveBeenCalledTimes(1))
    const before = requests.length
    expect(() => runtime.action('write', {})).toThrow(/replaced/)
    registryEvent(socket, a, 3, 'plugin.changed')
    registryEvent(socket, a, 4, 'system.dropped')
    const lateSetup = vi.fn(() => ({ name: 'NeverRevived' }))
    pendingImport.resolve({ setup: lateSetup })
    pendingList.resolve([page('A', '/revived-page')])
    await rejectLate
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(requests).toHaveLength(before)
    expect(fetches).toBe(1)
    expect(lateSetup).not.toHaveBeenCalled()
    expect(loader.navItems).toEqual([])
    expect(a.router.resolve('/revived-page').name).toBe('not-found')
    socket.deliver(a.session, 5)
    expect(seen).toHaveBeenCalledTimes(1)
    // A new owner can restart it; an old idempotent disposer cannot stop that owner.
    respond = url => url.pathname.endsWith('/plugins') ? [loadedPlugin()] : [page('A')]
    const stopReplacement = loader.start()
    stopOther()
    registryEvent(socket, a, 6, 'plugin.changed')
    await vi.waitFor(() => expect(loader.navItems).toEqual([page('A')]))
    stopReplacement()
  })
})

describe('reactive retained node/previewer descriptors and navigation recovery', () => {
  it.each([
    { kind: 'node', disable: 'disabled inventory' },
    { kind: 'previewer', disable: 'absent inventory' },
    { kind: 'previewer', disable: 'missed disable via gap' },
  ])('recovers mounted computed $kind after early schema refresh and never resurrects it after $disable', async ({ kind, disable }) => {
    const { default: MissingPluginPage } = await import('./MissingPluginPage.vue')
    const { pluginGrammars, pluginGrammarGeneration } = await import('@/lib/highlight/grammars')
    const a = host('A'); const b = host('B')
    const writeA = vi.fn(); const writeB = vi.fn()
    const withdrawA = provideNodeParamWriter(writeA, a.session)
    const withdrawB = provideNodeParamWriter(writeB, b.session)
    const runtimes = { A: [] as PluginRuntime[], B: [] as PluginRuntime[] }
    let revision = 'old'
    const importerA = vi.fn(async (_url: string) => factory(`A-${revision}`, runtime => { runtimes.A.push(runtime) }))
    const importerB = vi.fn(async (_url: string) => factory('B-stable', runtime => { runtimes.B.push(runtime) }))
    const la = new PluginLoader(a, importerA); const lb = new PluginLoader(b, importerB)
    const descriptor = shallowRef({ component_url: moduleUrl('A', `${kind}.js`), plugin_id: PLUGIN })
    const sourceB = moduleUrl('B', `${kind}.js`)
    const pane = (loader: PluginLoader, retained: () => { component_url: string; plugin_id: string }) => defineComponent({
      setup() {
        // Matches the host's node registry / FilePreview computed over retained props.
        const component = computed(() => loader.component(retained().component_url, retained().plugin_id))
        return () => h(component.value)
      },
    })
    const va = mountRenderer(a, la, pane(la, () => descriptor.value))
    const vb = mountRenderer(b, lb, pane(lb, () => ({ component_url: sourceB, plugin_id: PLUGIN })))
    await Promise.all([la.loadPages(), lb.loadPages()])
    await vi.waitFor(() => {
      expect(va.container.textContent).toContain('A-old')
      expect(vb.container.textContent).toContain('B-stable')
    })
    const oldA = runtimes.A[0]!; const runtimeB = runtimes.B[0]!
    const componentB = lb.component(sourceB, PLUGIN)
    const grammarsB = await pluginGrammars(b.session)
    const generationB = pluginGrammarGeneration(b.session)
    a.events.connect(); b.events.connect()
    const sa = Socket.all[0]!
    const pendingPages = deferred<unknown>(); const pendingInventory = deferred<unknown>()
    let holdPages = true; let enabledA = true; let holdInventory = false
    const fallback = respond
    respond = (url, init) => {
      if (!url.pathname.startsWith('/api/projects/A/')) return fallback(url, init)
      if (url.pathname.endsWith('/plugins')) return holdInventory ? pendingInventory.promise : (enabledA ? [loadedPlugin()] : [])
      if (url.pathname.endsWith('/plugins/pages')) return holdPages ? pendingPages.promise : []
      return fallback(url, init)
    }
    revision = 'fresh'
    registryEvent(sa, a, 1, 'plugin.changed')
    expect(() => oldA.setNodeParams('same-node', { stale: true })).toThrow(/replaced/)
    // The node-type REST / preview descriptor update wins the race against pages.
    descriptor.value = { ...descriptor.value }
    await nextTick()
    expect(va.container.textContent).not.toContain('A-old')
    expect(va.container.textContent).not.toContain('A-fresh')
    expect(importerA).toHaveBeenCalledTimes(1)
    pendingPages.resolve([])
    await la.waitForRefresh()
    await vi.waitFor(() => expect(va.container.textContent).toContain('A-fresh'))
    expect(runtimes.A).toHaveLength(2)
    const freshA = runtimes.A[1]!
    expect(freshA).not.toBe(oldA)
    freshA.setNodeParams('same-node', { recovered: true })
    expect(writeA).toHaveBeenCalledWith('same-node', { recovered: true })
    expect(() => oldA.action('write')).toThrow(/replaced/)

    holdPages = false; enabledA = false
    if (disable === 'missed disable via gap') {
      holdInventory = true
      registryEvent(sa, a, 2, 'system.dropped')
      await vi.waitFor(() => expect(requests.filter(({ url }) => url.pathname === '/api/projects/A/plugins')).toHaveLength(1))
      await nextTick()
      // A successful pages snapshot alone cannot authorize a retained descriptor.
      expect(la.state.loading).toBe(true)
      expect(importerA).toHaveBeenCalledTimes(2)
      expect(va.container.textContent).not.toContain('A-fresh')
      pendingInventory.resolve([])
    } else {
      registryEvent(sa, a, 2, 'plugin.changed', disable === 'disabled inventory' ? [loadedPlugin(false)] : [])
    }
    expect(() => freshA.setNodeParams('same-node', { disabled: true })).toThrow(/replaced/)
    await la.waitForRefresh()
    await vi.waitFor(() => expect(va.container.textContent).toContain(t('plugins.missingPage.title')))
    expect(la.state.error).toBeNull()
    expect(la.component(descriptor.value.component_url, PLUGIN)).toBe(MissingPluginPage)
    expect(importerA).toHaveBeenCalledTimes(2)
    expect(runtimes.A).toHaveLength(2)
    expect(writeA.mock.calls).toEqual([['same-node', { recovered: true }]])
    expect(lb.component(sourceB, PLUGIN)).toBe(componentB)
    expect(runtimes.B).toEqual([runtimeB])
    expect(importerB).toHaveBeenCalledTimes(1)
    expect(vb.container.textContent).toContain('B-stable')
    expect(await pluginGrammars(b.session)).toBe(grammarsB)
    expect(pluginGrammarGeneration(b.session)).toBe(generationB)
    runtimeB.setNodeParams('same-node', { unaffected: true })
    expect(writeB).toHaveBeenCalledWith('same-node', { unaffected: true })
    withdrawA(); withdrawB()
  })

  it('rejects an aborted mounted route replacement and retries identical inventory after the guard is released', async () => {
    const a = host('A'); let revision = 'OldPage'; let old!: PluginRuntime
    const loader = new PluginLoader(a, async () => factory(revision, runtime => { old ??= runtime }))
    pages.set('A', [page('A')])
    await a.router.push('/same-page')
    const view = mountRenderer(a, loader)
    await loader.loadPages()
    await vi.waitFor(() => expect(view.container.textContent).toContain('OldPage'))
    const release = a.router.beforeEach(() => false)
    const replace = vi.spyOn(a.router, 'replace')
    try {
      a.events.connect(); revision = 'FreshPage'
      registryEvent(Socket.all[0]!, a, 1, 'plugin.changed')
      await expect(loader.waitForRefresh()).rejects.toBeTruthy()
      expect(loader.state.error).toBeTruthy()
      expect(loader.state.loading).toBe(false)
      expect(replace).toHaveBeenCalledTimes(1)
      expect(() => old.action('write')).toThrow(/replaced/)
      expect(view.container.textContent).not.toContain('FreshPage')
      release()
      await loader.loadPages()
      await vi.waitFor(() => expect(view.container.textContent).toContain('FreshPage'))
      expect(replace).toHaveBeenCalledTimes(2)
      expect(loader.state.error).toBeNull()
      expect(view.container.textContent).not.toContain('OldPage')
    } finally { release() }
  })
})

describe('reacquired plugin ownership', () => {
  it('refetches authority after a disable missed between mounted owners, without reviving retained preview props', async () => {
    const a = host('A')
    const runtimes: PluginRuntime[] = []
    const importer = vi.fn(async () => factory('RetainedPreview', runtime => { runtimes.push(runtime) }))
    const loader = new PluginLoader(a, importer)
    pages.set('A', [])
    const descriptor = { component_url: moduleUrl('A', 'viewer.js'), plugin_id: PLUGIN }
    const pane = defineComponent({ setup() {
      const component = computed(() => loader.component(descriptor.component_url, descriptor.plugin_id))
      return () => h(component.value)
    } })
    const first = mountRenderer(a, loader, pane)
    await loader.loadPages()
    await vi.waitFor(() => expect(runtimes).toHaveLength(1))
    a.events.connect()
    const socket = Socket.all[0]!
    registryEvent(socket, a, 1, 'plugin.changed')
    await loader.waitForRefresh()
    await vi.waitFor(() => expect(runtimes).toHaveLength(2))
    first.unmount()
    const reads = requests.length
    registryEvent(socket, a, 2, 'plugin.changed', [loadedPlugin(false)])
    expect(requests).toHaveLength(reads)
    const fallback = respond
    respond = (url, init) => url.pathname.endsWith('/plugins') ? [loadedPlugin(false)] : fallback(url, init)
    const second = mountRenderer(a, loader, pane)
    await loader.loadPages()
    await nextTick()
    expect(loader.state).toMatchObject({ loading: false, error: null })
    expect(second.container.textContent).not.toContain('RetainedPreview')
    expect(importer).toHaveBeenCalledTimes(2)
    expect(runtimes).toHaveLength(2)
    expect(requests.slice(reads).filter(({ url }) => url.pathname === '/api/projects/A/plugins')).toHaveLength(1)
    expect(() => runtimes[1]!.action('write')).toThrow(/replaced/)
  })
})

describe('real application and plugin toggle lifecycle', () => {
  it.each([
    ['pipeline', 'before'], ['pipeline', 'after'], ['Explorer', 'before'], ['Explorer', 'after'],
  ] as const)('preserves mounted %s UI when initial pages arrive with its import %s pages', async (kind, importOrder) => {
    const { pluginGrammarGeneration } = await import('@/lib/highlight/grammars')
    const owner = rendererPluginHost; const loader = pluginLoader(owner)
    loader.reset()
    const pendingPages = deferred<unknown>(); const pendingModule = deferred<PluginModule>()
    const write = vi.fn(); const mounted = vi.fn(); const unmounted = vi.fn()
    let runtime!: PluginRuntime; let wrapper!: Component
    const source = `/api/plugins/${PLUGIN}/assets/startup-${kind}.js?v=${VERSION}`
    const importer = vi.spyOn(loader as unknown as { importer: (url: string) => Promise<PluginModule> }, 'importer')
      .mockImplementation(() => pendingModule.promise)
    respond = url => url.pathname.endsWith('/plugins/pages') ? pendingPages.promise : [loadedPlugin()]
    const ui = defineComponent({ setup() {
      mounted()
      if (kind === 'pipeline') onBeforeUnmount(provideNodeParamWriter(write, owner.session))
      onBeforeUnmount(unmounted)
      wrapper = pluginComponent(source, PLUGIN)
      return () => h('section', [h('input', { class: 'startup-draft' }), h(wrapper)])
    } })
    const view = await mountApplication(ui)
    await vi.waitFor(() => expect(importer).toHaveBeenCalledTimes(1))
    const generation = pluginGrammarGeneration(owner.session)
    const input = view.container.querySelector<HTMLInputElement>('.startup-draft')!
    input.value = 'unsaved startup draft'
    const b = host('B'); const lb = new PluginLoader(b, async () => factory('StableB'))
    const stableB = lb.component(moduleUrl('B'), PLUGIN)
    const vb = mountRenderer(b, lb, stableB)
    await vi.waitFor(() => expect(vb.container.textContent).toContain('StableB'))
    const finishImport = async () => {
      pendingModule.resolve(factory(`Live-${kind}`, value => { runtime = value }))
      await vi.waitFor(() => expect(view.container.textContent).toContain(`Live-${kind}`))
    }
    if (importOrder === 'before') await finishImport()
    // Nonempty first inventory used to reset already mounted runtime/imports.
    pendingPages.resolve([{ ...page('A'), component_url: `/api/plugins/${PLUGIN}/assets/page.js?v=${VERSION}` }])
    await vi.waitFor(() => expect(loader.state.loading).toBe(false))
    if (importOrder === 'after') await finishImport()
    expect(pluginGrammarGeneration(owner.session)).toBe(generation)
    expect(view.container.querySelector('.startup-draft')).toBe(input)
    expect(input.value).toBe('unsaved startup draft')
    expect(mounted).toHaveBeenCalledTimes(1); expect(unmounted).not.toHaveBeenCalled()
    expect(pluginComponent(source, PLUGIN)).toBe(wrapper)
    await expect(runtime.action('still-live')).resolves.toEqual([loadedPlugin()])
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    runtime.setNodeParams('same-node', { startup: true })
    expect(write).toHaveBeenCalledTimes(kind === 'pipeline' ? 1 : 0)
    expect(lb.component(moduleUrl('B'), PLUGIN)).toBe(stableB)
    expect(vb.container.textContent).toContain('StableB')
    expect(importer).toHaveBeenCalledTimes(1)
  })

  it('subscribes before App connects, displays refresh failure and retries without remounting its editor, then stops on unmount', async () => {
    const { pluginGrammarGeneration } = await import('@/lib/highlight/grammars')
    const owner = rendererPluginHost
    const initialGeneration = pluginGrammarGeneration(owner.session)
    const failure = deferred<unknown>()
    let reply: () => unknown = () => failure.promise
    respond = url => url.pathname.endsWith('/plugins/pages') ? reply() : []
    const mounted = vi.fn(); const unmounted = vi.fn()
    const editor = defineComponent({ setup() {
      mounted()
      onBeforeUnmount(unmounted)
      return () => h('input', { class: 'existing-editor', value: 'initial draft' })
    } })
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    const view = await mountApplication(editor, connectedOwner => {
      expect(connectedOwner.events).toBeInstanceOf(WsConnection)
      // This frame arrives synchronously inside session.load, immediately after
      // connect: it is lost if App acquires its loader lifetime after session.load.
      registryEvent(Socket.all[0]!, connectedOwner, 1, 'plugin.changed')
      expect(pluginGrammarGeneration(connectedOwner.session)).toBe(initialGeneration + 1)
    })
    expect(view.loadSession).toHaveBeenCalledTimes(1)
    const input = view.container.querySelector<HTMLInputElement>('.existing-editor')!
    expect(input).not.toBeNull()
    input.value = 'unsaved input survives both error and retry'
    failure.resolve(new Response(JSON.stringify({ detail: 'visible App plugin failure' }), { status: 503 }))
    await vi.waitFor(() => expect(view.container.querySelector('[role="alert"]')?.textContent)
      .toContain('visible App plugin failure'))
    expect(logged).toHaveBeenCalledTimes(1)
    expect(view.container.querySelector('.existing-editor')).toBe(input)
    expect(mounted).toHaveBeenCalledTimes(1); expect(unmounted).not.toHaveBeenCalled()
    const retry = view.container.querySelector<HTMLButtonElement>('[role="alert"] button')!
    expect(retry.textContent).toContain(t('common.retry'))
    reply = () => []
    retry.click()
    await vi.waitFor(() => {
      expect(view.loader.state).toMatchObject({ loading: false, error: null })
      expect(view.container.querySelector('[role="alert"]')).toBeNull()
    })
    expect(view.container.querySelector('.existing-editor')).toBe(input)
    expect(input.value).toBe('unsaved input survives both error and retry')
    expect(mounted).toHaveBeenCalledTimes(1); expect(unmounted).not.toHaveBeenCalled()
    expect(view.loadSession).toHaveBeenCalledTimes(1)
    expect(requests.filter(({ url }) => url.pathname.endsWith('/plugins/pages'))).toHaveLength(2)
    view.unmount()
    expect(unmounted).toHaveBeenCalledTimes(1)
    const generationAfterStop = pluginGrammarGeneration(owner.session)
    const requestsAfterStop = requests.length
    registryEvent(Socket.all[0]!, owner, 2, 'plugin.changed')
    await nextTick()
    expect(pluginGrammarGeneration(owner.session)).toBe(generationAfterStop)
    expect(requests).toHaveLength(requestsAfterStop)
  })

  it.each(['HTTP before delayed WS', 'WS before HTTP'] as const)('joins real PluginsView toggle with one runtime invalidation (%s), displays refresh errors and clears recovery', async order => {
    const { default: PluginsView } = await import('@/views/PluginsView.vue')
    const { pluginGrammarGeneration } = await import('@/lib/highlight/grammars')
    const mutation = deferred<void>(); const refresh = deferred<unknown>()
    let enabled = true
    const fixture = (): LoadedPlugin => ({ id: PLUGIN, name: 'Real toggle plugin', version: VERSION,
      author: '', description: '', scope: 'project', enabled, status: enabled ? 'loaded' : 'disabled',
      error: null, node_types: [], pages: [], previewers: [], grammars: [], hooks: [], actions: [],
      search_providers: [], automation_actions: [], has_backend: false, has_venv: false, env: null, root: 'D:\\plugin' })
    let pageReply: () => unknown = () => []
    respond = url => {
      if (url.pathname.endsWith('/plugins/pages')) return pageReply()
      if (url.pathname === '/api/plugins') return [fixture()]
      if (url.pathname.endsWith(`/plugins/${PLUGIN}/disable`)) {
        enabled = false
        return mutation.promise.then(fixture)
      }
      return []
    }
    const view = await mountApplication(PluginsView)
    await vi.waitFor(() => {
      expect(view.loader.state.loading).toBe(false)
      expect(view.container.textContent).toContain('Real toggle plugin')
    })
    const toggle = () => [...view.container.querySelectorAll<HTMLButtonElement>('li button')]
      .find(button => button.textContent?.includes(enabled ? t('common.disable') : t('common.enable')))!
    const button = toggle()
    expect(button).toBeTruthy()
    const generation = pluginGrammarGeneration(view.owner.session)
    const pageReads = () => requests.filter(({ url }) => url.pathname.endsWith('/plugins/pages')).length
    const before = pageReads()
    const socket = Socket.all[0]!
    pageReply = () => refresh.promise
    button.click()
    await vi.waitFor(() => expect(requests.filter(({ url }) => url.pathname.endsWith(`/${PLUGIN}/disable`))).toHaveLength(1))
    if (order === 'HTTP before delayed WS') {
      mutation.resolve()
      await vi.waitFor(() => expect(button.disabled).toBe(false))
      expect(pageReads()).toBe(before)
      expect(pluginGrammarGeneration(view.owner.session)).toBe(generation)
      registryEvent(socket, view.owner, 1, 'plugin.changed', [fixture()])
    } else {
      registryEvent(socket, view.owner, 1, 'plugin.changed', [fixture()])
      await vi.waitFor(() => expect(pageReads()).toBe(before + 1))
      mutation.resolve()
      await nextTick()
      expect(button.disabled).toBe(true)
    }
    expect(pluginGrammarGeneration(view.owner.session)).toBe(generation + 1)
    refresh.resolve(new Response(JSON.stringify({ detail: 'visible PluginsView refresh failure' }), { status: 503 }))
    await vi.waitFor(() => {
      expect(view.loader.state.loading).toBe(false)
      expect(view.container.querySelector('p.bg-danger-soft')?.textContent).toContain('visible PluginsView refresh failure')
      expect(view.container.querySelector('[role="alert"]')?.textContent).toContain('visible PluginsView refresh failure')
      expect(button.disabled).toBe(false)
    })
    expect(pageReads()).toBe(before + 1)
    expect(pluginGrammarGeneration(view.owner.session)).toBe(generation + 1)
    // Recovery must clear both the App banner and PluginsView's computed error,
    // including when toggle itself awaited and caught the rejected refresh.
    pageReply = () => []
    registryEvent(socket, view.owner, 2, 'system.dropped')
    await vi.waitFor(() => {
      expect(view.loader.state).toMatchObject({ loading: false, error: null })
      expect(view.container.querySelector('p.bg-danger-soft')).toBeNull()
      expect(view.container.querySelector('[role="alert"]')).toBeNull()
    })
    expect(pageReads()).toBe(before + 2)
    // The failed snapshot never installed new registrations: recovery refetches
    // the already-invalidated lifetime rather than resetting that lifetime twice.
    expect(pluginGrammarGeneration(view.owner.session)).toBe(generation + 1)
    expect(view.container.textContent).toContain('Real toggle plugin')
    expect(toggle().textContent).toContain(t('common.enable'))
  })
})

describe('resources, events and node writers', () => {
  it('scopes nested plugin-relative CSS/images and preserves import source stamps and authentication', () => {
    const a = host('A'); const runtime = createRuntime(PLUGIN, a)
    for (const path of ['styles/page.css', 'styles/icons/same image.svg']) {
      const asset = assertScoped(runtime.assetUrl(path), 'A', `/plugins/${PLUGIN}/assets/${path.replace(' ', '%20')}`, true)
      expect(asset.searchParams.getAll('token')).toEqual(['secret /&'])
    }
    const module = assertScoped(pluginResourceUrl(moduleUrl('A'), a.session, PLUGIN), 'A', `/plugins/${PLUGIN}/assets/page.js`)
    expect(module.searchParams.get('v')).toBe(VERSION)
  })

  it.each([
    `/api/plugins/${PLUGIN}/assets/page.js?v=${VERSION}`,
    moduleUrl('B'),
    moduleUrl('A').replace('epoch-A', 'wrong-epoch'),
    moduleUrl('A').replace('project_id=A', 'project_id=B'),
    moduleUrl('A', 'page.js', 'foreign-owner'),
    'https://foreign.example/plugin.js',
    moduleUrl('A', '%2e%2e/page.js'),
  ])('rejects unsupported or conflicting module URL %s before import', source => {
    const a = host('A'); const importer = vi.fn(async () => factory('Never'))
    const loader = new PluginLoader(a, importer)
    expect(() => loader.component(source, PLUGIN)).toThrow()
    expect(importer).not.toHaveBeenCalled()
  })

  it('subscribes through separate real WsConnections and suppresses old registration callbacks', async () => {
    const a = host('A'); const b = host('B')
    expect(a.events).toBeInstanceOf(WsConnection)
    expect(a.events).not.toBe(b.events)
    let old!: PluginRuntime
    const la = new PluginLoader(a, async () => factory('A', runtime => { old = runtime }))
    await load(la.component(moduleUrl('A'), PLUGIN))
    const seenA = vi.fn(); const seenB = vi.fn()
    old.onEvent('team.doc.changed', seenA)
    const offB = createRuntime(PLUGIN, b).onEvent('team.doc.changed', seenB)
    a.events.connect(); b.events.connect()
    const [sa, sb] = Socket.all as [Socket, Socket]
    expect(new URL(sa.url).pathname).toBe('/ws/projects/A')
    expect(new URL(sb.url).pathname).toBe('/ws/projects/B')
    expect(new URL(sa.url).searchParams.get('token')).toBe('secret /&')
    sa.deliver(b.session, 90)
    sa.deliver(a.session, 91, { epoch: 'wrong' })
    sa.deliver(a.session, 1)
    sb.deliver(b.session, 1)
    expect(seenA).toHaveBeenCalledTimes(1); expect(seenB).toHaveBeenCalledTimes(1)
    expect(a.events.lastSeq.value).toBe(1)
    la.reset()
    sa.deliver(a.session, 2)
    sb.deliver(b.session, 2)
    expect(seenA).toHaveBeenCalledTimes(1); expect(seenB).toHaveBeenCalledTimes(2)
    offB()
    sb.deliver(b.session, 3)
    expect(seenB).toHaveBeenCalledTimes(2)
  })

  it('isolates same-node writers and an old withdrawal cannot delete its replacement', () => {
    const a = host('A'); const b = host('B')
    const old = vi.fn(); const replacement = vi.fn(); const writerB = vi.fn()
    const withdrawOld = provideNodeParamWriter(old, a.session)
    const withdrawB = provideNodeParamWriter(writerB, b.session)
    const ra = createRuntime(PLUGIN, a); const rb = createRuntime(PLUGIN, b)
    ra.setNodeParams('same-node', { project: 'A' }); rb.setNodeParams('same-node', { project: 'B' })
    const withdrawReplacement = provideNodeParamWriter(replacement, a.session)
    withdrawOld()
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => {})
    ra.setNodeParams('same-node', { stale: true })
    expect(warning).toHaveBeenCalled()
    createRuntime(PLUGIN, a).setNodeParams('same-node', { project: 'A2' })
    rb.setNodeParams('same-node', { project: 'B2' })
    expect(old.mock.calls).toEqual([['same-node', { project: 'A' }]])
    expect(replacement.mock.calls).toEqual([['same-node', { project: 'A2' }]])
    expect(writerB.mock.calls).toEqual([['same-node', { project: 'B' }], ['same-node', { project: 'B2' }]])
    withdrawReplacement(); withdrawB()
  })

  it('does not withdraw a replacement registration even when it reuses the same callback', () => {
    const a = host('A'); const writer = vi.fn()
    const withdrawOld = provideNodeParamWriter(writer, a.session)
    const old = createRuntime(PLUGIN, a)
    const withdrawNew = provideNodeParamWriter(writer, a.session)
    const current = createRuntime(PLUGIN, a)
    withdrawOld()
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    old.setNodeParams('same-node', { stale: true })
    current.setNodeParams('same-node', { current: true })
    expect(writer.mock.calls).toEqual([['same-node', { current: true }]])
    withdrawNew()
  })

  it('captures the editor before a pending import and cannot acquire its replacement writer', async () => {
    const a = host('A'); const oldWriter = vi.fn(); const newWriter = vi.fn()
    const withdrawOld = provideNodeParamWriter(oldWriter, a.session)
    const pending = deferred<PluginModule>()
    let old!: PluginRuntime
    const loader = new PluginLoader(a, () => pending.promise)
    const first = loader.component(moduleUrl('A'), PLUGIN)
    const completion = load(first)
    const withdrawNew = provideNodeParamWriter(newWriter, a.session)
    withdrawOld()
    pending.resolve(factory('Delayed', runtime => { old = runtime }))
    await completion
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    old.setNodeParams('same-node', { stale: true })
    expect(oldWriter).not.toHaveBeenCalled(); expect(newWriter).not.toHaveBeenCalled()
    expect(loader.component(moduleUrl('A'), PLUGIN)).not.toBe(first)
    createRuntime(PLUGIN, a).setNodeParams('same-node', { current: true })
    expect(newWriter).toHaveBeenCalledWith('same-node', { current: true })
    withdrawNew()
  })

  it('rejects host routers and event connections bound to another project', () => {
    const a = host('A'); const b = host('B')
    expect(() => createPluginHost(a.session, { router: b.router })).toThrow(/router/)
    expect(() => createPluginHost(a.session, { router: a.router, events: b.events })).toThrow(/event connection/)
  })
})

describe('project grammar registries use real tokenizer sources', () => {
  it('separates identical grammar ids/scopes/version stamps and reloads or disables only A', async () => {
    const { highlightFile } = await import('@/lib/highlight/highlighter')
    const { resetPluginGrammars } = await import('@/lib/highlight/grammars')
    const a = host('A'); const b = host('B')
    let ruleA = 'keyword.control'; let enabledA = true
    const grammar = (id: string): SyntaxGrammar => ({ id: 'same-language', plugin_id: PLUGIN, name: 'Same Language',
      extensions: ['.projectgrammar'], aliases: [], scope_name: 'source.same', grammar_url: moduleUrl(id, 'same.tmLanguage.json') })
    respond = url => {
      const id = /^\/api\/projects\/([^/]+)\//.exec(url.pathname)?.[1]
      if (url.pathname.endsWith('/plugins/grammars')) return id === 'A' && !enabledA ? [] : [grammar(id!)]
      return { name: 'Same Language', scopeName: 'source.same', patterns: [
        { match: 'SAME', name: id === 'A' ? ruleA : 'string.quoted.double' },
      ] }
    }
    const highlight = (h: PluginHost) => highlightFile('same.projectgrammar', 'SAME', 'dark', h.session)
    const resultA = await highlight(a); const resultB = await highlight(b)
    const colour = (result: typeof resultA) => result?.lines[0]?.[0]?.color
    expect(resultA?.language).toBe('same-language'); expect(resultB?.language).toBe('same-language')
    expect(colour(resultA)).toBeTruthy(); expect(colour(resultB)).toBeTruthy()
    expect(colour(resultA)).not.toBe(colour(resultB))
    ruleA = 'string.quoted.double'
    resetPluginGrammars(a.session)
    expect(colour(await highlight(a))).toBe(colour(resultB))
    enabledA = false
    resetPluginGrammars(a.session)
    expect(await highlight(a)).toBeNull()
    expect(colour(await highlight(b))).toBe(colour(resultB))
    const grammarRequests = requests.filter(({ url }) => url.pathname.endsWith('.tmLanguage.json'))
    expect(grammarRequests).toHaveLength(3)
    for (const { url, init } of grammarRequests) {
      const id = url.pathname.includes('/projects/A/') ? 'A' : 'B'
      expect(assertScoped(url.href, id, `/plugins/${PLUGIN}/assets/same.tmLanguage.json`).searchParams.get('v')).toBe(VERSION)
      expect(new Headers(init.headers).get('Authorization')).toBe('Bearer secret /&')
    }
    expect(requests.filter(({ url }) => url.pathname === '/api/projects/B/plugins/grammars')).toHaveLength(1)
  })
})
describe('grammar reset races and host-generated asset URLs', () => {
  it('drops delayed A JSON after reset, without replacing fresh A or touching B', async () => {
    const { highlightFile } = await import('@/lib/highlight/highlighter')
    const { resetPluginGrammars } = await import('@/lib/highlight/grammars')
    const a = host('A'); const b = host('B')
    const oldJson = deferred<unknown>(); const started = deferred<void>()
    let holdA = true
    const grammar = (id: string): SyntaxGrammar => ({ id: 'race-language', plugin_id: PLUGIN,
      name: 'Race Language', extensions: ['.grammar-race'], aliases: [], scope_name: 'source.race',
      grammar_url: moduleUrl(id, 'race.tmLanguage.json') })
    const json = (name: string) => ({ name: 'Race Language', scopeName: 'source.race',
      patterns: [{ match: 'SAME', name }] })
    respond = url => {
      const id = /^\/api\/projects\/([^/]+)\//.exec(url.pathname)?.[1]
      if (url.pathname.endsWith('/plugins/grammars')) return [grammar(id!)]
      if (id === 'A' && holdA) { holdA = false; started.resolve(); return oldJson.promise }
      return json(id === 'A' ? 'string.quoted.double' : 'keyword.control')
    }
    const highlight = (h: PluginHost) => highlightFile('same.grammar-race', 'SAME', 'dark', h.session)
    const late = highlight(a)
    await started.promise
    resetPluginGrammars(a.session)
    const freshA = await highlight(a); const resultB = await highlight(b)
    expect(freshA?.language).toBe('race-language'); expect(resultB?.language).toBe('race-language')
    expect(freshA?.lines[0]?.[0]?.color).not.toBe(resultB?.lines[0]?.[0]?.color)
    oldJson.resolve(json('keyword.control'))
    expect(await late).toBeNull()
    const requestCount = requests.length
    expect(await highlight(a)).toEqual(freshA)
    expect(await highlight(b)).toEqual(resultB)
    expect(requests).toHaveLength(requestCount)
  })

  it.each([false, true])('does not let an old grammar list erase its replacement (failed=%s)', async fail => {
    const { pluginGrammars, resetPluginGrammars } = await import('@/lib/highlight/grammars')
    const a = host('A'); const pending = deferred<unknown>()
    const fixture: SyntaxGrammar = { id: 'list-race', plugin_id: PLUGIN, name: 'List Race',
      extensions: ['.list-race'], aliases: [], scope_name: 'source.list-race',
      grammar_url: moduleUrl('A', 'list-race.json') }
    let first = true
    respond = () => first ? (first = false, pending.promise) : [fixture]
    const old = pluginGrammars(a.session)
    resetPluginGrammars(a.session)
    const fresh = await pluginGrammars(a.session)
    pending.resolve(fail ? new Response('{}', { status: 500 }) : [])
    expect((await old).byId.size).toBe(0)
    expect(await pluginGrammars(a.session)).toBe(fresh)
    expect(fresh.byId.has('list-race')).toBe(true)
    expect(requests).toHaveLength(2)
  })

  it('normalizes documented legacy grammar payloads only; rejects foreign, external or escaping asset URLs', async () => {
    const { fetchGrammar, pluginGrammars } = await import('@/lib/highlight/grammars')
    const a = host('A')
    const fixture: SyntaxGrammar = { id: 'legacy-payload', plugin_id: PLUGIN, name: 'Legacy Payload',
      extensions: ['.legacy-grammar'], aliases: [], scope_name: 'source.legacy-payload',
      grammar_url: `/api/plugins/${PLUGIN}/assets/legacy.json?v=${VERSION}` }
    respond = url => url.pathname.endsWith('/plugins/grammars') ? [fixture] : {
      name: 'Human Name', scopeName: 'source.authoritative', patterns: [],
    }
    const bound = (await pluginGrammars(a.session)).byId.get(fixture.id)!
    expect(assertScoped(bound.grammar_url, 'A', `/plugins/${PLUGIN}/assets/legacy.json`).searchParams.get('v')).toBe(VERSION)
    expect(await fetchGrammar(bound, a.session)).toMatchObject({ name: fixture.id, scopeName: 'source.authoritative' })
    const before = requests.length
    for (const url of [
      moduleUrl('B', 'legacy.json'),
      `${moduleUrl('A', 'legacy.json')}&project_id=B`,
      `${moduleUrl('A', 'legacy.json')}&epoch=wrong`,
      `/api/plugins/${PLUGIN}/assets/%2e%2e/private.json`,
      `/api/plugins/${PLUGIN}/assets/%2fprivate.json`,
      `/api/plugins/not-the-owner/assets/legacy.json`,
      `https://other.example/api/plugins/${PLUGIN}/assets/legacy.json`,
    ]) await expect(fetchGrammar({ ...fixture, grammar_url: url }, a.session)).rejects.toThrow()
    expect(requests).toHaveLength(before)
  })
})


describe('nested previewers and mounted editor lifetime', () => {
  it('opens the same registered page after page → pipeline → page without borrowing that editor', async () => {
    const a = host('A'); const b = host('B')
    const writeA = vi.fn(); const writeB = vi.fn()
    const stopB = provideNodeParamWriter(writeB, b.session)
    const runtimes = new Map<string, PluginRuntime>()
    const importer = vi.fn(async (url: string) => factory(url, runtime => { runtimes.set(url.includes('viewer.js') ? 'viewer' : 'page', runtime) }))
    const loader = new PluginLoader(a, importer)
    pages.set('A', [page('A')])
    await loader.loadPages()
    a.router.addRoute({ path: '/pipeline', component: defineComponent({ setup() {
      onBeforeUnmount(provideNodeParamWriter(writeA, a.session))
      return () => h('div', 'pipeline')
    } }) })
    await a.router.push('/same-page')
    const view = mountRenderer(a, loader)
    await vi.waitFor(() => expect(runtimes.has('page')).toBe(true))
    const retained = runtimes.get('page')!
    await a.router.push('/pipeline')
    await nextTick()
    await a.router.push('/same-page')
    await nextTick()
    expect(runtimes.get('page')).toBe(retained)
    const fallback = respond
    respond = (url, init) => url.pathname.endsWith('/explorer/file') ? { path: 'same.vox', previewer: {
      id: 'same-viewer', plugin_id: PLUGIN, component_url: moduleUrl('A', 'viewer.js'),
    } } : fallback(url, init)
    const result = await retained.filePreviewer('same.vox')
    expect(result.component).not.toBeNull()
    expect(view.container.textContent).toContain('page.js')
    // The page remains useful even while a later, unrelated editor is mounted.
    const stopA = provideNodeParamWriter(writeA, a.session)
    await expect(retained.filePreviewer('same.vox')).resolves.toMatchObject({ preview: { path: 'same.vox' } })
    expect(writeA).not.toHaveBeenCalled(); expect(writeB).not.toHaveBeenCalled()
    stopA(); stopB()
  })

  it('keeps an ordinary preview runtime and its pending nested preview independent of later editors', async () => {
    const a = host('A')
    let runtime!: PluginRuntime
    const nestedRuntimes: PluginRuntime[] = []
    const loader = pluginLoader(a)
    vi.spyOn(loader as unknown as { importer: (url: string) => Promise<PluginModule> }, 'importer')
      .mockImplementation(async url => factory(url.includes('nested.js') ? 'NestedPreview' : 'ExplorerPreview', value => {
        if (url.includes('nested.js')) nestedRuntimes.push(value)
        else runtime = value
      }))
    const wrapper = loader.component(moduleUrl('A', 'viewer.js'), PLUGIN)
    const view = mountRenderer(a, loader, wrapper)
    await vi.waitFor(() => expect(view.container.textContent).toContain('ExplorerPreview'))
    const pending = deferred<unknown>()
    respond = () => pending.promise
    const completion = runtime.filePreviewer('same.vox')
    const write = vi.fn(); const stop = provideNodeParamWriter(write, a.session)
    pending.resolve({ previewer: { id: 'nested', plugin_id: PLUGIN, component_url: moduleUrl('A', 'nested.js') } })
    const nested = await completion
    expect(nested.component).not.toBeNull()
    const nestedView = mountRenderer(a, loader, nested.component!)
    await vi.waitFor(() => expect(nestedView.container.textContent).toContain('NestedPreview'))
    expect(loader.component(moduleUrl('A', 'viewer.js'), PLUGIN)).not.toBe(wrapper)
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    runtime.setNodeParams('same-node', { stale: true })
    nestedRuntimes[0]!.setNodeParams('same-node', { nested: true })
    // A further nested viewer must still inherit the absence of a writer.
    respond = () => ({ previewer: { id: 'nested', plugin_id: PLUGIN, component_url: moduleUrl('A', 'nested.js') } })
    const deeper = await nestedRuntimes[0]!.filePreviewer('same.vox')
    expect(deeper.component).toBe(nested.component)
    expect(write).not.toHaveBeenCalled()
    stop()
  })

  it('registers pages without an editor capability even when their module is also a live node', async () => {
    const a = host('A'); const write = vi.fn()
    const stop = provideNodeParamWriter(write, a.session)
    const seen: PluginRuntime[] = []
    const loader = new PluginLoader(a, async () => factory('SharedModule', value => { seen.push(value) }))
    const node = loader.component(moduleUrl('A'), PLUGIN)
    await load(node)
    pages.set('A', [page('A')])
    await loader.loadPages()
    const registered = a.router.getRoutes().find(route => route.meta.pluginId === PLUGIN)!.components!.default!
    expect(registered).not.toBe(node)
    stop()
    await a.router.push('/same-page')
    const view = mountRenderer(a, loader)
    await vi.waitFor(() => expect(view.container.textContent).toContain('SharedModule'))
    respond = () => ({ path: 'same.vox' })
    await expect(seen[1]!.filePreviewer('same.vox')).resolves.toMatchObject({ component: null })
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    seen[0]!.setNodeParams('same-node', { oldNode: true })
    seen[1]!.setNodeParams('same-node', { page: true })
    expect(write).not.toHaveBeenCalled()
  })

  it('does not lend a replacement writer to a nested component resolved before but mounted after editor replacement', async () => {
    const a = host('A'); const b = host('B')
    const oldWrite = vi.fn(); const newWrite = vi.fn(); const writeB = vi.fn()
    const stopOld = provideNodeParamWriter(oldWrite, a.session)
    const stopB = provideNodeParamWriter(writeB, b.session)
    const retainedB = createRuntime(PLUGIN, b)
    const runtime = createRuntime(PLUGIN, a)
    const pending = deferred<PluginModule>(); let nested!: PluginRuntime
    const loader = pluginLoader(a)
    const importer = vi.spyOn(loader as unknown as { importer: (url: string) => Promise<PluginModule> }, 'importer')
      .mockImplementation(() => pending.promise)
    respond = () => ({ previewer: { id: 'nested', plugin_id: PLUGIN, component_url: moduleUrl('A', 'nested.js') } })
    const result = await runtime.filePreviewer('same.vox')
    const stopNew = provideNodeParamWriter(newWrite, a.session)
    const view = mountRenderer(a, loader, result.component!)
    await vi.waitFor(() => expect(importer).toHaveBeenCalledTimes(1))
    pending.resolve(factory('LateNested', value => { nested = value }))
    await vi.waitFor(() => expect(view.container.textContent).toContain('LateNested'))
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    runtime.setNodeParams('same-node', { stale: true })
    nested.setNodeParams('same-node', { staleNested: true })
    await expect(nested.filePreviewer('same.vox')).rejects.toThrow('Pipeline editor binding has been replaced')
    expect(oldWrite).not.toHaveBeenCalled(); expect(newWrite).not.toHaveBeenCalled()
    createRuntime(PLUGIN, a).setNodeParams('same-node', { current: true })
    retainedB.setNodeParams('same-node', { stillB: true })
    expect(newWrite).toHaveBeenCalledExactlyOnceWith('same-node', { current: true })
    expect(writeB).toHaveBeenCalledExactlyOnceWith('same-node', { stillB: true })
    stopOld(); stopNew(); stopB()
  })

  it('resolves a delayed previewer through its captured host, not the active legacy project', async () => {
    const a = host('A'); const b = host('B')
    const pending = deferred<unknown>()
    respond = url => url.pathname.endsWith('/plugins') ? [loadedPlugin()] : pending.promise
    const completion = createRuntime(PLUGIN, a).filePreviewer('same.vox')
    legacyProject = 'B'
    pending.resolve({ path: 'A/same.vox', previewer: {
      id: 'same-viewer', plugin_id: PLUGIN, component_url: moduleUrl('A', 'viewer.js'),
    } })
    const result = await completion
    expect(result.preview.path).toBe('A/same.vox')
    expect(result.component).toBe(pluginComponent(moduleUrl('A', 'viewer.js'), PLUGIN, 'Previewer', a))
    expect(result.component).not.toBe(pluginComponent(moduleUrl('B', 'viewer.js'), PLUGIN, 'Previewer', b))
    assertScoped(requests[0]!.url.href, 'A', '/explorer/file')
  })

  it('cannot pass a replacement editor writer into a delayed nested previewer', async () => {
    const a = host('A'); const oldWriter = vi.fn(); const replacement = vi.fn()
    const withdrawOld = provideNodeParamWriter(oldWriter, a.session)
    const runtime = createRuntime(PLUGIN, a)
    const pending = deferred<unknown>()
    respond = url => url.pathname.endsWith('/plugins') ? [loadedPlugin()] : pending.promise
    const completion = runtime.filePreviewer('same.vox')
    const withdrawNew = provideNodeParamWriter(replacement, a.session)
    pending.resolve({ previewer: { id: 'same-viewer', plugin_id: PLUGIN, component_url: moduleUrl('A', 'viewer.js') } })
    await expect(completion).rejects.toThrow('Pipeline editor binding has been replaced')
    expect(oldWriter).not.toHaveBeenCalled(); expect(replacement).not.toHaveBeenCalled()
    withdrawOld(); withdrawNew()
  })

  it('remounts only a changed pipeline route so stale node callbacks cannot write its same-id node', async () => {
    const { default: AppShell } = await import('@/components/layout/AppShell.vue')
    const a = host('A')
    const mounted: string[] = []
    const runtimes = new Map<string, PluginRuntime>()
    const writes = vi.fn()
    const editor = defineComponent({
      props: { id: { type: String, required: true } },
      setup(props) {
        const id = props.id
        mounted.push(id)
        const withdraw = provideNodeParamWriter((node, patch) => writes(id, node, patch), a.session)
        onBeforeUnmount(withdraw)
        runtimes.set(id, createRuntime(PLUGIN, a))
        return () => h('div', props.id)
      },
    })
    a.router.addRoute({ path: '/pipelines/:id', name: 'pipeline-editor', component: editor, props: true })
    await a.router.push('/pipelines/first')
    const container = document.createElement('div')
    const app = createApp(AppShell).use(a.router)
    document.body.append(container)
    app.mount(container)
    try {
      await nextTick()
      await a.router.push('/pipelines/second')
      await nextTick()
      expect(mounted).toEqual(['first', 'second'])
      expect(container.textContent).toContain('second')
      vi.spyOn(console, 'warn').mockImplementation(() => {})
      runtimes.get('first')!.setNodeParams('same-node', { stale: true })
      runtimes.get('second')!.setNodeParams('same-node', { current: true })
      expect(writes.mock.calls).toEqual([['second', 'same-node', { current: true }]])
    } finally {
      app.unmount()
      container.remove()
    }
  })
})
