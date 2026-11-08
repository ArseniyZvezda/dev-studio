import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { expect, it } from 'vitest'
import type { ContextInventory, ContextSource } from '@/api/types'
import { setLocale } from '@/i18n'
import { contextFieldLocator } from '@/lib/context-source'
import ContextProvider from './ContextProvider.vue'

function render(fields: Partial<ContextSource>, inventory: ContextInventory['origin'], compact = false) {
  const source: ContextSource = { id: '0', category: 'system', label: '', text: '', tokens: 1, truncated: false, ...fields }
  return renderToString(createSSRApp(ContextProvider, { source, inventory, compact }))
}

it('names the record and evidence independently of built-in ownership in both languages', async () => {
  for (const [locale, journal, toolCall] of [
    ['en', 'Codex session journal', 'Tool call'], ['ru', 'Журнал сессии Codex', 'Вызов инструмента'],
  ] as const) {
    await setLocale(locale)
    const tool = { source_kind: 'tool_call', source_detail: 'studio.read_file' }
    const full = await render(tool, 'codex_journal')
    expect(full).toContain(`>${journal} · ${toolCall} · studio.read_file<`)
    expect(full).not.toContain('<svg')
    const compact = await render(tool, 'codex_journal', true)
    expect(compact).toContain(`>${toolCall} · studio.read_file<`)
  }
})

it('labels only confirmed built-in sources with their backend marks and readable names', async () => {
  await setLocale('en')
  const providers = { codex: 'Codex', claude_code: 'Claude Code', qwen: 'Qwen Code' } as const
  const paths = new Set<string>()
  for (const backend of Object.keys(providers) as (keyof typeof providers)[]) {
    const html = await render({ provided_by: backend }, `${backend}_journal`)
    expect(html).toContain(`Built into ${providers[backend]}`)
    const mark = html.match(/<path d="([^"]+)"/)
    expect(mark).not.toBeNull()
    paths.add(mark![1])
  }
  expect(paths.size).toBe(3)
})

it('uses available evidence for legacy or unrecognized records and leaves the remainder unattributed', async () => {
  await setLocale('en')
  expect(await render({}, 'studio')).toContain('>Studio snapshot and timeline<')
  expect(await render({ source_kind: 'future_kind', label: 'level.md' }, 'studio')).toContain('>Studio snapshot and timeline · level.md<')
  expect(await render({ category: 'unattributed' }, 'studio')).not.toContain('<span')
})

it('shows the physical JSONL line alongside the precise field, including compacted and sliced records', async () => {
  const path = 'session.jsonl'
  await setLocale('en')
  for (const prefix of ['journal', 'session_meta', 'compacted', 'response_item']) {
    const field = `${prefix}[14].payload.replacement_history[7].content[0].text[12:30]`
    expect(contextFieldLocator(field, path)).toBe(`Line 15 · ${field}`)
    expect(contextFieldLocator(field)).toBe(field)
  }
  expect(contextFieldLocator('studio.messages[14].text', path)).toBe('studio.messages[14].text')
})
