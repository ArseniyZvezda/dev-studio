/**
 * Version control state (spec §9.6).
 *
 * Two consumers ask very different questions of the same payload. The Version Control
 * page wants the whole picture — which system is driving, where it sits, what a commit
 * would do; the Explorer's file tree asks "what happened to this one path?" once per
 * visible row, on every render. That second caller is why the change list is folded into
 * a `Map` the moment it lands — scanning the list per row is O(rows × changes), and in a
 * game project mid-generation both numbers are large.
 *
 * The tree is also the **only** place per-file status is shown (§6.2). The page used to
 * repeat it as a staged/unstaged list, which meant the same answer was drawn twice in two
 * vocabularies and the one place a file is actually looked at — the tree — was the copy
 * people did not know about.
 *
 * The same pressure from the other side is why refreshes are debounced: a pipeline run
 * writing assets emits `explorer.file_changed` in bursts, and every refresh here is a
 * status subprocess on the backend (`git status`, `cm status`, …).
 *
 * Nothing in this module parses one tool's status letters — `kind` is normalised by the
 * backend, because the whole point of `VcsBackend` is that the client never learns which
 * system it is talking to.
 */

import { defineStore } from 'pinia'
import { computed, ref, shallowRef } from 'vue'

import { api } from '@/api/client'
import type {
  VcsChangeKind,
  VcsInfo,
  VcsOverview,
  VcsSettingsUpdate,
  VcsStatus,
} from '@/api/types'
import { t, type MessageKey } from '@/i18n'
import { onEvent } from '@/ws/connection'

/**
 * Trailing, and deliberately not short: a burst of writes is one logical change to the
 * working copy, and the answer only becomes interesting once the writing stops.
 */
export const REFRESH_DEBOUNCE_MS = 400

/** How one changed file is drawn: a colour, a letter, and a word for the tooltip. */
export interface VcsMark {
  /** Utility classes, never a literal colour (§7). */
  class: string
  /**
   * The same thing said without colour (§6.2). A row is legible to a colour-blind reader
   * and in either theme only because this is beside the name.
   */
  letter: string
  /** In the interface language, looked up each time it is read (`vcs.change.*`). */
  label: string
}

/**
 * A mark whose word is a getter, so a tooltip drawn from this module-level table follows
 * a language switch. The letter stays as it is in every language: it is the tool's own
 * shorthand, the same one `git status` and every VCS client print.
 */
function mark(className: string, letter: string, labelKey: MessageKey): VcsMark {
  return {
    class: className,
    letter,
    get label() {
      return t(labelKey)
    },
  }
}

/**
 * The one vocabulary for a change, and now the only one: every surface that shows a
 * file's state reads it from here, so a file cannot be amber in one place and green in
 * another.
 *
 * The colours are §6.2's: green for a path that was not there before, amber for one whose
 * contents moved, red for a loss, and red **bold** for a conflict, which is the only state
 * that needs acting on before anything else. `copied` reads as `added`; `unknown` is the
 * backend saying it does not recognise its own tool's letter, so it gets the muted
 * treatment rather than a guessed colour.
 */
export const VCS_MARKS: Record<VcsChangeKind, VcsMark> = {
  added: mark('text-success', 'A', 'vcs.change.added'),
  untracked: mark('text-success', 'U', 'vcs.change.untracked'),
  copied: mark('text-success', 'C', 'vcs.change.copied'),
  modified: mark('text-warning', 'M', 'vcs.change.modified'),
  renamed: mark('text-warning', 'R', 'vcs.change.renamed'),
  deleted: mark('text-danger', 'D', 'vcs.change.deleted'),
  conflicted: mark('text-danger font-semibold', '!', 'vcs.change.conflicted'),
  unknown: mark('text-muted', '?', 'vcs.change.unknown'),
}

/** The rail's one-line summary of version control: a colour, a name, the story for the tooltip. */
export interface VcsIndicator {
  /** A CSS colour — a `var(--vxs-…)` token, never a literal (§7). */
  color: string
  /** What follows `VCS:` in the rail — the system's name, or `none`. */
  label: string
  title: string
}

/**
 * What the rail says about version control, from the same answer the project header and the
 * Version Control page read (`VcsInfo`, §9.6).
 *
 * The colour is about the *system*, not the working copy: green when a backend is driving
 * and its tool answers; red when one is chosen and the tool is missing or refuses — the state
 * that fails every commit node and every isolated task until someone acts; grey for a project
 * under nothing the Studio recognises, which is a normal state rather than a fault. Whether
 * files changed is the trees' job, one row at a time.
 */
export function vcsIndicator(
  info: VcsInfo | null | undefined,
  position: { branch?: string | null; revision?: string | null } = {},
): VcsIndicator {
  if (!info) {
    return {
      color: 'var(--vxs-text-faint)',
      label: t('vcs.indicator.none'),
      title: t('vcs.indicator.noneTitle'),
    }
  }
  const where = [position.branch, position.revision].filter(Boolean).join(' · ')
  const lines = [info.name]
  if (!info.available) {
    lines.push(
      info.reason
        ? t('vcs.indicator.unusable', { reason: info.reason })
        : t('vcs.indicator.toolNotFound'),
    )
  }
  if (where) lines.push(where)
  lines.push(t('vcs.indicator.workspace', { root: info.root }))
  return {
    color: info.available ? 'var(--vxs-success)' : 'var(--vxs-danger)',
    label: info.name,
    title: lines.join('\n'),
  }
}

function messageOf(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause)
}

