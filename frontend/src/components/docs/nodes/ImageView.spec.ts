import { renderToString } from '@vue/server-renderer'
import { createSSRApp } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import ImageView from './ImageView.vue'

vi.mock('@/stores/docs', () => ({ useDocsStore: () => ({ open: { path: 'guide.md' } }) }))
vi.mock('@/api/client', () => ({ docAssetUrl: () => '/api/docs/asset' }))

async function renderImage(src: string): Promise<string> {
  const app = createSSRApp(ImageView, {
    node: { attrs: { src, alt: 'Reference image' } },
    editor: { extensionManager: { extensions: [] }, isEditable: false },
    selected: false,
    decorations: [],
    innerDecorations: {},
    view: {},
    extension: {},
    getPos: () => 0,
    updateAttributes: () => {},
    deleteNode: () => {},
  })
  app.provide('onDragStart', () => {})
  app.provide('decorationClasses', '')
  return renderToString(app)
}

describe('document image references', () => {
  it('shows an ordinary remote image as a link without fetching it', async () => {
    const html = await renderImage('https://example.invalid/image.png')
    expect(html).toContain('href="https://example.invalid/image.png"')
    expect(html).not.toContain('<img')
  })

  it('shows executable and local-file references as text, with no clickable anchor', async () => {
    for (const src of ['javascript:alert(document.domain)', 'data:text/html,<script>alert(1)</script>',
      'file:///C:/private.txt', 'studio:run']) {
      const html = await renderImage(src)
      expect(html, src).toContain('Reference image')
      expect(html, src).not.toContain('<a ')
      expect(html, src).not.toContain('<img')
    }
  })
})
