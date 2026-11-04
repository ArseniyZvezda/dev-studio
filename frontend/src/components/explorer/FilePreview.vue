<script setup lang="ts">
import { Box, Download, FileQuestion, FileSearch } from 'lucide-vue-next'
import { computed } from 'vue'

import { rawFileUrl } from '@/api/client'
import type { PreviewResult } from '@/api/types'
import CodeLines, { type CodeRow } from '@/components/ui/CodeLines.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import { t } from '@/i18n'
import type { HighlightMode } from '@/lib/highlight/highlighter'
import {
  MAX_HIGHLIGHT_CHARS,
  MAX_HIGHLIGHT_LINES,
  useHighlight,
  type HighlightSource,
} from '@/lib/highlight/use-highlight'
import { formatBytes, formatTime } from '@/lib/status'
import { pluginComponent } from '@/plugin-runtime/loader'
import { useThemeStore } from '@/stores/theme'

const props = defineProps<{ preview: PreviewResult | null; loading: boolean }>()

const name = computed(() => props.preview?.path.split('/').pop() ?? '')
const downloadUrl = computed(() => (props.preview ? rawFileUrl(props.preview.path) : '#'))

/**
 * A plugin that claimed this file type takes over the pane (spec §6.2) — that is how
 * 3D formats get a viewer without the Studio depending on a 3D library itself.
 */
const previewerComponent = computed(() => {
  const previewer = props.preview?.previewer
  if (!previewer) return null
  return pluginComponent(
    previewer.component_url,
    previewer.plugin_id,
    `Previewer ${previewer.id}`,
  )
})

/**
 * Which Shiki theme to tokenize against. The store publishes `mode: "system"` already
 * resolved and reactive, so a theme switch reflows the colours without a reload.
 */
const themeStore = useThemeStore()
const mode = computed<HighlightMode>(() => themeStore.resolvedMode)

const highlightSource = computed<HighlightSource | null>(() => {
  const preview = props.preview
  if (!preview || preview.kind !== 'text' || preview.text === null) return null
  return { path: preview.path, text: preview.text }
})

const { lines, language, skipped } = useHighlight(highlightSource, mode)

const rows = computed<CodeRow[] | null>(
  () =>
    lines.value?.map((tokens, index) => ({ key: index, numbers: [index + 1], tokens, text: '' })) ??
    null,
)

const skippedNote = computed(() => {
  if (skipped.value === 'size') {
    return t('explorer.preview.plainSize', { size: formatBytes(MAX_HIGHLIGHT_CHARS) })
  }
  if (skipped.value === 'lines') {
    // `lines`, not `count`: a limit is printed as written, and the note is worded so that no
    // language needs a plural form for it.
    return t('explorer.preview.plainLines', { lines: MAX_HIGHLIGHT_LINES })
  }
  return null
})
</script>

<template>
  <div v-if="loading" class="grid h-full place-items-center text-muted">
    {{ t('explorer.preview.loading') }}
  </div>

  <!-- The same empty state the Documents section shows with nothing open: the two panes are
       one layout, and a bare line of faint text in one of them read as a pane still loading. -->
  <EmptyState v-else-if="!preview" :title="t('explorer.preview.selectFile')">
    <template #icon><FileSearch class="mx-auto mb-3 size-8 text-faint" /></template>
  </EmptyState>

  <div v-else class="flex h-full flex-col">
    <header class="vxs-pane flex items-center gap-3 border-b border-pane px-pane py-2">
      <div class="min-w-0 flex-1">
        <p class="truncate font-medium">{{ name }}</p>
        <p class="truncate text-small text-faint">{{ preview.path }}</p>
      </div>
      <div class="shrink-0 text-right text-small text-faint">
        <p>
          {{ formatBytes(preview.size) }}
          <!-- How the bytes were decoded, not what the suffix claims — a file the backend
               sniffed as cp1251 is a different file from one it read as UTF-8 (§6.2). -->
          <span v-if="preview.encoding"> · {{ preview.encoding }}</span>
          <span v-if="language"> · {{ language }}</span>
        </p>
        <p>{{ formatTime(preview.modified) }}</p>
      </div>
      <a
        :href="downloadUrl"
        download
        class="grid size-7 shrink-0 place-items-center rounded-md border border-line hover:bg-hover"
        :title="t('explorer.preview.download')"
      >
        <Download class="size-4" />
      </a>
    </header>

    <div class="min-h-0 flex-1 overflow-auto">
      <!-- Keyed on mtime as well as path: a previewer reads the file once when it
           mounts, so a node regenerating the artifact under it must force a remount. -->
      <component
        :is="previewerComponent"
        v-if="previewerComponent"
        :key="`${preview.path}@${preview.modified}`"
        :path="preview.path"
        :preview="preview"
      />

      <template v-else-if="preview.kind === 'text' && preview.text !== null">
        <CodeLines v-if="rows" :rows="rows" />

        <!-- Until the highlighter's chunk resolves — and for everything it declines —
             the pane is the plain `<pre>` it has always been. -->
        <pre
          v-else
          class="p-pane font-mono text-[12px] leading-relaxed whitespace-pre-wrap"
          >{{ preview.text }}</pre
        >
      </template>

      <div v-else-if="preview.kind === 'image' && preview.data_url" class="grid h-full place-items-center p-pane">
        <img :src="preview.data_url" :alt="name" class="max-h-full max-w-full object-contain" />
      </div>

      <!-- A hex dump is what makes a file with no extension inspectable at all: the
           alternative was "No preview available", which tells the user nothing (§6.2). -->
      <pre
        v-else-if="preview.kind === 'binary' && preview.hex"
        class="p-pane font-mono text-[12px] leading-relaxed whitespace-pre"
        >{{ preview.hex }}</pre
      >

      <div v-else class="grid h-full place-items-center p-pane text-center text-muted">
        <div>
          <component
            :is="preview.kind === 'model' ? Box : FileQuestion"
            class="mx-auto mb-2 size-8 text-faint"
          />
          <p>{{ preview.message ?? t('explorer.preview.unavailable') }}</p>
          <p v-if="preview.mime" class="mt-1 text-small text-faint">{{ preview.mime }}</p>
        </div>
      </div>
    </div>

    <p v-if="preview.truncated" class="border-t border-line px-pane py-1 text-small text-warning">
      {{ t('explorer.preview.truncated') }}
    </p>
    <p v-else-if="skippedNote" class="border-t border-line px-pane py-1 text-small text-faint">
      {{ skippedNote }}
    </p>
  </div>
</template>
