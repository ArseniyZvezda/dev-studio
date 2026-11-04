/**
 * The Explorer store (spec §6.2) — the half of the file operations that is data.
 *
 * What is pinned here is what a browser check cannot see reliably: which client call an
 * action makes and with what payload, that a cut and a copy resolve to *different*
 * operations from the same gesture, that a refusal is surfaced rather than swallowed and
 * leaves nothing half-applied, and that the tree's lazy loading and the user's expansion
 * survive the refresh a file-change event triggers.
 *
 * The API is a stub, so these are the store's decisions rather than HTTP. Pinia needs no
 * DOM, which is what lets this run in the same node environment as the rest.
 */

import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import type { FileEntry, TreeNode } from '@/api/types'

const api = {
  tree: vi.fn(),
  preview: vi.fn(),
  createEntry: vi.fn(),
  renameEntry: vi.fn(),
  moveEntries: vi.fn(),
  copyEntries: vi.fn(),
  deleteEntries: vi.fn(),
}

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client')
  return {
    ApiError: actual.ApiError,
    api: new Proxy({}, { get: (_, key: string) => api[key as keyof typeof api] }),
  }
})

/** The event handlers are registered on the app's one socket; nothing here dispatches. */
const handlers = new Map<string, (payload: unknown) => unknown>()
vi.mock('@/ws/connection', () => ({
  onEvent: vi.fn((type: string, handler: (payload: unknown) => unknown) => {
    handlers.set(type, handler)
  }),
}))

const { useExplorerStore } = await import('./explorer')

function file(path: string): FileEntry {
  return {
    name: path.slice(path.lastIndexOf('/') + 1),
    path,
    is_dir: false,
    size: 12,
    modified: '2026-08-04T10:00:00',
    kind: 'text',
    has_children: false,
  }
}

function dir(path: string): FileEntry {
  return { ...file(path), is_dir: true, size: 0, kind: 'directory', has_children: true }
}

function listing(path: string, entries: FileEntry[]): TreeNode {
  return { path, name: path.slice(path.lastIndexOf('/') + 1), entries, children: {} }
}

/** `Assets/` holds one texture; the root holds `Assets/` and a readme. */
const TREE: Record<string, FileEntry[]> = {
  '': [dir('Assets'), file('README.md')],
  Assets: [file('Assets/hero.png')],
}

beforeEach(() => {
  setActivePinia(createPinia())
  handlers.clear()
  for (const stub of Object.values(api)) stub.mockReset()
  api.tree.mockImplementation(async (path: string) => listing(path, TREE[path] ?? []))
  api.preview.mockResolvedValue({ kind: 'text', path: 'README.md', text: 'hi' })
})

/** The root loaded, `Assets/` expanded, `Assets/hero.png` selected. */
async function opened() {
  const store = useExplorerStore()
  await store.init()
  await store.toggle('Assets')
  await store.selectPath('Assets/hero.png')
  return store
}

describe('operations reach the client the backend published', () => {
  it('creates inside the folder it was asked about', async () => {
    const store = await opened()
    api.createEntry.mockResolvedValue({ paths: ['Assets/new.txt'] })

    await store.createEntry('Assets', '  new.txt  ', 'file')

    expect(api.createEntry).toHaveBeenCalledWith({
      parent: 'Assets',
      name: 'new.txt',
      kind: 'file',
    })
    // What the operation produced is what is selected, so the next action is about it.
    expect(store.selectedPath).toBe('Assets/new.txt')
    expect(store.selectedPaths).toEqual(['Assets/new.txt'])
  })

  it('renames with a basename, never a path, and refuses a no-op', async () => {
    const store = await opened()
    api.renameEntry.mockResolvedValue({ paths: ['Assets/villain.png'] })

    await store.renameEntry('Assets/hero.png', 'villain.png')
    expect(api.renameEntry).toHaveBeenCalledWith({ path: 'Assets/hero.png', name: 'villain.png' })

    // The same name is not a rename; asking the backend would only earn a 409.
    expect(await store.renameEntry('Assets/villain.png', 'villain.png')).toBeNull()
    expect(api.renameEntry).toHaveBeenCalledTimes(1)
  })

  it('deletes the whole selection in one request', async () => {
    const store = await opened()
    api.deleteEntries.mockResolvedValue({ paths: ['Assets/hero.png', 'README.md'] })

    await store.remove(['Assets/hero.png', 'README.md'])

    expect(api.deleteEntries).toHaveBeenCalledWith({ paths: ['Assets/hero.png', 'README.md'] })
    // Nothing will arrive to replace the preview of a file that no longer exists.
    expect(store.selectedPath).toBeNull()
    expect(store.preview).toBeNull()
  })

  it('duplicates through a copy into the source’s own directory', async () => {
    // There is no duplicate endpoint and no copy-with-a-new-name, so this is the only form
    // the frozen client can express. Naming the result is the backend's business.
    const store = await opened()
    api.copyEntries.mockResolvedValue({ paths: ['Assets/hero copy.png'] })

    await store.duplicate(['Assets/hero.png'])

    expect(api.copyEntries).toHaveBeenCalledWith({
      paths: ['Assets/hero.png'],
      destination: 'Assets',
      overwrite: false,
    })
  })
})

