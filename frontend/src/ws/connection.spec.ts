import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ProjectSession } from '@/session/project-session'
import { WsConnection } from './connection'

vi.mock('@/api/auth', () => ({ withTokenParam: (url: string) => `${url}${url.includes('?') ? '&' : '?'}token=secret` }))
vi.mock('@/i18n', () => ({ t: (key: string) => key }))

class Socket {
  static OPEN = 1
  static all: Socket[] = []
  readyState = 0
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event: { code: number; reason?: string }) => void) | null = null
  onerror: (() => void) | null = null
  sent: Array<Record<string, unknown>> = []
  constructor(readonly url: string) { Socket.all.push(this) }
  send(raw: string): void { this.sent.push(JSON.parse(raw)) }
  open(): void { this.readyState = 1; this.onopen?.() }
  deliver(frame: unknown): void { this.onmessage?.({ data: JSON.stringify(frame) }) }
  drop(code = 1006, reason?: string): void { this.readyState = 3; this.onclose?.({ code, reason }) }
  close(): void { this.drop(1000) }
}

const connections: WsConnection[] = []
function bound(id: string, epoch = 'epoch-1'): ProjectSession {
  const session = new ProjectSession(id)
  session.bind({ project_id: id, epoch })
  return session
}
function connection(session = new ProjectSession()): WsConnection {
  const result = new WsConnection(session)
  connections.push(result)
  return result
}
function frame(session: ProjectSession, type: string, data = {}, seq?: number): object {
  return { type, data, seq, ...session.scope }
}
function hello(socket: Socket, session: ProjectSession, seq = 0, resumed: number | null = null): void {
  socket.deliver(frame(session, 'system.hello', { project: '/project', resumed_from: resumed, ...session.scope }, seq))
}

