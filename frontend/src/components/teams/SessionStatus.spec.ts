// @vitest-environment jsdom
/** VS-144: every Status subsection participates in the same scroll content (§10.4). */
import { createApp, h, type App } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'

import SessionStatus from './SessionStatus.vue'

let app: App | undefined
afterEach(() => {
  app?.unmount()
  localStorage.clear()
})

describe('Status common scroll content', () => {
  it('places Files touched after the other subsections inside the only scroll area', () => {
    const host = document.createElement('div')
    app = createApp({
      render: () => h(SessionStatus, { messages: [], subagents: [], writePaths: ['src/main.ts'] }, {
        default: () => h('section', { 'data-status-files': '' }, 'Files touched'),
      }),
    })
    app.mount(host)

    const status = host.querySelector('[data-session-status]')!
    const content = status.querySelector('[data-status-content]')!
    const files = status.querySelector('[data-status-files]')!
    expect(content.classList.contains('overflow-y-auto')).toBe(true)
    expect(content.classList.contains('min-h-0')).toBe(true)
    expect(content.classList.contains('flex-1')).toBe(true)
    expect(files.parentElement).toBe(content)
    expect(content.lastElementChild).toBe(files)
    expect(content.querySelector('[data-status-skills]')).not.toBeNull()
    expect(content.querySelector('[data-status-mcp]')).not.toBeNull()
    expect(content.querySelector('[data-status-write-paths]')).not.toBeNull()
    expect(status.querySelectorAll('[class*="overflow-y-"]')).toHaveLength(1)
    // No independent Files pane below the common scroller.
    expect(status.children).toHaveLength(2)
  })
})
