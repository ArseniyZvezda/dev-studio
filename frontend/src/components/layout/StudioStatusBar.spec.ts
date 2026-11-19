// @vitest-environment jsdom
import { createApp, defineComponent, h, nextTick, reactive } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import StudioStatusBar from './StudioStatusBar.vue'
import { apiConnections } from '@/lib/api-backends'
import en from '@/locales/en.json'
import ru from '@/locales/ru.json'

const mocks = vi.hoisted(() => ({ session: null as any }))
vi.mock('@/stores/session', () => ({ useSessionStore: () => mocks.session }))
vi.mock('@/stores/vcs', () => ({
  useVcsStore: () => ({}),
  vcsIndicator: () => ({ label: 'Git', color: 'green', title: 'Git' }),
}))
vi.mock('@/i18n', () => ({ formatNumber: (value: number, options?: Intl.NumberFormatOptions) => new Intl.NumberFormat('en', options).format(value), t: (key: string, params?: any) => {
  switch (key) {
    case 'nav.agentBackends.resetsIn': return `Resets in: ${params.time}`
    case 'nav.agentBackends.resetDays': return `${params.days}d ${params.hours}h`
    case 'nav.agentBackends.resetHours': return `${params.hours}h ${params.minutes}m`
    case 'nav.agentBackends.resetMinutes': return `${params.minutes}m`
    case 'nav.agentBackends.resetSoon': return '<1m'
    case 'nav.agentBackends.tokenSpeed': return `${params.rate} tok/s`
    case 'nav.agentBackends.tokenSpeedDescription': return `Average output for last response/run: ${params.speed}`
    default: return params ? `${key}: ${params.window ?? ''} ${params.percent ?? ''}` : key
  }
} }))

let unmount: (() => void) | undefined
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-16T12:00:00Z'))
  apiConnections.value = []
  mocks.session = reactive({
    connection: 'open', project: null,
    status: { agent_backends: {
      claude_code: { available: true, reason: 'Ready', usage_limits: [
        { window: 'five_hour', used_percent: 0, resets_at: null },
        { window: 'seven_day', used_percent: 64.7, resets_at: null },
      ] },
      codex: { available: true, reason: 'Ready', usage_limits: [
        { window: 'primary', used_percent: 100, resets_at: null },
      ] },
      qwen: { available: false, reason: 'Not installed' },
      antigravity: { available: true, reason: 'Antigravity CLI installed' },
    } },
    refreshStatus: vi.fn(), refreshTaskCounts: vi.fn(),
  })
})
afterEach(() => { unmount?.(); apiConnections.value = []; vi.useRealTimers() })

function mount() {
  const host = document.createElement('div')
  const app = createApp(StudioStatusBar)
  app.component('RouterLink', defineComponent({ setup: (_, { slots }) => () => h('a', slots.default?.()) }))
  app.mount(host)
  unmount = () => app.unmount()
  return host
}

