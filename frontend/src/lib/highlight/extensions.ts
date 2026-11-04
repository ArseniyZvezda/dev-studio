/**
 * Which language a file's name implies (spec §6.2).
 *
 * Pure data and pure functions: no Shiki, no Vue, no DOM, nothing that could drag the
 * highlighter into a chunk that merely wants to know "is this file a language at all".
 * That is also what makes it the part of this feature a node-environment test can pin
 * (`.claude/rules/testing.md`) — everything downstream of it needs a browser.
 *
 * Two rules the maps encode rather than approximate:
 *
 * - **A leading dot is not an extension.** `.gitignore` is a whole file name, so the
 *   suffix search starts at index 1 and that file resolves to nothing rather than to a
 *   `gitignore` grammar that does not exist. Dotfiles that *are* a language
 *   (`.env`, `.bashrc`) are named in `BUILTIN_FILENAMES` instead.
 * - **The name wins over the suffix.** `CMakeLists.txt` is CMake, not text.
 *
 * The values are Shiki's own language ids — the file names under `@shikijs/langs/` — not
 * the friendlier labels a fence uses. `bash` and `makefile` are aliases there; the
 * canonical `shellscript`/`make` are used so two extensions never pull two copies of one
 * grammar into two chunks. `languages.spec.ts` fails if a value here has no loader.
 */

import type { SyntaxGrammar } from '@/api/types'

/** Lower-cased, dot-prefixed suffix → Shiki language id. */
export type ExtensionMap = Readonly<Record<string, string>>

export const BUILTIN_EXTENSIONS: ExtensionMap = {
  // web
  '.ts': 'typescript',
  '.mts': 'typescript',
  '.cts': 'typescript',
  '.tsx': 'tsx',
  '.js': 'javascript',
  '.mjs': 'javascript',
  '.cjs': 'javascript',
  '.jsx': 'jsx',
  '.vue': 'vue',
  '.svelte': 'svelte',
  '.astro': 'astro',
  '.html': 'html',
  '.htm': 'html',
  '.css': 'css',
  '.scss': 'scss',
  '.sass': 'sass',
  '.less': 'less',

  // data and config
  '.json': 'json',
  '.jsonc': 'jsonc',
  '.json5': 'json5',
  '.jsonl': 'jsonl',
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.toml': 'toml',
  '.ini': 'ini',
  '.cfg': 'ini',
  '.conf': 'ini',
  '.properties': 'properties',
  '.xml': 'xml',
  '.xsd': 'xml',
  '.svg': 'xml',
  '.plist': 'xml',
  '.csv': 'csv',
  '.tsv': 'tsv',
  '.sql': 'sql',
  '.graphql': 'graphql',
  '.gql': 'graphql',
  '.proto': 'proto',
  '.tf': 'terraform',
  '.tfvars': 'terraform',
  '.hcl': 'hcl',
  '.nix': 'nix',
  '.cmake': 'cmake',
  '.gradle': 'groovy',
  '.reg': 'reg',
  '.desktop': 'desktop',
  '.service': 'systemd',

  // prose
  '.md': 'markdown',
  '.markdown': 'markdown',
  '.mdx': 'mdx',
  '.rst': 'rst',
  '.tex': 'latex',
  '.adoc': 'asciidoc',
  '.mmd': 'mermaid',

  // shells and diffs
  '.sh': 'shellscript',
  '.bash': 'shellscript',
  '.zsh': 'shellscript',
  '.fish': 'fish',
  '.ps1': 'powershell',
  '.psm1': 'powershell',
  '.psd1': 'powershell',
  '.bat': 'bat',
  '.cmd': 'bat',
  '.diff': 'diff',
  '.patch': 'diff',
  '.log': 'log',

  // general purpose
  '.py': 'python',
  '.pyi': 'python',
  '.pyw': 'python',
  '.c': 'c',
  '.h': 'c',
  '.cpp': 'cpp',
  '.cc': 'cpp',
  '.cxx': 'cpp',
  '.hpp': 'cpp',
  '.hh': 'cpp',
  '.hxx': 'cpp',
  '.inl': 'cpp',
  '.cs': 'csharp',
  '.rs': 'rust',
  '.go': 'go',
  '.java': 'java',
  '.kt': 'kotlin',
  '.kts': 'kotlin',
  '.swift': 'swift',
  '.m': 'objective-c',
  '.mm': 'objective-cpp',
  '.zig': 'zig',
  '.lua': 'lua',
  '.rb': 'ruby',
  '.php': 'php',
  '.pl': 'perl',
  '.pm': 'perl',
  '.r': 'r',
  '.jl': 'julia',
  '.dart': 'dart',
  '.scala': 'scala',
  '.ex': 'elixir',
  '.exs': 'elixir',
  '.erl': 'erlang',
  '.hs': 'haskell',
  '.clj': 'clojure',
  '.cljs': 'clojure',
  '.nim': 'nim',
  '.tcl': 'tcl',
  '.awk': 'awk',
  '.asm': 'asm',
  '.s': 'asm',
  '.wat': 'wasm',

  // what a game project actually holds
  '.glsl': 'glsl',
  '.vert': 'glsl',
  '.frag': 'glsl',
  '.geom': 'glsl',
  '.comp': 'glsl',
  '.hlsl': 'hlsl',
  '.fx': 'hlsl',
  '.fxh': 'hlsl',
  // Unreal's shader dialect is HLSL with its own preprocessor.
  '.usf': 'hlsl',
  '.ush': 'hlsl',
  '.wgsl': 'wgsl',
  '.shader': 'shaderlab',
  '.cginc': 'hlsl',
  '.gd': 'gdscript',
  '.gdshader': 'gdshader',
  '.tres': 'tres',
  '.tscn': 'tscn',
}

