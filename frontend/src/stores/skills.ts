/**
 * The project's skills (§10.10): `.voxarium_studio/skills/` and who has been given what.
 *
 * One store for three surfaces — the Skills page, the member editor's picker and the org
 * chart's cards — because all three have to agree about which names are skills. A card marking
 * a skill missing while the editor offers it would be two answers to one question.
 *
 * Kept live off `explorer.file_changed` rather than an event of its own: a skill arrives by a
 * copy in the file manager or a checkout as often as by the page, the watcher already reports
 * both, and the page's own writes come back through it too. A burst of writes — a folder of
 * scripts pasted in — is one change, so the re-read waits for it to stop.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { SkillEntry, SkillsOverview } from '@/api/types'
import { inSkillsFolder } from '@/lib/skills'
import { onEvent } from '@/ws/connection'

/** Trailing: a folder copied in is dozens of events, and one re-read after the last is enough. */
export const SKILLS_REFRESH_DEBOUNCE_MS = 300

export const useSkillsStore = defineStore('skills', () => {
  const overview = ref<SkillsOverview | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  /** Null until the folder has been read — see `skillChips` for why that is not an empty list. */
  const skills = computed<SkillEntry[] | null>(() => overview.value?.skills ?? null)
  const byName = computed(
    () => new Map((overview.value?.skills ?? []).map((skill) => [skill.name, skill])),
  )

  /** Bumped by every answer adopted, so a read that started before a write cannot land after it. */
  let generation = 0
  let pending: { promise: Promise<void>; started: number } | null = null

  /**
   * Read the folder. Callers share a request in flight — unless an answer was adopted since it
   * started, because that request's answer is going to be dropped and the caller asked for now.
   */
  function load(): Promise<void> {
    if (pending && pending.started === generation) return pending.promise
    const request = { started: generation, promise: Promise.resolve() }
    pending = request
    loading.value = true
    request.promise = (async () => {
      try {
        const result = await api.skills()
        if (request.started === generation) adopt(result)
      } catch (cause) {
        error.value = cause instanceof Error ? cause.message : String(cause)
      } finally {
        // A newer read may have taken over; only the latest one says when loading is over.
        if (pending === request) {
          pending = null
          loading.value = false
        }
      }
    })()
    return request.promise
  }

  /** Take an answer as the page's state — every write answers with the whole page. */
  function adopt(result: SkillsOverview): void {
    generation += 1
    overview.value = result
    error.value = null
  }

  /**
   * Read again after a write that did not answer with the page (a `SKILL.md` save). A read
   * already in flight started before the write, so it is let go rather than joined.
   */
  function refresh(): Promise<void> {
    generation += 1
    return load()
  }

  let subscribed = false
  let timer: ReturnType<typeof setTimeout> | null = null

  function subscribe(): void {
    if (subscribed) return
    subscribed = true
    onEvent('plugin.changed', () => {
      if (overview.value) void refresh()
    })
    onEvent('explorer.file_changed', (payload) => {
      // Nobody has asked for the folder yet, so there is nothing on screen to correct.
      const folder = overview.value?.path
      if (!folder) return
      const touched = payload.changes.some((change) => inSkillsFolder(change.path, folder))
      if (!touched && !payload.truncated) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        timer = null
        void load()
      }, SKILLS_REFRESH_DEBOUNCE_MS)
    })
  }

  return { overview, skills, byName, loading, error, load, adopt, refresh, subscribe }
})
