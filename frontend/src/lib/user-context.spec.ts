import { describe, expect, it } from 'vitest'

import {
  MAX_SELECTION,
  basename,
  describeLocation,
  pageLabel,
  selectedText,
} from './user-context'

describe('pageLabel', () => {
  it('names a known route the way the rail does', () => {
    expect(pageLabel({ name: 'docs', fullPath: '/documents', query: {} })).toBe('Documents')
    expect(pageLabel({ name: 'vcs', fullPath: '/vcs', query: {} })).toBe('Version Control')
  })

  it('titles a plugin page from its first path segment', () => {
    expect(
      pageLabel({ name: 'plugin-page', fullPath: '/plugins/graphify/settings?x=1', query: {} }),
    ).toBe('Plugins')
    expect(pageLabel({ name: undefined, fullPath: '/hunyuan-3d/status', query: {} })).toBe(
      'Hunyuan 3d',
    )
    expect(pageLabel({ name: null, fullPath: '/', query: {} })).toBe('Home')
  })
})

describe('describeLocation', () => {
  it('carries the open document and its live heading on Documents', () => {
    const context = describeLocation(
      { name: 'docs', fullPath: '/documents?path=features/dash.md', query: { path: 'features/dash.md' } },
      { docPath: 'features/dash.md', docTitle: '  Dash ability ' },
    )
    expect(context).toEqual({
      page: 'Documents',
      route: '/documents?path=features/dash.md',
      title: 'Dash ability',
      document: 'features/dash.md',
      file: null,
    })
  })

  it('falls back to the address when the store has not opened the document yet', () => {
    const context = describeLocation({
      name: 'docs',
      fullPath: '/documents?path=qa/notes.md',
      query: { path: 'qa/notes.md' },
    })
    expect(context.document).toBe('qa/notes.md')
    expect(context.title).toBe('')
  })

  it('carries the Explorer file and nothing else elsewhere', () => {
    expect(
      describeLocation({ name: 'explorer', fullPath: '/explorer?path=Assets/a.png', query: { path: 'Assets/a.png' } }),
    ).toMatchObject({ page: 'Explorer', file: 'Assets/a.png', document: null })
    expect(describeLocation({ name: 'tasks', fullPath: '/tasks', query: {} })).toEqual({
      page: 'Tasks',
      route: '/tasks',
      title: '',
      document: null,
      file: null,
    })
  })
})

describe('selectedText', () => {
  const selection = (text: string, anchor: unknown = 'page') => ({
    isCollapsed: text.length === 0,
    rangeCount: text.length === 0 ? 0 : 1,
    anchorNode: anchor,
    toString: () => text,
  })

  it('is the trimmed text of a selection on the page', () => {
    expect(selectedText(selection('  the dash lasts 0.3 s \n'))).toBe('the dash lasts 0.3 s')
  })

  it('is empty for no selection, a collapsed one, or one inside the widget', () => {
    expect(selectedText(null)).toBe('')
    expect(selectedText(selection(''))).toBe('')
    expect(selectedText(selection('reply text', 'widget'), (node) => node === 'widget')).toBe('')
    expect(selectedText(selection('page text', 'page'), (node) => node === 'widget')).toBe(
      'page text',
    )
  })

  it('clips a selection the size of a document', () => {
    expect(selectedText(selection('x'.repeat(MAX_SELECTION + 10)))).toHaveLength(MAX_SELECTION)
  })
})

describe('basename', () => {
  it('is the last segment', () => {
    expect(basename('features/dash.md')).toBe('dash.md')
    expect(basename('dash.md')).toBe('dash.md')
    expect(basename('a/b/')).toBe('b')
  })
})
