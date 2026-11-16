/**
 * The one transcript widget, rendered (§10.4, §10.9): the folds it draws at each level, the
 * same for a task and for a chat, and the Status column beside a task's transcript.
 */
import { createPinia } from 'pinia'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import type { AgentChat, AgentMessage } from '@/api/types'
import ConversationPanel from '@/components/teams/ConversationPanel.vue'
import SessionStatus from '@/components/teams/SessionStatus.vue'
import { setLocale } from '@/i18n'
import type { Verbosity } from '@/lib/transcript'
import { useVerbosity } from '@/lib/transcript-verbosity'
import { useChatStore } from '@/stores/chat'

let sequence = 0
const message = (overrides: Partial<AgentMessage> = {}): AgentMessage => ({
  id: `msg_${(sequence += 1)}`,
  task_id: 'task_1',
  member_id: 'team_default__dev',
  role: 'assistant',
  kind: 'text',
  text: '',
  data: {},
  created_at: '2026-11-10T10:00:00',
  ...overrides,
})
const call = (name: string, input: Record<string, unknown>, id: string) =>
  message({ kind: 'tool_use', text: name, data: { id, name, input } })

const feed = [
  message({ role: 'user', text: 'Fix the dash cooldown.' }),
  message({ kind: 'thinking', text: 'The cooldown is checked before the timer ticks.' }),
  call('Read', { file_path: 'dash.ts' }, 'toolu_1'),
  call('Bash', { command: 'npm test' }, 'toolu_2'),
  message({ role: 'system', kind: 'tool_result', text: '12 passed', data: { tool_use_id: 'toolu_2' } }),
  message({ text: 'Fixed: the timer now ticks first.' }),
]

async function render(level: Verbosity, chat = false): Promise<string> {
  useVerbosity().value = level
  const app = createSSRApp(ConversationPanel, {
    conversationId: 'task_1', messages: feed, emptyText: 'Nothing yet.',
    ...(chat ? { chat: { name: 'Dev', running: false } } : {}),
  })
  return renderToString(app.use(createPinia()))
}

beforeAll(async () => {
  await setLocale('en')
})

