/** Project-bound UI modules and routes (dev spec §5.3.2, §9.3). */
import { defineAsyncComponent, defineComponent, h, markRaw, reactive, toRaw, type Component } from 'vue'
import { isNavigationFailure, NavigationFailureType } from 'vue-router'
import { withTokenParam } from '@/api/auth'
import type { LoadedPlugin, PluginPage } from '@/api/types'
import { t } from '@/i18n'
import { resetPluginGrammars } from '@/lib/highlight/grammars'
import MissingPluginPage from './MissingPluginPage.vue'
import { createRuntime, pluginResourceUrl, rendererPluginHost, type PluginHost } from './index'
import { captureNodeParamBinding, type NodeParamBinding } from './node-params'

export type PluginModule = {
  default?: Component | ((runtime: ReturnType<typeof createRuntime>) => Component)
  setup?: (runtime: ReturnType<typeof createRuntime>) => Component
}
type ModuleImporter = (url: string) => Promise<PluginModule>
const importModule: ModuleImporter = (url) => import(/* @vite-ignore */ url)
const LoadingPane = () => h('div', { class: 'grid h-full place-items-center text-muted' }, t('common.loading'))

/** State belongs to a captured renderer, never to the selected legacy project. */
export class PluginLoader {
  readonly navItems = reactive<PluginPage[]>([])
  readonly state = reactive({ loading: false, error: null as string | null })
  private components = new Map<string, Component>()
  private routes: (() => void)[] = []
  private cleanups = new Set<() => void>()
  // component() is also consumed by computed custom-node/previewer registries.
  // Notify those consumers on withdrawal AND recovery, not just page routers.
  private readonly registration = reactive({ generation: 0, invalidated: false })
  private get generation(): number { return this.registration.generation }
  private set generation(value: number) { this.registration.generation = value }
  private get invalidated(): boolean { return this.registration.invalidated }
  private set invalidated(value: boolean) { this.registration.invalidated = value }
  private revision = 0
  private inventory: string | null = null
  private pending = false
  private refresh: { promise: Promise<void> } | null = null
  private owners = 0
  private subscriptions: (() => void)[] = []
  private activePlugins: ReadonlySet<string> | null = null
  private pluginSnapshot: ReadonlySet<string> | null = null
  private resyncPlugins = false

  constructor(readonly host: PluginHost, private readonly importer: ModuleImporter = importModule) {}

  /** Subscribe before connecting the renderer's socket; never own/disconnect the socket.
   * Multiple mounted consumers share one subscription and one refresh queue (§5.3.2).
   */
  start(): () => void {
    if (this.owners++ === 0) {
      this.subscriptions = [
        this.host.events.on('plugin.changed', data => this.changed(data.plugins)),
        this.host.events.on('system.dropped', data => { if (data.resync) this.changed() }),
      ]
    }
    let stopped = false
    return () => {
      if (stopped) return
      stopped = true
      if (--this.owners !== 0) return
      this.subscriptions.splice(0).forEach(stop => stop())
      this.cancelRefresh()
      // The shared socket may keep running with no mounted plugin owner. Changes
      // missed during that interval require authoritative inventory on reacquire.
      this.pluginSnapshot = null
      this.resyncPlugins = true
      this.withdraw()
    }
  }

  private changed(plugins?: LoadedPlugin[]): void {
    // Revoke retained writes NOW, not when REST eventually returns. A second gap
    // invalidates the in-flight snapshot even if it has the same ids/source stamps.
    this.revision += 1
    // A retained preview/node descriptor is not authority to resurrect a disabled
    // module. The change event carries the inventory; a gap must recover it by REST.
    this.pluginSnapshot = plugins ? this.enabledPlugins(plugins) : null
    this.resyncPlugins = plugins === undefined
    if (!this.invalidated || this.components.size || this.cleanups.size || this.routes.length) this.withdraw()
    this.invalidated = true
    this.pending = true
    // An event has no promise consumer. Keep the failure observable to the mounted
    // host and to a local toggle joining loadPages(), rather than reporting success.
    void this.ensureRefresh().catch(() => {})
  }

  private capture(): () => void {
    const current = this.host.session.capture()
    // Capability checks are imperative, not render dependencies. URL helpers are
    // valid in render; reading the reactive registry here would schedule the old
    // plugin's render on withdrawal, where the same helper must now reject.
    const registration = toRaw(this.registration)
    const generation = registration.generation
    const registered = !registration.invalidated
    return () => {
      current()
      if (!registered || generation !== registration.generation || registration.invalidated) throw new Error('Plugin registration has been replaced')
    }
  }

