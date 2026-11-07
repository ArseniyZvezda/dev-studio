import { createPinia } from 'pinia'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { describe, expect, it } from 'vitest'
import ContextFragmentText from './ContextFragmentText.vue'

async function render(text: string, truncated = false) {
  const app = createSSRApp(ContextFragmentText, {
    source: { id: '7', label: 'tool', category: 'tools', text, tokens: 20, truncated },
  })
  return renderToString(app.use(createPinia()))
}

describe('the initial Context Viewer text (§10.4)', () => {
  it.each([
    '  [1,\t2]\r\n\r\n',
    '~~~json5\r\n{answer: 42, // keep\n}\r\n~~~\t\n',
    'Read\r\n{ "id":1,"id":2 }',
  ])('renders structured fragments expanded on first display', async text => {
    const html = await render(text)
    expect(html).toContain('class="json-lines"')
    expect(html).toContain('aria-expanded="true"')
    expect(html).not.toContain('aria-pressed')
  })

  it('keeps spacing in text without a recognized format', async () => {
    const html = await render('  plain\r\ntext\t')
    expect(html).toContain('>  plain\r\ntext\t</pre>')
    expect(html).not.toContain('class="format-bar"')
  })

  it('displays a truncated preview verbatim even if that prefix alone is valid JSON', async () => {
    const html = await render(' [1,2]  ', true)
    expect(html).toContain('> [1,2]  </pre>')
    expect(html).not.toContain('aria-pressed')
  })
})
