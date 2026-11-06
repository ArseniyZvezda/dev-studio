/**
 * A member's skills against the folder (§10.10): what the editor offers, what a card marks
 * missing — and not before the folder has been read — and which watcher paths are the folder's.
 */

import { describe, expect, it } from 'vitest'

import type { SkillEntry } from '@/api/types'

import { describeFiles, filterSkillOptions, filterSkills, inSkillsFolder, skillChips, skillOptions, skillPickerScope } from './skills'

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

  it('offers shared and native installed skills for the selected backend', () => {
    const all = [skill('project'), skill('shared', { backends: [] }),
      skill('claude', { backends: ['claude_code'] }), skill('codex', { backends: ['codex'] })]
    expect(skillOptions(all, ['project'], 'codex').map((entry) => entry.name)).toEqual(['shared', 'codex'])
    expect(skillOptions(all, ['shared'], 'claude_code').map((entry) => entry.name)).toEqual(['project', 'claude'])
  })
})

describe('filterSkills', () => {
  it('finds installed skills by their readable names and source, independently of ids', () => {
    const installed = skill('installed:opaque', { display_name: 'Level Design', source: 'codex' })
    expect(filterSkills([skill('other'), installed], 'design CODEX')).toEqual([installed])
    expect(filterSkills([installed], 'missing')).toEqual([])
    expect(filterSkills([installed], '  ')).toEqual([installed])
  })
})

describe('skill picker filters', () => {
  const studio = skill('level-design', { scope: 'project', description: 'Design a playable level' })
  const bundled = skill('voxarium_studio:studio', { scope: 'builtin', source: 'voxarium_studio', automatic: true })
  const shared = skill('installed:shared', { scope: 'global', source: 'shared', display_name: 'Level Design', backends: [] })
  const codex = skill('installed:codex', { scope: 'global', source: 'codex', display_name: 'Level Design', backends: ['codex'] })
  const project = skill('installed:project', { scope: 'project', source: 'claude_code', backends: ['claude_code'] })
  const catalog = [studio, bundled, shared, codex, project]

  it('distinguishes Studio content from native project and global installations', () => {
    expect(catalog.map(skillPickerScope)).toEqual(['studio', 'studio', 'global', 'global', 'project'])
    expect(filterSkillOptions(catalog, '', 'all', 'studio')).toEqual([studio, bundled])
    expect(filterSkillOptions(catalog, '', 'all', 'project')).toEqual([project])
    expect(skillPickerScope(skill('legacy'))).toBe('studio')
  })

  it('intersects search, backend/shared, and scope without treating shared skills as native', () => {
    expect(filterSkillOptions(catalog, 'LEVEL', 'shared', 'global')).toEqual([shared])
    expect(filterSkillOptions(catalog, 'level', 'codex', 'global')).toEqual([codex])
    expect(filterSkillOptions(catalog, 'playable', 'shared', 'studio')).toEqual([studio])
    expect(filterSkillOptions(catalog, '', 'codex', 'studio')).toEqual([])
    expect(filterSkillOptions(catalog, '', 'all', 'all')).toEqual(catalog)
  })

  it('keeps compatibility and existing assignments excluded before narrowing the catalog', () => {
    const offered = skillOptions(catalog, [shared.name], 'codex')
    expect(filterSkillOptions(offered, '', 'shared')).toEqual([studio, bundled])
    expect(filterSkillOptions(offered, '', 'all', 'global')).toEqual([codex])
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
