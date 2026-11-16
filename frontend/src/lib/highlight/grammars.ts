/**
 * Grammars plugins taught the Studio to highlight (spec §6.2).
 *
 * A plugin ships a stock TextMate `.tmLanguage.json` — the format VS Code uses — and
 * writes no highlighting code. That is the whole reason the engine below is Oniguruma
 * rather than Shiki's JavaScript one: the JS engine silently degrades on regex constructs
 * it cannot translate, and a third-party grammar has to work unmodified or the extension
 * point is a promise the Studio does not keep.
 *
 * The list is fetched **on demand** — the first time a file's suffix is not one of the
 * built-ins — and then cached per captured project until its registry resets. Opening
 * the Explorer costs nothing, and neither does browsing a project full of `.ts` and `.py`.
 */

import { authHeaders } from '@/api/auth'
import { api, createApiClient } from '@/api/client'
import { scopedApiUrl, type Query } from '@/api/project-scope'
import type { SyntaxGrammar } from '@/api/types'
import { ProjectSession, rendererSession, StaleProjectResponseError } from '@/session/project-session'
import type { LanguageRegistration } from 'shiki/types'

import { grammarExtensions, type ExtensionMap } from './extensions'

export interface PluginGrammars {
  /** Suffix → grammar id, ready to hand to `languageForPath`. */
  extensions: ExtensionMap
  byId: ReadonlyMap<string, SyntaxGrammar>
}

const NONE: PluginGrammars = { extensions: {}, byId: new Map() }

interface GrammarState {
  generation: number
  pending: Promise<PluginGrammars> | null
  onReset: Set<() => void>
}

// §5.3: neither a list request nor a reset may reach another renderer's project.
const states = new WeakMap<ProjectSession, GrammarState>()

function stateFor(session: ProjectSession): GrammarState {
  let state = states.get(session)
  if (!state) {
    state = { generation: 0, pending: null, onReset: new Set() }
    states.set(session, state)
  }
  return state
}

/** Also invalidates loaded Shiki registries, not just the list of suffix claims. */
export function pluginGrammarGeneration(session: ProjectSession = rendererSession): number {
  return stateFor(session).generation
}

/** Loaded registries can release their resources when this project's list resets. */
export function onPluginGrammarsReset(session: ProjectSession, listener: () => void): () => void {
  const listeners = stateFor(session).onReset
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/**
 * Normalize host-generated grammar descriptors: the documented legacy asset shape or
 * an already scoped backend URL (§5.3.2), for this grammar's owning plugin only. This
 * does not widen the runtime's contract for arbitrary URLs written in plugin code.
 * Reject foreign selectors rather than silently borrowing their data.
 * Do not parse through `new URL` first: it normalises away traversal segments.
 */
function grammarUrl(grammar: SyntaxGrammar, session: ProjectSession): string {
  const url = grammar.grammar_url
  if (!url.startsWith('/api/') || /[#\\]/.test(url)) throw new Error('Invalid grammar URL')
  const queryAt = url.indexOf('?')
  let path = queryAt < 0 ? url.slice(4) : url.slice(4, queryAt)
  const params = new URLSearchParams(queryAt < 0 ? '' : url.slice(queryAt + 1))
  if (path.startsWith('/projects/')) {
    const match = /^\/projects\/([^/]+)(\/.*)$/.exec(path)
    if (!match || session.isLegacy || match[1] !== session.projectId) {
      throw new Error('Conflicting grammar project scope')
    }
    path = match[2]!
  }
  if (!path.startsWith(`/plugins/${encodeURIComponent(grammar.plugin_id)}/assets/`)) {
    throw new Error('Invalid grammar asset URL')
  }
  for (const key of ['project_id', 'epoch'] as const) {
    const expected = session.scope?.[key]
    if (params.getAll(key).some(value => value !== expected)) {
      throw new Error('Conflicting grammar project scope')
    }
  }
  // scopedApiUrl checks encoded path segments and appends the immutable scope, while
  // retaining the source's version stamp (part of the highlighter's cache identity).
  const query: Query = Object.fromEntries(params)
  return scopedApiUrl(session, path, query)
}

/**
 * The registered grammars, fetched once.
 *
 * A failure resolves to "no plugin grammars" rather than rejecting: the endpoint not
 * being there yet, or a plugin being broken, must cost the file its colours and nothing
 * else. The rejected attempt is not cached, so a later file tries again.
 */
export function pluginGrammars(session: ProjectSession = rendererSession): Promise<PluginGrammars> {
  const state = stateFor(session)
  const generation = state.generation
  const current = session.capture()
  state.pending ??= (session === rendererSession ? api : createApiClient(session))
    .pluginGrammars()
    .then((grammars) => {
      current()
      if (state.generation !== generation) throw new StaleProjectResponseError()
      const bound = grammars.map(grammar => ({ ...grammar, grammar_url: grammarUrl(grammar, session) }))
      return {
        extensions: grammarExtensions(bound),
        byId: new Map(bound.map((grammar) => [grammar.id, grammar])),
      }
    })
    .catch((error: unknown) => {
      if (!(error instanceof StaleProjectResponseError)) console.warn('Could not load plugin syntax grammars', error)
      // A rejected request from before a reset cannot erase the new list's promise.
      if (state.generation === generation) state.pending = null
      return NONE
    })
  return state.pending
}

/**
 * Forget the cached list, so the next file asks again.
 *
 * Plugins can be enabled and disabled from Settings without a reload, and a grammar that
 * arrived or left with one should not wait for the next page load to be noticed. Also the
 * seam the tests use, which is why it is exported rather than inlined into an event
 * handler this module does not own.
 */
export function resetPluginGrammars(session: ProjectSession = rendererSession): void {
  const state = stateFor(session)
  state.generation += 1
  state.pending = null
  for (const listener of state.onReset) listener()
}

/**
 * Fetch one grammar's JSON and shape it into a Shiki language registration.
 *
 * `fetch` with `authHeaders()` rather than `withTokenParam` — the token belongs in a
 * header wherever a header is possible, and only `import()` and the WebSocket cannot send
 * one. `name` is overridden with the registry id because Shiki keys its language map on
 * `name`, and a `.tmLanguage.json` names itself for humans (`AngelScript`), not for a
 * lookup. `scopeName` is *not* overridden: a grammar's internal `include` rules refer to
 * its own scope, so the file is the authority and the manifest is only the fallback.
 */
export async function fetchGrammar(
  grammar: SyntaxGrammar,
  session: ProjectSession = rendererSession,
): Promise<LanguageRegistration> {
  const current = session.capture()
  const generation = pluginGrammarGeneration(session)
  const assertCurrent = () => {
    current()
    if (pluginGrammarGeneration(session) !== generation) throw new StaleProjectResponseError()
  }
  const response = await fetch(grammarUrl(grammar, session), { headers: authHeaders() })
  assertCurrent()
  if (!response.ok) {
    throw new Error(`${grammar.plugin_id}: grammar ${grammar.id} responded ${response.status}`)
  }
  const raw = (await response.json()) as Partial<LanguageRegistration>
  assertCurrent()
  const scopeName = raw.scopeName ?? grammar.scope_name
  if (!scopeName) throw new Error(`${grammar.plugin_id}: grammar ${grammar.id} has no scopeName`)

  return {
    ...(raw as LanguageRegistration),
    name: grammar.id,
    scopeName,
    displayName: grammar.name || raw.displayName,
    aliases: grammar.aliases.filter((alias) => alias !== grammar.id),
  }
}
