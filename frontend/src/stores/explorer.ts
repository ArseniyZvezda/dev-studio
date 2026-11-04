/**
 * Explorer state (spec §6.2).
 *
 * The tree is fetched one directory at a time (`depth=1`) as folders are expanded,
 * rather than pulled in one deep request: a managed game project can hold tens of
 * thousands of files under `Assets/`, and the backend caps `depth` at 4 anyway.
 *
 * File-change events name individual paths, so a change only invalidates the directory
 * that contains it — an asset landing in `Assets/Generated` never disturbs the rest of
 * the tree or the user's expansion state.
 *
 * **File operations converge through that same event.** `POST /api/explorer/{create,
 * rename,move,copy,delete}` publish nothing of their own; the watcher sees the change and
 * `explorer.file_changed` arrives, which is also the path an edit made in an external
 * editor or by a pipeline node takes. So nothing here re-reads the tree after a write.
 * What an operation *does* do locally is the part no event can carry:
 *
 * - it drops the cached listing under a path that no longer exists, because
 *   `loadDirectory` is only called for a directory nobody has read yet — a stale entry
 *   would be shown verbatim if a folder of the same name reappeared. The same pruning runs
 *   on the event, against the refreshed listing rather than the event's `is_dir`, which a
 *   deletion cannot be trusted to set (see `pruneAgainst`);
 * - it expands the ancestors of what was produced, so a file created inside a collapsed
 *   folder is visible rather than merely written (only directories never read are
 *   fetched, which is the same lazy load an expand arrow performs);
 * - it forgets the preview of a file that was deleted, since nothing will ever arrive to
 *   replace it.
 */

import { defineStore } from 'pinia'
import { computed, ref, shallowRef } from 'vue'

import { ApiError, api } from '@/api/client'
import type { EntryKind, FileEntry, PreviewResult, TransferRequest } from '@/api/types'
import { t } from '@/i18n'
import { onEvent } from '@/ws/connection'

/** How a click changes the selection: plain, `Ctrl+click`, `Shift+click`. */
export type SelectMode = 'replace' | 'toggle' | 'range'

export interface Clipboard {
  mode: 'cut' | 'copy'
  paths: string[]
}

/**
 * A transfer the backend refused because the destination already holds one of the names.
 *
 * Kept as data rather than a closure so the retry is re-dispatched through the same code
 * path with `overwrite` set — a 409 is a question, not a dead end, and the request type
 * carries the flag that answers it.
 */
export interface Conflict {
  detail: string
  operation: 'move' | 'copy'
  paths: string[]
  destination: string
}

/**
 * What a multi-path operation answers with when it fails partway: the message, and the
 * paths that *did* land before it stopped.
 *
 * `client.ts` stringifies any detail that is not a string, so this arrives as JSON and has
 * to be read back — reporting it raw shows the user a blob instead of a sentence. Every
 * single-path failure still carries a plain string.
 */
interface PartialFailure {
  message: string
  paths: string[]
}

function partialFailure(detail: string): PartialFailure | null {
  if (!detail.startsWith('{')) return null
  try {
    const parsed: unknown = JSON.parse(detail)
    if (parsed === null || typeof parsed !== 'object') return null
    const { message, paths } = parsed as Partial<PartialFailure>
    if (typeof message !== 'string' || !Array.isArray(paths)) return null
    return { message, paths: paths.map(String) }
  } catch {
    // Not the structured form after all — a message that merely begins with a brace.
    return null
  }
}

export function parentOf(path: string): string {
  const index = path.lastIndexOf('/')
  return index === -1 ? '' : path.slice(0, index)
}

export function basename(path: string): string {
  return path.slice(path.lastIndexOf('/') + 1)
}

/** True for the path itself and for anything inside it, so a folder carries its subtree. */
function covers(ancestor: string, path: string): boolean {
  return path === ancestor || path.startsWith(`${ancestor}/`)
}

