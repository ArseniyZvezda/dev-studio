/**
 * The one answer the client reads as a stream: a folder rename's progress (block-editor spec
 * §5.2).
 *
 * What is pinned is what a network does to a stream and a JSON parser does not expect — lines
 * cut in half, a character cut in half — and the two ways a rename can fail that a status code
 * alone does not tell apart: refused before anything moved, and stopped after the folder did.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DocFolderRenameFrame } from '@/api/types'
import { setLocale } from '@/i18n'

vi.mock('@/api/auth', () => ({
  authHeaders: () => ({}),
  withTokenParam: (url: string) => url,
}))

const { api, ApiError, readNdjson, renameDocFolder } = await import('./client')

/** A body that arrives in exactly these pieces, the way a network may cut it. */
function streamOf(...chunks: (string | Uint8Array)[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk)
      }
      controller.close()
    },
  })
}

function ndjson(...frames: object[]): string {
  return frames.map((frame) => `${JSON.stringify(frame)}\n`).join('')
}

const RENAMED = {
  type: 'renamed',
  path: 'mechanics',
  previous_path: 'features',
  moved: 2,
  documents: [{ path: 'qa/plan.md', previous_path: null, title: 'Plan', links: 3 }],
}
const DOCUMENT = {
  type: 'document',
  path: 'qa/plan.md',
  previous_path: null,
  title: 'Plan',
  links: 3,
  status: 'updated',
  error: null,
}
const DONE = {
  type: 'done',
  path: 'mechanics',
  previous_path: 'features',
  moved: 2,
  updated: 1,
  links: 3,
  failed: 0,
}

afterEach(async () => {
  vi.unstubAllGlobals()
  await setLocale('en')
})

describe('task summary language (§10.4)', () => {
  it('captures the current reader language when creating, releasing, running or reviewing', async () => {
    const fetcher = vi.fn(async () => new Response('{}', {
      headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetcher)

    for (const language of ['ru', 'en'] as const) {
      await setLocale(language)
      fetcher.mockClear()
      await api.createTask('team', { assignee_id: 'agent', title: 'A task' })
      await api.updateTask('team', 'task', { status: 'pending' })
      await api.startTask('team', 'task')
      await api.startAllTasks('team')
      await api.requestChanges('team', 'task', 'Try again')
      expect(fetcher.mock.calls).toHaveLength(5)
      for (const [, init] of vi.mocked(fetch).mock.calls) {
        expect(JSON.parse(String(init?.body)).summary_locale).toBe(language)
      }
    }
  })

  it('does not change a task language for an unrelated edit', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', {
      headers: { 'Content-Type': 'application/json' },
    })))
    await setLocale('ru')
    await api.updateTask('team', 'task', { title: 'New title' })
    const [, init] = vi.mocked(fetch).mock.calls[0]!
    expect(JSON.parse(String(init?.body))).toEqual({ title: 'New title' })
  })
})

describe('reading newline-delimited JSON', () => {
  it('puts back together a line and a character that arrived in two pieces', async () => {
    const values: unknown[] = []
    const bytes = new TextEncoder().encode('{"name":"Тоа"}\n')
    // Split inside the two-byte `о`, and separately inside the next line.
    await readNdjson(
      streamOf(bytes.slice(0, 12), bytes.slice(12), '{"n":', '1}\n\n{"n":2}'),
      (value) => values.push(value),
    )
    expect(values).toEqual([{ name: 'Тоа' }, { n: 1 }, { n: 2 }])
  })
})

describe('renaming a folder over the stream', () => {
  function answer(response: Response): void {
    vi.stubGlobal('fetch', vi.fn(async () => response))
  }

  it('hands over every frame as it comes and resolves with the last', async () => {
    answer(new Response(streamOf(ndjson(RENAMED), ndjson(DOCUMENT, DONE)), { status: 200 }))
    const heard: DocFolderRenameFrame['type'][] = []

    const done = await renameDocFolder('features', 'mechanics', (frame) => heard.push(frame.type))

    expect(heard).toEqual(['renamed', 'document', 'done'])
    expect(done).toEqual(DONE)
    const [url, init] = vi.mocked(fetch).mock.calls[0]
    expect(url).toBe('/api/docs/folders/rename')
    expect(JSON.parse(String(init?.body))).toEqual({ path: 'features', name: 'mechanics' })
  })

  it('throws the reason, and hands over nothing, when the rename is refused', async () => {
    answer(
      new Response(JSON.stringify({ detail: "'qa' already exists" }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const onFrame = vi.fn()

    const refused = renameDocFolder('features', 'qa', onFrame)

    await expect(refused).rejects.toBeInstanceOf(ApiError)
    await expect(refused).rejects.toMatchObject({ status: 409, detail: "'qa' already exists" })
    expect(onFrame).not.toHaveBeenCalled()
  })

  it('throws when the stream ends without saying the rename is done', async () => {
    answer(new Response(streamOf(ndjson(RENAMED)), { status: 200 }))
    const onFrame = vi.fn()

    await expect(renameDocFolder('features', 'mechanics', onFrame)).rejects.toThrow(
      'stopped before it said it was done',
    )
    expect(onFrame, 'the folder had already moved, and the caller heard so').toHaveBeenCalledTimes(1)
  })

  it('throws what the backend said when mending the links failed', async () => {
    answer(new Response(streamOf(ndjson(RENAMED, { type: 'error', error: 'disk full' })), { status: 200 }))

    await expect(renameDocFolder('features', 'mechanics', () => {})).rejects.toThrow('disk full')
  })
})
