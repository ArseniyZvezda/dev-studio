/**
 * The `/` menu's search (block-editor spec §4.3).
 *
 * §4.3 names the queries that have to work — `заг`, `head`, `таб`, `table`, `чек`, `todo`,
 * `галочка` — so they are the tests. The point is not that a fuzzy matcher is correct in the
 * abstract; it is that a designer typing three Cyrillic letters gets the block they meant
 * while the button still says "Heading".
 */

import { describe, expect, it } from 'vitest'

import { groupSlashItems, searchSlashItems, SLASH_ITEMS } from './slash-items'

const top = (query: string) => searchSlashItems(query)[0]?.id
const ids = (query: string) => searchSlashItems(query).map((item) => item.id)

describe('the queries §4.3 names', () => {
  it.each([
    ['заг', 'heading1'],
    ['head', 'heading1'],
    ['таб', 'table'],
    ['table', 'table'],
    ['чек', 'taskList'],
    ['todo', 'taskList'],
    ['галочка', 'taskList'],
  ])('%s finds %s', (query, expected) => {
    expect(top(query)).toBe(expected)
  })
})

describe('searching', () => {
  it('offers everything for an empty query', () => {
    expect(searchSlashItems('')).toHaveLength(SLASH_ITEMS.length)
  })

  it('matches a subsequence, not only a prefix', () => {
    // How people type once they know what they want.
    expect(top('hd1')).toBe('heading1')
    expect(top('blist')).toBe('bulletList')
  })

  it('is case insensitive in both alphabets', () => {
    expect(top('TABLE')).toBe('table')
    expect(top('ЗАГ')).toBe('heading1')
  })

  it('prefers the visible label over an alias of equal quality', () => {
    // "Code" is a label; "код" is an alias. Typing what is on screen must select it.
    expect(top('code')).toBe('codeBlock')
    expect(top('quote')).toBe('blockquote')
  })

  it('keeps the heading levels in order', () => {
    expect(ids('heading').slice(0, 3)).toEqual(['heading1', 'heading2', 'heading3'])
  })

  it('finds a callout by any of its four kinds', () => {
    // One item covers all of them (§3.1 makes the kind a property), but nobody types
    // "callout" when they want a warning.
    for (const kind of ['note', 'tip', 'warning', 'danger', 'предупреждение']) {
      expect(top(kind)).toBe('callout')
    }
  })

  it('returns nothing for a query that is not a block at all', () => {
    // §4.3 closes the menu on this, which needs the search to actually say no.
    expect(searchSlashItems('zzzqqq')).toHaveLength(0)
  })
})

describe('grouping', () => {
  it('uses the three groups from §4.3, in order', () => {
    expect(groupSlashItems(SLASH_ITEMS).map((entry) => entry.group)).toEqual([
      'Basic',
      'Lists',
      'Advanced',
    ])
  })

  it('drops a group with no matches rather than showing an empty heading', () => {
    const groups = groupSlashItems(searchSlashItems('таб'))
    expect(groups.map((entry) => entry.group)).toEqual(['Advanced'])
  })

  it('keeps every item in exactly one group', () => {
    const grouped = groupSlashItems(SLASH_ITEMS).flatMap((entry) => entry.items)
    expect(grouped).toHaveLength(SLASH_ITEMS.length)
  })
})

describe('the offer itself', () => {
  it('leaves headings 4 to 6 out of the menu', () => {
    // §3.1: they open and save correctly, and are noise in a picker.
    expect(SLASH_ITEMS.some((item) => /heading[456]/.test(item.id))).toBe(false)
  })

  it('gives every item a unique id', () => {
    expect(new Set(SLASH_ITEMS.map((item) => item.id)).size).toBe(SLASH_ITEMS.length)
  })
})