describe('the clipboard decides whether a paste moves or copies', () => {
  it('cut then paste is a move, and the board is spent', async () => {
    const store = await opened()
    api.moveEntries.mockResolvedValue({ paths: ['hero.png'] })

    store.cut(['Assets/hero.png'])
    expect(store.isCut('Assets/hero.png')).toBe(true)
    await store.paste('')

    expect(api.moveEntries).toHaveBeenCalledWith({
      paths: ['Assets/hero.png'],
      destination: '',
      overwrite: false,
    })
    expect(api.copyEntries).not.toHaveBeenCalled()
    expect(store.clipboard).toBeNull()
  })

  it('copy then paste is a copy, and the board survives for the next folder', async () => {
    const store = await opened()
    api.copyEntries.mockResolvedValue({ paths: ['hero.png'] })

    store.copy(['Assets/hero.png'])
    // A copy is not pending in the tree the way a cut is — nothing is going to move.
    expect(store.isCut('Assets/hero.png')).toBe(false)
    await store.paste('')

    expect(api.copyEntries).toHaveBeenCalledWith({
      paths: ['Assets/hero.png'],
      destination: '',
      overwrite: false,
    })
    expect(api.moveEntries).not.toHaveBeenCalled()
    expect(store.clipboard).toEqual({ mode: 'copy', paths: ['Assets/hero.png'] })
  })

  it('dims everything inside a folder that was cut', async () => {
    const store = await opened()
    store.cut(['Assets'])
    expect(store.isCut('Assets')).toBe(true)
    expect(store.isCut('Assets/hero.png')).toBe(true)
    expect(store.isCut('README.md')).toBe(false)
  })

  it('pastes nothing when the board is empty', async () => {
    const store = await opened()
    expect(await store.paste('Assets')).toBeNull()
    expect(api.moveEntries).not.toHaveBeenCalled()
    expect(api.copyEntries).not.toHaveBeenCalled()
  })
})