beforeEach(() => {
  vi.useFakeTimers()
  Socket.all = []
  vi.stubGlobal('WebSocket', Socket)
  vi.stubGlobal('window', {
    location: { host: 'localhost:8765', protocol: 'http:' },
    setTimeout, clearTimeout, setInterval, clearInterval,
  })
})
afterEach(() => {
  connections.splice(0).forEach(value => value.disconnect())
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('immutable project event connections (§5.3.2)', () => {
  it('uses scoped URLs and does not cross A/B or epoch streams', () => {
    const a = bound('A'); const b = bound('B')
    const ca = connection(a); const cb = connection(b)
    const seenA = vi.fn(); const seenB = vi.fn()
    ca.on('team.doc.changed', seenA); cb.on('team.doc.changed', seenB)
    ca.connect(); cb.connect()
    const [sa, sb] = Socket.all as [Socket, Socket]
    expect(new URL(sa.url).pathname).toBe('/ws/projects/A')
    expect(new URL(sa.url).searchParams.get('epoch')).toBe('epoch-1')
    expect(new URL(sa.url).searchParams.get('project_id')).toBe('A')
    expect(new URL(sa.url).searchParams.get('token')).toBe('secret')
    sa.open(); sb.open(); hello(sa, a); hello(sb, b)
    sa.deliver(frame(b, 'team.doc.changed', { path: 'doc.md' }, 500))
    sa.deliver({ ...frame(a, 'team.doc.changed', {}, 501), epoch: 'old' })
    sa.deliver({ type: 'team.doc.changed', data: {}, seq: 502 })
    sa.deliver(frame(a, 'team.doc.changed', { path: 'doc.md' }, 1))
    sb.deliver(frame(b, 'team.doc.changed', { path: 'doc.md' }, 1))
    expect(seenA).toHaveBeenCalledTimes(1); expect(seenB).toHaveBeenCalledTimes(1)
    expect(ca.lastSeq.value).toBe(1); expect(cb.lastSeq.value).toBe(1)
  })

  it('resumes a tuple, including sequence zero, without advancing past queued replay', () => {
    const a = bound('A'); const ca = connection(a); ca.connect()
    const first = Socket.all[0]!
    first.open(); hello(first, a, 0); first.drop()
    vi.advanceTimersByTime(500)
    const second = Socket.all[1]!
    expect(new URL(second.url).searchParams.get('since_seq')).toBe('0')
    second.open(); hello(second, a, 20, 0)
    second.deliver(frame(a, 'team.doc.changed', {}, 1))
    expect(ca.lastSeq.value).toBe(1)
    second.drop(); vi.advanceTimersByTime(500)
    const third = Socket.all[2]!
    expect(new URL(third.url).searchParams.get('since_seq')).toBe('1')
    expect(new URL(third.url).searchParams.get('project_id')).toBe('A')
    expect(new URL(third.url).searchParams.get('epoch')).toBe('epoch-1')
  })

  it('resyncs only its own subscribers and never loops on dropped/resync', () => {
    const a = bound('A'); const b = bound('B')
    const ca = connection(a); const cb = connection(b)
    const resyncA = vi.fn(); const resyncB = vi.fn()
    ca.on('system.dropped', resyncA); cb.on('system.dropped', resyncB)
    ca.connect(); cb.connect()
    const sa = Socket.all[0]!
    sa.open(); hello(sa, a, 20)
    sa.deliver(frame(b, 'system.dropped', { resync: true, count: 1 }))
    sa.deliver(frame(a, 'system.dropped', { resync: true, reason: 'history_gap', count: 0 }))
    sa.deliver(frame(a, 'system.dropped', { resync: true, reason: 'overflow', count: 2 }))
    expect(resyncA).toHaveBeenCalledTimes(2); expect(resyncB).not.toHaveBeenCalled()
    expect(sa.sent).toEqual([])
    sa.deliver(frame(a, 'system.dropped', { resync: false, count: 1 }))
    expect(sa.sent).toEqual([{ action: 'replay', since_seq: 20, ...a.scope }])
  })

  it('ignores old socket callbacks and invalidated generations, including reconnect timers', () => {
    const a = bound('A'); const ca = connection(a); const seen = vi.fn()
    ca.on('team.doc.changed', seen); ca.connect()
    const first = Socket.all[0]!
    first.open(); hello(first, a, 3)
    const lateMessage = first.onmessage!; const lateClose = first.onclose!; const lateOpen = first.onopen!
    ca.disconnect(); ca.connect()
    const second = Socket.all[1]!
    second.open(); hello(second, a, 3, 3)
    lateMessage({ data: JSON.stringify(frame(a, 'team.doc.changed', {}, 999)) })
    lateClose({ code: 1006 }); lateOpen()
    expect(ca.state.value).toBe('open'); expect(ca.lastSeq.value).toBe(3)
    expect(seen).not.toHaveBeenCalled()
    a.invalidate()
    second.deliver(frame(a, 'team.doc.changed', {}, 10))
    second.drop(); vi.advanceTimersByTime(30_000)
    expect(seen).not.toHaveBeenCalled(); expect(Socket.all).toHaveLength(2)

    const b = bound('B'); const cb = connection(b); cb.connect()
    Socket.all[2]!.drop(); b.invalidate(); vi.advanceTimersByTime(10_000)
    expect(Socket.all).toHaveLength(3)
  })

  it('does not fall back for unbound sessions or policy violations', () => {
    expect(() => connection(new ProjectSession('A')).connect()).toThrow()
    expect(Socket.all).toHaveLength(0)
    const ca = connection(bound('A')); ca.connect(); Socket.all[0]!.drop(1008)
    vi.advanceTimersByTime(20_000)
    expect(Socket.all).toHaveLength(1)
    expect(ca.lastError.value).toBe('api.ws.unauthorised')
    const expired = connection(bound('expired')); expired.connect()
    Socket.all[1]!.drop(1008, 'Unknown or expired project id')
    expect(expired.lastError.value).toBe('Unknown or expired project id')
    vi.advanceTimersByTime(20_000)
    expect(Socket.all).toHaveLength(2)
  })

  it('deduplicates scoped replay frames and isolates a throwing subscriber', () => {
    const a = bound('A'); const ca = connection(a); const seen = vi.fn()
    vi.spyOn(console, 'error').mockImplementation(() => {})
    ca.on('team.doc.changed', () => { throw new Error('subscriber') })
    ca.on('team.doc.changed', seen); ca.connect()
    const sa = Socket.all[0]!; sa.open(); hello(sa, a)
    sa.deliver(frame(a, 'team.doc.changed', {}, 1)); sa.deliver(frame(a, 'team.doc.changed', {}, 1))
    expect(seen).toHaveBeenCalledTimes(1)
    vi.mocked(console.error).mockRestore()
  })
})

describe('legacy event adapter compatibility', () => {
  it('keeps the legacy URL, wire frames, restart and dropped replay semantics', () => {
    const legacy = new ProjectSession(); const ca = connection(legacy); ca.connect()
    const socket = Socket.all[0]!
    expect(new URL(socket.url).pathname).toBe('/ws')
    socket.open(); hello(socket, legacy, 40)
    socket.deliver(frame(legacy, 'system.dropped', { count: 3, resync: true }))
    expect(socket.sent).toEqual([{ action: 'replay', since_seq: 40 }])
    socket.drop(); vi.advanceTimersByTime(500)
    const second = Socket.all[1]!
    expect(new URL(second.url).searchParams.get('since_seq')).toBe('40')
    second.open(); hello(second, legacy, 2)
    expect(ca.lastSeq.value).toBe(2)
    vi.advanceTimersByTime(25_000)
    expect(second.sent).toContainEqual({ action: 'ping' })
  })
})
