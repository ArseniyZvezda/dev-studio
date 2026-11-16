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
import type { SyntaxGrammar } from '@/api/types'
import { ProjectSession, rendererSession, StaleProjectResponseError } from '@/session/project-session'

import { languageForPath } from './extensions'
import { fetchGrammar, onPluginGrammarsReset, pluginGrammarGeneration, pluginGrammars } from './grammars'
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

interface CoreState {
  core: Promise<HighlighterCore>
  themes: Map<HighlightMode, Promise<void>>
  languages: Map<string, Promise<void>>
}

interface ProjectHighlighters {
  sources: Map<string, CoreState>
}

const highlighters = new WeakMap<ProjectSession, ProjectHighlighters>()
// The regex engine holds no project grammar registry. Share its WASM-backed factory;
// only the TextMate registries below must be isolated by project and source.
let oniguruma: ReturnType<typeof createOnigurumaEngine> | null = null

function retire(project: ProjectHighlighters): void {
  for (const state of project.sources.values()) {
    // Reset invalidates every old caller before it can tokenize or start more loads.
    // Let existing imports/fetches finish before disposing their private registry.
    void state.core.then(async highlighter => {
      await Promise.allSettled([...state.themes.values(), ...state.languages.values()])
      highlighter.dispose()
    }).catch((error: unknown) => { console.warn('Could not dispose syntax highlighter', error) })
  }
  project.sources.clear()
}

function coreFor(session: ProjectSession, grammar?: SyntaxGrammar): CoreState {
  let project = highlighters.get(session)
  if (!project) {
    project = { sources: new Map() }
    highlighters.set(session, project)
    const captured = project
    onPluginGrammarsReset(session, () => { retire(captured) })
  }
  // A distinct TextMate registry per source is essential: replacing a language name
  // does not replace vscode-textmate's cached scopeName. Two sources (or projects)
  // may legitimately use both the same id and the same scope for different code.
  // The full scoped URL includes the backend's version stamp (§5.3.2, §6.2).
  const key = grammar ? JSON.stringify([
    grammar.plugin_id, grammar.id, grammar.grammar_url, grammar.scope_name,
    grammar.name, grammar.aliases,
  ]) : 'builtin'
  let state = project.sources.get(key)
  if (!state) {
    state = {
      core: createHighlighterCore({
        themes: [],
        langs: [],
        // Plugin grammars require the real Oniguruma regex dialect, unmodified.
        engine: oniguruma ??= createOnigurumaEngine(() => import('shiki/wasm')),
      }),
      themes: new Map(),
      languages: new Map(),
    }
    project.sources.set(key, state)
    const failed = state
    void state.core.catch(() => {
      if (project.sources.get(key) === failed) project.sources.delete(key)
    })
  }
  return state
}

function ensureTheme(state: CoreState, highlighter: HighlighterCore, mode: HighlightMode): Promise<void> {
  let loading = state.themes.get(mode)
  if (!loading) {
    loading = THEME_LOADERS[mode]()
      .then((module) => highlighter.loadTheme(module as never))
      .catch((error: unknown) => {
        state.themes.delete(mode)
        throw error
      })
    state.themes.set(mode, loading)
  }
  return loading
}

async function loadLanguage(
  highlighter: HighlighterCore,
  language: string,
  session: ProjectSession,
  grammar?: SyntaxGrammar,
): Promise<void> {
  const builtin = LANGUAGE_LOADERS[language]
  if (builtin) {
    const module = await builtin()
    await highlighter.loadLanguage(module.default)
    return
  }

  if (!grammar) throw new Error(`No grammar registered for "${language}"`)
  await highlighter.loadLanguage(await fetchGrammar(grammar, session))
}

function ensureLanguage(
  state: CoreState,
  highlighter: HighlighterCore,
  language: string,
  session: ProjectSession,
  grammar?: SyntaxGrammar,
): Promise<void> {
  let loading = state.languages.get(language)
  if (!loading) {
    loading = loadLanguage(highlighter, language, session, grammar).catch((error: unknown) => {
      // Not cached as a failure: a plugin that was still starting up gets another go.
      state.languages.delete(language)
      throw error
    })
    state.languages.set(language, loading)
  }
  return loading
}

/**
 * Which grammar a path resolves to, consulting plugin grammars only when the built-in
 * maps miss — which is what keeps `GET /api/plugins/grammars` off the app's startup path.
 */
export async function resolveLanguage(
  path: string,
  session: ProjectSession = rendererSession,
): Promise<string | null> {
  const builtin = languageForPath(path)
  if (builtin) return builtin
  const { extensions } = await pluginGrammars(session)
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
  session: ProjectSession = rendererSession,
): Promise<HighlightResult | null> {
  let language: string | null = null
  try {
    const current = session.capture()
    const generation = pluginGrammarGeneration(session)
    const assertCurrent = () => {
      current()
      if (pluginGrammarGeneration(session) !== generation) throw new StaleProjectResponseError()
    }
    language = await resolveLanguage(path, session)
    assertCurrent()
    if (!language) return null

    const grammar = LANGUAGE_LOADERS[language] ? undefined : (await pluginGrammars(session)).byId.get(language)
    assertCurrent()
    if (!LANGUAGE_LOADERS[language] && !grammar) return null
    const state = coreFor(session, grammar)
    const highlighter = await state.core
    assertCurrent()
    await Promise.all([
      ensureTheme(state, highlighter, mode),
      ensureLanguage(state, highlighter, language, session, grammar),
    ])
    assertCurrent()
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
  } catch (error) {
    if (!(error instanceof StaleProjectResponseError)) console.warn(`Could not highlight as "${language}"`, error)
    return null
  }
}
