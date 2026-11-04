/**
 * What a file name resolves to (spec §6.2).
 *
 * This is the whole of the feature that is data, and therefore the whole of it a node
 * environment can check (`.claude/rules/testing.md`). Tokenizing needs WASM and rendering
 * needs a DOM; deciding that `Makefile` is Make, that `.gitignore` is nothing, and that a
 * plugin may claim `.as` but not `.ts` does not.
 */

import { describe, expect, it } from 'vitest'

import type { SyntaxGrammar } from '@/api/types'

import {
  BUILTIN_EXTENSIONS,
  BUILTIN_FILENAMES,
  extensionOf,
  fileNameOf,
  grammarExtensions,
  languageForPath,
  normalizeExtension,
} from './extensions'
import { LANGUAGE_LOADERS } from './languages'

function grammar(overrides: Partial<SyntaxGrammar> & Pick<SyntaxGrammar, 'id'>): SyntaxGrammar {
  return {
    plugin_id: 'angelscript',
    name: 'AngelScript',
    extensions: ['.as'],
    scope_name: 'source.angelscript',
    aliases: [],
    grammar_url: '/api/plugins/angelscript/assets/angelscript.tmLanguage.json',
    ...overrides,
  }
}

describe('suffixes', () => {
  it.each([
    ['main.ts', 'typescript'],
    ['App.vue', 'vue'],
    ['setup.py', 'python'],
    ['engine/Actor.cpp', 'cpp'],
    ['engine/Actor.h', 'c'],
    ['voxarium_studio/config.yaml', 'yaml'],
    ['package.json', 'json'],
    ['Content/Shaders/water.usf', 'hlsl'],
    ['shaders/blur.glsl', 'glsl'],
    ['scenes/player.gd', 'gdscript'],
    ['README.md', 'markdown'],
    ['run.ps1', 'powershell'],
    ['run.cmd', 'bat'],
  ])('%s is %s', (path, language) => {
    expect(languageForPath(path)).toBe(language)
  })

  it('does not care about case', () => {
    expect(languageForPath('README.MD')).toBe('markdown')
    expect(languageForPath('Main.PY')).toBe('python')
    expect(languageForPath('MAKEFILE')).toBe('make')
  })

  it('reads the last path segment, separator either way', () => {
    expect(languageForPath('a/b/c/main.rs')).toBe('rust')
    expect(languageForPath('a\\b\\c\\main.rs')).toBe('rust')
    // A directory that looks like a file must not colour the file inside it.
    expect(languageForPath('vendor.ts/LICENSE')).toBeNull()
  })

  it('answers null for anything unclaimed', () => {
    expect(languageForPath('data.qqq')).toBeNull()
    expect(languageForPath('notes.txt')).toBeNull()
    expect(languageForPath('')).toBeNull()
  })
})

describe('files with no extension', () => {
  it.each([
    ['Makefile', 'make'],
    ['GNUmakefile', 'make'],
    ['Dockerfile', 'docker'],
    ['CMakeLists.txt', 'cmake'],
    ['justfile', 'just'],
    ['Gemfile', 'ruby'],
  ])('%s is %s', (path, language) => {
    expect(languageForPath(path)).toBe(language)
  })

  it('treats a leading dot as part of the name, not as a suffix', () => {
    // `.gitignore` would otherwise resolve to a `gitignore` grammar that does not exist.
    expect(extensionOf('.gitignore')).toBeNull()
    expect(languageForPath('.gitignore')).toBeNull()
    expect(languageForPath('LICENSE')).toBeNull()
  })

  it('still knows the dotfiles that are a language', () => {
    expect(languageForPath('.env')).toBe('dotenv')
    expect(languageForPath('.bashrc')).toBe('shellscript')
    expect(languageForPath('.editorconfig')).toBe('ini')
  })

  it('follows the family-of-files convention', () => {
    expect(languageForPath('Dockerfile.dev')).toBe('docker')
    expect(languageForPath('.env.local')).toBe('dotenv')
  })

  it('lets the name beat the suffix', () => {
    // `.txt` is deliberately not in the suffix map, but this one file is CMake.
    expect(languageForPath('CMakeLists.txt')).toBe('cmake')
  })
})

describe('extensionOf', () => {
  it.each([
    ['main.ts', '.ts'],
    ['archive.tar.gz', '.gz'],
    ['.hidden.ts', '.ts'],
    ['UPPER.TS', '.ts'],
  ])('%s → %s', (path, extension) => {
    expect(extensionOf(path)).toBe(extension)
  })

  it.each(['noextension', '.gitignore', 'trailing.', ''])('%s has none', (path) => {
    expect(extensionOf(path)).toBeNull()
  })

  it('lower-cases the name it works from', () => {
    expect(fileNameOf('src/Components/App.Vue')).toBe('app.vue')
  })
})

describe('normalizeExtension', () => {
  it.each([
    ['as', '.as'],
    ['.as', '.as'],
    ['*.as', '.as'],
    ['  .AS  ', '.as'],
  ])('%s → %s', (raw, expected) => {
    expect(normalizeExtension(raw)).toBe(expected)
  })

  it.each(['', '   ', '.', '*'])('rejects %s', (raw) => {
    expect(normalizeExtension(raw)).toBeNull()
  })
})

describe('plugin grammars', () => {
  it('extends the map with what a plugin claims', () => {
    const extensions = grammarExtensions([grammar({ id: 'angelscript' })])

    expect(extensions).toEqual({ '.as': 'angelscript' })
    expect(languageForPath('scripts/combat.as')).toBeNull()
    expect(languageForPath('scripts/combat.as', extensions)).toBe('angelscript')
  })

  it('normalizes whatever form the manifest used', () => {
    const extensions = grammarExtensions([grammar({ id: 'angelscript', extensions: ['*.AS', 'ash'] })])
    expect(extensions).toEqual({ '.as': 'angelscript', '.ash': 'angelscript' })
  })

  it('cannot take a built-in suffix away', () => {
    // An over-eager plugin claiming `.ts` would otherwise stop TypeScript highlighting
    // everywhere in the project, which is a much larger regression than its own file.
    const extensions = grammarExtensions([
      grammar({ id: 'angelscript', extensions: ['.as', '.ts'] }),
    ])

    expect(extensions).toEqual({ '.as': 'angelscript' })
    expect(languageForPath('main.ts', extensions)).toBe('typescript')
  })

  it('resolves a contested suffix the same way every load', () => {
    const first = grammar({ id: 'angelscript', plugin_id: 'angelscript' })
    const second = grammar({ id: 'actionscript', plugin_id: 'flash' })
    expect(grammarExtensions([first, second])['.as']).toBe('angelscript')
  })

  it('ignores a grammar that claims nothing usable', () => {
    expect(grammarExtensions([grammar({ id: 'empty', extensions: ['', '.'] })])).toEqual({})
  })
})

describe('the maps and the loaders agree', () => {
  // A typo in a language id is invisible until someone opens that kind of file, and then
  // it is an unresolvable dynamic import in production. Cheaper to fail here.
  const mapped = new Set([
    ...Object.values(BUILTIN_EXTENSIONS),
    ...Object.values(BUILTIN_FILENAMES),
  ])

  it('has a loader for every language a file name can resolve to', () => {
    const missing = [...mapped].filter((language) => !(language in LANGUAGE_LOADERS))
    expect(missing).toEqual([])
  })

  it('carries no loader nothing can reach', () => {
    const unreachable = Object.keys(LANGUAGE_LOADERS).filter((language) => !mapped.has(language))
    expect(unreachable).toEqual([])
  })
})
