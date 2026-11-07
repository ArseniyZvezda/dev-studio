<script setup lang="ts">
/** Format each visible context fragment using its detected language or JSON structure (§10.4). */
import { computed, ref, watch } from 'vue'
import { useIntersectionObserver } from '@vueuse/core'
import { ChevronRight } from 'lucide-vue-next'
import type { ContextSource } from '@/api/types'
import CodeLines from '@/components/ui/CodeLines.vue'
import { t } from '@/i18n'
import { contextFormat } from '@/lib/context-format'
import { visibleJsonRows } from '@/lib/context-json'
import { languageForPath } from '@/lib/highlight/extensions'
import { oversizeReason, useHighlight } from '@/lib/highlight/use-highlight'
import { useThemeStore } from '@/stores/theme'

const props = defineProps<{ source: ContextSource }>()
const element = ref<HTMLElement | null>(null)
const viewed = ref(typeof IntersectionObserver === 'undefined')
// An overview can contain hundreds of journal entries. Only format them as they enter the reader.
const { stop } = useIntersectionObserver(element, entries => {
  if (entries.some(entry => entry.isIntersecting)) { viewed.value = true; stop() }
}, { rootMargin: '100px' })
const format = computed(() => viewed.value && !props.source.truncated && !oversizeReason(props.source.text)
  ? contextFormat(props.source.text, props.source.label, props.source.category)
  : null)
const json = computed(() => format.value?.json)
const formatName = computed(() => json.value?.format ?? (format.value?.path && languageForPath(format.value.path)))
const folded = ref(new Set<number>())
watch([() => props.source.id, () => props.source.text], () => {
  folded.value = new Set()
}, { flush: 'sync' })
const visible = computed(() => json.value ? visibleJsonRows(json.value.rows, folded.value) : [])
const branches = computed(() => json.value?.rows.flatMap((row, i) => row.end === undefined ? [] : [i]) ?? [])
function toggle(i: number) {
  if (folded.value.has(i)) folded.value.delete(i)
  else folded.value.add(i)
}
const theme = useThemeStore()
const { lines } = useHighlight(() => {
  const value = format.value
  return value?.path && !value.json ? { path: value.path, text: value.body } : null
}, () => theme.resolvedMode)
const code = computed(() => lines.value?.map((tokens, key) => ({ tokens, key, numbers: [], text: '' })))
</script>

<template>
  <div ref="element" class="fragment-text">
    <div v-if="formatName" class="format-bar">
      <span class="format-name">{{ formatName }}</span>
      <template v-if="branches.length">
        <button type="button" :disabled="!folded.size" @click="folded.clear()">{{ t('contextViewer.expandAll') }}</button>
        <button type="button" :disabled="folded.size === branches.length" @click="folded = new Set(branches)">{{ t('contextViewer.collapseAll') }}</button>
      </template>
    </div>
    <pre v-if="!format">{{ source.text }}</pre>
    <template v-else>
      <pre v-if="format.before" class="text-faint">{{ format.before.replace(/\r?\n$/, '') }}</pre>
      <div v-if="json" class="json-lines">
        <div v-for="i in visible" :key="i" class="json-row" :style="{ paddingInlineStart: Math.min(json.rows[i].depth, 16) * 2 + 'ch' }">
          <button v-if="json.rows[i].end !== undefined" type="button" class="fold"
            :aria-expanded="!folded.has(i)" :aria-label="t(folded.has(i) ? 'contextViewer.expandBranch' : 'contextViewer.collapseBranch', { branch: json.rows[i].tokens.map(token => token.text).join('') })"
            @click="toggle(i)"><ChevronRight :class="{ 'rotate-90': !folded.has(i) }" class="size-3.5" /></button>
          <span v-else class="fold-space" aria-hidden="true"></span>
          <code><span v-for="(token, n) in json.rows[i].tokens" :key="n" :class="token.kind">{{ token.text }}</span><template v-if="folded.has(i)"><span class="text-faint"> … </span>{{ json.rows[json.rows[i].end!].tokens.map(token => token.text).join('') }}</template></code>
        </div>
      </div>
      <CodeLines v-else-if="code" :rows="code" :columns="0" wrap />
      <pre v-else>{{ format.body }}</pre>
      <pre v-if="format.after" class="text-faint">{{ format.after.replace(/^\r?\n/, '') }}</pre>
    </template>
  </div>
</template>

<style scoped>
.fragment-text pre, .json-lines { font: 12px/1.65 var(--vxs-font-mono, monospace); tab-size: 2; }
.fragment-text pre, .json-row code { white-space: pre-wrap; overflow-wrap: anywhere; }
.format-bar { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; margin-bottom: 8px; color: var(--vxs-text-muted); font-size: 11px; }
.format-name { font-weight: 600; margin-right: 6px; }
.format-bar button { padding: 2px 6px; border: 1px solid var(--vxs-border); border-radius: 4px; cursor: pointer; }
.format-bar button:disabled { opacity: .5; cursor: default; }
.format-bar button:enabled:hover, .fold:hover { background: var(--vxs-bg-hover); color: var(--vxs-text-primary); }
.json-row { display: flex; align-items: baseline; }
.fold, .fold-space { flex: 0 0 18px; }
.fold { display: inline-flex; align-self: flex-start; align-items: center; justify-content: center; height: 20px; border-radius: 3px; color: var(--vxs-text-muted); cursor: pointer; }
.key { color: var(--vxs-info); }
.string { color: var(--vxs-success); }
.number { color: var(--vxs-warning); }
.literal { color: var(--vxs-accent); }
.comment { color: var(--vxs-text-muted); font-style: italic; }
button:focus-visible { outline: 2px solid var(--vxs-accent); }
</style>
