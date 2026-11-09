<script setup lang="ts">
/**
 * One file's change, in the pane the transcript usually occupies (§10.4).
 *
 * The Explorer's code viewer with the patch laid over it: the same highlighter colours the
 * code, and the patch colours the rows — green for added, red for removed, with each side's
 * line number in the gutter. Still a unified patch and nothing cleverer: no side-by-side, no
 * word-level highlighting. What a reviewer is asking here is "what did this task do to this
 * file", and the answer is the shape the tool itself prints.
 *
 * Each side is tokenized on its own (`lib/highlight/diff.ts` splits them), because a removed
 * line is code of the old file. A side is only the lines the patch shows, so a hunk that
 * opens inside a block comment highlights as code — the same guess every diff viewer makes.
 */
import { Loader2 } from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'

import { api } from '@/api/client'
import CodeLines, { type CodeRow } from '@/components/ui/CodeLines.vue'
import CodeCopyButton from '@/components/ui/CodeCopyButton.vue'
import { t } from '@/i18n'
import { parsePatch } from '@/lib/highlight/diff'
import type { HighlightMode } from '@/lib/highlight/highlighter'
import { useHighlight, type HighlightSource } from '@/lib/highlight/use-highlight'
import { useThemeStore } from '@/stores/theme'

const props = defineProps<{ teamId: string; taskId: string; path: string }>()

const patch = ref('')
const loading = ref(false)
const error = ref<string | null>(null)

/** Parsed once per patch, in script: a 4000-line patch must not re-derive this on every re-render. */
const parsed = computed(() => parsePatch(patch.value))

const themeStore = useThemeStore()
const mode = computed<HighlightMode>(() => themeStore.resolvedMode)

function side(lines: string[]): HighlightSource | null {
  return lines.length ? { path: props.path, text: lines.join('\n') } : null
}

const before = useHighlight(() => side(parsed.value.sides.old), mode)
const after = useHighlight(() => side(parsed.value.sides.new), mode)

const language = computed(() => after.language.value ?? before.language.value)
const plain = computed(() => Boolean(before.skipped.value || after.skipped.value))

const rows = computed<CodeRow[]>(() => {
  const oldTokens = before.lines.value
  const newTokens = after.lines.value
  return parsed.value.rows.map((row, key) => ({
    key,
    numbers: [row.oldLine, row.newLine],
    // A context line is in both sides; the new one draws it, as the file now reads.
    tokens:
      row.newIndex !== null
        ? (newTokens?.[row.newIndex] ?? null)
        : row.oldIndex !== null
          ? (oldTokens?.[row.oldIndex] ?? null)
          : null,
    text: row.text,
    tone: row.kind === 'context' ? undefined : row.kind,
  }))
})

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  patch.value = ''
  try {
    patch.value = (await api.taskPatch(props.teamId, props.taskId, props.path)).patch
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

watch(() => [props.taskId, props.path], () => void load(), { immediate: true })
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <p
      class="flex shrink-0 gap-3 border-b border-line-subtle px-pane py-1 font-mono text-small text-faint"
    >
      <span class="min-w-0 flex-1 truncate">{{ path }}</span>
      <span v-if="language" class="shrink-0">{{ language }}</span>
      <CodeCopyButton v-if="patch.trim()" :text="patch" inline />
    </p>

    <!-- The canvas the Explorer's viewer sits on, so the sticky gutter is the same grey as the
         code it scrolls over. -->
    <div class="min-h-0 flex-1 overflow-auto bg-canvas">
      <p v-if="loading" class="flex items-center gap-2 p-pane text-faint">
        <Loader2 class="size-3.5 animate-spin" /> {{ t('tasks.diff.reading') }}
      </p>
      <p v-else-if="error" class="m-pane rounded-md bg-danger-soft p-2 text-danger">{{ error }}</p>
      <p v-else-if="!patch.trim()" class="p-pane text-faint">
        {{ t('tasks.diff.empty') }}
      </p>

      <!-- `pre` rather than `pre-wrap`: alignment is the whole point of a patch, so long
           lines scroll rather than fold. -->
      <CodeLines v-else :rows="rows" :columns="parsed.numbered ? 2 : 0" markers />
    </div>

    <p v-if="plain" class="shrink-0 border-t border-line px-pane py-1 text-small text-faint">
      {{ t('tasks.diff.plain') }}
    </p>
  </div>
</template>
