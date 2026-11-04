/**
 * Highlighting as the preview pane sees it (spec §6.2).
 *
 * The composable owns the two things that keep highlighting from ever being in the way:
 *
 * - **It is lazy.** Shiki is behind an `import()` here and nowhere else, so the Explorer's
 *   own chunk contains none of it. Until that import resolves `lines` is null and the pane
 *   draws plain text — the first paint never waits on a highlighter.
 * - **It is bounded.** A generated file — a baked lightmap dump, a 40 MB log — is rendered
 *   plain with a note saying so. Tokenizing it would lock the tab for the length of the
 *   file, which is a worse answer than no colours.
 *
 * Re-running on a theme change is the whole of "theme-aware": the highlighter, its WASM
 * and the grammar are already resident by then, so switching dark/light re-tokenizes
 * against the other theme in the time it takes to fetch one theme JSON, once.
 */

import { ref, shallowRef, toValue, watch, type MaybeRefOrGetter, type Ref } from 'vue'

import type { HighlightLine, HighlightMode } from './highlighter'

/** Roughly 300 KB of source. UTF-16 units, not bytes — close enough, and free to measure. */
export const MAX_HIGHLIGHT_CHARS = 300 * 1024
export const MAX_HIGHLIGHT_LINES = 5000

export type OversizeReason = 'size' | 'lines'

/** Newlines counted with an early exit, so the check itself is not the expensive part. */
export function lineCount(text: string, stopAfter = Number.MAX_SAFE_INTEGER): number {
  let count = 1
  let index = text.indexOf('\n')
  while (index !== -1) {
    count += 1
    if (count > stopAfter) return count
    index = text.indexOf('\n', index + 1)
  }
  return count
}

/** Why this file will not be highlighted, or null if it will be. */
export function oversizeReason(text: string): OversizeReason | null {
  if (text.length > MAX_HIGHLIGHT_CHARS) return 'size'
  if (lineCount(text, MAX_HIGHLIGHT_LINES) > MAX_HIGHLIGHT_LINES) return 'lines'
  return null
}

export interface HighlightSource {
  path: string
  text: string
}

export interface UseHighlight {
  /** Token lines once they exist; null while loading, and for anything not highlighted. */
  lines: Ref<HighlightLine[] | null>
  language: Ref<string | null>
  skipped: Ref<OversizeReason | null>
}

export function useHighlight(
  source: MaybeRefOrGetter<HighlightSource | null>,
  mode: MaybeRefOrGetter<HighlightMode>,
): UseHighlight {
  const lines = shallowRef<HighlightLine[] | null>(null)
  const language = ref<string | null>(null)
  const skipped = ref<OversizeReason | null>(null)

  // Every run claims a number; a run that is no longer the newest throws its answer away.
  // Without it, clicking down a file tree faster than a grammar loads paints file N's
  // tokens over file N+1's text.
  let generation = 0

  async function run(): Promise<void> {
    const current = toValue(source)
    const theme = toValue(mode)
    const token = ++generation

    lines.value = null
    language.value = null
    skipped.value = current ? oversizeReason(current.text) : null
    if (!current || skipped.value) return

    try {
      const { highlightFile } = await import('./highlighter')
      const result = await highlightFile(current.path, current.text, theme)
      if (token !== generation || !result) return
      lines.value = result.lines
      language.value = result.language
    } catch (error) {
      // An unknown suffix already answers null rather than throwing, so reaching here
      // means the highlighter itself is unavailable — still just plain text.
      console.warn('Syntax highlighting is unavailable', error)
    }
  }

  watch([() => toValue(source), () => toValue(mode)], run, { immediate: true })

  return { lines, language, skipped }
}
