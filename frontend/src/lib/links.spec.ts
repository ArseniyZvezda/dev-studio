import { describe, expect, it } from 'vitest'

import { safeLinkHref } from './links'

describe('links in agent-written content', () => {
  it('preserves ordinary links without fetching remote content', () => {
    for (const href of ['https://example.invalid/image.png', 'HTTP://example.invalid',
      'mailto:studio@example.invalid', '//example.invalid/image.png', './image.png', '#section']) {
      expect(safeLinkHref(href)).toBe(href)
    }
  })

  it('does not turn executable, local-file or custom schemes into anchors', () => {
    for (const href of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'java\nscript:alert(1)',
      '\u0000javascript:alert(1)', 'data:text/html,<script>alert(1)</script>', 'vbscript:msgbox(1)',
      'file:///C:/private.txt', 'C:/private.txt', 'studio:run', '   ']) {
      expect(safeLinkHref(href), href).toBeNull()
    }
  })
})
