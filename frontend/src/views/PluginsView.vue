<script setup lang="ts">
/** Plugin registry: enable/disable per project, provision isolated venvs (spec §9.4). */
import { Blocks, Boxes, HardDriveDownload, Power } from 'lucide-vue-next'
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { LoadedPlugin } from '@/api/types'
import ClampText from '@/components/ui/ClampText.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import { hasMessage, t } from '@/i18n'
import { resetPluginGrammars } from '@/lib/highlight/grammars'
import { loadPluginPages } from '@/plugin-runtime/loader'
import { usePipelinesStore } from '@/stores/pipelines'

const pipelines = usePipelinesStore()

const plugins = ref<LoadedPlugin[]>([])
const enabledFilter = ref<'all' | 'enabled' | 'disabled'>('all')
const filters = ['all', 'enabled', 'disabled'] as const
const filteredPlugins = computed(() => plugins.value.filter(plugin => enabledFilter.value === 'all'
  || plugin.enabled === (enabledFilter.value === 'enabled')))
function filterCount(filter: typeof filters[number]): number {
  return plugins.value.filter(plugin => filter === 'all' || plugin.enabled === (filter === 'enabled')).length
}
const loading = ref(true)
const error = ref<string | null>(null)
const busyId = ref<string | null>(null)
const envLog = ref<{ id: string; lines: string[] } | null>(null)

/**
 * Where a plugin comes from, in words — `plugins.scope.<scope>`, looked up while rendering so it
 * follows the interface language. A scope this build has no words for is shown as it arrives.
 */
function scopeHint(scope: string): string {
  const key = `plugins.scope.${scope}`
  return hasMessage(key) ? t(key) : scope
}

onMounted(load)

async function load() {
  loading.value = true
  try {
    plugins.value = await api.plugins()
    error.value = null
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

async function toggle(plugin: LoadedPlugin) {
  busyId.value = plugin.id
  try {
    await (plugin.enabled ? api.disablePlugin(plugin.id) : api.enablePlugin(plugin.id))
    await load()
    // The node palette and the nav both change shape when a plugin toggles.
    await pipelines.loadRegistry()
    await loadPluginPages()
    // So does the set of languages the Explorer can highlight: the grammar list is
    // cached after its first miss, so without this, enabling `angelscript` leaves `.as`
    // plain until a reload — which reads as the plugin not working.
    resetPluginGrammars()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    busyId.value = null
  }
}

async function installEnv(plugin: LoadedPlugin) {
  busyId.value = plugin.id
  envLog.value = null
  try {
    const result = await api.installPluginEnv(plugin.id)
    envLog.value = { id: plugin.id, lines: result.log }
    if (result.detail) error.value = result.detail
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    busyId.value = null
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
                <template v-if="plugin.has_backend">
                  · {{ plugin.has_venv ? t('plugins.card.venvReady') : t('plugins.card.venvNotBuilt') }}
                </template>
              </p>
            </div>

            <div class="flex shrink-0 gap-2">
              <button
                v-if="plugin.has_backend && !plugin.has_venv"
                class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                :disabled="busyId === plugin.id"
                @click="installEnv(plugin)"
              >
                <HardDriveDownload class="size-3.5" /> {{ t('plugins.card.buildEnv') }}
              </button>
              <button
                class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                :disabled="busyId === plugin.id"
                @click="toggle(plugin)"
              >
                <Power class="size-3.5" />
                {{ plugin.enabled ? t('common.disable') : t('common.enable') }}
              </button>
            </div>
          </div>

          <pre
            v-if="envLog?.id === plugin.id"
            class="mt-2 max-h-48 overflow-auto rounded-md bg-inset p-2 font-mono text-small whitespace-pre-wrap"
            >{{ envLog.lines.join('\n') }}</pre
          >
        </li>
      </ul>
    </div>
  </div>
</template>
