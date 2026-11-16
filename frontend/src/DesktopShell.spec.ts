// @vitest-environment jsdom
import { createApp, h, nextTick, ref, shallowRef } from 'vue'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DesktopShellController, DesktopShellState } from '@/desktop/tabs'
import DesktopShell from './DesktopShell.vue'
import DesktopTitleBar from '@/components/layout/DesktopTitleBar.vue'

vi.mock('@/i18n', () => ({ t: (key: string, params?: Record<string, unknown>) => `${key}${params ? JSON.stringify(params) : ''}`, locale: ref('en') }))

const apps: Array<() => void> = []
afterEach(() => { apps.splice(0).forEach(dispose => dispose()); delete window.voxariumWindow })
async function flush() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
function fixture() {
  const controller: DesktopShellController = {
    state: shallowRef<DesktopShellState | null>({ role: 'shell', window: { windowId: 'w', revision: 1, tabIds: [], activeTabId: null, insets: { top: 44, left: 0, right: 0, bottom: 0 } }, tabs: [] }),
    pending: ref([]), error: ref(null), menuOpen: ref(false), recent: ref([{ root: 'D:/Old', name: 'Old', exists: false, initialised: true, opened_at: '', task_counts: null }]),
    listing: ref(null), selected: ref(null), picker: ref({ available: true, reason: '', home: '', default_start: '' }), busy: ref(false),
    start: vi.fn(async () => {}), dispose: vi.fn(), activate: vi.fn(async () => {}),
    setMenuOpen: vi.fn(async (open: boolean) => { controller.menuOpen.value = open }),
    openProject: vi.fn(async () => {}), refreshRecent: vi.fn(async () => {}),
    inspect: vi.fn(async (path: string) => { controller.selected.value = { path, name: 'Old', exists: true, is_dir: true, is_project: false } }),
    browse: vi.fn(async () => {}), pick: vi.fn(async () => {}), forgetRecent: vi.fn(async () => {}),
  }
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp(DesktopShell, { controller })
  app.mount(host)
  apps.push(() => { app.unmount(); host.remove() })
  return { controller, host, open: host.querySelector<HTMLButtonElement>('[aria-haspopup="dialog"]')! }
}