export const useExplorerStore = defineStore('explorer', () => {
  /** Directory path -> its immediate entries. The empty string is the project root. */
  const children = ref<Record<string, FileEntry[]>>({})
  const expanded = ref<Set<string>>(new Set(['']))
  const loadingPaths = ref<Set<string>>(new Set())
  /** The active row: what the preview shows, and where a `Shift+click` range ends. */
  const selectedPath = ref<string | null>(null)
  /** Every selected row. The operations take path lists, so the menu can act on all of them. */
  const selectedPaths = ref<string[]>([])
  const preview = shallowRef<PreviewResult | null>(null)
  const previewLoading = ref(false)
  const error = ref<string | null>(null)
  /** An operation is in flight; the menu and the dialogs disable their buttons on it. */
  const busy = ref(false)
  const clipboard = ref<Clipboard | null>(null)
  const conflict = ref<Conflict | null>(null)

  /** Where a `Shift+click` range starts — the last row picked without one. */
  const anchorPath = ref<string | null>(null)

  let subscribed = false

  /**
   * Every row the tree is drawing, in the order it draws them.
   *
   * `FileTreeNode` recurses in exactly this order, so a `Shift+click` range means the same
   * thing here as it looks like on screen — and it is derived state, so nothing has to be
   * kept in step with the component.
   */
  const visiblePaths = computed<string[]>(() => {
    const rows: string[] = []
    const walk = (directory: string): void => {
      for (const entry of children.value[directory] ?? []) {
        rows.push(entry.path)
        if (entry.is_dir && expanded.value.has(entry.path)) walk(entry.path)
      }
    }
    walk('')
    return rows
  })

  const cutPaths = computed(() => (clipboard.value?.mode === 'cut' ? clipboard.value.paths : []))

  /** Every row asks, so the lookup is a set rather than a scan of the selection. */
  const selectedSet = computed(() => new Set(selectedPaths.value))

  /** A pending cut dims the row — and everything inside it, when a folder was cut. */
  function isCut(path: string): boolean {
    return cutPaths.value.some((cut) => covers(cut, path))
  }

  function isSelected(path: string): boolean {
    return selectedSet.value.has(path)
  }

  function entryFor(path: string): FileEntry | null {
    return children.value[parentOf(path)]?.find((entry) => entry.path === path) ?? null
  }

  /**
   * The directory an action aimed at this row applies to: the folder itself, or the folder
   * holding the file. "New file beside the clicked file" and "paste into the clicked
   * folder" are the same rule.
   */
  function directoryFor(path: string | null): string {
    if (!path) return ''
    return entryFor(path)?.is_dir ? path : parentOf(path)
  }

  async function loadDirectory(path: string): Promise<void> {
    if (loadingPaths.value.has(path)) return
    loadingPaths.value = new Set(loadingPaths.value).add(path)
    try {
      const tree = await api.tree(path, 1)
      children.value = { ...children.value, [path]: tree.entries }
      error.value = null
    } catch (cause) {
      error.value = messageOf(cause)
    } finally {
      const next = new Set(loadingPaths.value)
      next.delete(path)
      loadingPaths.value = next
    }
  }

  async function toggle(path: string): Promise<void> {
    const next = new Set(expanded.value)
    if (next.has(path)) {
      next.delete(path)
    } else {
      next.add(path)
      if (!children.value[path]) await loadDirectory(path)
    }
    expanded.value = next
  }

  async function select(entry: FileEntry, mode: SelectMode = 'replace'): Promise<void> {
    if (mode === 'range' && anchorPath.value) {
      const rows = visiblePaths.value
      const from = rows.indexOf(anchorPath.value)
      const to = rows.indexOf(entry.path)
      selectedPaths.value =
        from === -1 || to === -1
          ? [entry.path]
          : rows.slice(Math.min(from, to), Math.max(from, to) + 1)
    } else if (mode === 'toggle') {
      const next = selectedPaths.value.filter((path) => path !== entry.path)
      selectedPaths.value =
        next.length === selectedPaths.value.length ? [...next, entry.path] : next
      anchorPath.value = entry.path
    } else {
      selectedPaths.value = [entry.path]
      anchorPath.value = entry.path
    }

    selectedPath.value = entry.path

    // Selecting a folder does not walk the tree. Expanding is the row's own decision, made
    // by `FileTreeNode` for a plain click alone — a `Ctrl+click` that is only building a
    // selection, or a right-click opening the menu, must not collapse the folder underneath
    // what the user is about to act on.
    if (entry.is_dir) {
      preview.value = null
      return
    }

    previewLoading.value = true
    try {
      preview.value = await api.preview(entry.path)
    } catch (cause) {
      preview.value = null
      error.value = messageOf(cause)
    } finally {
      previewLoading.value = false
    }
  }

  /** Select by path — the keyboard's way in, and how a row is picked before its menu opens. */
  async function selectPath(path: string, mode: SelectMode = 'replace'): Promise<void> {
    const entry = entryFor(path)
    if (entry) await select(entry, mode)
  }

  function selectAllVisible(): void {
    selectedPaths.value = [...visiblePaths.value]
  }

  function clearSelection(): void {
    selectedPaths.value = []
    selectedPath.value = null
    anchorPath.value = null
    preview.value = null
  }

  // ------------------------------------------------------------------ operations

  function messageOf(cause: unknown): string {
    if (!(cause instanceof ApiError)) {
      return cause instanceof Error ? cause.message : String(cause)
    }
    const partial = partialFailure(cause.detail)
    if (!partial) return cause.detail
    return partial.paths.length
      ? t('explorer.partialFailure', { message: partial.message, paths: partial.paths.join(', ') })
      : partial.message
  }

  /**
   * Run one operation and turn its failure into something the user can read or answer.
   *
   * Two shapes of failure get special handling. A 409 on a transfer becomes a `conflict`
   * rather than an error, because the request type has an `overwrite` flag and dead-ending
   * on "destination already exists" would make the user go and delete the file by hand. And
   * a multi-path operation that stopped partway names what already landed, which the caller
   * has to reconcile locally — those paths really did move or vanish.
   */
  async function perform(
    action: () => Promise<{ paths: string[] }>,
    options: {
      retry?: Omit<Conflict, 'detail'>
      /** Called with the paths a partly-applied operation did complete, before anything else. */
      settle?: (landed: string[]) => void
    } = {},
  ): Promise<string[] | null> {
    busy.value = true
    error.value = null
    conflict.value = null
    try {
      const result = await action()
      return result.paths
    } catch (cause) {
      const landed = cause instanceof ApiError ? (partialFailure(cause.detail)?.paths ?? []) : []
      if (landed.length) options.settle?.(landed)

      // The retry cannot ask again for what already went through — those paths are no
      // longer where they were, and re-sending them earns a 404 on top of the 409.
      const remaining = options.retry?.paths.filter((path) => !landed.includes(path)) ?? []
      if (options.retry && remaining.length && cause instanceof ApiError && cause.status === 409) {
        conflict.value = { ...options.retry, paths: remaining, detail: messageOf(cause) }
      } else {
        error.value = messageOf(cause)
      }
      return null
    } finally {
      busy.value = false
    }
  }

  async function createEntry(
    parent: string,
    name: string,
    kind: EntryKind,
  ): Promise<string[] | null> {
    const trimmed = name.trim()
    if (!trimmed) return null
    const paths = await perform(() => api.createEntry({ parent, name: trimmed, kind }))
    if (paths) await reveal(paths)
    return paths
  }

  async function renameEntry(path: string, name: string): Promise<string[] | null> {
    const trimmed = name.trim()
    if (!trimmed || trimmed === basename(path)) return null
    const paths = await perform(() => api.renameEntry({ path, name: trimmed }))
    if (paths) {
      forget([path])
      await reveal(paths)
    }
    return paths
  }

  async function transfer(
    operation: 'move' | 'copy',
    paths: string[],
    destination: string,
    overwrite = false,
  ): Promise<string[] | null> {
    if (!paths.length) return null
    const body: TransferRequest = { paths: [...paths], destination, overwrite }
    const result = await perform(
      () => (operation === 'move' ? api.moveEntries(body) : api.copyEntries(body)),
      {
        retry: { operation, paths: [...paths], destination },
        // A move that stopped halfway really did move these, so their cached listings are
        // as stale as a successful one's.
        settle: (landed) => {
          if (operation === 'move') forget(landed)
        },
      },
    )
    if (!result) return null

    if (operation === 'move') {
      forget(paths)
      // The cut that asked for this move is spent. Checked against the paths rather than
      // cleared blindly, so an unrelated move does not throw away a pending one.
      const board = clipboard.value
      if (board?.mode === 'cut' && paths.every((path) => board.paths.includes(path))) {
        clipboard.value = null
      }
    }
    await reveal(result)
    return result
  }

  function cut(paths: string[]): void {
    clipboard.value = paths.length ? { mode: 'cut', paths: [...paths] } : null
  }

  function copy(paths: string[]): void {
    clipboard.value = paths.length ? { mode: 'copy', paths: [...paths] } : null
  }

  function clearClipboard(): void {
    clipboard.value = null
  }

  /** Cut then paste is a move; copy then paste is a copy. The board decides, not the caller. */
  function paste(destination: string): Promise<string[] | null> {
    const board = clipboard.value
    if (!board?.paths.length) return Promise.resolve(null)
    return transfer(board.mode === 'cut' ? 'move' : 'copy', board.paths, destination)
  }

  /**
   * Duplicate, expressed with the client the Studio actually has.
   *
   * There is no duplicate endpoint and no copy-with-a-new-name, so the only form available
   * is a copy whose destination is the source's own directory — naming the result is the
   * backend's business. No overwrite is offered on a refusal: replacing the original with
   * itself is not what anyone means by "duplicate".
   */
  async function duplicate(paths: string[]): Promise<string[] | null> {
    if (!paths.length) return null
    const destination = parentOf(paths[0])
    const result = await perform(() =>
      api.copyEntries({ paths: [...paths], destination, overwrite: false }),
    )
    if (result) await reveal(result)
    return result
  }

  /** Irreversible, and recursive for a folder — the view confirms before calling this. */
  async function remove(paths: string[]): Promise<string[] | null> {
    if (!paths.length) return null
    const result = await perform(() => api.deleteEntries({ paths: [...paths] }), {
      settle: (landed) => {
        forget(landed)
        dropSelection(landed)
      },
    })
    if (result) {
      forget(paths)
      dropSelection(paths)
    }
    return result
  }

  /**
   * Show an entry in the machine's own file manager.
   *
   * Not routed through `perform`: that one is about operations returning a path list and
   * about conflicts, and this changes nothing on disk. It fails for one interesting
   * reason — a Studio running on another machine, answered 409 with an explanation — and
   * the banner is the right place for that, the same as any other refusal.
   */
  async function openInSystemFileManager(path: string): Promise<boolean> {
    if (!path) return false
    busy.value = true
    error.value = null
    try {
      await api.revealEntry(path)
      return true
    } catch (cause) {
      error.value = messageOf(cause)
      return false
    } finally {
      busy.value = false
    }
  }

  function dismissConflict(): void {
    conflict.value = null
  }

  /** "Yes, replace it." The same request, with the flag the refusal was about. */
  function resolveConflict(): Promise<string[] | null> {
    const pending = conflict.value
    if (!pending) return Promise.resolve(null)
    return transfer(pending.operation, [...pending.paths], pending.destination, true)
  }

  // ------------------------------------------------------- local state after a write

  /** Drop cached listings and expansion for paths that are gone, so nothing stale is shown. */
  function forget(paths: string[]): void {
    const gone = (candidate: string) => paths.some((path) => covers(path, candidate))

    const nextChildren: Record<string, FileEntry[]> = {}
    for (const [directory, entries] of Object.entries(children.value)) {
      if (!gone(directory)) nextChildren[directory] = entries
    }
    children.value = nextChildren
    expanded.value = new Set([...expanded.value].filter((path) => !gone(path)))
    if (clipboard.value) {
      const remaining = clipboard.value.paths.filter((path) => !gone(path))
      clipboard.value = remaining.length ? { ...clipboard.value, paths: remaining } : null
    }
  }

  /** A deleted file's preview has nothing to be replaced by, so it goes with the selection. */
  function dropSelection(paths: string[]): void {
    const gone = (candidate: string) => paths.some((path) => covers(path, candidate))
    selectedPaths.value = selectedPaths.value.filter((path) => !gone(path))
    if (selectedPath.value && gone(selectedPath.value)) {
      selectedPath.value = null
      preview.value = null
    }
    if (anchorPath.value && gone(anchorPath.value)) anchorPath.value = null
  }

  /**
   * Put what an operation produced on screen and select it.
   *
   * Only a directory nobody has read is fetched — that is the tree's ordinary lazy load,
   * not a refresh. An already-loaded directory learns about the new entry from
   * `explorer.file_changed`, like every other change to the project.
   */
  async function reveal(paths: string[]): Promise<void> {
    if (!paths.length) return
    const target = paths[paths.length - 1]

    const next = new Set(expanded.value)
    const pending: Promise<void>[] = []
    let directory = ''
    for (const segment of target.split('/').slice(0, -1)) {
      directory = directory ? `${directory}/${segment}` : segment
      next.add(directory)
      if (!children.value[directory]) pending.push(loadDirectory(directory))
    }
    expanded.value = next
    await Promise.all(pending)

    selectedPaths.value = [...paths]
    selectedPath.value = target
    anchorPath.value = target
  }

  /**
   * Anything cached under a directory that its parent no longer lists is gone.
   *
   * This is how a *deleted* directory is cleaned up, and it is deliberately not driven by
   * the event's `is_dir`: the watcher stats a path that has already been removed, so a
   * deleted directory arrives with `is_dir: false` (pinned by a backend test). Refreshing
   * the parent is what makes the row disappear either way; without this, `children` and
   * `expanded` keep the vanished directory forever — and a directory later recreated with
   * the same name renders its old contents, because `loadDirectory` only runs for one
   * nobody has read.
   */
  function pruneAgainst(reread: string[]): void {
    if (!reread.length) return
    const parents = new Set(reread)
    const listed = new Set<string>()
    for (const directory of reread) {
      for (const entry of children.value[directory] ?? []) listed.add(entry.path)
    }

    const cached = new Set([...Object.keys(children.value), ...expanded.value])
    const gone = [...cached].filter(
      (path) => path !== '' && parents.has(parentOf(path)) && !listed.has(path),
    )
    if (!gone.length) return
    forget(gone)
    dropSelection(gone)
  }

  /** Re-read only the directories that actually changed, keeping expansion intact. */
  function subscribe(): void {
    if (subscribed) return
    subscribed = true

    onEvent('explorer.file_changed', async (payload) => {
      const dirty = new Set<string>()
      for (const change of payload.changes) {
        dirty.add(parentOf(change.path))
        // A directory appearing changes its own listing too. It is only ever true for one
        // that exists — see `pruneAgainst` for why a deletion cannot be read off this flag.
        if (change.is_dir) dirty.add(change.path)
      }
      // A truncated batch means we cannot trust the path list, so refresh everything
      // currently on screen instead of guessing.
      const targets = (payload.truncated ? Object.keys(children.value) : [...dirty])
        .filter((path) => path === '' || expanded.value.has(path))
        .filter((path) => children.value[path] !== undefined)

      await Promise.all(targets.map((path) => loadDirectory(path)))
      pruneAgainst(targets)

      if (selectedPath.value && payload.changes.some((c) => c.path === selectedPath.value)) {
        const entry = entryFor(selectedPath.value)
        // Gone rather than changed — something outside the Studio removed it.
        if (entry) await select(entry)
        else dropSelection([selectedPath.value])
      }
    })
  }

  async function init(): Promise<void> {
    subscribe()
    if (!children.value['']) await loadDirectory('')
  }

  return {
    children,
    expanded,
    loadingPaths,
    selectedPath,
    selectedPaths,
    preview,
    previewLoading,
    error,
    busy,
    clipboard,
    conflict,
    visiblePaths,
    cutPaths,
    isCut,
    isSelected,
    entryFor,
    directoryFor,
    init,
    toggle,
    select,
    selectPath,
    reveal,
    selectAllVisible,
    clearSelection,
    loadDirectory,
    createEntry,
    renameEntry,
    transfer,
    cut,
    copy,
    clearClipboard,
    paste,
    duplicate,
    remove,
    openInSystemFileManager,
    dismissConflict,
    resolveConflict,
  }
})
