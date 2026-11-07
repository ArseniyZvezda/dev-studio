/** Lossless presentation of JSON-shaped context (§10.4). Parse to validate, but display
 * the original lexemes: converting through JS values would round IDs and lose duplicate
 * keys, comments, and escapes. Fold ranges address rows, never property names. */
import JSON5 from 'json5'
import { MAX_HIGHLIGHT_LINES, oversizeReason } from './highlight/use-highlight'

export interface JsonToken { text: string; kind?: 'key' | 'string' | 'number' | 'literal' | 'comment' }
export interface JsonRow { depth: number; tokens: JsonToken[]; end?: number }
export interface ContextJson { format: 'JSON' | 'JSON5' | 'JSONL'; rows: JsonRow[] }

const lexemes = /"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|\/\/[^\r\n\u2028\u2029]*|\/\*[\s\S]*?\*\/|[{}[\],:]|[^\s{}[\],:'"/]+/gu

export function readContextJson(text: string): ContextJson | null {
  if (!/^\s*(?:[[{]|\/\/|\/\*)/.test(text) || oversizeReason(text)) return null
  let documents = [text]
  let format: ContextJson['format'] = 'JSON'
  try {
    JSON.parse(text)
  } catch {
    try {
      const value = JSON5.parse(text)
      if (value === null || typeof value !== 'object') return null
      format = 'JSON5'
    } catch {
      documents = text.split(/\r?\n/).filter(line => line.trim())
      if (documents.length < 2) return null
      try {
        for (const line of documents) {
          if (!/^\s*[[{]/.test(line)) return null
          JSON.parse(line)
        }
      } catch { return null }
      format = 'JSONL'
    }
  }

  const rows: JsonRow[] = []
  const opens: number[] = []
  let tokens: JsonToken[] = []
  function flush() {
    if (tokens.length) rows.push({ depth: opens.length, tokens })
    tokens = []
  }
  for (const document of documents) {
    let previousValue: JsonToken | undefined
    for (const [lexeme] of document.matchAll(lexemes)) {
      if (rows.length >= MAX_HIGHLIGHT_LINES) return null
      if (lexeme === '{' || lexeme === '[') {
        tokens.push({ text: lexeme })
        flush()
        opens.push(rows.length - 1)
      } else if (lexeme === '}' || lexeme === ']') {
        flush()
        const start = opens.pop()!
        // Keep {} and [] inline; they have no children to hide.
        if (start === rows.length - 1) tokens = rows.pop()!.tokens
        else rows[start].end = rows.length
        tokens.push({ text: lexeme })
      } else if (lexeme === ',') {
        tokens.push({ text: ',' })
        flush()
      } else if (lexeme === ':') {
        if (previousValue) previousValue.kind = 'key'
        tokens.push({ text: ': ' })
      } else if (lexeme.startsWith('/')) {
        if (tokens.length) tokens.push({ text: ' ' })
        tokens.push({ text: lexeme, kind: 'comment' })
        flush()
      } else {
        previousValue = { text: lexeme, kind: /^["']/.test(lexeme) ? 'string'
          : /^(?:true|false|null)$/.test(lexeme) ? 'literal' : 'number' }
        tokens.push(previousValue)
      }
    }
    flush()
  }
  return { format, rows }
}

export function visibleJsonRows(rows: JsonRow[], folded: ReadonlySet<number>) {
  const visible: number[] = []
  for (let index = 0; index < rows.length; index++) {
    visible.push(index)
    if (folded.has(index)) index = rows[index].end ?? index
  }
  return visible
}