describe('status bar account limits', () => {
  it('adds a separate reset countdown after each quota window using Unix seconds', () => {
    const now = Date.now() / 1000
    mocks.session.status.agent_backends.claude_code.usage_limits = [
      { window: 'five_hour', used_percent: 20, resets_at: now + 27 * 3600 },
      { window: 'seven_day', used_percent: 40, resets_at: now + 2 * 3600 + 34 * 60 },
    ]
    const host = mount()
    const lines = host.querySelector('.backend-usage')!.parentElement!.title.split('\n')
    const firstWindow = lines.findIndex(line => line.includes('five_hour'))
    const secondWindow = lines.findIndex(line => line.includes('seven_day'))
    expect(firstWindow).toBeGreaterThanOrEqual(0)
    expect(secondWindow).toBeGreaterThan(firstWindow)
    expect(lines[firstWindow + 1]).toBe('Resets in: 1d 3h')
    expect(lines[secondWindow + 1]).toBe('Resets in: 2h 34m')
    expect(host.querySelector('.backend-usage')!.textContent).toBe('80% / 60%')
    expect(host.querySelectorAll('.backend-usage')[1]!.parentElement!.title).not.toContain('Resets in:')
  })

  it.each([
    [5 * 60, '5m'],
    [60, '1m'],
    [59, '<1m'],
    [1, '<1m'],
  ])('formats %s remaining seconds as %s', (seconds, label) => {
    mocks.session.status.agent_backends.codex.usage_limits[0].resets_at = Date.now() / 1000 + seconds
    const host = mount()
    expect(host.querySelectorAll('.backend-usage')[1]!.parentElement!.title.split('\n'))
      .toContain(`Resets in: ${label}`)
  })

  it.each([null, undefined, NaN, Infinity, -Infinity, -1, 0, '1794916800', true, false, 'invalid'])
    ('omits reset countdown for invalid timestamp %s', (resets_at) => {
      mocks.session.status.agent_backends.codex.usage_limits[0].resets_at = resets_at
      const host = mount()
      expect(host.querySelectorAll('.backend-usage')[1]!.parentElement!.title).not.toContain('Resets in:')
    })

  it('updates the countdown on the existing 15-second timer without new telemetry and hides expired windows', async () => {
    mocks.session.status.agent_backends.codex.usage_limits[0].resets_at = Date.now() / 1000 + 70
    const host = mount()
    const usage = host.querySelectorAll('.backend-usage')[1]!
    expect(usage.parentElement!.title).toContain('Resets in: 1m')
    mocks.session.refreshStatus.mockClear()

    vi.advanceTimersByTime(15_000)
    await nextTick()
    expect(mocks.session.refreshStatus).toHaveBeenCalledTimes(1)
    expect(usage.parentElement!.title).toContain('Resets in: <1m')

    vi.advanceTimersByTime(60_000)
    await nextTick()
    expect(usage.parentElement!.title).not.toContain('Resets in:')
    expect(usage.parentElement!.title).not.toContain('windows.primary')
    expect(usage.textContent).toBe('—')
  })

  it('shows muted remaining percentages after each name, including full and exhausted windows', () => {
    const host = mount()
    const usage = [...host.querySelectorAll('.backend-usage')]
    expect(usage.map(node => node.textContent)).toEqual(['100% / 35%', '0%', '—', '—'])
    expect(usage.every(node => node.classList.contains('text-faint'))).toBe(true)
    expect(usage[0]!.parentElement!.textContent).toContain('nav.agentBackends.claude_code100% / 35%')
    expect(usage[0]!.getAttribute('aria-label')).toContain('limitRemaining')
    expect(usage[0]!.getAttribute('aria-label')).toContain('35%')
    expect(usage[0]!.parentElement!.title).toContain('seven_day')
    expect(usage[2]!.getAttribute('aria-label')).toContain('limitsUnknown')
  })

  it('does not show stale usage while disconnected and accepts live updates', async () => {
    const host = mount()
    mocks.session.connection = 'closed'
    await nextTick()
    expect([...host.querySelectorAll('.backend-usage')].map(node => node.textContent)).toEqual(['—', '—', '—', '—'])
    mocks.session.connection = 'open'
    mocks.session.status.agent_backends.codex.usage_limits[0].used_percent = 42
    await nextTick()
    expect(host.querySelectorAll('.backend-usage')[1]!.textContent).toBe('58%')
    vi.advanceTimersByTime(15_000)
    expect(mocks.session.refreshStatus).toHaveBeenCalled()
  })

  it('treats invalid and missing data as unknown rather than zero', async () => {
    mocks.session.status.agent_backends.codex.usage_limits = [
      ...[NaN, Infinity, -1, 101, null, true, '30'].map(used_percent => ({ window: 'primary', used_percent })),
    ]
    const host = mount()
    expect(host.querySelectorAll('.backend-usage')[1]!.textContent).toBe('—')
    mocks.session.status = null
    await nextTick()
    expect([...host.querySelectorAll('.backend-usage')].map(node => node.textContent)).toEqual(['—', '—', '—', '—'])
  })
})

