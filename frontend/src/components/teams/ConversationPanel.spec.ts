/**
 * The one transcript widget, rendered (§10.4, §10.9): the folds it draws at each level, the
 * same for a task and for a chat, and the Status column beside a task's transcript.
 */
import { createPinia } from 'pinia'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { beforeAll, describe, expect, it } from 'vitest'

import type { AgentMessage } from '@/api/types'
import ConversationPanel from '@/components/teams/ConversationPanel.vue'
import SessionStatus from '@/components/teams/SessionStatus.vue'
import { setLocale } from '@/i18n'
import type { Verbosity } from '@/lib/transcript'
import { useVerbosity } from '@/lib/transcript-verbosity'

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

  it('offers only Default and Verbose', async () => {
    const html = await render('default')
    expect(html).toContain('<option value="default"')
    expect(html).toContain('<option value="verbose"')
    expect(html).not.toContain('value="compact"')
  })
})

describe('the Status column', () => {
  it('lists the subagents, skills and MCP servers above the files', async () => {
    const messages = [
      message({ role: 'system', kind: 'skills', data: {
        skills: ['voxarium_studio:studio', 'level-design'], loaded: ['voxarium_studio:studio'], missing: [], labels: {},
      } }),
      message({ role: 'system', kind: 'mcp', data: { servers: ['voxarium_studio'] } }),
      message({ role: 'system', kind: 'system', text: 'init', data: {
        subtype: 'init', mcp_servers: [{ name: 'voxarium_studio', status: 'connected' }],
      } }),
      call('Skill', { skill: 'level-design' }, 'toolu_3'),
    ]
    const app = createSSRApp({
      components: { SessionStatus },
      setup: () => ({ messages }),
      template: '<SessionStatus :messages="messages" :subagents="[]"><p>FILES</p></SessionStatus>',
    })
    const html = await renderToString(app.use(createPinia()))
    for (const text of ['Status', 'Delegated work', 'none started', 'Skills', 'level-design', 'used',
      'voxarium_studio:studio', 'loaded', 'MCP servers', 'connected']) expect(html).toContain(text)
    expect(html.indexOf('MCP servers')).toBeLessThan(html.indexOf('FILES'))
  })
})
