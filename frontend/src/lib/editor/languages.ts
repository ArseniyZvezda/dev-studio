/**
 * Languages offered for a code block's fence (block-editor spec §3.1).
 *
 * A list, not a free-text field, because the value is written straight after the backticks
 * and a typo there is a language nothing highlights. The field searches it rather than
 * scrolling it — §4.5 asks for "with search over the list" — and an empty value is a fence
 * with no language, which is a legitimate answer and the default.
 *
 * Weighted towards what this project actually contains: Python for the backend and node
 * scripts, TypeScript and Vue for the Studio, C++ and shaders for Unreal, YAML and JSON for
 * the manifests.
 *
 * The names are names and stay as they are in every language — except the fence with none,
 * whose label is a getter so it is read in the interface language each time it is shown.
 */

import { t } from '@/i18n'

export const CODE_LANGUAGES: { value: string; label: string }[] = [
  {
    value: '',
    get label() {
      return t('editor.languages.plainText')
    },
  },
  { value: 'bash', label: 'Bash' },
  { value: 'c', label: 'C' },
  { value: 'cpp', label: 'C++' },
  { value: 'csharp', label: 'C#' },
  { value: 'css', label: 'CSS' },
  { value: 'diff', label: 'Diff' },
  { value: 'dockerfile', label: 'Dockerfile' },
  { value: 'glsl', label: 'GLSL' },
  { value: 'go', label: 'Go' },
  { value: 'graphql', label: 'GraphQL' },
  { value: 'hlsl', label: 'HLSL' },
  { value: 'html', label: 'HTML' },
  { value: 'ini', label: 'INI' },
  { value: 'java', label: 'Java' },
  { value: 'javascript', label: 'JavaScript' },
  { value: 'json', label: 'JSON' },
  { value: 'kotlin', label: 'Kotlin' },
  { value: 'lua', label: 'Lua' },
  { value: 'makefile', label: 'Makefile' },
  { value: 'markdown', label: 'Markdown' },
  { value: 'nginx', label: 'Nginx' },
  { value: 'objectivec', label: 'Objective-C' },
  { value: 'perl', label: 'Perl' },
  { value: 'php', label: 'PHP' },
  { value: 'powershell', label: 'PowerShell' },
  { value: 'python', label: 'Python' },
  { value: 'r', label: 'R' },
  { value: 'ruby', label: 'Ruby' },
  { value: 'rust', label: 'Rust' },
  { value: 'scss', label: 'SCSS' },
  { value: 'sql', label: 'SQL' },
  { value: 'swift', label: 'Swift' },
  { value: 'toml', label: 'TOML' },
  { value: 'typescript', label: 'TypeScript' },
  { value: 'tsx', label: 'TSX' },
  { value: 'vue', label: 'Vue' },
  { value: 'xml', label: 'XML' },
  { value: 'yaml', label: 'YAML' },
]

export function searchLanguages(query: string): { value: string; label: string }[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return CODE_LANGUAGES
  return CODE_LANGUAGES.filter(
    (entry) => entry.label.toLowerCase().includes(needle) || entry.value.includes(needle),
  )
}