describe('the transcript widget', () => {
  it.each([false, true])('draws thinking and the tool calls as collapsed dashed folds at Default (chat: %s)', async (chat) => {
    const html = await render('default', chat)
    expect(html.match(/data-transcript-fold/g)).toHaveLength(2)
    expect(html).toContain('border-dashed')
    expect(html).toContain('Thinking')
    expect(html).toContain('Tools used: 2')
    expect(html).toContain('Read, Bash')
    // Collapsed: the header names the work, the content is not rendered until opened.
    expect(html).not.toContain('The cooldown is checked')
    expect(html).not.toContain('dash.ts')
    expect(html).toContain('aria-expanded="false"')
    expect(html).toContain('Fixed: the timer now ticks first.')
    expect(html).toContain('<option value="default" selected')
  })

  it.each([false, true])('draws the same folds open at Verbose, the results inside them (chat: %s)', async (chat) => {
    const html = await render('verbose', chat)
    expect(html.match(/data-transcript-fold/g)).toHaveLength(2)
    expect(html).not.toContain('aria-expanded="false"')
    expect(html.match(/aria-expanded="true"/g)).toHaveLength(2)
    expect(html).toContain('The cooldown is checked before the timer ticks.')
    expect(html).toContain('dash.ts')
    expect(html).toContain('12 passed')
  })

  const studioFeed = (result?: AgentMessage) => [
    message({ text: 'Splitting the work.' }),
    call('Read', { file_path: 'dash.ts' }, 'toolu_1'),
    call('mcp__voxarium_studio__spawn_subagent', { description: 'Review dash.ts' }, 'toolu_2'),
    ...(result ? [result] : []),
    message({ text: 'Waiting for the review.' }),
  ]
  const spawned = message({
    role: 'system', kind: 'tool_result', data: { tool_use_id: 'toolu_2' },
    text: JSON.stringify({ id: 'subagent_1', effort: 'medium', effort_solver: {
      scores: { complexity: 2, creativity: 4, depth: 3, coverage: 7 }, effort: 2, effort_level: 'medium',
    } }),
  })

  async function renderFeed(messages: AgentMessage[], level: Verbosity, taskStatus = 'done', chat = false, auto = false): Promise<string> {
    useVerbosity().value = level
    const app = createSSRApp(ConversationPanel, {
      conversationId: 'task_1', messages, emptyText: 'Nothing yet.', taskStatus,
      ...(chat ? { chat: { name: 'Dev', running: taskStatus === 'in_progress' } } : {}),
    })
    const pinia = createPinia()
    if (auto) useChatStore(pinia).chats = [{ id: 'task_1', producer_effort_mode: 'auto' } as AgentChat]
    return renderToString(app.use(pinia))
  }

  it('draws Studio tools as a violet dashed fold of their own', async () => {
    const html = await renderFeed(studioFeed(spawned), 'default')
    expect(html.match(/data-transcript-fold/g)).toHaveLength(2)
    expect(html.match(/data-studio-tools/g)).toHaveLength(1)
    expect(html).toContain('Tools used: 1')
    expect(html).toContain('Studio tools used: 1')
    expect(html).toContain('border-violet-500/60')
  })

  it('uses violet only for Studio spawn and create_task, leaving other tools standard', async () => {
    const feed = [
      call('Read', {}, 'mixed_1'),
      call('mcp__voxarium_studio__get_task', {}, 'mixed_2'),
      call('mcp__voxarium_studio__spawn_subagent', {}, 'mixed_3'),
      call('mcp__voxarium_studio__wait_subagents', {}, 'mixed_4'),
      call('mcp__voxarium_studio__create_task', {}, 'mixed_5'),
      call('mcp__voxarium_studio__finish_task', {}, 'mixed_6'),
      call('mcp__other__create_task', {}, 'mixed_7'),
    ]
    for (const level of ['default', 'verbose'] as const) {
      const html = await renderFeed(feed, level)
      expect(html.match(/data-transcript-fold/g)).toHaveLength(5)
      expect(html.match(/data-studio-tools/g)).toHaveLength(2)
      expect(html.match(/border-violet-500[/]60/g)).toHaveLength(2)
      expect(html.match(/Studio tools used: 1/g)).toHaveLength(1)
      expect(html.match(/Tools used: 2/g)).toHaveLength(2)
    }
  })

  const surfaces = (['default', 'verbose'] as const).flatMap((level) =>
    [false, true].map((chat) => ({ level, chat })))

  // Inspect headers, excluding any buttons belonging to the tool's content.
  const foldHeaders = (html: string) =>
    [...html.matchAll(/data-transcript-fold[^>]*>\s*(<(?:button|div)\b[\s\S]*?<\/(?:button|div)>)/g)]
      .map((match) => match[1]!)

  it.each(surfaces)('gives each creation a titled page-and-pencil block at $level (chat: $chat)', async ({ level, chat }) => {
    const first = call('mcp__voxarium_studio__create_task', { title: 'First task' }, 'create_a')
    const second = call('mcp__voxarium_studio__create_task', { title: 'Second task' }, 'create_b')
    const answer = { ...spawned, data: { tool_use_id: 'create_a' }, text: JSON.stringify({
      task_id: 'created_a', effort_solver: JSON.parse(spawned.text).effort_solver,
    }) }
    const event = message({ kind: 'effort_solver', data: {
      task_id: 'created_a', ...JSON.parse(spawned.text).effort_solver,
    } })
    const html = await renderFeed([first, second, event, answer], level, 'in_progress', chat, true)
    expect(html.match(/data-task-creation/g)).toHaveLength(2)
    expect(html.match(/data-task-evaluation/g)).toHaveLength(1)
    expect(html.match(/aria-label="Effort Solver"/g)).toHaveLength(1)
    expect(html).toContain('Effort Solver is scoring this task')
    const headers = foldHeaders(html)
    expect(headers).toHaveLength(2)
    expect(headers[0]).toContain('First task')
    expect(headers[1]).toContain('Second task')
    expect(headers.every(header => header.startsWith('<div') && header.includes('lucide-file-pen'))).toBe(true)
  })

  it('does not invent task scoring for Agent mode, stopped calls, errors or legacy answers', async () => {
    const create = call('mcp__voxarium_studio__create_task', { title: 'Create movement' }, 'create_a')
    for (const [status, auto, result] of [
      ['in_progress', false, undefined], ['cancelled', true, undefined],
      ['in_progress', true, { ...spawned, data: { tool_use_id: 'create_a', is_error: true } }],
      ['done', true, { ...spawned, text: 'Created VS-01', data: { tool_use_id: 'create_a' } }],
    ] as const) {
      const html = await renderFeed([create, ...(result ? [result] : [])], 'default', status, true, auto)
      expect(html).not.toContain('Effort Solver is scoring')
      expect(html).not.toContain('data-task-evaluation')
      expect(foldHeaders(html)[0]).toContain('Create movement')
    }
  })

  it.each(surfaces)('keeps a completed spawn open at $level (chat: $chat)', async ({ level, chat }) => {
    const html = await renderFeed(studioFeed(spawned), level, 'done', chat)
    expect(html).toContain('data-subagent-spawn')
    expect(html.match(/data-spawn-evaluation/g)).toHaveLength(1)
    expect(html).toContain('Review dash.ts')
    expect(html).toContain('Complexity')
    expect(html).toContain('Mid')
    const headers = foldHeaders(html)
    expect(headers).toHaveLength(2)
    expect(headers[0]).toMatch(/^<button\b/)
    expect(headers[0]).toContain('aria-expanded="' + (level === 'verbose') + '"')
    expect(headers[1]).toMatch(/^<div\b/)
    expect(headers[1]).not.toContain('aria-expanded')
    expect(headers[1]).not.toContain('<button')
    expect(headers[1]).not.toContain('vxs-chevron')
  })

  it.each(surfaces)('keeps pending Solver progress visible at $level (chat: $chat)', async ({ level, chat }) => {
    const html = await renderFeed(studioFeed(), level, 'in_progress', chat)
    expect(html).toContain('data-subagent-spawn')
    expect(html).toContain('Review dash.ts')
    expect(html).toContain('Effort Solver is scoring this subagent')
    expect(html).not.toContain('data-spawn-evaluation')
    expect(foldHeaders(html)[1]).toMatch(/^<div\b/)
  })

  it.each(surfaces)('does not invent an evaluation after a stop or error at $level (chat: $chat)', async ({ level, chat }) => {
    const failed = { ...spawned, data: { ...spawned.data, is_error: true } }
    const legacy = { ...spawned, text: JSON.stringify({ id: 'subagent_legacy' }) }
    for (const [result, status] of [
      [undefined, 'cancelled'], [undefined, 'error'], [failed, 'in_progress'], [legacy, 'done'],
    ] as const) {
      const html = await renderFeed(studioFeed(result), level, status, chat)
      expect(html).toContain('data-subagent-spawn')
      expect(html).not.toContain('Effort Solver is scoring')
      expect(html).not.toContain('data-spawn-evaluation')
      expect(html).not.toContain('Complexity')
      expect(foldHeaders(html)[1]).toMatch(/^<div\b/)
    }
  })

  it.each(surfaces)('isolates adjacent spawns and keeps create_task open at $level (chat: $chat)', async ({ level, chat }) => {
    const messages = [
      call('mcp__voxarium_studio__create_task', { title: 'First task' }, 'task_a'),
      call('mcp__voxarium_studio__spawn_subagent', { description: 'First review' }, 'spawn_a'),
      call('mcp__voxarium_studio__spawn_subagent', { description: 'Second review' }, 'spawn_b'),
      call('mcp__voxarium_studio__create_task', { title: 'Second task' }, 'task_b'),
      message({ text: 'Continuing while results arrive.' }),
      // Delayed results arrive beyond prose and in reverse order.
      { ...spawned, id: 'result_b', data: { tool_use_id: 'spawn_b' } },
      { ...spawned, id: 'result_a', data: { tool_use_id: 'spawn_a' },
        text: JSON.stringify({ id: 'subagent_a', effort: 'high', effort_solver: {
          scores: { complexity: 8, creativity: 5, depth: 6, coverage: 9 }, effort: 3, effort_level: 'high',
        } }) },
    ]
    const html = await renderFeed(messages, level, 'in_progress', chat)
    expect(html.match(/data-transcript-fold/g)).toHaveLength(4)
    expect(html.match(/data-subagent-spawn/g)).toHaveLength(2)
    expect(html.match(/data-spawn-evaluation/g)).toHaveLength(2)
    expect(html).not.toContain('Effort Solver is scoring')
    const headers = foldHeaders(html)
    expect(headers.map((header) => header.startsWith('<div'))).toEqual([true, true, true, true])
    for (const index of [0, 3]) {
      expect(headers[index]).not.toContain('aria-expanded')
      expect(headers[index]).not.toContain('vxs-chevron')
      expect(headers[index]).toContain(index === 0 ? 'First task' : 'Second task')
    }
    const first = html.indexOf('First review', html.indexOf('data-subagent-spawn'))
    const second = html.indexOf('Second review', html.indexOf('data-subagent-spawn'))
    expect(html.slice(first, second)).toContain('High')
    expect(html.slice(first, second)).not.toContain('Mid')
    expect(html.slice(second)).toContain('Mid')
  })

  it('offers only Default and Verbose', async () => {
    const html = await render('default')
    expect(html).toContain('<option value="default"')
    expect(html).toContain('<option value="verbose"')
    expect(html).not.toContain('value="compact"')
  })

  it('draws the Effort Solver evaluation of an Auto task at its start, outside a chat too', async () => {
    const evaluation = message({
      role: 'system', kind: 'effort_solver', text: 'high',
      data: { scores: { complexity: 6, creativity: 3, depth: 5, coverage: 8 }, effort: 3, effort_level: 'high' },
    })
    const html = await renderFeed([evaluation, message({ text: 'Starting.' })], 'default')
    expect(html).toContain('aria-label="Effort Solver"')
    expect(html).toContain('Complexity')
    expect(html).toContain('Starting.')
  })
})

