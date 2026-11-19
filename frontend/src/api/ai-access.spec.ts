import { afterEach, describe, expect, it, vi } from 'vitest'
import { aiAccessApi } from './ai-access'

vi.mock('./auth', () => ({ authHeaders: () => ({ Authorization: 'Bearer studio-test-token' }) }))

afterEach(() => vi.unstubAllGlobals())

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })

describe('local Remote AI API', () => {
  it('encodes the target cursor and sends read-only receipt recovery without a resend', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(json({ targets: [], next_after: null })).mockResolvedValueOnce(json({}))
    vi.stubGlobal('fetch', fetch)
    await expect(aiAccessApi.targets('requester', 'page cursor')).resolves.toEqual({ targets: [], next_after: null })
    await aiAccessApi.recover('command', 'epoch')
    expect(fetch).toHaveBeenNthCalledWith(1, '/api/ai-access/registrations/requester/targets?after=page%20cursor',
      expect.objectContaining({ method: 'GET', redirect: 'error', cache: 'no-store' }))
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/ai-access/receipts/command/recover',
      expect.objectContaining({ method: 'POST', body: '{"executor_epoch":"epoch","resend":false}' }))
  })

  it('sends only the displayed draft evidence and distinct export and grant revisions', async () => {
    const fetch = vi.fn().mockImplementation(async () => json({}))
    vi.stubGlobal('fetch', fetch)
    const draft = {
      registration_id: 'executor', profile_id: 'profile', server_instance_id: 'server',
      project_id: 'runtime', project_epoch: 'generation', identity_revision: 7,
      persistent_id: 'catalog-project', storage_epoch: 'storage', display_name: 'Work copy',
    }
    const grant = {
      expected_export_revision: 3, execution_policy_digest: 'digest', team_ids: ['team'], member_ids: ['member'],
      operations: ['view_status'] as 'view_status'[],
    }
    const grantee = { ...grant, grantee_registration_id: 'device', grantee_user_id: 'user' }
    await aiAccessApi.createExport(draft)
    await aiAccessApi.createGrant('export', grantee)
    await aiAccessApi.updateGrant('export', 'grant', { ...grant, expected_revision: 5 })
    expect(fetch.mock.calls.map(([url, init]) => [url, init.method, JSON.parse(init.body)])).toEqual([
      ['/api/ai-access/exports', 'POST', draft],
      ['/api/ai-access/exports/export/grants', 'POST', grantee],
      ['/api/ai-access/exports/export/grants/grant', 'PUT', { ...grant, expected_revision: 5 }],
    ])
  })

  it('refuses a URL dot segment locally instead of changing endpoints', () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    expect(() => aiAccessApi.policy('..', 'team', 'member')).toThrow('invalid_request')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('preserves structured failures without leaking backend error details or retrying a request', async () => {
    const fetch = vi.fn().mockResolvedValue(json({ detail: { code: 'outcome_unknown', extra: 'discard' } }, 503))
    vi.stubGlobal('fetch', fetch)
    await expect(aiAccessApi.recover('command', 'epoch')).rejects.toMatchObject({ status: 503, code: 'outcome_unknown' })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('uses a single authenticated fetch stream and parses fragmented events and heartbeat comments', async () => {
    const bytes = new TextEncoder()
    const stream = new ReadableStream({ start(writer) {
      for (const chunk of [': heartbeat\r\n\r', '\neve', 'nt: remote\r\ndata: {"text":"', 'ок"}\r\n\r\n',
        'event: resync_required\ndata: {}\n\n']) writer.enqueue(bytes.encode(chunk))
      writer.close()
    } })
    const fetch = vi.fn().mockResolvedValue(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }))
    vi.stubGlobal('fetch', fetch)
    const abort = new AbortController(), receive = vi.fn(), ready = vi.fn()
    await aiAccessApi.events(abort.signal, receive, ready)
    expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/ai-access/events', expect.objectContaining({
      signal: abort.signal, redirect: 'error', cache: 'no-store',
      headers: { Authorization: 'Bearer studio-test-token', Accept: 'text/event-stream' },
    }))
    expect(ready).toHaveBeenCalledTimes(1)
    expect(receive.mock.calls).toEqual([[{ kind: 'remote', data: { text: 'ок' } }], [{ kind: 'resync_required', data: {} }]])
  })

  it('rejects invalid UTF-8 without delivering a frame or restarting the stream', async () => {
    const receive = vi.fn()
    const stream = new ReadableStream({ start(writer) {
      writer.enqueue(new Uint8Array([0xff, 0x0a]))
      writer.close()
    } })
    const fetch = vi.fn().mockResolvedValue(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }))
    vi.stubGlobal('fetch', fetch)
    await expect(aiAccessApi.events(new AbortController().signal, receive)).rejects.toMatchObject({ code: 'invalid_response' })
    expect(receive).not.toHaveBeenCalled()
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('releases the stream on abort and refuses a non-SSE response', async () => {
    const cancelled = vi.fn(), abort = new AbortController()
    const response = new Response(new ReadableStream({ cancel: cancelled }), {
      headers: { 'Content-Type': 'text/event-stream' },
    })
    const fetch = vi.fn().mockResolvedValueOnce(response).mockResolvedValueOnce(json({}))
    vi.stubGlobal('fetch', fetch)
    const receive = vi.fn()
    await aiAccessApi.events(abort.signal, receive, () => abort.abort())
    expect(cancelled).toHaveBeenCalledTimes(1)
    expect(receive).not.toHaveBeenCalled()
    await expect(aiAccessApi.events(new AbortController().signal, receive)).rejects.toMatchObject({ code: 'invalid_response' })
    expect(receive).not.toHaveBeenCalled()
  })
})