describe('a refusal is surfaced, not swallowed', () => {
  it('reports the backend’s own detail and changes nothing', async () => {
    const store = await opened()
    api.deleteEntries.mockRejectedValue(new ApiError(403, 'voxarium_studio/ is not yours to delete'))

    expect(await store.remove(['voxarium_studio'])).toBeNull()
    expect(store.error).toBe('voxarium_studio/ is not yours to delete')
    expect(store.busy).toBe(false)
    // Nothing was applied optimistically, so the tree still holds what the backend has.
    expect(store.children['Assets']).toHaveLength(1)
    expect(store.selectedPath).toBe('Assets/hero.png')
  })

  it('turns a collision into an offer to overwrite rather than a dead end', async () => {
    const store = await opened()
    api.moveEntries.mockRejectedValueOnce(new ApiError(409, 'hero.png already exists there'))

    store.cut(['Assets/hero.png'])
    await store.paste('')

    // A 409 is a question the request type can answer, so it is not reported as an error.
    expect(store.error).toBeNull()
    expect(store.conflict).toEqual({
      detail: 'hero.png already exists there',
      operation: 'move',
      paths: ['Assets/hero.png'],
      destination: '',
    })
    // The cut is still pending: the move never happened.
    expect(store.clipboard).toEqual({ mode: 'cut', paths: ['Assets/hero.png'] })

    api.moveEntries.mockResolvedValue({ paths: ['hero.png'] })
    await store.resolveConflict()

    expect(api.moveEntries).toHaveBeenLastCalledWith({
      paths: ['Assets/hero.png'],
      destination: '',
      overwrite: true,
    })
    expect(store.conflict).toBeNull()
    expect(store.clipboard).toBeNull()
  })

  it('leaves a failed create with the dialog’s state intact', async () => {
    const store = await opened()
    api.createEntry.mockRejectedValue(new ApiError(400, 'A name cannot contain a separator'))

    expect(await store.createEntry('Assets', 'a/b', 'file')).toBeNull()
    expect(store.error).toBe('A name cannot contain a separator')
    // The selection did not move to something that was never created.
    expect(store.selectedPath).toBe('Assets/hero.png')
  })

  it('reads a partly-applied multi-path failure back into a sentence', async () => {
    // `client.ts` stringifies any detail that is not a string, so the structured form a
    // multi-path failure uses arrives as JSON. Showing that blob is not reporting an error.
    const store = await opened()
    api.deleteEntries.mockRejectedValue(
      new ApiError(
        403,
        JSON.stringify({
          message: 'voxarium_studio/ is not yours to delete.',
          paths: ['Assets/hero.png'],
        }),
      ),
    )

    expect(await store.remove(['Assets/hero.png', 'voxarium_studio'])).toBeNull()
    expect(store.error).toBe('voxarium_studio/ is not yours to delete. These did land: Assets/hero.png.')
    // The one that really was deleted takes its preview with it, failure or not.
    expect(store.selectedPath).toBeNull()
    expect(store.preview).toBeNull()
  })

  it('does not ask again for the paths a partial collision already moved', async () => {
    const store = await opened()
    api.moveEntries.mockRejectedValueOnce(
      new ApiError(
        409,
        JSON.stringify({ message: 'README.md is already there.', paths: ['Assets/hero.png'] }),
      ),
    )

    await store.transfer('move', ['Assets/hero.png', 'README.md'], 'docs')

    // Re-sending what already went through would earn a 404 on top of the 409.
    expect(store.conflict?.paths).toEqual(['README.md'])
    expect(store.conflict?.detail).toBe(
      'README.md is already there. These did land: Assets/hero.png.',
    )

    api.moveEntries.mockResolvedValue({ paths: ['docs/README.md'] })
    await store.resolveConflict()

    expect(api.moveEntries).toHaveBeenLastCalledWith({
      paths: ['README.md'],
      destination: 'docs',
      overwrite: true,
    })
  })

  it('does not offer to overwrite a duplicate onto its own original', async () => {
    const store = await opened()
    api.copyEntries.mockRejectedValue(new ApiError(409, 'hero.png already exists there'))

    await store.duplicate(['Assets/hero.png'])

    expect(store.conflict).toBeNull()
    expect(store.error).toBe('hero.png already exists there')
  })
})

describe('local state after a write, which no event carries', () => {
  it('drops the cached listing under a path that is gone', async () => {
    const store = await opened()
    api.deleteEntries.mockResolvedValue({ paths: ['Assets'] })

    expect(store.children['Assets']).toBeDefined()
    await store.remove(['Assets'])

    // `loadDirectory` only runs for a directory nobody has read, so a stale listing would
    // be shown verbatim if a folder of the same name reappeared.
    expect(store.children['Assets']).toBeUndefined()
    expect(store.expanded.has('Assets')).toBe(false)
    expect(store.children['']).toBeDefined()
  })

  it('expands the ancestors of what an operation produced', async () => {
    const store = useExplorerStore()
    await store.init()
    api.createEntry.mockResolvedValue({ paths: ['Assets/new.txt'] })

    // `Assets/` was never opened, so the new file would otherwise be written out of sight.
    await store.createEntry('Assets', 'new.txt', 'file')

    expect(store.expanded.has('Assets')).toBe(true)
    expect(api.tree).toHaveBeenCalledWith('Assets', 1)
  })

  it('never re-reads the tree after an operation', async () => {
    const store = await opened()
    api.tree.mockClear()
    api.renameEntry.mockResolvedValue({ paths: ['Assets/villain.png'] })

    await store.renameEntry('Assets/hero.png', 'villain.png')

    // The watcher publishes `explorer.file_changed`, and that is what re-reads `Assets/`.
    // Both directories are already loaded, so nothing is fetched here.
    expect(api.tree).not.toHaveBeenCalled()
  })

  it('takes a moved path off the clipboard', async () => {
    const store = await opened()
    api.moveEntries.mockResolvedValue({ paths: ['hero.png'] })

    store.cut(['Assets/hero.png'])
    await store.transfer('move', ['Assets/hero.png'], '')

    expect(store.clipboard).toBeNull()
  })
})