/**
 * Whole file names, matched before any suffix. Lower-cased, because `Makefile` and
 * `makefile` are the same file to everyone except a case-sensitive lookup.
 */
export const BUILTIN_FILENAMES: ExtensionMap = {
  makefile: 'make',
  gnumakefile: 'make',
  justfile: 'just',
  dockerfile: 'docker',
  containerfile: 'docker',
  'cmakelists.txt': 'cmake',
  rakefile: 'ruby',
  gemfile: 'ruby',
  podfile: 'ruby',
  vagrantfile: 'ruby',
  brewfile: 'ruby',
  'cargo.lock': 'toml',
  'uv.lock': 'toml',
  codeowners: 'codeowners',
  '.env': 'dotenv',
  '.bashrc': 'shellscript',
  '.bash_profile': 'shellscript',
  '.zshrc': 'shellscript',
  '.profile': 'shellscript',
  '.editorconfig': 'ini',
  '.gitconfig': 'ini',
  '.npmrc': 'ini',
  '.gitmodules': 'ini',
}

/**
 * Prefixes for the family-of-files convention — `Dockerfile.base`, `.env.local`. Cheaper
 * than enumerating every suffix people invent, and checked only after both maps miss.
 */
const FILENAME_PREFIXES: readonly (readonly [string, string])[] = [
  ['dockerfile.', 'docker'],
  ['.env.', 'dotenv'],
]

/** Last path segment, lower-cased. Accepts `/` and `\` so a Windows path is not one name. */
export function fileNameOf(path: string): string {
  const segments = path.split(/[\\/]/)
  return (segments[segments.length - 1] ?? '').toLowerCase()
}

/**
 * Dot-prefixed suffix of a file name, or null when it has none.
 *
 * Searched from index 1: a file called `.gitignore` has no extension, it *is* the name.
 */
export function extensionOf(path: string): string | null {
  const name = fileNameOf(path)
  const dot = name.lastIndexOf('.')
  if (dot < 1 || dot === name.length - 1) return null
  return name.slice(dot)
}

/**
 * The language to highlight a file as, or null for "render it plain".
 *
 * `extra` is where plugin-contributed grammars arrive (§6.2) — folded in as a second map
 * rather than mutated into the built-in one, so a failed grammar fetch or a plugin being
 * disabled cannot leave a stale claim behind.
 */
export function languageForPath(path: string, extra?: ExtensionMap): string | null {
  const name = fileNameOf(path)
  if (!name) return null

  const byName = BUILTIN_FILENAMES[name]
  if (byName) return byName

  const extension = extensionOf(path)
  if (extension) {
    const builtin = BUILTIN_EXTENSIONS[extension]
    if (builtin) return builtin
    const contributed = extra?.[extension]
    if (contributed) return contributed
  }

  for (const [prefix, language] of FILENAME_PREFIXES) {
    if (name.startsWith(prefix)) return language
  }

  return null
}

/** `AS` / `*.as` / `.as` all mean the same suffix; the wire format promises none of them. */
export function normalizeExtension(raw: string): string | null {
  let value = raw.trim().toLowerCase()
  if (value.startsWith('*')) value = value.slice(1)
  if (!value || value === '.') return null
  return value.startsWith('.') ? value : `.${value}`
}

/**
 * Fold the grammars plugins registered into an extension map.
 *
 * Two precedence rules, both deliberate. **A built-in wins**, so an over-eager plugin
 * claiming `.json` cannot take JSON away from every other file in the project; and among
 * plugins **the first registration wins**, so two plugins claiming one suffix resolve the
 * same way on every load rather than by response order. The value is the grammar's `id`,
 * which is also the name it is registered with in Shiki.
 */
export function grammarExtensions(grammars: readonly SyntaxGrammar[]): Record<string, string> {
  const map: Record<string, string> = {}
  for (const grammar of grammars) {
    for (const raw of grammar.extensions) {
      const extension = normalizeExtension(raw)
      if (!extension) continue
      if (extension in BUILTIN_EXTENSIONS) continue
      if (extension in map) continue
      map[extension] = grammar.id
    }
  }
  return map
}
