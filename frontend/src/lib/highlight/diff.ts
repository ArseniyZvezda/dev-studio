/**
 * A unified patch, read into rows the code viewer can draw (spec §10.4).
 *
 * The task's diff pane is the Explorer's viewer with the patch laid over it: syntax colours
 * from the same highlighter, red and green from the patch. That takes two things a
 * first-character colouring never needed — line numbers for each side, and each side's text
 * on its own, because a removed line is code of the *old* file and has to be tokenized as such.
 *
 * **Hunk headers are trusted for counts.** `@@ -12,4 +12,5 @@` says how many lines of each side
 * follow, and that is what tells a removed Lua comment (`--- old`) from a file header — the
 * two look identical by their first characters. A header that does not parse (a dialect the
 * Studio has never seen, a combined `@@@` diff) drops the rest of the patch back to the old
 * reading by first character, with no line numbers: wrong numbers would be worse than none.
 * Text that is not a patch at all — the summary a backend without one answers with — comes out
 * as plain rows, which is what it is.
 */

export type DiffRowKind = 'added' | 'removed' | 'context' | 'hunk' | 'meta'

export interface DiffRow {
  kind: DiffRowKind
  /** The line without its one-character prefix; a hunk or meta line whole. */
  text: string
  oldLine: number | null
  newLine: number | null
  /** Position in {@link DiffSides.old} / `new` — which tokenized line this row draws. */
  oldIndex: number | null
  newIndex: number | null
}

export interface DiffSides {
  /** Context and removed lines, in order — the old file as far as the patch shows it. */
  old: string[]
  /** Context and added lines — the new file as far as the patch shows it. */
  new: string[]
}

export interface ParsedPatch {
  rows: DiffRow[]
  sides: DiffSides
  /** Whether any row carries a line number — a patch with no hunk gets no number gutter. */
  numbered: boolean
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/

/**
 * The per-file header. The path bar above the pane already names the file, so these say
 * nothing a reader needs; `new file mode`, `Binary files … differ` and the like do, and stay.
 */
const HIDDEN_HEADER = ['diff ', 'index ', '--- ', '+++ ']

export function parsePatch(patch: string): ParsedPatch {
  const rows: DiffRow[] = []
  const sides: DiffSides = { old: [], new: [] }

  // `header` until the first hunk, `hunk` while its counts last, `loose` once a hunk header
  // would not parse. Between hunks the state is `header` again: that is where a second
  // file's `diff --git` would sit.
  let state: 'header' | 'hunk' | 'loose' = 'header'
  let oldLine = 0
  let newLine = 0
  let oldLeft = 0
  let newLeft = 0

  const source = patch.split('\n')
  // The newline that ends the last line opens no line of its own.
  if (source.at(-1) === '') source.pop()

  const push = (kind: DiffRowKind, text: string, old: boolean, fresh: boolean): void => {
    rows.push({
      kind,
      text,
      oldLine: old && state === 'hunk' ? oldLine : null,
      newLine: fresh && state === 'hunk' ? newLine : null,
      oldIndex: old ? sides.old.push(text) - 1 : null,
      newIndex: fresh ? sides.new.push(text) - 1 : null,
    })
  }

  for (const raw of source) {
    // A CRLF file's patch keeps the CR on every line, and it is not part of the code.
    const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw

    if (state === 'hunk' && oldLeft <= 0 && newLeft <= 0) state = 'header'

    if (state === 'hunk') {
      const prefix = line[0]
      const body = line.slice(1)
      if (prefix === '-') {
        push('removed', body, true, false)
        oldLine += 1
        oldLeft -= 1
      } else if (prefix === '+') {
        push('added', body, false, true)
        newLine += 1
        newLeft -= 1
      } else if (prefix === '\\') {
        rows.push(meta(line))
      } else {
        // A space, or nothing at all: some tools strip the trailing space of an empty
        // context line, and an empty line inside a hunk can be nothing else.
        push('context', body, true, true)
        oldLine += 1
        newLine += 1
        oldLeft -= 1
        newLeft -= 1
      }
      continue
    }

    if (line.startsWith('@@')) {
      const match = HUNK.exec(line)
      if (match && state !== 'loose') {
        oldLine = Number(match[1])
        oldLeft = match[2] === undefined ? 1 : Number(match[2])
        newLine = Number(match[3])
        newLeft = match[4] === undefined ? 1 : Number(match[4])
        state = 'hunk'
      } else {
        state = 'loose'
      }
      rows.push({ ...meta(line), kind: 'hunk' })
      continue
    }

    if (state === 'loose') {
      if (line.startsWith('+++') || line.startsWith('---')) rows.push(meta(line))
      else if (line.startsWith('+')) push('added', line.slice(1), false, true)
      else if (line.startsWith('-')) push('removed', line.slice(1), true, false)
      else if (line.startsWith(' ')) push('context', line.slice(1), true, true)
      else rows.push(meta(line))
      continue
    }

    // `\ No newline at end of file` arrives after the hunk's counts are spent.
    if (HIDDEN_HEADER.some((prefix) => line.startsWith(prefix))) continue
    rows.push(meta(line))
  }

  return {
    rows,
    sides,
    numbered: rows.some((row) => row.oldLine !== null || row.newLine !== null),
  }
}

function meta(text: string): DiffRow {
  return { kind: 'meta', text, oldLine: null, newLine: null, oldIndex: null, newIndex: null }
}
