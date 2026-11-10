import { createPinia } from 'pinia'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { describe, expect, it } from 'vitest'

import type { AgentChatMessage } from '@/api/types'
import { setLocale } from '@/i18n'
import { transcriptRows, VERBOSITY_LEVELS } from '@/lib/transcript'
import EffortSolverResult from './EffortSolverResult.vue'
import ConversationPanel from '@/components/teams/ConversationPanel.vue'

const data = { scores: { complexity: 9, creativity: 6, depth: 8, coverage: 8 }, effort: 4, effort_level: 'xhigh' }
const message: AgentChatMessage = {
  id: 'solver-1', chat_id: 'producer', member_id: 'solver', role: 'system', kind: 'effort_solver',
  text: 'Raw reasoning must stay hidden', data: { ...data, reasoning: 'Private reasoning', title: 'Hidden task title' },
  created_at: '2026-11-06T00:00:00Z',
}

describe('Effort Solver result in Producer dialogue', () => {
  it.each(VERBOSITY_LEVELS)('keeps the result visible at %s verbosity', level => {
    expect(transcriptRows([message], level).map(row => row.key)).toEqual([message.id])
  })

  it.each(['ordinary chat', 'Producer Mode'])('renders only the scores and effort in %s', async surface => {
    await setLocale('en')
    // §10.9: both surfaces are the one transcript widget now; only the turn state differs.
    const app = createSSRApp(ConversationPanel, {
      messages: [message], conversationId: 'producer', emptyText: '',
      chat: { name: 'Producer', running: surface === 'Producer Mode' },
    })
    const html = await renderToString(app.use(createPinia()))
    expect(html).toContain('border-violet-500/40')
    expect(html).toContain('aria-label="Effort Solver"')
    for (const label of ['Complexity', 'Creativity', 'Depth', 'Coverage', 'XHigh']) expect(html).toContain(label)
    expect([...html.matchAll(/<dd[^>]*>([^<]*)<\/dd>/g)].map(match => match[1])).toEqual(['9', '6', '8', '8', 'XHigh'])
    expect(html).not.toContain('Raw reasoning')
    expect(html).not.toContain('Private reasoning')
    expect(html).not.toContain('Hidden task title')
  })

  it('labels level 2 as Mid and localizes criterion names', async () => {
    await setLocale('ru')
    try {
      const html = await renderToString(createSSRApp(EffortSolverResult, { data: { ...data, effort: 2 } }))
      expect(html).toContain('Сложность')
      expect(html).toContain('Полнота описания')
      expect(html).toContain('Mid')
    } finally {
      await setLocale('en')
    }
  })

  it('does not fabricate results from malformed stored records', async () => {
    const html = await renderToString(createSSRApp(EffortSolverResult, { data: { ...data, effort: 6 } }))
    expect(html).not.toContain('<section')
  })
})