describe('selection and expansion survive a refresh', () => {
  it('keeps both when a file-change event re-reads a directory', async () => {
    const store = await opened()
    expect(store.expanded.has('Assets')).toBe(true)
    expect(store.selectedPath).toBe('Assets/hero.png')

    const changed = handlers.get('explorer.file_changed')
    expect(changed).toBeDefined()
    await changed?.({
      changes: [{ change: 'modified', path: 'Assets/hero.png', is_dir: false }],
      truncated: false,
    })

    expect(store.expanded.has('Assets')).toBe(true)
    expect(store.selectedPath).toBe('Assets/hero.png')
    // Only the directory that changed was re-read — the root was not disturbed.
    expect(api.tree).toHaveBeenLastCalledWith('Assets', 1)
    // Still the previewed file, refetched because it is the one on screen.
    expect(api.preview).toHaveBeenCalledTimes(2)
  })

  it('prunes a deleted directory the watcher could only report as a file', async () => {
    // The watcher stats a path that is already gone, so `is_dir` comes back false for a
    // deleted directory — pinned by a backend test. What was cached under it has to be
    // found by comparing the refreshed listing instead.
    const store = await opened()
    api.tree.mockImplementation(async (path: string) =>
      listing(path, path === '' ? [file('README.md')] : (TREE[path] ?? [])),
    )

    await handlers.get('explorer.file_changed')?.({
      changes: [{ change: 'deleted', path: 'Assets', is_dir: false }],
      truncated: false,
    })

    expect(store.children['Assets']).toBeUndefined()
    expect(store.expanded.has('Assets')).toBe(false)
    // The file that was selected inside it goes with it.
    expect(store.selectedPath).toBeNull()
    expect(store.preview).toBeNull()
  })

  it('keeps a directory that is merely quiet', async () => {
    const store = await opened()

    await handlers.get('explorer.file_changed')?.({
      changes: [{ change: 'added', path: 'README.md', is_dir: false }],
      truncated: false,
    })

    expect(store.children['Assets']).toBeDefined()
    expect(store.expanded.has('Assets')).toBe(true)
  })

  it('lets go of a selected file that vanished from outside the Studio', async () => {
    const store = await opened()
    api.tree.mockImplementation(async (path: string) =>
      listing(path, path === 'Assets' ? [] : (TREE[path] ?? [])),
    )

    await handlers.get('explorer.file_changed')?.({
      changes: [{ change: 'deleted', path: 'Assets/hero.png', is_dir: false }],
      truncated: false,
    })

    expect(store.selectedPath).toBeNull()
    expect(store.preview).toBeNull()
    expect(store.expanded.has('Assets')).toBe(true)
  })
})

describe('the rows a range selection runs over', () => {
  it('lists them in the order the tree draws them', async () => {
    const store = await opened()
    expect(store.visiblePaths).toEqual(['Assets', 'Assets/hero.png', 'README.md'])

    await store.toggle('Assets')
    expect(store.visiblePaths).toEqual(['Assets', 'README.md'])
  })

  it('selects a range between the anchor and the row that was shift-clicked', async () => {
    const store = await opened()
    await store.selectPath('Assets')
    await store.selectPath('README.md', 'range')

    expect(store.selectedPaths).toEqual(['Assets', 'Assets/hero.png', 'README.md'])
    expect(store.selectedPath).toBe('README.md')
  })

  it('adds and removes one row at a time with a toggle', async () => {
    const store = await opened()
    await store.selectPath('README.md')
    await store.selectPath('Assets', 'toggle')
    expect(store.selectedPaths).toEqual(['README.md', 'Assets'])

    await store.selectPath('Assets', 'toggle')
    expect(store.selectedPaths).toEqual(['README.md'])
  })

  it('never expands or collapses a folder just by selecting it', async () => {
    const store = await opened()
    await store.selectPath('Assets')
    // The row decides that, for a plain click alone — a right-click that opens the menu
    // would otherwise collapse the folder the user is about to paste into.
    expect(store.expanded.has('Assets')).toBe(true)
  })
})

describe('where an action aimed at a row lands', () => {
  it('is the folder itself, or the folder holding the file', async () => {
    const store = await opened()
    expect(store.directoryFor('Assets')).toBe('Assets')
    expect(store.directoryFor('Assets/hero.png')).toBe('Assets')
    expect(store.directoryFor('README.md')).toBe('')
    expect(store.directoryFor(null)).toBe('')
  })
})