describe('status bar token speed', () => {
  const sample = (rate: unknown, expiry: unknown = Date.now() / 1000 + 60) => ({
    tokens_per_second: rate, expires_at: expiry,
  })

  it('shows an independent rate next to each matching backend, with a descriptive accessible label', () => {
    mocks.session.status.agent_backends.claude_code.token_speed = sample(12.345)
    mocks.session.status.agent_backends.codex.token_speed = sample(0.01234)
    const host = mount()
    const rows = host.querySelectorAll('.status-item')
    expect(rows[0]!.querySelector('.backend-speed')!.textContent).toBe('12.3 tok/s')
    expect(rows[1]!.querySelector('.backend-speed')!.textContent).toBe('0.0123 tok/s')
    expect(rows[2]!.querySelector('.backend-speed')).toBeNull()
    const speed = rows[0]!.querySelector('.backend-speed')!
    expect(speed.getAttribute('aria-label')).toBe('Average output for last response/run: 12.3 tok/s')
    expect(rows[0]!.getAttribute('title')).toContain(speed.getAttribute('aria-label'))
    expect(rows[0]!.querySelector('.backend-usage')!.textContent).toBe('100% / 35%')
    expect(en.nav.agentBackends.tokenSpeedDescription).toMatch(/average output token rate.*last measured response or run.*not quota.*exact model generation speed/i)
    expect(ru.nav.agentBackends.tokenSpeedDescription).toMatch(/средняя скорость выходных токенов.*последний измеренный ответ или запуск.*не квота.*не точная скорость генерации модели/i)
    expect(ru.nav.agentBackends.tokenSpeed).toBe('{rate} ток/с')
  })

  it.each([undefined, null, {}, sample(0), sample(-1), sample(NaN), sample(Infinity),
    sample(-Infinity), sample('12'), sample(true), sample(null),
    { tokens_per_second: 5 }, sample(5, null), sample(5, NaN), sample(5, Infinity),
    sample(5, '1794916860'), sample(5, true), sample(5, 0)])
  ('hides missing or invalid telemetry: %s', (speed) => {
    mocks.session.status.agent_backends.codex.token_speed = speed
    const host = mount()
    expect(host.querySelectorAll('.backend-speed')).toHaveLength(0)
  })

  it('hides expired samples on the refresh clock and responds to replacements and disconnects', async () => {
    const codex = mocks.session.status.agent_backends.codex
    codex.token_speed = sample(3, Date.now() / 1000)
    const host = mount()
    expect(host.querySelector('.backend-speed')).toBeNull()
    codex.token_speed = sample(4, Date.now() / 1000 + 15)
    await nextTick()
    expect(host.querySelector('.backend-speed')!.textContent).toBe('4 tok/s')
    vi.advanceTimersByTime(15_000)
    await nextTick()
    expect(host.querySelector('.backend-speed')).toBeNull()
    codex.token_speed = sample(9)
    await nextTick()
    expect(host.querySelector('.backend-speed')!.textContent).toBe('9 tok/s')
    mocks.session.connection = 'closed'
    await nextTick()
    expect(host.querySelector('.backend-speed')).toBeNull()
    mocks.session.connection = 'open'
    await nextTick()
    expect(host.querySelector('.backend-speed')!.textContent).toBe('9 tok/s')
  })

  it('isolates named API connections from each other and the built-in backend', async () => {
    apiConnections.value = [{ id: 'a', name: 'Provider A' }, { id: 'b', name: 'Provider B' }] as any
    mocks.session.status.agent_backends['openai_api:a'] = { available: true, token_speed: sample(22) }
    mocks.session.status.agent_backends['openai_api:b'] = { available: true, token_speed: sample(33) }
    const host = mount()
    const rows = host.querySelectorAll('.status-item')
    expect(rows[0]!.querySelector('.backend-speed')).toBeNull()
    expect(rows[4]!.textContent).toContain('Provider A')
    expect(rows[4]!.querySelector('.backend-speed')!.textContent).toBe('22 tok/s')
    expect(rows[5]!.querySelector('.backend-speed')!.textContent).toBe('33 tok/s')
    mocks.session.status.agent_backends['openai_api:a'].token_speed = null
    await nextTick()
    expect(rows[4]!.querySelector('.backend-speed')).toBeNull()
    expect(rows[5]!.querySelector('.backend-speed')!.textContent).toBe('33 tok/s')
  })
})
