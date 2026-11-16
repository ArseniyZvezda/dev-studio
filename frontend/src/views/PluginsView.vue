<script setup lang="ts">
/**
 * Plugin registry: enable/disable per project (spec §9.4). A plugin's isolated environment
 * is built by the Studio itself (§2.1), so the card only says where that build stands.
 */
import { Blocks, Boxes, Loader2, Power } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import type { LoadedPlugin } from '@/api/types'
import ClampText from '@/components/ui/ClampText.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import { hasMessage, t } from '@/i18n'
import { rendererPluginHost } from '@/plugin-runtime'
import { pluginLoader } from '@/plugin-runtime/loader'

const { api, events, session: projectSession } = rendererPluginHost
const loader = pluginLoader(rendererPluginHost)
const assertCurrent = projectSession.capture()
let disposed = false
function isCurrent(): boolean {
  if (disposed) return false
  try { assertCurrent(); return true } catch { return false }
}

const plugins = ref<LoadedPlugin[]>([])
const enabledFilter = ref<'all' | 'enabled' | 'disabled'>('all')
const filters = ['all', 'enabled', 'disabled'] as const
const filteredPlugins = computed(() => plugins.value.filter(plugin => enabledFilter.value === 'all'
  || plugin.enabled === (enabledFilter.value === 'enabled')))
function filterCount(filter: typeof filters[number]): number {
  return plugins.value.filter(plugin => filter === 'all' || plugin.enabled === (filter === 'enabled')).length
}
const loading = ref(true)
const listError = ref<string | null>(null)
const mutationError = ref<string | null>(null)
const error = computed(() => mutationError.value ?? listError.value ?? loader.state.error)
const busyId = ref<string | null>(null)
let listRequest: Promise<void> | null = null
let listDirty = false
let envRevision = 0
const envUpdates = new Map<string, { revision: number; env: LoadedPlugin['env'] }>()

/** The environment's state in words — `pending` reads differently on a disabled card. */
function envLabel(plugin: LoadedPlugin): string {
  const env = plugin.env
  if (!env) return ''
  if (env.state !== 'pending') return t(`plugins.card.env.${env.state}`)
  if (plugin.status === 'loaded') return t('plugins.card.env.queued')
  return plugin.enabled ? t('plugins.card.env.notBuilt') : t('plugins.card.env.onEnable')
}

// A build starts and ends on its own, so the card follows the backend rather than a click.
const stopEnvironment = events.on('plugin.env', ({ plugin }) => {
  if (!isCurrent()) return
  envUpdates.set(plugin.id, { revision: ++envRevision, env: plugin.env })
  const index = plugins.value.findIndex(entry => entry.id === plugin.id)
  // Environment progress must not revive an old enabled/status inventory snapshot.
  if (index >= 0) plugins.value[index] = { ...plugins.value[index]!, env: plugin.env }
})
const stopInventory = events.on('plugin.changed', () => {
  if (!isCurrent()) return
  envUpdates.clear()
  refreshList(true)
})
const stopResync = events.on('system.dropped', ({ resync }) => {
  if (resync && isCurrent()) refreshList(true)
})
onBeforeUnmount(() => {
  disposed = true
  stopEnvironment()
  stopInventory()
  stopResync()
  envUpdates.clear()
})

/**
 * Where a plugin comes from, in words — `plugins.scope.<scope>`, looked up while rendering so it
 * follows the interface language. A scope this build has no words for is shown as it arrives.
 */
function scopeHint(scope: string): string {
  const key = `plugins.scope.${scope}`
  return hasMessage(key) ? t(key) : scope
}

onMounted(() => refreshList())

function refreshList(invalidate = false): void {
  void load(invalidate).catch(cause => {
    if (isCurrent()) listError.value = cause instanceof Error ? cause.message : String(cause)
  })
}

/** Coalesce reads; an inventory event during a read requires one fresh snapshot (§5.3.2). */
function load(invalidate = false): Promise<void> {
  if (!isCurrent()) return Promise.resolve()
  if (listRequest) {
    if (invalidate) listDirty = true
    return listRequest
  }
  listDirty = true
  loading.value = true
  listRequest = Promise.resolve().then(async () => {
    do {
      if (!isCurrent()) return
      listDirty = false
      const revision = envRevision
      try {
        const inventory = await api.plugins()
        if (!isCurrent()) return
        if (listDirty) continue
        // An in-flight list must not overwrite newer environment progress events.
        plugins.value = inventory.map(plugin => {
          const update = envUpdates.get(plugin.id)
          return update && update.revision > revision ? { ...plugin, env: update.env } : plugin
        })
        listError.value = null
      } catch (cause) {
        if (!isCurrent()) return
        if (listDirty) continue
        throw cause
      }
    } while (listDirty && isCurrent())
  }).then(() => finishLoad(), cause => finishLoad(cause, true))
  return listRequest
}

function finishLoad(cause?: unknown, failed = false): Promise<void> | void {
  listRequest = null
  if (!isCurrent()) return
  // An invalidation may arrive after the loop settles but before this continuation.
  // Keep all existing callers joined to its replacement, including after an error.
  if (listDirty) return load()
  loading.value = false
  if (failed) throw cause
}