export const useVcsStore = defineStore('vcs', () => {
  const overview = ref<VcsOverview | null>(null)
  const status = ref<VcsStatus | null>(null)
  /** True for the first load and for every re-read; the page distinguishes on `overview`. */
  const loading = ref(false)
  const saving = ref(false)
  const error = ref<string | null>(null)

  /**
   * Prepared lookups, rebuilt whenever a status lands. `shallowRef` on purpose: these are
   * replaced wholesale, never mutated, and making a `Map` of ten thousand paths deeply
   * reactive would cost more than the scan they exist to avoid.
   */
  const kinds = shallowRef(new Map<string, VcsChangeKind>())
  const dirtyDirs = shallowRef(new Map<string, number>())

  let subscribed = false
  let timer: ReturnType<typeof setTimeout> | null = null
  let inFlight: Promise<void> | null = null

  /** How many files changed in the whole working copy — what a commit would record. */
  const changeCount = computed<number>(() => status.value?.changes.length ?? 0)
  /** `null` when the project is under nothing the Studio recognises — a normal answer. */
  const backendId = computed<string | null>(() => status.value?.backend ?? null)

  function applyStatus(next: VcsStatus): void {
    status.value = next

    const kindByPath = new Map<string, VcsChangeKind>()
    // Counts rather than a set of names: a collapsed folder is the only thing standing
    // between a change and the user, so its marker says how much is hidden under it.
    const dirs = new Map<string, number>()
    const bump = (dir: string): void => void dirs.set(dir, (dirs.get(dir) ?? 0) + 1)

    for (const change of next.changes) {
      // One path can be reported twice — staged, and changed again since. The first
      // entry stands unless the second is a conflict, which outranks everything. The
      // roll-up counts the *path* once for the same reason.
      const seen = kindByPath.has(change.path)
      if (change.kind === 'conflicted' || !seen) kindByPath.set(change.path, change.kind)
      if (seen) continue

      // A rename's `origin` is deliberately not indexed: that path is gone from disk, so
      // it is not a row the tree can colour.
      bump('')
      for (
        let cut = change.path.lastIndexOf('/');
        cut > 0;
        cut = change.path.lastIndexOf('/', cut - 1)
      ) {
        bump(change.path.slice(0, cut))
      }
    }
    kinds.value = kindByPath
    dirtyDirs.value = dirs
  }

  /**
   * What happened to exactly this file, or `null`.
   *
   * Exact: a directory containing a change is not itself changed, and neither is a
   * sibling. Paths are the backend's — relative to the workspace root, forward slashes —
   * which is the project root in every layout the Explorer can draw.
   */
  function kindFor(path: string): VcsChangeKind | null {
    return kinds.value.get(path) ?? null
  }

  /** Whether anything under this directory changed. `''` is the project root. */
  function dirtyUnder(path: string): boolean {
    return dirtyDirs.value.has(path)
  }

  /**
   * How many files changed under this directory, at any depth. What the tree's folder
   * marker says in words — a dot alone cannot distinguish one stray file from a whole
   * regenerated asset folder, and the files themselves may be collapsed out of view.
   */
  function changesUnder(path: string): number {
    return dirtyDirs.value.get(path) ?? 0
  }

  async function read(): Promise<void> {
    loading.value = true
    try {
      applyStatus(await api.vcsStatus())
      error.value = null
    } catch (cause) {
      // The last good status stays on screen: a tree half-coloured is better than a tree
      // that loses its colours because one poll failed.
      error.value = messageOf(cause)
    } finally {
      loading.value = false
    }
  }

  /**
   * Re-read the working copy.
   *
   * A caller arriving while a read is running joins it rather than starting a second —
   * each one is a subprocess, and two of them answer the same question. Never rejects:
   * a failure lands in `error` beside whatever was last known.
   */
  function refresh(): Promise<void> {
    if (inFlight) return inFlight
    const run = read().finally(() => {
      inFlight = null
    })
    inFlight = run
    return run
  }

  /** Everything the page needs: which systems exist, and what the working copy holds. */
  async function load(): Promise<void> {
    loading.value = true
    try {
      overview.value = await api.vcs()
      error.value = null
    } catch (cause) {
      error.value = messageOf(cause)
    } finally {
      loading.value = false
    }
    await refresh()
  }

  /**
   * `PUT /api/vcs/settings` merges rather than replaces and answers with the same shape
   * as `GET /api/vcs`, so the response is adopted instead of provoking a second read.
   *
   * It rethrows on purpose: only the page knows which field the user was editing, and a
   * form that silently reverts to the stored value has thrown away the typing that
   * failed to save.
   */
  async function updateSettings(patch: VcsSettingsUpdate): Promise<VcsOverview> {
    saving.value = true
    try {
      const next = await api.updateVcs(patch)
      overview.value = next
      return next
    } finally {
      saving.value = false
    }
  }

  /** Trailing debounce — one read once the writing stops, however long the burst was. */
  function schedule(): void {
    if (timer !== null) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      // A read already in flight was started before these events and may answer with a
      // picture that predates them, so wait for it rather than joining it.
      void (inFlight ? inFlight.then(refresh) : refresh())
    }, REFRESH_DEBOUNCE_MS)
  }

  /**
   * Idempotent, like the Explorer's: the page and the tree both ask for it and there is
   * only one socket.
   */
  function subscribe(): void {
    if (subscribed) return
    subscribed = true
    onEvent('explorer.file_changed', schedule)
  }

  return {
    overview,
    status,
    changeCount,
    backendId,
    loading,
    saving,
    error,
    kindFor,
    dirtyUnder,
    changesUnder,
    load,
    refresh,
    updateSettings,
    subscribe,
  }
})