describe('the Status column', () => {
  const statusMessages = () => [
    message({ role: 'system', kind: 'skills', data: {
      skills: ['voxarium_studio:studio', 'level-design'], loaded: ['voxarium_studio:studio'], missing: [], labels: {},
    } }),
    message({ role: 'system', kind: 'mcp', data: { servers: ['voxarium_studio'] } }),
    message({ role: 'system', kind: 'system', text: 'init', data: {
      subtype: 'init', mcp_servers: [{ name: 'voxarium_studio', status: 'connected' }],
    } }),
    call('Skill', { skill: 'level-design' }, 'toolu_3'),
  ]
  const renderStatus = () => {
    const messages = statusMessages()
    const app = createSSRApp({
      components: { SessionStatus },
      setup: () => ({ messages, writePaths: ['src/dash.ts', 'docs/dash.md'] }),
      template: '<SessionStatus :messages="messages" :subagents="[]" :write-paths="writePaths"><p>FILES</p></SessionStatus>',
    })
    return renderToString(app.use(createPinia()))
  }

  afterEach(() => vi.unstubAllGlobals())

  it('folds the skills, MCP servers and files to write to their headings by default, above the files', async () => {
    const html = await renderStatus()
    for (const text of ['Status', 'Delegated work', 'none started', 'Skills', 'MCP servers', 'Files to write'])
      expect(html).toContain(text)
    expect(html.match(/aria-expanded="false"/g)).toHaveLength(4)
    for (const text of ['level-design', 'voxarium_studio:studio', 'connected', 'src/dash.ts']) expect(html).not.toContain(text)
    expect(html.indexOf('MCP servers')).toBeLessThan(html.indexOf('Files to write'))
    expect(html.indexOf('Files to write')).toBeLessThan(html.indexOf('FILES'))
  })

  it('lists them when the reader left them open', async () => {
    const stored: Record<string, string> = {
      'voxarium_studio.status.skills': 'open',
      'voxarium_studio.status.mcp': 'open',
      'voxarium_studio.status.writePaths': 'open',
    }
    vi.stubGlobal('window', { localStorage: {
      getItem: (key: string) => stored[key] ?? null,
      setItem: (key: string, value: string) => { stored[key] = value },
    } })
    const html = await renderStatus()
    for (const text of ['level-design', 'used', 'voxarium_studio:studio', 'loaded', 'voxarium_studio', 'connected',
      'src/dash.ts', 'docs/dash.md']) expect(html).toContain(text)
  })
})
