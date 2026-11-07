/** A journal fragment usually has a tool name or a message role instead of a filename
 * (§10.4). Prefer explicit fences and filenames, then distinctive content signatures. */
import type { ContextCategory } from '@/api/types'
import { BUILTIN_EXTENSIONS, languageForPath } from './highlight/extensions'
import { oversizeReason } from './highlight/use-highlight'
import { readContextJson } from './context-json'

const fencePaths = new Map<string, string>()
for (const [suffix, language] of Object.entries(BUILTIN_EXTENSIONS)) {
  if (!fencePaths.has(language)) fencePaths.set(language, 'fragment' + suffix)
}
function fencePath(name: string) {
  const path = 'fragment.' + name.toLowerCase()
  return languageForPath(path) ? path : fencePaths.get(name.toLowerCase())
}

function sniffPath(text: string): string | undefined {
  const head = text.slice(0, 3000).trimStart()
  if (/^(?:diff --git |@@ -\d|--- [^\n]+\r?\n\+\+\+ )/.test(head)) return 'fragment.diff'
  if (/^<!(?:doctype html)\b|^<html\b/i.test(head)) return 'fragment.html'
  if (/^<\?xml\b|^<[\w:-]+(?:\s[^<>]*)?\/?>/.test(head)) return 'fragment.xml'
  // Check Markdown boundaries before looking for code declarations inside the message.
  if (/^(?:`{3,}|~{3,})/m.test(head)) return 'fragment.md'
  if (/^#![^\n]*\bpython[\d.]*\b|^(?:from [\w.]+ import |import [\w.]+\s*$|(?:async )?def \w+\()/m.test(head)) return 'fragment.py'
  if (/^#![^\n]*\b(?:bash|sh|zsh)\b/.test(head)) return 'fragment.sh'
  if (/^(?:param\s*\(|\$(?:env:|ErrorActionPreference\b)|(?:Get|Set|Write|Invoke)-[A-Z]\w+)/.test(head)) return 'fragment.ps1'
  if (/^(?:export\s+)?(?:interface|type)\s+\w+\s*[<={]/m.test(head)) return 'fragment.ts'
  if (/^(?:(?:export|async)\s+)*(?:function\s+\w+\s*\(|(?:const|let|var)\s+[\w$]+\s*=)|^import .+ from ['"]/m.test(head)) return 'fragment.js'
  if (/^#\s*include\s*[<"]/.test(head)) return 'fragment.cpp'
  if (/^(?:SELECT\s+.+?\s+FROM\b|CREATE\s+TABLE\b|INSERT\s+INTO\b)/is.test(head)) return 'fragment.sql'
  if (/^(?:---\s*\n)?[\w.-]+:\s[^\n]*\n\s*[\w.-]+:\s/.test(head)) return 'fragment.yaml'
  if (/^(?:#{1,6}\s|[-*] \S|> \S)/m.test(head)) return 'fragment.md'
  if (/^(?:\{\s*(?:["']|[\w$]+\s*:)|\[\s*[{"\d])/.test(head)) return 'fragment.json5'
}

export function contextFormat(text: string, label: string, category: ContextCategory, truncated = false) {
  let body = text, before = '', after = '', hint: string | undefined
  const filename = label.trim()
  if (!oversizeReason(text)) {
    const fence = /^(\s*(`{3,}|~{3,})([\w+.#-]*)[^\S\r\n]*\r?\n)([\s\S]*?)(\r?\n\2[^\S\r\n]*\s*)$/.exec(text)
    // A message with multiple fences is one Markdown document.
    if (fence && !/^(?:`{3,}|~{3,})/m.test(fence[4])) {
      before = fence[1]
      body = fence[4]
      after = fence[5]
      hint = fencePath(fence[3])
    } else if (category === 'tools') {
      // Studio's timeline may prefix the JSON arguments with the tool's name.
      const heading = /^([^\r\n]{1,200}\r?\n)(?=\s*[[{])/.exec(text)
      if (heading && !/^\s*[[{]/.test(heading[1])) {
        before = heading[1]
        body = text.slice(before.length)
      }
    }
  }
  const json = truncated ? null : readContextJson(body)
  const path = hint ?? (languageForPath(filename) ? filename : undefined)
    ?? sniffPath(body) ?? (category === 'skills' || category === 'documents' ? 'fragment.md' : undefined)
  return { before, body, after, json, path }
}