describe('application-only desktop shell', () => {
  it('keeps window buttons accessible and accepts the legacy project switcher only as a parent slot', async () => {
    const nativeState = { maximized: false, focused: true }
    const unsubscribe = vi.fn()
    window.voxariumWindow = {
      getState: vi.fn(async () => nativeState), minimize: vi.fn(async () => nativeState),
      toggleMaximize: vi.fn(async () => nativeState), close: vi.fn(async () => null),
      onStateChange: vi.fn(() => unsubscribe),
    }
    const host = document.createElement('div')
    const app = createApp({ render: () => h(DesktopTitleBar, {}, { project: () => h('button', { type: 'button' }, 'Legacy switcher') }) })
    app.mount(host)
    try {
      await flush()
      expect(host.querySelector('.desktop-titlebar__project')?.textContent).toBe('Legacy switcher')
      const minimize = host.querySelector<HTMLButtonElement>('button[aria-label="desktop.minimize"]')!
      expect(minimize.type).toBe('button')
      minimize.click()
      expect(window.voxariumWindow.minimize).toHaveBeenCalledOnce()
    } finally { app.unmount() }
    expect(unsubscribe).toHaveBeenCalledOnce()
  })
  it('has no transitive runtime dependency on project stores, sessions, APIs, sockets or plugins', () => {
    const root = dirname(fileURLToPath(import.meta.url))
    const visited = new Set<string>()
    function visit(file: string) {
      if (visited.has(file)) return
      visited.add(file)
      const source = readFileSync(file, 'utf8')
      const imports = source.matchAll(/\bimport\s+(?!type\b)(?:[\w*{},\s]+?\s+from\s+)?['"]([^'"]+)['"]/g)
      for (const [, specifier] of imports) {
        if (!specifier?.startsWith('@/') && !specifier?.startsWith('.')) continue
        const base = specifier.startsWith('@/') ? resolve(root, specifier.slice(2)) : resolve(dirname(file), specifier)
        expect(base.replaceAll('\\', '/')).not.toMatch(/\/(?:stores|session|ws|plugin-runtime)\/|\/api\/(?:client|project-scope|auth)(?:\.|$)/)
        const target = [base, `${base}.ts`, `${base}.vue`, resolve(base, 'index.ts')].find(path => existsSync(path) && /\.(?:ts|vue|json)$/.test(path))
        if (target && !target.endsWith('.d.ts') && !target.endsWith('.json')) visit(target)
      }
    }
    visit(resolve(root, 'DesktopShell.vue'))
    expect([...visited].some(file => file.endsWith('DesktopTitleBar.vue'))).toBe(true)
    expect([...visited].some(file => file.endsWith('tabs.ts'))).toBe(true)
  })
  it('starts and disposes the injected controller and renders empty/error/pending feedback', async () => {
    const { controller, host } = fixture()
    expect(controller.start).toHaveBeenCalledOnce()
    expect(host.textContent).toContain('desktop.tabs.empty')
    controller.error.value = 'Denied'
    controller.pending.value = ['D:/Project']
    await flush()
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('Denied')
    expect(host.querySelector('[role="status"]')?.textContent).toContain('desktop.tabs.pending')
    apps.pop()!()
    expect(controller.dispose).toHaveBeenCalledOnce()
  })

  it('waits for native menu reservation, then handles Escape/outside close and focus restoration', async () => {
    const { controller, host, open } = fixture()
    let release!: () => void
    controller.setMenuOpen = vi.fn(async value => { await new Promise<void>(resolve => { release = resolve }); controller.menuOpen.value = value })
    open.focus(); open.click()
    await nextTick()
    expect(host.querySelector('[role="dialog"]')).toBeNull()
    release(); await flush()
    expect(host.querySelector('[role="dialog"]')).not.toBeNull()
    controller.setMenuOpen = vi.fn(async value => { controller.menuOpen.value = value })
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
    expect(host.querySelector('[role="dialog"]')).toBeNull()
    expect(controller.setMenuOpen).toHaveBeenCalledWith(false)
    expect(document.activeElement).toBe(open)
    open.click(); await flush()
    host.querySelector('.desktop-shell__backdrop')?.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    await flush()
    expect(host.querySelector('[role="dialog"]')).toBeNull()
  })

  it('inspects stale recent paths and requires an explicit Create and open click', async () => {
    const { controller, host, open } = fixture()
    open.click(); await flush()
    const recent = Array.from(host.querySelectorAll<HTMLButtonElement>('button')).find(button => button.title === 'D:/Old')!
    recent.click(); await flush()
    expect(controller.inspect).toHaveBeenCalledWith('D:/Old')
    expect(controller.openProject).not.toHaveBeenCalled()
    const create = Array.from(host.querySelectorAll<HTMLButtonElement>('button')).find(button => button.textContent === 'project.open.createAndOpen')!
    expect(create.disabled).toBe(false)
    create.click(); await flush()
    expect(controller.openProject).toHaveBeenCalledWith('D:/Old', true)
  })

  it('keeps the menu visible and retryable when native close is rejected', async () => {
    const { controller, host, open } = fixture()
    open.click(); await flush()
    controller.setMenuOpen = vi.fn(async () => { controller.error.value = 'stale revision' })
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
    expect(host.querySelector('[role="dialog"]')).not.toBeNull()
    expect(host.textContent).toContain('stale revision')
    controller.setMenuOpen = vi.fn(async value => { controller.menuOpen.value = value })
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flush()
    expect(host.querySelector('[role="dialog"]')).toBeNull()
  })

  it('does not create missing paths and opens inspected existing projects without create', async () => {
    const { controller, host, open } = fixture()
    open.click(); await flush()
    controller.inspect = vi.fn(async path => { controller.selected.value = { path, name: 'Old', exists: false, is_dir: false, is_project: false } })
    const recent = host.querySelector<HTMLButtonElement>('button[title="D:/Old"]')!
    recent.click(); await flush()
    expect(controller.openProject).not.toHaveBeenCalled()
    expect(host.querySelector<HTMLButtonElement>('footer button')?.disabled).toBe(true)
    controller.inspect = vi.fn(async path => { controller.selected.value = { path, name: 'Old', exists: true, is_dir: true, is_project: true } })
    recent.click(); await flush()
    expect(controller.openProject).toHaveBeenCalledWith('D:/Old', false)
  })

  it('routes manual inspection, browser navigation, picker and recent removal through the adapter', async () => {
    const { controller, host, open } = fixture()
    open.click(); await flush()
    const input = host.querySelector<HTMLInputElement>('input:not([type="checkbox"])')!
    input.value = 'D:/Typed'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await flush()
    expect(controller.inspect).toHaveBeenCalledWith('D:/Typed')
    controller.listing.value = { path: 'D:/', name: 'Drive', parent: '', is_project: false, entries: [{ path: 'D:/Folder', name: 'Folder', hidden: false, is_project: false }], truncated: false }
    await flush()
    const button = (label: string) => Array.from(host.querySelectorAll<HTMLButtonElement>('button')).find(node => node.textContent === label)!
    button('Folder ').click()
    expect(controller.browse).toHaveBeenCalledWith('D:/Folder')
    button('project.open.browseComputer').click()
    expect(controller.pick).toHaveBeenCalledOnce()
    button('project.open.forget').click()
    expect(controller.forgetRecent).toHaveBeenCalledWith('D:/Old')
  })
})
