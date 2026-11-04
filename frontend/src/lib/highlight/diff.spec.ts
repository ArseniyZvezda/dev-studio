/**
 * Reading a patch for the task's diff pane (spec §10.4).
 *
 * What is pinned is what the pane draws from: each row's kind and line numbers, and which
 * line of which side it takes its tokens from. Getting a number wrong is worse than showing
 * none, so the cases are the ones a first-character reading gets wrong.
 */

import { describe, expect, it } from 'vitest'

import { parsePatch } from './diff'

const GIT = [
  'diff --git a/src/dash.ts b/src/dash.ts',
  'index 1234567..89abcde 100644',
  '--- a/src/dash.ts',
  '+++ b/src/dash.ts',
  '@@ -10,4 +10,5 @@ export function dash() {',
  '   const speed = 4',
  '-  const cooldown = 1.2',
  '+  const cooldown = 0.8',
  '+  const charges = 2',
  '   return { speed }',
  ' }',
  '',
].join('\n')

describe('parsePatch', () => {
  it('numbers each side and hides the file header the path bar already names', () => {
    const { rows, numbered } = parsePatch(GIT)
    expect(numbered).toBe(true)
    expect(rows.map((row) => [row.kind, row.oldLine, row.newLine, row.text])).toEqual([
      ['hunk', null, null, '@@ -10,4 +10,5 @@ export function dash() {'],
      ['context', 10, 10, '  const speed = 4'],
      ['removed', 11, null, '  const cooldown = 1.2'],
      ['added', null, 11, '  const cooldown = 0.8'],
      ['added', null, 12, '  const charges = 2'],
      ['context', 12, 13, '  return { speed }'],
      ['context', 13, 14, '}'],
    ])
  })

  it('splits the sides so each can be tokenized as the file it came from', () => {
    const { rows, sides } = parsePatch(GIT)
    expect(sides.old).toEqual([
      '  const speed = 4',
      '  const cooldown = 1.2',
      '  return { speed }',
      '}',
    ])
    expect(sides.new).toEqual([
      '  const speed = 4',
      '  const cooldown = 0.8',
      '  const charges = 2',
      '  return { speed }',
      '}',
    ])
    for (const row of rows) {
      if (row.oldIndex !== null) expect(sides.old[row.oldIndex]).toBe(row.text)
      if (row.newIndex !== null) expect(sides.new[row.newIndex]).toBe(row.text)
    }
  })

  it('reads a removed line that starts with dashes as a removed line', () => {
    // A Lua or SQL comment removed is `--- comment`, which reads as a file header by its
    // first characters — the hunk's counts are what say it is not one.
    const { rows } = parsePatch(
      ['--- a/x.lua', '+++ b/x.lua', '@@ -1,1 +1,1 @@', '--- old', '+++ new'].join('\n'),
    )
    expect(rows.map((row) => [row.kind, row.text])).toEqual([
      ['hunk', '@@ -1,1 +1,1 @@'],
      ['removed', '-- old'],
      ['added', '++ new'],
    ])
  })

  it('follows several hunks and keeps the no-newline marker as a note', () => {
    const { rows } = parsePatch(
      [
        '@@ -1 +1 @@',
        '-a',
        '+b',
        '\\ No newline at end of file',
        '@@ -40,2 +40,2 @@',
        ' x',
        '-y',
        '+z',
      ].join('\n'),
    )
    expect(rows.map((row) => [row.kind, row.oldLine, row.newLine])).toEqual([
      ['hunk', null, null],
      ['removed', 1, null],
      ['added', null, 1],
      ['meta', null, null],
      ['hunk', null, null],
      ['context', 40, 40],
      ['removed', 41, null],
      ['added', null, 41],
    ])
  })

  it('treats an empty line inside a hunk as context, as tools that strip the space write it', () => {
    const { rows } = parsePatch(['@@ -1,3 +1,3 @@', ' a', '', '-b', '+c'].join('\n'))
    expect(rows.map((row) => [row.kind, row.oldLine, row.newLine, row.text])).toEqual([
      ['hunk', null, null, '@@ -1,3 +1,3 @@'],
      ['context', 1, 1, 'a'],
      ['context', 2, 2, ''],
      ['removed', 3, null, 'b'],
      ['added', null, 3, 'c'],
    ])
  })

  it('drops the carriage return a CRLF file carries on every line', () => {
    const { rows } = parsePatch('@@ -1 +1 @@\r\n-old\r\n+new\r\n')
    expect(rows.slice(1).map((row) => row.text)).toEqual(['old', 'new'])
  })

  it('keeps a new file as additions numbered from one, and the mode line as a note', () => {
    const { rows, sides } = parsePatch(
      [
        'diff --git a/n.md b/n.md',
        'new file mode 100644',
        '--- /dev/null',
        '+++ b/n.md',
        '@@ -0,0 +1,2 @@',
        '+# N',
        '+body',
      ].join('\n'),
    )
    expect(rows.map((row) => [row.kind, row.newLine])).toEqual([
      ['meta', null],
      ['hunk', null],
      ['added', 1],
      ['added', 2],
    ])
    expect(sides.old).toEqual([])
  })

  it('falls back to the first character, unnumbered, past a header it cannot read', () => {
    const { rows, numbered } = parsePatch(
      ['@@@ -1,2 -1,2 +1,3 @@@', '+added', '-removed', ' kept'].join('\n'),
    )
    expect(numbered).toBe(false)
    expect(rows.map((row) => row.kind)).toEqual(['hunk', 'added', 'removed', 'context'])
  })

  it('shows text that is not a patch at all as plain rows', () => {
    const { rows, numbered, sides } = parsePatch('  modified  Script/SampleGame/UI/CraftPanelWidget.as')
    expect(numbered).toBe(false)
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      kind: 'meta',
      text: '  modified  Script/SampleGame/UI/CraftPanelWidget.as',
    })
    expect(sides).toEqual({ old: [], new: [] })
  })

  it('has nothing to say about an empty patch', () => {
    expect(parsePatch('').rows).toEqual([])
  })
})
