/**
 * The rules about skills that are data rather than layout (§10.10) — which names a member can
 * still be given, what a member's list says about each name, and which file changes are news
 * to the skills folder. Pure, so `skills.spec.ts` can hold them without a DOM.
 */

import type { SkillEntry } from '@/api/types'
import { t } from '@/i18n'

/** One entry of a member's list, resolved against the folder. */
export interface SkillChip {
  name: string
  /** What the folder has under that name, or null when it has nothing. */
  skill: SkillEntry | null
  /**
   * Given, and not in the folder — renamed or deleted since. Only ever true once the folder has
   * been read: before that nothing is known to be missing, and a card must not claim it is.
   */
  missing: boolean
}

/**
 * A member's skills as the editor and the card draw them, in the member's own order.
 *
 * `skills` is null until the folder has been read, and then every chip is simply a name —
 * marking all of them missing for the length of a request would flash a warning at somebody
 * whose roster is fine.
 */
export function skillChips(
  names: readonly string[],
  skills: readonly SkillEntry[] | null,
): SkillChip[] {
  const byName = new Map((skills ?? []).map((skill) => [skill.name, skill]))
  return names.map((name) => ({
    name,
    skill: byName.get(name) ?? null,
    missing: skills !== null && !byName.has(name),
  }))
}

/** What the member editor's picker offers: every skill the member does not have yet, by name. */
export function skillOptions(
  skills: readonly SkillEntry[],
  given: readonly string[],
  backend?: string,
): SkillEntry[] {
  const taken = new Set(given)
  return skills.filter((skill) => !taken.has(skill.name) &&
    (!backend || !skill.backends?.length || skill.backends.includes(backend)))
}

export function skillDisplayName(skill: Pick<SkillEntry, 'name' | 'display_name'>): string {
  return skill.display_name || skill.name
}

/** Keep identically named skills distinguishable without showing their opaque assignment ids. */
export function skillOptionLabel(skill: SkillEntry): string {
  const name = skillDisplayName(skill)
  if (!skill.source) return name
  const scope = skill.scope ? t(`skills.scope.${skill.scope}`) : ''
  return `${name} · ${[skill.source, scope].filter(Boolean).join(' · ')}`
}

export function filterSkills(skills: readonly SkillEntry[], query: string): SkillEntry[] {
  const terms = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean)
  return skills.filter((skill) => {
    const text = [skillDisplayName(skill), skill.description, skill.source, skill.path,
      ...(skill.backends ?? [])].join(' ').toLocaleLowerCase()
    return terms.every((term) => text.includes(term))
  })
}

/**
 * Whether a change the file watcher reported (`explorer.file_changed`) happened inside the
 * skills folder. Both paths are the project's frame with forward slashes — the watcher's and
 * `SkillsOverview.path` — so a prefix is the whole test; the separator keeps `voxarium_studio/skills-old`
 * from counting as `voxarium_studio/skills`.
 */
export function inSkillsFolder(path: string, folder: string): boolean {
  const base = folder.replace(/\/+$/, '')
  return path === base || path.startsWith(`${base}/`)
}

/**
 * `SKILL.md + 3 files`, or just `SKILL.md` — what a skill carries, in the page's words and the
 * language on screen. The count chooses the plural, but the figure is printed as it was
 * (`2000+`, ungrouped): a ceiling is not a quantity to format, and the `+` belongs to it.
 */
export function describeFiles(skill: Pick<SkillEntry, 'file_count' | 'file_count_capped'>): string {
  if (!skill.file_count) return 'SKILL.md'
  const amount = `${skill.file_count}${skill.file_count_capped ? '+' : ''}`
  return t('skills.files', { count: skill.file_count, amount })
}
