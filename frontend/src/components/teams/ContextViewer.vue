<script setup lang="ts">
/** Context inventory and proportional source map (§10.4). */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowLeft, Maximize2, LayoutGrid, RefreshCw, X } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { ContextCategory, ContextInventory, ContextSource, ContextTarget, ContextView } from '@/api/types'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import ContextFragmentText from './ContextFragmentText.vue'
import { t } from '@/i18n'
import { contextMap, retainedFragment } from '@/lib/context-map'
import { formatTokens } from '@/lib/status'

const props = defineProps<{ target: ContextTarget; revision: number | null }>()
const emit = defineEmits<{ close: [] }>()
const data = ref<ContextInventory | null>(null)
const localView = ref<ContextView>('auto')
const retryView = ref<ContextView | 'runtime'>('auto')
const loading = ref(false)
const inspecting = ref(false)
const error = ref('')
const selected = ref<ContextCategory | null>(null)
const expanded = ref(false)
const fragmentId = ref<string | null>(null)
const fullText = ref<string | null>(null)
const fragmentError = ref(false)
const retryFragment = ref(0)
const map = ref<HTMLElement | null>(null)
const content = ref<HTMLElement | null>(null)
const closeButton = ref<HTMLButtonElement | null>(null)
function categoryLabel(category: ContextCategory) {
  return category === 'task' && 'chatId' in props.target
    ? t('contextViewer.chatInput') : t(`contextViewer.categories.${category}`)
}
const colors: Record<ContextCategory, string> = {
  system: '#a78bfa', developer: '#b882b8', skills: '#56b8ed', documents: '#4dc9a5', task: '#e6b866',
  history: '#7a9ef0', tools: '#dc92c2', thinking: '#b6ba73', summary: '#75cdd1', unattributed: '#969da9',
  memory: '#d3a878', environment: '#80b3ac', tool_definitions: '#879bd4', agents: '#b3a5d9', runtime: '#99aaa4',
}
const groups = computed(() => {
  const grouped = new Map<ContextCategory, { id: ContextCategory; tokens: number; sources: ContextSource[] }>()
  for (const source of data.value?.sources ?? []) {
    const group = grouped.get(source.category) ?? { id: source.category, tokens: 0, sources: [] }
    group.tokens += source.tokens
    group.sources.push(source)
    grouped.set(source.category, group)
  }
  return [...grouped.values()].sort((a, b) => b.tokens - a.tokens)
})
const active = computed(() => groups.value.find(group => group.id === selected.value))
function fragmentLabel(source: ContextSource, index: number) {
  return source.label ? t('contextViewer.namedFragment', { index: index + 1, label: source.label })
    : t('contextViewer.fragment', { index: index + 1 })
}
interface MapEntry {
  id: string; category: ContextCategory; label: string; tokens: number; color: string
  source?: ContextSource
}
const entries = computed<MapEntry[]>(() => {
  if (expanded.value && active.value) {
    const { id: category, sources } = active.value
    return sources.map((source, index) => ({
      id: source.id, category, source, tokens: source.tokens,
      label: fragmentLabel(source, index),
      color: `color-mix(in srgb, ${colors[category]} ${100 - index % 5 * 10}%, ${index % 2 ? '#fff' : '#64748b'})`,
    }))
  }
  return groups.value.map(({ id, tokens }) => ({
    id, category: id, tokens, label: categoryLabel(id), color: colors[id],
  }))
})
const blocks = computed(() => contextMap(entries.value))
const total = computed(() => entries.value.reduce((sum, entry) => sum + entry.tokens, 0))
const fragment = computed(() => expanded.value
  ? entries.value.find(entry => entry.id === fragmentId.value) ?? entries.value[0]
  : undefined)
const selectedId = computed(() => fragment.value?.id ?? selected.value)
const detailSources = computed(() => {
  const source = fragment.value?.source
  if (!source) return active.value?.sources ?? []
  if (fullText.value === null) return [source]
  return [{ ...source, text: fullText.value, truncated: false }]
})
const percent = (tokens: number) => total.value ? `${(tokens / total.value * 100).toFixed(1)}%` : '0%'
const count = (value: number | null | undefined) => value == null ? '—' : formatTokens(value)

