import { describe, expect, it } from 'vitest'
import { contextFormat } from './context-format'
import { readContextJson, visibleJsonRows } from './context-json'

describe('context fragment reader (§10.4)', () => {
  it('formats nested JSON without coercing numbers, deduplicating keys or decoding escapes', () => {
    const source = '{"id":9007199254740993123,"id":[{"v":"\\u0041"},{}],"next":true}'
    const { format, rows } = readContextJson(source)!
    const lines = rows.map(row => '  '.repeat(row.depth) + row.tokens.map(token => token.text).join(''))
    expect(format).toBe('JSON')
    expect(lines).toEqual(['{', '  "id": 9007199254740993123,', '  "id": [', '    {',
      '      "v": "\\u0041"', '    },', '    {}', '  ],', '  "next": true', '}'])
    expect(visibleJsonRows(rows, new Set([2]))).toEqual([0, 1, 2, 8, 9])
    expect(visibleJsonRows(rows, new Set([0, 2]))).toEqual([0])
    expect(visibleJsonRows(rows, new Set())).toHaveLength(10)
  })

  it('preserves JSON5 comments, trailing commas and delimiters inside strings', () => {
    const { format, rows } = readContextJson("{x: 'a}/*b', // keep [\n y: [0xff,],}")!
    expect(format).toBe('JSON5')
    expect(rows.flatMap(row => row.tokens).map(t => [t.kind, t.text])).toEqual(expect.arrayContaining([
      ['string', "'a}/*b'"], ['comment', '// keep ['], ['number', '0xff'], ['key', 'x'],
    ]))
    expect(rows[0].end).toBe(rows.length - 1)
  })

  it('folds JSONL records independently; a corrupt record falls back to the actual text', () => {
    const source = '{"x":[1]}\r\n\r\n{"y":2}'
    const { rows, format } = readContextJson(source)!
    expect(format).toBe('JSONL')
    expect(visibleJsonRows(rows, new Set([0, 5]))).toEqual([0, 5])
    expect(readContextJson(source + '\n{bad')).toBeNull()
  })

  it('keeps the tool caption and fence markers separate from the structured body', () => {
    const fenced = '\x60\x60\x60json\r\n{"ok":true}\r\n\x60\x60\x60'
    for (const text of [fenced, 'Read\n{"ok":true}']) {
      const { before, body, after, json } = contextFormat(text, '', 'tools')
      expect(before + body + after).toBe(text)
      expect(json?.format).toBe('JSON')
    }
    expect(contextFormat('[1]', '', 'tools', true).json).toBeNull()
    expect(contextFormat('{"unfinished":', '', 'tools').json).toBeNull()
  })

  it('prefers explicit language evidence and leaves ordinary prose alone', () => {
    expect(contextFormat('\x60\x60\x60python\ndef work(): pass\n\x60\x60\x60', '', 'history').path).toMatch(/\.py\w*$/)
    expect(contextFormat('result = 2', 'Read · main.py', 'tools').path).toBe('Read · main.py')
    expect(contextFormat('diff --git a/x b/x', '', 'tools').path).toBe('fragment.diff')
    expect(contextFormat('# Notes\n\nHello', '', 'system').path).toBe('fragment.md')
    expect(contextFormat('Example:\n```js\nconst x = 1\n```\nDone.', '', 'history').path).toBe('fragment.md')
    expect(contextFormat('Ordinary conversation.', '', 'history').path).toBeUndefined()
  })

  it('bounds pretty-print expansion even for a small minified input', () => {
    expect(readContextJson('[' + '1,'.repeat(6000) + '2]')).toBeNull()
  })
})
