import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { afterEach, describe, expect, it } from 'vitest'
import type { MemberOptions } from '@/api/types'
import ModelSelector from './ModelSelector.vue'
import { apiConnections } from '@/lib/api-backends'

afterEach(() => { apiConnections.value = [] })

const options: MemberOptions = {
  agent_backends: {}, permission_modes: [], effort_levels: [], common_tools: [], studio_tool_prefix: '',
  suggested_models: [
    { id: 'sonnet', label: 'Sonnet', group: 'Aliases', note: '', backend: 'claude_code' },
    { id: 'gpt-test', label: 'GPT Test', group: 'Models', note: '', backend: 'codex' },
    { id: 'qwen-test', label: 'Qwen Test', group: 'Models', note: '', backend: 'qwen' },
  ],
}

async function markup(backend: string | null, model: string | null): Promise<string> {
  return renderToString(createSSRApp(ModelSelector, { backend, model, options }))
}

describe('model selector', () => {
  it('uses the current API catalogue, deduplicates the default and ignores stale suggestions', async () => {
    apiConnections.value = [{ id: 'first', name: 'First API', base_url: 'https://example.test/v1',
      model: 'default', models: ['default', 'second'], api_mode: 'responses', use_limits: false, tpm: 1_000_000, context_window: null,
      icon: null, has_api_key: false }]
    const html = await renderToString(createSSRApp(ModelSelector, {
      backend: 'openai_api:first', model: 'second', options: { ...options,
        suggested_models: [{ id: 'removed', label: 'Removed', group: 'Old', backend: 'openai_api:first', note: '' }] },
    }))
    expect(html).toContain('<select')
    expect(html).toContain('value="second"')
    expect(html.match(/value="default"/g)).toHaveLength(1)
    expect(html).not.toContain('Removed')
    expect(html).not.toContain('Sonnet')
  })
  it('offers multiple API connections as distinct labelled choices', async () => {
    const html = await renderToString(createSSRApp(ModelSelector, {
      backend: 'openai_api:first', model: 'custom-api-model', options: {
        ...options, agent_backends: {
          'openai_api:first': { available: true, reason: '', label: 'First API' },
          'openai_api:second': { available: true, reason: '', label: 'Second API' },
        },
      },
    }))
    expect(html).toContain('aria-label="First API" aria-pressed="true"')
    expect(html).toContain('aria-label="Second API" aria-pressed="false"')
    expect(html).toContain('value="custom-api-model"')
  })
  it('shows four icon-only backend buttons and only the selected backend models', async () => {
    const html = await markup('codex', 'gpt-test')
    expect((html.match(/<button\b/g) ?? []).length).toBe(4)
    expect(html).toContain('aria-label="codex" aria-pressed="true"')
    expect(html).toContain('GPT Test')
    expect(html).not.toContain('Sonnet')
    expect(html).not.toContain('Qwen Test')
  })

  it('offers only the backends the caller can run', async () => {
    const html = await renderToString(createSSRApp(ModelSelector, {
      backend: 'claude_code', model: 'sonnet', options, backends: ['claude_code'],
    }))
    expect((html.match(/<button\b/g) ?? []).length).toBe(1)
    expect(html).toContain('aria-label="claude_code" aria-pressed="true"')
    expect(html).toContain('Sonnet')
    expect(html).not.toContain('GPT Test')
  })

  it('keeps an unlisted model editable in the same row', async () => {
    const html = await markup('codex', 'future-model')
    expect(html).toContain('value="future-model"')
    expect(html).toContain('<input')
  })
})


describe('Orchestrator Auto model choice', () => {
  it('offers literal auto as a listed choice only when enabled by the agent editor', async () => {
    const html = await renderToString(createSSRApp(ModelSelector, {
      backend: 'codex', model: 'auto', options, allowAuto: true,
    }))
    expect(html).toContain('value="auto"')
    expect(html).toContain('<select')
    expect(html).not.toContain('<input')
    const explicit = await markup('codex', 'gpt-test')
    expect(explicit).not.toContain('value="auto"')
  })
})
