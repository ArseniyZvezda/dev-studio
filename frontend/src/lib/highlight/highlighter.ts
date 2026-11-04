/**
 * The highlighter itself (spec §6.2) — the one module that pulls Shiki in.
 *
 * Nothing imports this statically. `use-highlight.ts` reaches it through `import()`, so
 * Shiki's core, the Oniguruma WASM, the themes and every grammar sit in chunks the
 * Explorer only fetches once a text file is actually open, and the pane's first paint is
 * the plain `<pre>` it always was.
 *
 * **Tokens, not HTML.** `codeToTokens` is used rather than `codeToHtml` because the
 * renderer is Vue elements: the Explorer shows files an agent wrote and a project
 * contains, and `.claude/rules/frontend-vue.md` forbids `v-html` on file content. A
 * token's `color` is the one place a literal colour is legitimate in this codebase — the
 * theme *is* the source of the colours, and there is no `--vxs-*` for "keyword".
 */

import { createHighlighterCore, type HighlighterCore } from 'shiki/core'
import { createOnigurumaEngine } from 'shiki/engine/oniguruma'

import { languageForPath } from './extensions'
import { fetchGrammar, pluginGrammars } from './grammars'
import { LANGUAGE_LOADERS } from './languages'

export type HighlightMode = 'dark' | 'light'

/**
 * One dark theme and one light one.
 *
 * GitHub's defaults, because the Studio's own palette is neutral and these are the
 * colours most developers already read code in — a theme with opinions of its own would
 * fight `--vxs-accent`. Both ids are files in `@shikijs/themes/dist/`.
 */
export const SHIKI_THEMES: Readonly<Record<HighlightMode, string>> = {
  dark: 'github-dark-default',
  light: 'github-light-default',
}

const THEME_LOADERS: Readonly<Record<HighlightMode, () => Promise<unknown>>> = {
  dark: () => import('@shikijs/themes/github-dark-default'),
  light: () => import('@shikijs/themes/github-light-default'),
}

/** Bit flags from vscode-textmate's `FontStyle`, which is a `const enum` and unimportable. */
const ITALIC = 1
const BOLD = 2
const UNDERLINE = 4
const STRIKETHROUGH = 8

export interface HighlightToken {
  text: string
  color?: string
  italic?: boolean
  bold?: boolean
  underline?: boolean
  strike?: boolean
}

export type HighlightLine = HighlightToken[]

export interface HighlightResult {
  lines: HighlightLine[]
  /** The grammar that was used — named in the pane's header so the answer is inspectable. */
  language: string
}

let corePromise: Promise<HighlighterCore> | null = null

function core(): Promise<HighlighterCore> {
  corePromise ??= createHighlighterCore({
    themes: [],
    langs: [],
    // Oniguruma over the JavaScript engine: plugin grammars are written for the real
    // regex dialect and the JS engine quietly mistokenizes what it cannot translate.
    engine: createOnigurumaEngine(() => import('shiki/wasm')),
  })
  return corePromise
}

/** Loads are memoised on their key, so re-opening a file never re-fetches a grammar. */
const loadedThemes = new Map<HighlightMode, Promise<void>>()
const loadedLanguages = new Map<string, Promise<void>>()

function ensureTheme(highlighter: HighlighterCore, mode: HighlightMode): Promise<void> {
  let loading = loadedThemes.get(mode)
  if (!loading) {
    loading = THEME_LOADERS[mode]()
      .then((module) => highlighter.loadTheme(module as never))
      .catch((error: unknown) => {
        loadedThemes.delete(mode)
        throw error
      })
    loadedThemes.set(mode, loading)
  }
  return loading
}

async function loadLanguage(highlighter: HighlighterCore, language: string): Promise<void> {
  const builtin = LANGUAGE_LOADERS[language]
  if (builtin) {
    const module = await builtin()
    await highlighter.loadLanguage(module.default)
    return
  }

  const { byId } = await pluginGrammars()
  const grammar = byId.get(language)
  if (!grammar) throw new Error(`No grammar registered for "${language}"`)
  await highlighter.loadLanguage(await fetchGrammar(grammar))
}

function ensureLanguage(highlighter: HighlighterCore, language: string): Promise<void> {
  let loading = loadedLanguages.get(language)
  if (!loading) {
    loading = loadLanguage(highlighter, language).catch((error: unknown) => {
      // Not cached as a failure: a plugin that was still starting up gets another go.
      loadedLanguages.delete(language)
      throw error
    })
    loadedLanguages.set(language, loading)
  }
  return loading
}

/**
 * Which grammar a path resolves to, consulting plugin grammars only when the built-in
 * maps miss — which is what keeps `GET /api/plugins/grammars` off the app's startup path.
 */
export async function resolveLanguage(path: string): Promise<string | null> {
  const builtin = languageForPath(path)
  if (builtin) return builtin
  const { extensions } = await pluginGrammars()
  return languageForPath(path, extensions)
}

/**
 * Tokenize a file, or answer null when there is nothing honest to say.
 *
 * Null covers all three "not highlighted" cases the same way — an unknown suffix, a
 * grammar that would not load, a file the backend could not classify — because the pane's
 * answer to each of them is the same plain monospace, and an error banner over someone's
 * `.txt` would be noise.
 */
export async function highlightFile(
  path: string,
  code: string,
  mode: HighlightMode,
): Promise<HighlightResult | null> {
  const language = await resolveLanguage(path)
  if (!language) return null

  const highlighter = await core()
  try {
    await Promise.all([ensureTheme(highlighter, mode), ensureLanguage(highlighter, language)])
  } catch (error) {
    console.warn(`Could not highlight as "${language}"`, error)
    return null
  }

  const result = highlighter.codeToTokens(code, { lang: language, theme: SHIKI_THEMES[mode] })

  return {
    language,
    lines: result.tokens.map((line) =>
      line.map((token) => {
        const style = token.fontStyle ?? 0
        return {
          text: token.content,
          color: token.color,
          italic: (style & ITALIC) !== 0 || undefined,
          bold: (style & BOLD) !== 0 || undefined,
          underline: (style & UNDERLINE) !== 0 || undefined,
          strike: (style & STRIKETHROUGH) !== 0 || undefined,
        }
      }),
    ),
  }
}
