/**
 * What the Status column says a session was equipped with, for each of the three runtimes —
 * the Studio's own records first, then what Claude Code and Qwen Code report at start-up,
 * then what the feed shows was used.
 */
import { describe, expect, it } from 'vitest'

import type { AgentMessage } from '@/api/types'
import { sessionMcpServers, sessionSkills } from '@/lib/session-status'

let sequence = 0

const message = (overrides: Partial<AgentMessage> = {}): AgentMessage => ({
  id: `msg_${(sequence += 1)}`,
  task_id: 'task_1',
  member_id: 'team_default__dev',
  role: 'system',
  kind: 'system',
  text: '',
  data: {},
  created_at: '2026-11-10T10:00:00',
  ...overrides,
})

const call = (name: string, input: Record<string, unknown> = {}) =>
  message({ role: 'assistant', kind: 'tool_use', text: name, data: { name, input } })

const skillsRecord = (data: Record<string, unknown>) => message({ kind: 'skills', data })
const mcpRecord = (servers: string[]) => message({ kind: 'mcp', data: { servers } })

describe('skills', () => {
  it('lists what the run was told, loaded-at-start apart from merely offered', () => {
    const skills = sessionSkills([
      skillsRecord({
        skills: ['voxarium_studio:studio', 'installed:0123', 'level-design'],
        loaded: ['voxarium_studio:studio'],
        missing: ['old-skill'],
        labels: { 'installed:0123': 'doc-tools' },
      }),
    ])
    expect(skills.map((skill) => [skill.label, skill.state])).toEqual([
      ['voxarium_studio:studio', 'loaded'],
      ['doc-tools', 'offered'],
      ['level-design', 'offered'],
      ['old-skill', 'missing'],
    ])
  })

  it('marks a skill used when any runtime loads it — Claude’s Skill call or Codex’s named read', () => {
    const feed = [
      skillsRecord({ skills: ['level-design', 'installed:0123'], labels: { 'installed:0123': 'doc-tools' } }),
      // Claude Code and Qwen Code call the tool; the Codex backend names a SKILL.md read the same way.
      call('Skill', { skill: 'level-design' }),
      call('Skill', { skill: 'doc-tools' }),
    ]
    expect(sessionSkills(feed).map((skill) => [skill.label, skill.state])).toEqual([
      ['doc-tools', 'used'],
      ['level-design', 'used'],
    ])
  })

  it('matches a plugin’s namespaced name to the bare one, and adds skills only the runtime knew', () => {
    const feed = [
      skillsRecord({ skills: ['studio'] }),
      message({ text: 'init', data: { subtype: 'init', skills: ['voxarium_studio:studio', 'user-notes'] } }),
      call('Skill', { command: 'voxarium_studio:studio' }),
      call('Skill', { skill: 'pdf' }),
    ]
    expect(sessionSkills(feed).map((skill) => [skill.name, skill.state])).toEqual([
      ['pdf', 'used'],
      ['studio', 'used'],
      ['user-notes', 'offered'],
    ])
  })

  it('says nothing for a transcript that recorded none', () => {
    expect(sessionSkills([message({ role: 'assistant', kind: 'text', text: 'Hello.' })])).toEqual([])
  })
})

describe('MCP servers', () => {
  it('takes the servers from the Studio’s record and their states from Claude Code’s init frame', () => {
    const feed = [
      mcpRecord(['unreal', 'voxarium_studio']),
      message({
        text: 'init',
        data: { subtype: 'init', mcp_servers: [
          { name: 'voxarium_studio', status: 'connected' },
          { name: 'unreal', status: 'failed' },
        ] },
      }),
      call('mcp__voxarium_studio__list_tasks'),
      call('mcp__voxarium_studio__create_task'),
    ]
    expect(sessionMcpServers(feed)).toEqual([
      { name: 'unreal', status: 'failed', calls: 0 },
      { name: 'voxarium_studio', status: 'connected', calls: 2 },
    ])
  })

  it('reads Qwen Code’s init frame by its subtype, as it carries no other name', () => {
    const feed = [
      mcpRecord(['voxarium_studio']),
      message({ text: 'system', data: { type: 'system', subtype: 'init', mcp_servers: [{ name: 'voxarium_studio', status: 'connected' }] } }),
    ]
    expect(sessionMcpServers(feed)).toEqual([{ name: 'voxarium_studio', status: 'connected', calls: 0 }])
  })

  it('leaves Codex’s servers without a state, with the calls made to each', () => {
    const feed = [
      mcpRecord(['voxarium_studio']),
      message({ text: 'thread.started', data: { subtype: 'thread.started' } }),
      call('mcp__voxarium_studio__finish_task'),
    ]
    expect(sessionMcpServers(feed)).toEqual([{ name: 'voxarium_studio', status: null, calls: 1 }])
  })

  it('ignores a start-up frame from an earlier run', () => {
    const feed = [
      message({ text: 'init', data: { subtype: 'init', mcp_servers: [{ name: 'voxarium_studio', status: 'failed' }] } }),
      mcpRecord(['voxarium_studio']),
    ]
    expect(sessionMcpServers(feed)).toEqual([{ name: 'voxarium_studio', status: null, calls: 0 }])
  })
})
