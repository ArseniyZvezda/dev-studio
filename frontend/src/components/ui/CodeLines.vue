<script lang="ts">
import type { HighlightLine } from '@/lib/highlight/highlighter'

/** What a row is to a diff; a file's own lines carry none. */
export type CodeTone = 'added' | 'removed' | 'hunk' | 'meta'

export interface CodeRow {
  key: number
  /** One cell per gutter column; null or missing leaves that cell blank. */
  numbers: (number | null)[]
  /** Highlighted tokens once they exist; until then — and for every row never highlighted — `text`. */
  tokens: HighlightLine | null
  text: string
  tone?: CodeTone
}
</script>

<script setup lang="ts">
/**
 * Code as the Explorer draws it (spec §6.2): a sticky number gutter and one row per line,
 * tokens as elements. The task diff (§10.4) is the same viewer with a tone per row laid over
 * it, which is why this is one component rather than two copies that drift apart.
 *
 * Tokens are drawn as elements, never as an HTML string: this is file content, and
 * `.claude/rules/frontend-vue.md` keeps `v-html` away from it.
 */
import type { HighlightToken } from '@/lib/highlight/highlighter'

withDefaults(defineProps<{ rows: CodeRow[]; columns?: number; markers?: boolean; wrap?: boolean }>(), {
  columns: 1,
  markers: false,
})

/**
 * Colours come from the Shiki theme, which is the one place a literal colour belongs in a
 * component (`.claude/rules/styling.md`): the theme *is* the token source, and there is no
 * `--vxs-*` for "keyword". Everything around the code — gutter, chrome, background — is
 * still a token.
 */
function tokenStyle(token: HighlightToken): Record<string, string | undefined> {
  return {
    color: token.color,
    fontStyle: token.italic ? 'italic' : undefined,
    fontWeight: token.bold ? '600' : undefined,
    textDecoration: token.underline ? 'underline' : token.strike ? 'line-through' : undefined,
  }
}

/** The soft tones are translucent, so the line's own tint is laid over the gutter's opaque canvas. */
const TONE: Record<CodeTone, string> = {
  added: 'bg-success-soft',
  removed: 'bg-danger-soft',
  hunk: 'bg-info-soft text-info',
  meta: 'text-faint',
}

const MARKER: Partial<Record<CodeTone, string>> = { added: '+', removed: '−' }
const MARKER_TONE: Partial<Record<CodeTone, string>> = {
  added: 'text-success',
  removed: 'text-danger',
}
</script>

<template>
  <div class="font-mono text-[12px] leading-relaxed" :class="wrap ? 'w-full' : 'w-max min-w-full py-pane'">
    <div v-for="row in rows" :key="row.key" class="flex">
      <span
        v-if="columns || markers"
        class="sticky left-0 flex shrink-0 border-r border-line-subtle bg-canvas text-faint select-none"
      >
        <span class="flex" :class="row.tone ? TONE[row.tone] : ''">
          <span
            v-for="column in columns"
            :key="column"
            class="shrink-0 text-right"
            :class="columns > 1 ? 'w-12 pr-2' : 'w-16 pr-3'"
            >{{ row.numbers[column - 1] ?? '' }}</span
          >
          <span
            v-if="markers"
            class="w-5 shrink-0 text-center"
            :class="row.tone ? MARKER_TONE[row.tone] : ''"
            >{{ (row.tone && MARKER[row.tone]) || '' }}</span
          >
        </span>
      </span>
      <span class="grow" :class="[wrap ? 'min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]' : 'px-3 whitespace-pre', row.tone && TONE[row.tone]]">
        <template v-if="row.tokens">
          <span v-for="(token, position) in row.tokens" :key="position" :style="tokenStyle(token)">{{
            token.text
          }}</span>
          <span v-if="!row.tokens.length">{{ ' ' }}</span>
        </template>
        <template v-else>{{ row.text || ' ' }}</template>
      </span>
    </div>
  </div>
</template>