async function toggle(plugin: LoadedPlugin) {
  if (busyId.value || !isCurrent()) return
  busyId.value = plugin.id
  mutationError.value = null
  let mutated = false
  try {
    await (plugin.enabled ? api.disablePlugin(plugin.id) : api.enablePlugin(plugin.id))
    mutated = true
    if (!isCurrent()) return
    // Only plugin.changed owns runtime invalidation, regardless of HTTP/WS ordering.
    await loader.waitForRefresh()
    if (!isCurrent()) return
    await load(true)
  } catch (cause) {
    if (isCurrent()) {
      const message = cause instanceof Error ? cause.message : String(cause)
      if (!mutated) mutationError.value = message
      // Keep runtime failures reactive: a successful later event clears loader.state.error.
      else if (!loader.state.error) listError.value = message
    }
  } finally {
    if (isCurrent()) busyId.value = null
  }
}
</script>

<template>
  <div class="flex h-full flex-col">
    <header class="vxs-pane shrink-0 border-b border-pane bg-surface px-pane py-2">
      <h1 class="text-title font-medium">{{ t('plugins.title') }}</h1>
      <p class="text-small text-faint">{{ t('plugins.warning') }}</p>
    </header>

    <p v-if="error" class="shrink-0 bg-danger-soft px-pane py-1 text-danger">{{ error }}</p>

    <div class="min-h-0 flex-1 overflow-auto p-pane">
      <div v-if="plugins.length" role="group" :aria-label="t('plugins.filters.label')" class="mb-3 flex flex-wrap gap-1.5">
        <button v-for="filter in filters" :key="filter" type="button"
          class="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-small hover:bg-hover"
          :class="enabledFilter === filter ? 'border-accent-border bg-accent-soft text-accent' : 'border-line text-muted'"
          :aria-pressed="enabledFilter === filter" @click="enabledFilter = filter">
          {{ t(`plugins.filters.${filter}`) }}
          <span class="font-mono tabular-nums">{{ filterCount(filter) }}</span>
        </button>
      </div>
      <!-- Bars only for a first read: enabling a plugin re-reads the list, and swapping it for a
           placeholder there would take every card off screen and bring them back in (§7). Each
           card is one of the page's bands (`vxs-enter`). -->
      <LoadingBars v-if="loading && !plugins.length" />

      <EmptyState v-else-if="!plugins.length" class="vxs-enter" :title="t('plugins.empty')">
        <template #icon><Blocks class="mx-auto mb-3 size-8 text-faint" /></template>
      </EmptyState>

      <!-- Held to a reading width: a card is mostly its description, and across a wide window
           that ran to lines of two hundred characters. -->
      <EmptyState v-else-if="!filteredPlugins.length" :title="t('plugins.filters.empty')" />
      <ul v-else class="max-w-4xl space-y-2">
        <li
          v-for="plugin in filteredPlugins"
          :key="plugin.id"
          class="vxs-enter rounded-lg border border-line bg-surface p-3"
          :class="{ 'opacity-60': !plugin.enabled }"
        >
          <div class="flex items-start gap-3">
            <Boxes class="mt-0.5 size-5 shrink-0 text-accent" />

            <div class="min-w-0 flex-1">
              <p class="flex items-baseline gap-2">
                <span class="truncate font-medium">{{ plugin.name }}</span>
                <span class="font-mono text-small text-faint">{{ plugin.version }}</span>
                <span
                  v-if="plugin.status === 'error'"
                  class="rounded-sm bg-danger-soft px-1.5 text-small text-danger"
                >
                  {{ t('plugins.card.error') }}
                </span>
              </p>
              <p class="truncate text-small text-faint">
                {{ plugin.id }} · {{ scopeHint(plugin.scope) }}
                <template v-if="plugin.author"> · {{ plugin.author }}</template>
              </p>
              <ClampText v-if="plugin.description" class="mt-1 text-muted" :text="plugin.description" />
              <p v-if="plugin.error" class="mt-1 font-mono text-small text-danger">
                {{ plugin.error }}
              </p>
              <p class="mt-1 text-small text-faint">
                {{ t('plugins.card.nodeTypes', { count: plugin.node_types.length }) }} ·
                {{ t('plugins.card.pages', { count: plugin.pages.length }) }}
                <template v-if="plugin.previewers.length">
                  · {{ t('plugins.card.previewers', { count: plugin.previewers.length }) }}
                </template>
                <template v-if="plugin.search_providers.length">
                  · {{ t('plugins.card.search', { providers: plugin.search_providers.join(', ') }) }}
                </template>
                <template v-if="plugin.env">
                  ·
                  <span
                    class="inline-flex items-center gap-1"
                    :class="{ 'text-danger': plugin.env.state === 'failed' }"
                  >
                    <Loader2 v-if="plugin.env.state === 'building'" class="size-3 animate-spin text-accent" />
                    {{ envLabel(plugin) }}
                  </span>
                </template>
              </p>
              <template v-if="plugin.env?.state === 'failed'">
                <pre
                  v-if="plugin.env.error"
                  class="mt-1 max-h-32 overflow-auto font-mono text-small whitespace-pre-wrap text-danger"
                >{{ plugin.env.error }}</pre>
                <p class="mt-1 text-small text-faint">{{ t('plugins.card.env.retry') }}</p>
              </template>
            </div>

            <div class="flex shrink-0 gap-2">
              <button
                class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                :disabled="busyId !== null"
                @click="toggle(plugin)"
              >
                <Power class="size-3.5" />
                {{ plugin.enabled ? t('common.disable') : t('common.enable') }}
              </button>
            </div>
          </div>
        </li>
      </ul>
    </div>
  </div>
</template>