  /** Own the render dependency outside plugin code (dev spec §5.3.2).
   * Route removal alone leaves RouterView's previous match mounted until the async
   * inventory/navigation finishes. This parent retires that subtree on the next
   * Vue flush, before any queued child render; retained capabilities revoke NOW.
   * Generation equality prevents reset/retry from reviving an old mounted tree.
   */
  private registeredComponent(component: Component, generation: number): Component {
    return markRaw(defineComponent({
      name: component.name ?? 'PluginRegistration',
      inheritAttrs: false,
      setup: (_, { attrs, slots }) => () =>
        !this.invalidated && generation === this.generation
          ? h(component, attrs, slots)
          : h(LoadingPane),
    }))
  }

  /** Shared by pages, previewers and custom node UI; URL includes project/source stamp. */
  component(componentUrl: string, pluginId: string, what = 'Plugin module',
    binding: NodeParamBinding = captureNodeParamBinding(this.host.session)): Component {
    const url = pluginResourceUrl(componentUrl, this.host.session, pluginId)
    // Do not cache a permanently rejected wrapper if the palette's REST finishes
    // before the page snapshot. Readiness is reactive, so it will retry on recovery.
    if (this.invalidated) return LoadingPane
    if (this.activePlugins && !this.activePlugins.has(pluginId)) return MissingPluginPage
    const key = JSON.stringify([this.host.session.scope, pluginId, url, this.generation,
      binding.generation])
    const cached = this.components.get(key)
    if (cached) return cached
    const current = this.capture()
    const generation = this.generation
    // Native import() also caches by URL. Rebuilding just the Vue wrapper would keep
    // the previous module's globals/code after a registry reload with an equal stamp.
    const importUrl = `${url}${url.includes('?') ? '&' : '?'}studio_generation=${this.generation}`
    const component = defineAsyncComponent({
      loader: async () => {
        current()
        // Capture BEFORE await: a late A import cannot get B's runtime or registration.
        const module = await this.importer(withTokenParam(importUrl))
        current()
        const runtime = createRuntime(pluginId, this.host, current, binding,
          cleanup => this.cleanups.add(cleanup))
        const entry = module.setup ?? module.default
        if (typeof entry === 'function') {
          const produced = (entry as (r: ReturnType<typeof createRuntime>) => Component)(runtime)
          current()
          if (produced && typeof produced === 'object') return this.registeredComponent(produced, generation)
          return this.registeredComponent(entry as Component, generation)
        }
        if (entry && typeof entry === 'object') return this.registeredComponent(entry, generation)
        throw new Error(`${what} exported no usable component`)
      },
      loadingComponent: LoadingPane,
      errorComponent: MissingPluginPage,
      timeout: 15_000,
    })
    this.components.set(key, component)
    return component
  }

  /** Withdraw only this renderer's registrations; invalidates pending factories too. */
  reset(): void {
    this.cancelRefresh()
    this.withdraw()
    this.invalidated = false
  }

  private cancelRefresh(): void {
    this.revision += 1
    // Detach, rather than await, an abandoned request. A subsequent recovery must
    // not be blocked by a response belonging to a disposed owner.
    this.refresh = null
    this.pending = false
    this.state.loading = false
    this.inventory = null
  }

  private withdraw(): void {
    this.invalidated = true
    this.generation += 1
    for (const cleanup of this.cleanups) cleanup()
    this.cleanups.clear()
    for (const remove of this.routes) remove()
    this.routes = []
    this.components.clear()
    this.navItems.splice(0)
    resetPluginGrammars(this.host.session)
  }

  /** Ordinary reconciliation is idempotent. In particular a local toggle joins the
   * event's refresh without a second reset, and token/retry reads preserve editors.
   */
  loadPages(): Promise<void> {
    if (!this.refresh) this.pending = true
    return this.ensureRefresh()
  }

  /** Mutations publish plugin.changed. Join that lifecycle instead of racing the
   * socket with a second inventory fetch/reset; a later event remains observable
   * through state.error even when HTTP and WS complete in the opposite order.
   */
  waitForRefresh(): Promise<void> {
    if (this.refresh) return this.refresh.promise
    return this.state.error === null ? Promise.resolve() : Promise.reject(new Error(this.state.error))
  }

