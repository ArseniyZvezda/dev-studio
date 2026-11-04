/**
 * The `[[` autocomplete's choices (block-editor spec §5).
 *
 * What is under test is the decision that reaches the *file*: which text goes between the
 * brackets. §5 asks for the bare name normally and the path when a name is taken twice, and
 * getting that wrong writes a link that resolves to the wrong document — or to none.
 */

import { describe, expect, it } from 'vitest'

import { searchDocuments } from './wiki-menu'

const DOCUMENTS = [
  { path: 'features/dash.md', title: 'Dash ability' },
  { path: 'qa/test-plan.md', title: 'Test plan' },
  { path: 'notes/ws-probe.md', title: 'WS probe' },
  { path: 'review/probe.md', title: 'Review probe' },
  // The same stem in two folders — §5's ambiguous case.
  { path: 'features/notes.md', title: 'Feature notes' },
  { path: 'qa/notes.md', title: 'QA notes' },
]

const targets = (query: string) => searchDocuments(DOCUMENTS, query).map((item) => item.target)
const first = (query: string) => searchDocuments(DOCUMENTS, query)[0]

describe('what gets written between the brackets', () => {
  it('is the bare name when it identifies one document', () => {
    // A name survives the file moving to another folder; a path does not.
    expect(first('dash').target).toBe('dash')
    expect(first('test-plan').target).toBe('test-plan')
  })

  it('is the path when two documents share a name', () => {
    // `[[notes]]` would resolve to neither, so it is never offered.
    expect(targets('notes')).toEqual(expect.arrayContaining(['features/notes', 'qa/notes']))
    expect(targets('notes')).not.toContain('notes')
  })

  it('never carries the file extension', () => {
    for (const item of searchDocuments(DOCUMENTS, '')) {
      expect(item.target).not.toMatch(/\.(md|markdown|txt)$/)
    }
  })

  it('decides ambiguity over every document, not over the matches shown', () => {
    // Searching "qa/notes" finds one row, and it still has to be written as a path — the
    // other file exists whether or not this query found it.
    expect(first('qa/notes').target).toBe('qa/notes')
  })
})

describe('searching', () => {
  it('finds by name, by title and by path (§5)', () => {
    expect(first('dash').path).toBe('features/dash.md')
    expect(first('Dash ability').path).toBe('features/dash.md')
    expect(first('features/').path.startsWith('features/')).toBe(true)
  })

  it('is case insensitive', () => {
    expect(first('DASH').path).toBe('features/dash.md')
  })

  it('puts the document actually called that first', () => {
    // `probe` finds both, and the one *named* probe outranks the one that merely contains it.
    expect(searchDocuments(DOCUMENTS, 'probe').map((item) => item.path)).toEqual([
      'review/probe.md',
      'notes/ws-probe.md',
    ])
  })

  it('puts a name hit above a folder-only hit', () => {
    // `notes` is a folder as well as two filenames; the files come first.
    const [one, other] = searchDocuments(DOCUMENTS, 'notes')
    expect([one.path, other.path].sort()).toEqual(['features/notes.md', 'qa/notes.md'])
  })

  it('offers everything for an empty query', () => {
    expect(searchDocuments(DOCUMENTS, '')).toHaveLength(DOCUMENTS.length)
  })

  it('returns nothing when the name is new, which is what offers to create it', () => {
    expect(searchDocuments(DOCUMENTS, 'combat-tuning')).toHaveLength(0)
  })
})

describe('what the row shows', () => {
  it('shows the path only when it is what tells two documents apart', () => {
    expect(first('dash').hint).toBeNull()
    const ambiguous = searchDocuments(DOCUMENTS, 'notes').filter((item) =>
      item.path.endsWith('notes.md'),
    )
    expect(ambiguous).toHaveLength(2)
    expect(ambiguous.every((item) => item.hint === item.path)).toBe(true)
  })

  it('shows the path when the query was aimed at a folder', () => {
    // Which document comes first is not the point; every row has to say where it lives,
    // because the query said nothing about the names.
    const results = searchDocuments(DOCUMENTS, 'qa/')
    expect(results).toHaveLength(2)
    expect(results.every((item) => item.hint === item.path)).toBe(true)
  })

  it('shows the document’s title, not its filename', () => {
    expect(first('dash').title).toBe('Dash ability')
  })
})
