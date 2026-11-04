/**
 * Runtime loading of plugin-contributed UI (spec §9.3): menu pages, and the file
 * previewers the Explorer mounts for claimed file types (§6.2).
 *
 * Pages are registered with the router and the nav as soon as the registry is known;
 * the module behind each one is only fetched when the user first navigates to it,
 * courtesy of `defineAsyncComponent`.
 */

import { defineAsyncComponent, h, markRaw, reactive, type Component } from 'vue'

import { withTokenParam } from '@/api/auth'
import { api } from '@/api/client'
import type { PluginPage } from '@/api/types'
import { t } from '@/i18n'
import { router } from '@/router'
import MissingPluginPage from './MissingPluginPage.vue'
import { createRuntime } from './index'

export const pluginNavItems = reactive<PluginPage[]>([])

/** Modules may default-export a factory (preferred) or a ready-made component. */
type PluginModule = {
  default?: Component | ((runtime: ReturnType<typeof createRuntime>) => Component)
  setup?: (runtime: ReturnType<typeof createRuntime>) => Component
}

/**
 * Import a plugin's ES module and turn what it exports into a component.
 *
 * The token goes in the query string because a dynamic `import()` cannot set an
 * `Authorization` header — the same constraint that makes the WebSocket use `?token=`.
 */
async function resolvePluginComponent(
  componentUrl: string,
  pluginId: string,
  what: string,
): Promise<Component> {
  // `@vite-ignore` is required: the URL is only known at runtime, and Vite would
  // otherwise try to resolve and pre-bundle it during the build.
  const module: PluginModule = await import(/* @vite-ignore */ withTokenParam(componentUrl))
  const runtime = createRuntime(pluginId)

  const entry = module.setup ?? module.default
  if (typeof entry === 'function') {
    // Could be a factory or a functional component. A factory declares a parameter;
    // a functional component is called by Vue with (props, ctx) and would break here,
    // so only treat single-argument functions that return an object as factories.
    const produced = (entry as (r: ReturnType<typeof createRuntime>) => Component)(runtime)
    if (produced && typeof produced === 'object') return markRaw(produced)
    return markRaw(entry as Component)
  }
  if (entry && typeof entry === 'object') return markRaw(entry)

  throw new Error(`${what} exported no usable component`)
}

const componentCache = new Map<string, Component>()

/** A render function, so the word is looked up each time it is drawn and follows the language. */
const LoadingPane = () =>
  h('div', { class: 'grid h-full place-items-center text-muted' }, t('common.loading'))

/**
 * Async component for a plugin module URL, cached so re-selecting a file does not
 * rebuild the wrapper (and lose the module's already-resolved state) each time.
 */
export function pluginComponent(
  componentUrl: string,
  pluginId: string,
  what = 'Plugin module',
): Component {
  const cached = componentCache.get(componentUrl)
  if (cached) return cached
  const component = defineAsyncComponent({
    loader: () => resolvePluginComponent(componentUrl, pluginId, what),
    loadingComponent: LoadingPane,
    errorComponent: MissingPluginPage,
    timeout: 15_000,
  })
  componentCache.set(componentUrl, component)
  return component
}

export async function loadPluginPages(): Promise<void> {
  let pages: PluginPage[]
  try {
    pages = await api.pluginPages()
  } catch (error) {
    console.error('Could not load plugin pages', error)
    return
  }

  pluginNavItems.splice(0, pluginNavItems.length, ...pages)

  for (const page of pages) {
    if (router.hasRoute(`plugin:${page.id}`)) continue
    router.addRoute({
      path: page.route,
      name: `plugin:${page.id}`,
      component: page.component_url
        ? pluginComponent(page.component_url, page.plugin_id, `Plugin page ${page.id}`)
        : MissingPluginPage,
      meta: { pluginId: page.plugin_id, title: page.title },
    })
  }

  // A page may have been registered for the route we are already sitting on (a reload
  // straight onto a plugin URL lands on the catch-all first), so re-resolve it.
  if (router.currentRoute.value.name === 'not-found') {
    await router.replace(router.currentRoute.value.fullPath)
  }
}
