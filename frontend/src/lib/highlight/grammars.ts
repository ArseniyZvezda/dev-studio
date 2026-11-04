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
 * built-ins — and then cached for the life of the page. Opening the Explorer costs
 * nothing, and neither does browsing a project full of `.ts` and `.py`.
 */

import { authHeaders } from '@/api/auth'
import { api } from '@/api/client'
import type { SyntaxGrammar } from '@/api/types'
import type { LanguageRegistration } from 'shiki/types'

import { grammarExtensions, type ExtensionMap } from './extensions'

export interface PluginGrammars {
  /** Suffix → grammar id, ready to hand to `languageForPath`. */
  extensions: ExtensionMap
  byId: ReadonlyMap<string, SyntaxGrammar>
}

const NONE: PluginGrammars = { extensions: {}, byId: new Map() }

let pending: Promise<PluginGrammars> | null = null

/**
 * The registered grammars, fetched once.
 *
 * A failure resolves to "no plugin grammars" rather than rejecting: the endpoint not
 * being there yet, or a plugin being broken, must cost the file its colours and nothing
 * else. The rejected attempt is not cached, so a later file tries again.
 */
export function pluginGrammars(): Promise<PluginGrammars> {
  pending ??= api
    .pluginGrammars()
    .then((grammars) => ({
      extensions: grammarExtensions(grammars),
      byId: new Map(grammars.map((grammar) => [grammar.id, grammar])),
    }))
    .catch((error: unknown) => {
      console.warn('Could not load plugin syntax grammars', error)
      pending = null
      return NONE
    })
  return pending
}

/**
 * Forget the cached list, so the next file asks again.
 *
 * Plugins can be enabled and disabled from Settings without a reload, and a grammar that
 * arrived or left with one should not wait for the next page load to be noticed. Also the
 * seam the tests use, which is why it is exported rather than inlined into an event
 * handler this module does not own.
 */
export function resetPluginGrammars(): void {
  pending = null
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
export async function fetchGrammar(grammar: SyntaxGrammar): Promise<LanguageRegistration> {
  const response = await fetch(grammar.grammar_url, { headers: authHeaders() })
  if (!response.ok) {
    throw new Error(`${grammar.plugin_id}: grammar ${grammar.id} responded ${response.status}`)
  }
  const raw = (await response.json()) as Partial<LanguageRegistration>
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
