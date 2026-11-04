/**
 * That a grammar actually reaches the tokenizer (spec §6.2).
 *
 * This is the one part of the highlighter that is not a component and not a browser API:
 * Shiki's core, the Oniguruma engine and its WASM all run in Node, so the seam
 * `.claude/rules/testing.md` draws — data yes, rendering no — puts this on the yes side.
 * What it pins is the path a **plugin** grammar takes, because that is the one with a
 * network fetch, a foreign JSON file and a name that has to be rewritten in the middle of
 * it. A unit test over the extension map would have said nothing about any of that.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SyntaxGrammar } from '@/api/types'

const ANGELSCRIPT: SyntaxGrammar = {
  id: 'angelscript',
  plugin_id: 'angelscript',
  name: 'AngelScript',
  extensions: ['.as'],
  scope_name: 'source.angelscript',
  aliases: ['as'],
  grammar_url: '/api/plugins/angelscript/assets/angelscript.tmLanguage.json',
}

/**
 * A real TextMate grammar, small on purpose: a `match` rule, a `begin`/`end` rule, and a
 * `name` that is the language's *display* name — which is exactly what a downloaded
 * `.tmLanguage.json` looks like, and why the loader replaces it with the registry id.
 */
const ANGELSCRIPT_JSON = {
  name: 'AngelScript',
  scopeName: 'source.angelscript',
  patterns: [
    { name: 'keyword.control.angelscript', match: '\\b(if|else|while|for|return|class)\\b' },
    { name: 'string.quoted.double.angelscript', begin: '"', end: '"' },
    { name: 'constant.numeric.angelscript', match: '\\b[0-9]+\\b' },
  ],
}

const pluginGrammarsCall = vi.fn(async (): Promise<SyntaxGrammar[]> => [ANGELSCRIPT])

vi.mock('@/api/client', () => ({
  api: { pluginGrammars: () => pluginGrammarsCall() },
}))

// `@/api/auth` reaches for `window.localStorage` the moment it is asked for a token.
vi.mock('@/api/auth', () => ({ authHeaders: () => ({ Authorization: 'Bearer test' }) }))

const fetched: { url: string; headers: HeadersInit | undefined }[] = []

vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
  fetched.push({ url, headers: init?.headers })
  return {
    ok: true,
    status: 200,
    json: async () => ANGELSCRIPT_JSON,
  } as Response
})

const { highlightFile } = await import('./highlighter')
const { resetPluginGrammars } = await import('./grammars')

/** Flattened text, so a tokenization can be compared against the input it came from. */
const textOf = (lines: { text: string }[][]) => lines.map((line) => line.map((t) => t.text).join(''))

beforeEach(() => {
  fetched.length = 0
  pluginGrammarsCall.mockClear()
})

describe('a built-in language', () => {
  it('tokenizes and keeps every character', async () => {
    const code = 'const answer = 42\n// a comment\n'
    const result = await highlightFile('src/main.ts', code, 'dark')

    expect(result?.language).toBe('typescript')
    // Every character survives, the trailing newline's empty last line included — the
    // renderer draws one row per entry and a lost line is a lost line.
    expect(result!.lines).toHaveLength(3)
    expect(textOf(result!.lines).join('\n')).toBe(code)
    // A keyword and a comment must not have come out the same colour, or the grammar
    // resolved to plain text and nobody noticed.
    const colours = new Set(result!.lines.flat().map((token) => token.color))
    expect(colours.size).toBeGreaterThan(2)
  })

  it('never asks a plugin about a suffix it already knows', async () => {
    await highlightFile('src/main.ts', 'const x = 1', 'dark')
    expect(pluginGrammarsCall).not.toHaveBeenCalled()
  })

  it('answers different colours for the two themes', async () => {
    const code = 'const answer = 42'
    const dark = await highlightFile('src/main.ts', code, 'dark')
    const light = await highlightFile('src/main.ts', code, 'light')

    expect(textOf(dark!.lines)).toEqual(textOf(light!.lines))
    expect(dark!.lines[0]?.[0]?.color).not.toBe(light!.lines[0]?.[0]?.color)
  })
})

describe('a plugin grammar', () => {
  beforeEach(() => resetPluginGrammars())

  it('reaches the tokenizer under the id the registry gave it', async () => {
    const code = 'class Weapon { int damage = 12; }'
    const result = await highlightFile('scripts/weapon.as', code, 'dark')

    // Registered as `angelscript`, not as the `AngelScript` the JSON calls itself.
    expect(result?.language).toBe('angelscript')
    expect(textOf(result!.lines)).toEqual([code])

    const tokens = result!.lines[0]!
    expect(tokens.find((token) => token.text.startsWith('class'))?.color).toBeTruthy()
    expect(new Set(tokens.map((token) => token.color)).size).toBeGreaterThan(1)

    // The JSON came over the plugin-asset endpoint with an auth *header*: `fetch` can set
    // one, and only `import()` and the WebSocket are stuck with `?token=`.
    expect(fetched).toHaveLength(1)
    expect(fetched[0]!.url).toBe(ANGELSCRIPT.grammar_url)
    expect(fetched[0]!.headers).toEqual({ Authorization: 'Bearer test' })

    // …and once only, however many files of that kind are opened afterwards.
    await highlightFile('scripts/enemy.as', 'class B {}', 'light')
    expect(fetched).toHaveLength(1)
  })
})

describe('the fallbacks', () => {
  beforeEach(() => resetPluginGrammars())

  it('declines a suffix nobody claims', async () => {
    expect(await highlightFile('notes.qqq', 'hello', 'dark')).toBeNull()
  })

  it('declines rather than throwing when the grammar list is unreachable', async () => {
    pluginGrammarsCall.mockRejectedValueOnce(new Error('404'))
    expect(await highlightFile('scripts/other.aslang', 'class A {}', 'dark')).toBeNull()
  })
})