function selectEntry(entry: MapEntry) {
  if (entry.source) fragmentId.value = entry.id
  else selected.value = entry.category
}
function focusSelection() {
  void nextTick(() => {
    const selector = 'button[aria-pressed="true"]'
    const button = map.value?.querySelector<HTMLButtonElement>(selector)
      ?? content.value?.querySelector<HTMLButtonElement>('.source-rows ' + selector)
    button?.focus({ preventScroll: true })
  })
}
function expandCategory(category: ContextCategory, sourceId: string | null = null) {
  if (expanded.value || category === 'unattributed') return
  selected.value = category
  fragmentId.value = sourceId
  expanded.value = true
  wheelTime = -Infinity
  focusSelection()
}
function backToCategories() {
  expanded.value = false
  fragmentId.value = null
  wheelTime = -Infinity
  focusSelection()
}

// Trackpad momentum keeps sending wheel events after the tiles have been replaced.
// Consume the rest of that gesture; reversing direction can navigate immediately.
let wheelDirection = 0
let wheelTime = -Infinity
function wheelMap(event: WheelEvent) {
  if (event.ctrlKey || event.metaKey || !event.deltaY || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return
  const direction = Math.sign(event.deltaY)
  const continuing = direction === wheelDirection && event.timeStamp - wheelTime < 180
  if (!continuing) {
    if (direction > 0 && expanded.value) backToCategories()
    else if (direction < 0 && !expanded.value) {
      const tile = event.target instanceof Element ? event.target.closest('button[data-category]') : null
      const group = groups.value.find(group => group.id === tile?.getAttribute('data-category'))
      if (!group || group.id === 'unattributed') return
      expandCategory(group.id)
    } else return
  }
  event.preventDefault()
  wheelDirection = direction
  wheelTime = event.timeStamp
}
let generation = 0
async function load(view: ContextView | 'runtime' = localView.value) {
  const native = view === 'runtime'
  if (native && inspecting.value) return
  const current = ++generation
  loading.value = true
  inspecting.value = native
  error.value = ''
  try {
    const result = native
      ? await api.inspectSessionContext(props.target)
      : await api.sessionContext(props.target, view)
    if (current !== generation) return
    const previous = fragment.value?.source
    if (data.value?.origin !== result.origin || localView.value !== view) {
      expanded.value = false
      fragmentId.value = null
    } else if (previous) {
      fragmentId.value = retainedFragment(previous, result.sources)?.id ?? null
      expanded.value = fragmentId.value !== null
    }
    data.value = result
    localView.value = native ? 'auto' : view
    if (fragmentError.value) retryFragment.value++
    if (!groups.value.some(group => group.id === selected.value)) {
      expanded.value = false
      fragmentId.value = null
      selected.value = (groups.value.find(group => group.id !== 'unattributed') ?? groups.value[0])?.id ?? null
    }
  } catch (cause) {
    if (current === generation) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      retryView.value = view
    }
  } finally {
    if (current === generation) { loading.value = false; inspecting.value = false }
  }
}
function trapFocus(event: KeyboardEvent) {
  const focusable = [...(content.value?.querySelectorAll<HTMLElement>('button:not(:disabled), summary, [tabindex="0"]') ?? [])]
  const first = focusable[0]
  const last = focusable.at(-1)
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(() => {
  void load()
  void nextTick(() => closeButton.value?.focus())
})
watch(() => props.revision, () => { void load() })
watch([() => fragment.value?.id, () => fragment.value?.source?.text_key, localView, retryFragment], async ([id, key, view], _, onCleanup) => {
  fullText.value = null
  fragmentError.value = false
  if (!id || !key) return
  let current = true
  onCleanup(() => { current = false })
  try {
    const result = await api.sessionContextFragment(props.target, id, key, view)
    if (current) fullText.value = result.text
  } catch {
    if (current) fragmentError.value = true
  }
})
onBeforeUnmount(() => { generation++ })
</script>

<template>
  <ModalDialog :label="t('contextViewer.title')" panel-class="context-viewer-panel h-[860px] w-[1320px]" @close="emit('close')">
    <div ref="content" class="context-viewer" @keydown.tab="trapFocus">
      <header class="viewer-header">
        <div class="flex min-w-0 items-center gap-3">
          <LayoutGrid class="size-5 shrink-0 text-accent" aria-hidden="true" />
          <div>
            <h2 class="text-lg font-semibold">{{ t('contextViewer.title') }}</h2>
            <p class="mt-0.5 text-small text-muted">{{ t('contextViewer.subtitle') }}</p>
          </div>
        </div>
        <div class="flex shrink-0 items-center gap-1">
          <button type="button" class="viewer-icon" :disabled="loading" :aria-label="t('contextViewer.refresh')"
            :title="t('contextViewer.refresh')" @click="load()">
            <RefreshCw class="size-4" :class="{ 'animate-spin': loading }" />
          </button>
          <button ref="closeButton" type="button" class="viewer-icon" :aria-label="t('common.close')" @click="emit('close')">
            <X class="size-5" />
          </button>
        </div>
      </header>

      <div v-if="error" class="mx-5 my-3 rounded-md bg-danger-soft p-3 text-small text-danger" role="alert">
        {{ error }} <button class="ml-2 underline" @click="load(retryView)">{{ t('contextViewer.retry') }}</button>
      </div>
      <p v-if="loading && !data" class="p-12 text-center text-muted" role="status">{{ t('contextViewer.loading') }}</p>
      <div v-else-if="data" class="viewer-body">
        <div class="viewer-workspace">
          <div class="viewer-metrics">
            <div><span>{{ t('contextViewer.reported') }}</span><strong>{{ count(data.reported_tokens) }} <small>/ {{ count(data.context_window) }}</small></strong></div>
            <div><span>{{ t(data.basis === 'runtime' ? 'contextViewer.attributed' : 'contextViewer.estimated') }}</span><strong>{{ data.basis === 'runtime' ? '' : '≈ ' }}{{ count(data.estimated_tokens) }}</strong></div>
            <div><span>{{ t('contextViewer.sources') }}</span><strong>{{ data.sources.filter(source => source.category !== 'unattributed').length }}</strong></div>
          </div>
          <div class="mb-4 flex flex-wrap items-center justify-between gap-3 text-small text-muted">
            <span>{{ t('contextViewer.origin', { source: t(`contextViewer.origins.${data.origin}`) }) }}</span>
            <div class="flex flex-wrap items-center gap-2">
              <button v-if="localView === 'studio' || data.has_studio_view && data.origin.endsWith('_journal')"
                type="button" class="map-action" :disabled="loading" @click="load(localView === 'studio' ? 'auto' : 'studio')">
                {{ t(localView === 'studio' ? 'contextViewer.showJournal' : 'contextViewer.showStudio') }}
              </button>
              <button v-if="data.can_inspect" type="button" class="map-action"
                :disabled="loading" :title="t('contextViewer.inspectHint')" @click="load('runtime')">
                {{ t(inspecting ? 'contextViewer.inspecting' : 'contextViewer.inspect') }}
              </button>
            </div>
          </div>
          <p v-if="data.can_inspect" class="mb-4 text-small text-faint">{{ t('contextViewer.inspectHint') }}</p>
          <p v-if="data.partial" class="viewer-notice">{{ t('contextViewer.partial') }}</p>
          <div v-if="!groups.length" class="py-16 text-center text-muted">{{ t('contextViewer.empty') }}</div>
          <div v-else class="viewer-grid">
            <section class="map-pane min-w-0" :aria-label="t('contextViewer.map')">
              <div class="map-toolbar">
                <div class="flex min-w-0 items-center gap-2">
                  <button v-if="expanded" type="button" class="map-action" @click="backToCategories">
                    <ArrowLeft class="size-3.5" /> {{ t('contextViewer.back') }}
                  </button>
                  <h3 class="truncate font-medium">{{ expanded && active ? categoryLabel(active.id) : t('contextViewer.map') }}</h3>
                </div>
                <button v-if="!expanded && active && active.id !== 'unattributed'" type="button" class="map-action"
                  :title="t('contextViewer.expandHint')" @click="expandCategory(active.id)">
                  <Maximize2 class="size-3.5" /> {{ t('contextViewer.openFragments') }}
                </button>
                <span v-else class="text-faint">{{ t('contextViewer.mapHint') }}</span>
              </div>
              <div ref="map" class="context-map" @wheel="wheelMap">
                <p v-if="!blocks.length" class="grid h-full place-items-center p-3 text-small text-faint">{{ t('contextViewer.zeroArea') }}</p>
                <button v-for="block in blocks" :key="(expanded ? 'fragment-' : 'category-') + block.id" type="button" class="context-block"
                  :data-category="block.category"
                  :class="{ 'is-selected': selectedId === block.id, 'is-small': block.width < 19 || block.height < 10, 'is-tiny': block.width < 6 || block.height < 4 }"
                  :style="{ left: `${block.x}%`, top: `${block.y / 62 * 100}%`, width: `${block.width}%`, height: `${block.height / 62 * 100}%`, '--source-color': block.color }"
                  :aria-pressed="selectedId === block.id"
                  :aria-label="`${block.label}: ${count(block.tokens)}, ${percent(block.tokens)}`"
                  :title="[`${block.label} · ${block.tokens.toLocaleString()} · ${percent(block.tokens)}`, expanded ? t('contextViewer.backHint') : block.category !== 'unattributed' && t('contextViewer.expandHint')].filter(Boolean).join('\n')"
                  @click="selectEntry(block)" @dblclick="expandCategory(block.category)">
                  <span class="block-label">{{ block.label }}</span>
                  <strong class="block-value">{{ count(block.tokens) }}</strong>
                  <span class="block-percent">{{ percent(block.tokens) }}</span>
                </button>
              </div>
            </section>
            <section v-if="active" class="source-detail" :aria-label="t('contextViewer.content')" aria-live="polite">
              <div class="map-toolbar">
                <div class="flex min-w-0 items-center gap-2">
                  <span class="source-dot" :style="{ background: colors[active.id] }" />
                  <h3 class="truncate font-medium" :title="fragment?.label ?? categoryLabel(active.id)">{{ fragment?.label ?? categoryLabel(active.id) }}</h3>
                </div>
                <span class="ml-auto shrink-0 text-small text-faint">{{ t(data.basis === 'runtime' ? 'contextViewer.tokenCount' : 'contextViewer.tokenEstimate', { tokens: count(fragment?.tokens ?? active.tokens) }) }}</span>
              </div>
              <p v-if="active.id === 'unattributed'" class="text-small text-muted leading-6">{{ t('contextViewer.otherHint') }}</p>
              <div v-else :key="fragment ? 'fragment-' + fragment.id : active.id" class="source-texts" tabindex="0">
                <article v-for="(source, index) in detailSources" :key="source.id" class="source-entry">
                  <div v-if="!expanded" class="mb-2 flex items-start justify-between gap-3 text-small text-muted">
                    <span>{{ fragmentLabel(source, index) }}</span>
                    <span class="shrink-0 font-mono">{{ data.basis === 'runtime' ? '' : '≈ ' }}{{ count(source.tokens) }}</span>
                  </div>
                  <div v-if="source.origin || source.representation === 'json' || source.representation === 'metadata'" class="source-origin">
                    <span v-if="source.representation === 'json' || source.representation === 'metadata'">{{ t(`contextViewer.representations.${source.representation}`) }}</span>
                    <code v-if="source.origin">{{ source.origin }}</code>
                  </div>
                  <ContextFragmentText v-if="source.text" :source="source" />
                  <p v-else-if="!source.truncated" class="text-small text-faint">{{ t(data.basis === 'runtime' ? 'contextViewer.noText' : 'contextViewer.emptyText') }}</p>
                  <p v-if="source.truncated" class="mt-2 text-small text-faint" role="status">
                    {{ t(expanded && source.text_key
                      ? (fragmentError ? 'contextViewer.fragmentUnavailable' : 'contextViewer.loadingFragment')
                      : 'contextViewer.truncated') }}
                    <button v-if="!expanded && source.text_key" type="button" class="ml-1 underline"
                      @click="expandCategory(active.id, source.id)">{{ t('contextViewer.fullText') }}</button>
                  </p>
                </article>
              </div>
            </section>
          </div>
          <p class="viewer-footnote">{{ t(data.basis === 'runtime' ? 'contextViewer.nativeMethod' : 'contextViewer.method') }}</p>
        </div>
        <aside v-if="groups.length" class="source-list" :class="{ 'fragment-list': expanded }" :aria-label="t(expanded ? 'contextViewer.fragments' : 'contextViewer.bySource')">
          <h3 class="map-toolbar font-medium">{{ t(expanded ? 'contextViewer.fragments' : 'contextViewer.bySource') }}</h3>
          <div :key="expanded ? active?.id : 'overview'" class="source-rows">
            <button v-for="entry in entries" :key="entry.id" type="button" class="source-row"
              :title="entry.label + ' · ' + count(entry.tokens) + ' · ' + percent(entry.tokens)"
              :aria-label="entry.label + ': ' + count(entry.tokens) + ', ' + percent(entry.tokens)"
              :class="{ 'is-selected': selectedId === entry.id }" :aria-pressed="selectedId === entry.id" @click="selectEntry(entry)">
              <span class="source-dot" :style="{ background: entry.color }" />
              <span class="source-name min-w-0 flex-1 truncate">{{ entry.label }}</span>
              <span class="source-count font-mono tabular-nums">{{ count(entry.tokens) }}</span>
              <span class="source-share text-right text-faint tabular-nums">{{ percent(entry.tokens) }}</span>
            </button>
          </div>
        </aside>
      </div>
    </div>
  </ModalDialog>
</template>

<style scoped>
.context-viewer { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.viewer-header { display: flex; flex: none; align-items: center; justify-content: space-between; gap: 16px; padding: 18px 22px; border-bottom: 1px solid var(--vxs-border); }
.viewer-icon { display: grid; width: 32px; height: 32px; place-items: center; border-radius: 6px; cursor: pointer; }
.viewer-icon:hover, .source-row:hover { background: var(--vxs-bg-hover); }
.viewer-body { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: 12px; padding: 16px 22px; }
.viewer-workspace { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: auto; overscroll-behavior: contain; }
.viewer-workspace > * { flex-shrink: 0; }
.viewer-metrics { display: flex; gap: 40px; margin-bottom: 20px; }
.viewer-metrics > div { display: flex; flex-direction: column; gap: 5px; }
.viewer-metrics span { font-size: 11px; color: var(--vxs-text-muted); }
.viewer-metrics strong { font-size: 23px; font-weight: 600; font-variant-numeric: tabular-nums; }
.viewer-metrics small { font-size: 15px; font-weight: 400; color: var(--vxs-text-faint); }
.viewer-notice { font-size: 12px; line-height: 1.6; color: var(--vxs-text-muted); padding: 10px 12px; margin-bottom: 16px; border: 1px solid var(--vxs-border); border-radius: 6px; }
.viewer-grid { flex: 1 0 180px; display: grid; grid-template: 'map reader' minmax(0, 1fr) / minmax(0, 0.95fr) minmax(0, 1.05fr); gap: 22px; }
.map-pane { grid-area: map; display: flex; flex-direction: column; min-height: 0; }
.map-toolbar { display: flex; min-height: 30px; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; font-size: 12px; }
.map-action { display: inline-flex; flex-shrink: 0; align-items: center; gap: 6px; padding: 4px 8px; border: 1px solid var(--vxs-border); border-radius: 5px; color: var(--vxs-text-muted); cursor: pointer; }
.map-action:hover { background: var(--vxs-bg-hover); color: var(--vxs-text-primary); }
.source-list { flex: none; min-width: 0; border-top: 1px solid var(--vxs-border); padding-top: 8px; }
.source-list .map-toolbar { min-height: 20px; margin-bottom: 4px; }
.source-rows { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 232px), 1fr)); gap: 2px 12px; }
/* §10.4: the overview is bounded by the category catalog; an individual category can have thousands of fragments. */
.fragment-list .source-rows { max-height: min(22dvh, 180px); overflow-y: auto; overscroll-behavior: contain; }
.context-map { position: relative; flex: 1; min-height: 0; width: 100%; overflow: hidden; border-radius: 8px; background: var(--vxs-bg-inset); }
.context-block { position: absolute; display: flex; flex-direction: column; align-items: flex-start; justify-content: center; min-width: 0; min-height: 0; padding: 0; border: 0; box-shadow: inset 0 0 0 1px var(--vxs-bg-surface); overflow: hidden; text-align: left; background: color-mix(in srgb, var(--source-color) 23%, var(--vxs-bg-surface)); color: var(--vxs-text-primary); cursor: pointer; }
.context-block > span, .context-block > strong { margin-inline: 14px; max-width: calc(100% - 28px); overflow: hidden; }
.context-block:hover { background: color-mix(in srgb, var(--source-color) 36%, var(--vxs-bg-surface)); }
.context-block.is-selected { box-shadow: inset 0 0 0 2px var(--source-color); }
.block-label { max-width: 100%; font-size: 12px; line-height: 1.4; font-weight: 500; overflow: hidden; }
.block-value { margin-top: 6px; font-size: clamp(16px, 2vw, 27px); font-weight: 600; font-variant-numeric: tabular-nums; }
.block-percent { margin-top: 3px; font-size: 11px; opacity: .65; }
.is-small > span, .is-small > strong { margin-inline: 6px; max-width: calc(100% - 12px); }
.is-small .block-value { margin-top: 2px; font-size: 13px; }
.is-small .block-percent { display: none; }
.is-tiny > * { visibility: hidden; }
.source-row { display: flex; align-items: center; min-width: 0; width: 100%; gap: 6px; border-radius: 6px; padding: 5px 6px; text-align: left; font-size: 12px; line-height: 1.3; cursor: pointer; }
.source-list:not(.fragment-list) .source-name { white-space: normal; overflow-wrap: anywhere; }
.source-share { min-width: 4ch; }
.source-row.is-selected { background: var(--vxs-bg-inset); }
.source-dot { display: inline-block; width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }
.source-detail { grid-area: reader; display: flex; flex-direction: column; min-width: 0; min-height: 0; contain: size; }
.source-texts { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; border-radius: 6px; background: var(--vxs-bg-inset); padding: 14px; }
.source-entry + .source-entry { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--vxs-border); }
.source-origin { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; margin-bottom: 8px; font-size: 11px; color: var(--vxs-text-muted); overflow-wrap: anywhere; }
.viewer-footnote { margin-top: 16px; font-size: 11px; line-height: 1.6; color: var(--vxs-text-faint); }
button:focus-visible, [tabindex]:focus-visible { outline: 2px solid var(--vxs-accent); outline-offset: 2px; }
@media (max-width: 800px) {
  .viewer-grid { flex: none; grid-template: 'map' clamp(145px, 25dvh, 240px) 'reader' clamp(180px, 38dvh, 340px) / minmax(0, 1fr); gap: 12px; }
  .viewer-metrics { gap: 8px 22px; flex-wrap: wrap; margin-bottom: 12px; }
  .viewer-header, .viewer-body { padding: 14px; }
}
@media (max-width: 640px), (max-height: 500px) {
  .source-list:not(.fragment-list) .source-rows { grid-template-columns: repeat(auto-fit, minmax(min(44%, 145px), 1fr)); gap: 2px 8px; }
  .source-list:not(.fragment-list) .source-row {
    display: grid; grid-template: 'dot name name' auto '. count share' auto / 8px auto 1fr;
    gap: 1px 5px; padding-block: 3px;
  }
  .source-list .source-dot { grid-area: dot; }
  .source-list .source-name { grid-area: name; }
  .source-list .source-count { grid-area: count; }
  .source-list .source-share { grid-area: share; text-align: left; }
  .source-list:not(.fragment-list) :is(.source-count, .source-share) { font-size: 10px; }
  .viewer-header { padding-block: 10px; }
}
@media (max-height: 490px) {
  .source-list:not(.fragment-list) .source-rows { grid-template-columns: repeat(auto-fit, minmax(min(44%, 116px), 1fr)); }
}
@media (max-height: 490px), (max-width: 359px) {
  :global(.vxs-veil:has(> .context-viewer-panel)) { padding: 4px; }
  .viewer-header, .viewer-body { padding: 6px 10px; }
  .viewer-header p { display: none; }
  .viewer-body { gap: 6px; }
  .source-list { padding-top: 4px; }
  .source-list:not(.fragment-list) .source-row { grid-template: 'dot name' auto / 8px 1fr; padding: 3px 4px; }
  .source-list:not(.fragment-list) :is(.source-count, .source-share) { display: none; }
}
</style>
