/**
 * A member's skills against the folder (§10.10): what the editor offers, what a card marks
 * missing — and not before the folder has been read — and which watcher paths are the folder's.
 */

import { describe, expect, it } from 'vitest'

import type { SkillEntry } from '@/api/types'

import { describeFiles, inSkillsFolder, skillChips, skillOptions } from './skills'

function skill(name: string, extra: Partial<SkillEntry> = {}): SkillEntry {
  return {
    name,
    path: `voxarium_studio/skills/${name}`,
    description: `${name} description`,
    warning: null,
    files: [],
    file_count: 0,
    file_count_capped: false,
    updated_at: '2026-09-16T10:00:00+00:00',
    used_by: [],
    ...extra,
  }
}

describe('skillChips', () => {
  it('keeps the member’s own order and marks a name the folder does not have', () => {
    const chips = skillChips(['writer', 'gone', 'tester'], [skill('tester'), skill('writer')])
    expect(chips.map((chip) => [chip.name, chip.missing])).toEqual([
      ['writer', false],
      ['gone', true],
      ['tester', false],
    ])
    expect(chips[0]!.skill?.description).toBe('writer description')
  })

  it('claims nothing is missing before the folder has been read', () => {
    expect(skillChips(['writer'], null)).toEqual([{ name: 'writer', skill: null, missing: false }])
  })

  it('matches names exactly, as the backend does', () => {
    expect(skillChips(['Writer'], [skill('writer')])[0]!.missing).toBe(true)
  })
})

describe('skillOptions', () => {
  it('offers every skill the member does not have yet, in the folder’s order', () => {
    const all = [skill('designer'), skill('tester'), skill('writer')]
    expect(skillOptions(all, ['tester', 'gone']).map((entry) => entry.name)).toEqual([
      'designer',
      'writer',
    ])
  })
})

describe('inSkillsFolder', () => {
  it('hears the folder, a skill in it and a file deep inside one', () => {
    expect(inSkillsFolder('voxarium_studio/skills', 'voxarium_studio/skills')).toBe(true)
    expect(inSkillsFolder('voxarium_studio/skills/tester', 'voxarium_studio/skills')).toBe(true)
    expect(inSkillsFolder('voxarium_studio/skills/tester/scripts/run.py', 'voxarium_studio/skills/')).toBe(true)
  })

  it('does not hear a sibling that only starts with the same letters', () => {
    expect(inSkillsFolder('voxarium_studio/skills-old/tester/SKILL.md', 'voxarium_studio/skills')).toBe(false)
    expect(inSkillsFolder('voxarium_studio/docs/skills.md', 'voxarium_studio/skills')).toBe(false)
  })
})

describe('describeFiles', () => {
  it('names SKILL.md and counts the rest', () => {
    expect(describeFiles({ file_count: 0, file_count_capped: false })).toBe('SKILL.md')
    expect(describeFiles({ file_count: 1, file_count_capped: false })).toBe('SKILL.md + 1 file')
    expect(describeFiles({ file_count: 2000, file_count_capped: true })).toBe(
      'SKILL.md + 2000+ files',
    )
  })
})