  private ensureRefresh(): Promise<void> {
    if (this.refresh) return this.refresh.promise
    const job = { promise: Promise.resolve() }
    this.refresh = job
    this.state.loading = true
    job.promise = Promise.resolve().then(async () => {
      while (this.pending && this.refresh === job) {
        this.pending = false
        const revision = this.revision
        try {
          const current = this.host.session.capture()
          const [pages, plugins] = await Promise.all([
            this.host.api.pluginPages(),
            this.resyncPlugins ? this.host.api.plugins().then(plugins => this.enabledPlugins(plugins))
              : Promise.resolve(this.pluginSnapshot),
          ])
          current()
          if (this.refresh !== job) return
          if (revision !== this.revision) continue
          this.validatePages(pages)
          if (plugins && pages.some(page => !plugins.has(page.plugin_id))) {
            throw new Error('Plugin page inventory has an inactive owner')
          }
          if (plugins) this.activePlugins = plugins
          const inventory = JSON.stringify(pages)
          if (this.invalidated || inventory !== this.inventory) {
            // Initial registration has nothing to revoke. Unchanged ordinary reads
            // must not reset grammar registries or custom node/preview components.
            if (!this.invalidated && this.inventory !== null) this.withdraw()
            if (this.invalidated) this.components.clear()
            this.invalidated = false
            try {
              this.installPages(pages)
              const route = this.host.router.currentRoute.value
              if (route.name === 'not-found' || route.meta.pluginId) {
                const failure = await this.host.router.replace(route.fullPath)
                // Vue Router resolves, rather than rejects, a guard cancellation.
                // A duplicate is harmless; an aborted replacement leaves old UI
                // with a revoked runtime and must remain an observable retry.
                if (isNavigationFailure(failure) && !isNavigationFailure(failure, NavigationFailureType.duplicated)) throw failure
              }
              if (this.refresh !== job) return
              current()
              if (revision !== this.revision) continue
            } catch (error) {
              if (this.refresh === job && revision === this.revision) {
                // A bad route/failing navigation cannot leave a half-installed
                // registry which the next retry would mistake for a success.
                this.withdraw()
                this.invalidated = true
              }
              throw error
            }
            this.inventory = inventory
          }
          if (this.refresh !== job) return
          current()
          if (revision !== this.revision) continue
          this.pluginSnapshot = plugins
          this.resyncPlugins = false
          this.state.error = null
        } catch (error) {
          if (this.refresh !== job) return
          if (revision !== this.revision) continue
          this.state.error = error instanceof Error ? error.message : String(error)
          throw error
        }
      }
    }).then(() => this.finishRefresh(job), error => this.finishRefresh(job, error, true))
    return job.promise
  }

  private finishRefresh(job: { promise: Promise<void> }, error?: unknown, failed = false): Promise<void> | void {
    if (this.refresh !== job) return
    this.refresh = null
    this.state.loading = false
    // A signal can land between the worker's last continuation and this one.
    // Join its successor even if the overtaken attempt failed at that boundary.
    if (this.pending) return this.ensureRefresh()
    if (failed) throw error
  }

  private validatePages(pages: PluginPage[]): void {
    for (const page of pages) {
      if (!page.route.startsWith('/') || page.route.startsWith('//') || /^\/p(?:\/|$)/.test(page.route)) {
        throw new Error('Plugin routes must be project-relative')
      }
      if (page.component_url) pluginResourceUrl(page.component_url, this.host.session, page.plugin_id)
    }
  }

  private enabledPlugins(plugins: LoadedPlugin[]): ReadonlySet<string> {
    return new Set(plugins.filter(plugin => plugin.enabled && plugin.status === 'loaded').map(plugin => plugin.id))
  }

  private installPages(pages: PluginPage[]): void {
    for (const remove of this.routes) remove()
    this.routes = []
    this.navItems.splice(0, this.navItems.length, ...pages)
    const { router, session } = this.host
    for (const page of pages) {
      const name = Symbol(JSON.stringify([session.scope, page.plugin_id, page.id, page.component_url]))
      this.routes.push(router.addRoute({
        path: page.route,
        name,
        component: page.component_url
          ? this.component(page.component_url, page.plugin_id, `Plugin page ${page.id}`,
            captureNodeParamBinding(session, false))
          : MissingPluginPage,
        meta: { pluginId: page.plugin_id, title: page.title },
      }))
    }
  }
}

const loaders = new WeakMap<PluginHost, PluginLoader>()
export function pluginLoader(host: PluginHost = rendererPluginHost): PluginLoader {
  let loader = loaders.get(host)
  if (!loader) { loader = new PluginLoader(host); loaders.set(host, loader) }
  return loader
}
export const pluginNavItems = pluginLoader().navItems
export function pluginComponent(componentUrl: string, pluginId: string, what = 'Plugin module', host: PluginHost = rendererPluginHost,
  binding?: NodeParamBinding): Component {
  return pluginLoader(host).component(componentUrl, pluginId, what, binding)
}
export function loadPluginPages(host: PluginHost = rendererPluginHost): Promise<void> {
  return pluginLoader(host).loadPages()
}
