/**
 * What each verbosity level puts on the screen, where Compact draws its blocks, and what
 * the one line under them says.
 *
 * The grouping is the part worth pinning: it decides what a reader sees of a run, and it
 * is plain data over a message list, so it can be checked without a transcript to scroll.
 */
import { describe, expect, it } from 'vitest'

import type { AgentMessage } from '@/api/types'
import { editStat, isFileEdit, isVerbosity, liveLine, transcriptRows, upsertTranscript } from '@/lib/transcript'

let sequence = 0

const message = (overrides: Partial<AgentMessage> = {}): AgentMessage => ({
  id: `msg_${(sequence += 1)}`,
  task_id: 'task_1',
  member_id: 'team_default__dev',
  role: 'assistant',
  kind: 'text',
  text: '',
  data: {},
  created_at: '2026-09-04T10:00:00',
  ...overrides,
})

/** The tool calls between two things anyone said — the half Compact folds away. */
const call = (name: string, input: Record<string, unknown> = {}, data = {}) =>
  message({ kind: 'tool_use', text: name, data: { name, input, ...data } })

/** A turn the runtime priced, as `_attach_usage` writes it onto the first of its events. */
const spent = (output: number, overrides: Partial<AgentMessage> = {}) =>
  message({ text: 'Working.', data: { usage: { output_tokens: output } }, ...overrides })

const kinds = (rows: ReturnType<typeof transcriptRows>) => rows.map((row) => row.message.kind)

describe('compressed task context (§10.4)', () => {
  it.each(['compact', 'advanced', 'verbose'] as const)('keeps the handoff visible in %s', (level) => {
    const summary = message({ kind: 'compressed summary', text: 'Completed work. Next steps.' })
    expect(kinds(transcriptRows([summary, message({ kind: 'tool_result', role: 'system' })], level)))
      .toContain('compressed summary')
  })
})

describe('bounded file observation (§10.4)', () => {
  it.each(['compact', 'advanced', 'verbose'] as const)('keeps preparation and incomplete evidence visible in %s', (level) => {
    const feed = [
      message({ role: 'system', kind: 'file_observation_status', text: 'Preparing file observation.' }),
      message({ role: 'system', kind: 'file_observation_status', text: 'Still scanning the project.' }),
      message({ role: 'system', kind: 'file_observation_error', text: 'Scan time limit reached; continuing with tool evidence.' }),
      message({ role: 'system', kind: 'file_observation', data: { paths: ['file.txt'] } }),
    ]
    expect(kinds(transcriptRows(feed, level)).slice(0, 3)).toEqual([
      'file_observation_status', 'file_observation_status', 'file_observation_error',
    ])
    expect(transcriptRows(feed, level).some((row) => row.message.kind === 'file_observation'))
      .toBe(level === 'verbose')
  })
})

describe('closing summary (§10.4)', () => {
  const report = () => message({ kind: 'summary', text: 'Detailed report.', data: { result_type: 'done' } })

  it.each(['compact', 'advanced', 'verbose'] as const)('highlights the closing reply in %s without changing stored messages', (level) => {
    const original = report()
    const closing = message({ text: 'Done. Tests passed.', data: { usage: { output_tokens: 12 } } })
    const feed = [original, message({ kind: 'tool_result', role: 'system' }), closing,
      message({ kind: 'result', role: 'system' }), message({ kind: 'status', role: 'system' })]
    const rows = transcriptRows(feed, level)
    expect(rows.find((row) => row.key === original.id)?.message.kind).toBe('text')
    expect(rows.find((row) => row.key === closing.id)?.message).toEqual({
      ...closing, kind: 'summary', data: { ...closing.data, result_type: 'done' },
    })
    expect(original.kind).toBe('summary')
    expect(closing.kind).toBe('text')
    expect(closing.data).not.toHaveProperty('result_type')
  })

  it('shows an identical report only once', () => {
    expect(kinds(transcriptRows([report(), message({ text: 'Detailed report.' })], 'compact'))).toEqual(['summary'])
  })

  it.each(['status', 'continued', 'result', 'error', 'cancelled', 'result_missing', 'note', 'review'])('does not borrow a closing reply across %s', (kind) => {
    const closing = message({ text: 'A later reply.' })
    const rows = transcriptRows([report(), message({ kind, role: 'system' }), closing], 'verbose')
    expect(rows.at(-1)?.message.kind).toBe('text')
    expect(rows[0]?.message.kind).toBe('summary')
  })

  it('keeps the report when there is no closing reply or work continues after the text', () => {
    const original = report()
    expect(transcriptRows([original], 'compact')[0]?.message).toEqual(original)
    const rows = transcriptRows([original, message({ text: 'Still working.' }), call('Bash')], 'verbose')
    expect(rows[0]?.message.kind).toBe('summary')
    expect(rows[1]?.message.kind).toBe('text')
  })

  it('does not promote another author or a human submission', () => {
    for (const role of ['human', 'user', 'assistant'] as const) {
      const rows = transcriptRows([report(), message({ role, member_id: 'other', text: 'Reply.' })], 'compact')
      expect(kinds(rows)).toEqual(['summary', 'text'])
    }
    expect(kinds(transcriptRows([message({ ...report(), role: 'human' }), message({ text: 'Reply.' })], 'compact'))).toEqual(['summary', 'text'])
  })

  it('uses the last reply and preserves the result type for each run', () => {
    const rows = transcriptRows([
      report(), message({ text: 'More detail.' }), message({ text: 'First done.' }),
      message({ kind: 'status', role: 'system' }),
      message({ ...report(), data: { result_type: 'question' } }), message({ text: 'Which option?' }),
    ], 'compact')
    const summaries = rows.filter((row) => row.message.kind === 'summary')
    expect(summaries.map((row) => [row.message.text, row.message.data?.result_type])).toEqual([
      ['First done.', 'done'], ['Which option?', 'question'],
    ])
  })
})

describe('what a level shows', () => {
  const feed = [
    message({ role: 'system', kind: 'status', text: 'created → todo' }),
    message({ kind: 'text', text: 'Starting.' }),
    call('Bash', { command: 'npm test' }),
    message({ role: 'system', kind: 'tool_result', text: '12 passed' }),
    message({ role: 'system', kind: 'system', text: 'init' }),
  ]

  it('keeps the tool calls and the task’s own history at every level', () => {
    // The audit trail carries `role: "system"` like the noise does, and hiding it would
    // defeat the point of the transcript being the history (§10.4).
    for (const level of ['compact', 'advanced', 'verbose'] as const) {
      expect(kinds(transcriptRows(feed, level))).toContain('status')
    }
    expect(kinds(transcriptRows(feed, 'advanced'))).toEqual([
      'status',
      'text',
      'tool_use',
      /* tool_result and the init frame are the bulky half */
    ])
  })

  it('adds the bulky half rather than rearranging anything', () => {
    expect(kinds(transcriptRows(feed, 'verbose'))).toEqual([
      'status',
      'text',
      'tool_use',
      'tool_result',
      'system',
    ])
  })

  it('carries the tail of the whole feed below Verbose, and nothing at Verbose', () => {
    // The `init` frame is the last thing logged and the last thing both levels below Verbose
    // hide, which is the case the line exists for. At Verbose it is already the last row,
    // and a copy of it under the scroller would say nothing.
    expect(liveLine(feed, 'compact').activity).toBe('init')
    expect(liveLine(feed, 'advanced').activity).toBe('init')
    expect(liveLine(feed, 'verbose').activity).toBe('')
  })

  it('recognises only the three levels', () => {
    expect(isVerbosity('compact')).toBe(true)
    expect(isVerbosity('quiet')).toBe(false)
    expect(isVerbosity(null)).toBe(false)
  })
})

describe('Compact', () => {
  it('opens a block where someone speaks, and draws no row for the work between', () => {
    const feed = [
      message({ kind: 'text', text: 'Looking at the movement code.' }),
      call('Read', { file_path: 'a.ts' }),
      call('Bash', { command: 'npm test' }),
      message({ kind: 'text', text: 'Tests pass.' }),
    ]
    const rows = transcriptRows(feed, 'compact')

    expect(rows).toHaveLength(2)
    expect(rows.map((row) => row.message.text)).toEqual([
      'Looking at the movement code.',
      'Tests pass.',
    ])
    // Nothing of those two calls is left between the blocks — a stale "Bash · npm test"
    // there read as something still happening. The line under the rows follows the log
    // rather than the blocks, so it is the sentence that closed them.
    expect(liveLine(feed, 'compact').activity).toBe('Tests pass.')
  })

  it('folds a file edit away with the calls around it', () => {
    // The changed-files column lists what was written, counts included, so a row per edit
    // in the feed said it twice. A `write_doc` goes through MCP and is still an edit.
    const feed = [
      message({ kind: 'text', text: 'Fixing the cooldown.' }),
      call('Read', { file_path: 'dash.ts' }),
      call('Edit', { file_path: 'dash.ts' }, { edit: { insertions: 4, deletions: 2 } }),
      call('mcp__voxarium_studio__write_doc', { path: 'qa/notes.md' }),
      call('Bash', { command: 'npm test' }),
    ]
    const rows = transcriptRows(feed, 'compact')

    expect(rows).toHaveLength(1)
    expect(rows[0].message.kind).toBe('text')
    // Advanced is untouched: the edit is a row there, as every call is.
    const advanced = transcriptRows(feed, 'advanced')
    expect(advanced.filter((row) => isFileEdit(row.message))).toHaveLength(2)
  })

  it('draws every MCP call as a row, however little it wrote', () => {
    // A call to the board or to a project's own server is a step the reader follows; the
    // built-in reads and shell commands around it still fold onto the tail.
    const rows = transcriptRows(
      [
        message({ kind: 'text', text: 'Checking the board.' }),
        call('Read', { file_path: 'a.ts' }),
        call('mcp__voxarium_studio__list_tasks', { status: 'needs_review' }),
        call('Bash', { command: 'ls' }),
        call('mcp__unreal__import_asset', { path: 'Content/Rock.fbx' }),
      ],
      'compact',
    )

    expect(rows.map((row) => row.message.text)).toEqual([
      'Checking the board.',
      'mcp__voxarium_studio__list_tasks',
      'mcp__unreal__import_asset',
    ])
    expect(transcriptRows([call('mcp__voxarium_studio__list_tasks')], 'advanced')).toHaveLength(1)
  })

  it('draws a skill being loaded as a row, and only the Skill call itself', () => {
    // Which instructions the agent took up is a step the reader follows; the Read of the
    // skill's reference file right after it is ordinary work and still folds away.
    const feed = [
      message({ kind: 'text', text: 'Checking how callouts are written.' }),
      call('Skill', { skill: 'voxarium_studio:studio' }),
      call('Read', { file_path: 'skills/studio/documents.md' }),
    ]
    const rows = transcriptRows(feed, 'compact')

    expect(rows.map((row) => row.message.text)).toEqual([
      'Checking how callouts are written.',
      'Skill',
    ])
    expect(liveLine([feed[1]], 'compact').activity).toBe('Skill · voxarium_studio:studio')
    expect(transcriptRows(feed, 'advanced')).toHaveLength(3)
  })

  it('leads with a person’s words, which no log line would carry', () => {
    // `note` and `review` are silent to `activityLine`, so a level that folded them would
    // drop them from the transcript rather than shorten them.
    const rows = transcriptRows(
      [
        message({ role: 'human', kind: 'note', text: 'Check the edge case.' }),
        message({ role: 'user', kind: 'review', text: 'Not merged yet.' }),
      ],
      'compact',
    )

    expect(rows.map((row) => row.message.text)).toEqual(['Check the edge case.', 'Not merged yet.'])
  })

  it('stands history on its own line', () => {
    const rows = transcriptRows(
      [
        message({ kind: 'text', text: 'Done.' }),
        message({ role: 'system', kind: 'status', text: 'In progress → Needs review' }),
        call('Bash', { command: 'git log' }),
      ],
      'compact',
    )

    expect(kinds(rows)).toEqual(['text', 'status'])
  })

  it('draws no row at all for a run that has only made tool calls', () => {
    // The work is not lost — it is the line under the rows, which is the whole of what
    // Compact says about a session nobody has narrated yet.
    expect(transcriptRows([call('Read', { file_path: 'spec.md' })], 'compact')).toEqual([])
  })
})

describe('the line under the rows', () => {
  it('names the newest thing logged, at both levels that hide something', () => {
    const feed = [
      message({ kind: 'text', text: 'Looking at the movement code.' }),
      call('Read', { file_path: 'a.ts' }),
      call('Bash', { command: 'npm test' }),
    ]

    expect(liveLine(feed, 'compact').activity).toBe('Bash · npm test')
    expect(liveLine(feed, 'advanced').activity).toBe('Bash · npm test')
  })

  it('shows what the level is hiding — which is the whole reason to look at it', () => {
    // The result of the call that just ran is a Verbose row and nothing else, so below
    // Verbose this line is the only place the answer appears at all.
    const feed = [
      call('Bash', { command: 'npm test' }),
      message({ role: 'system', kind: 'tool_result', text: '12 passed' }),
    ]

    expect(liveLine(feed, 'compact').activity).toBe('12 passed')
    expect(liveLine(feed, 'advanced').activity).toBe('12 passed')
    expect(liveLine(feed, 'verbose').activity).toBe('')
  })

  it('is the tail of the log, so a block above it takes nothing away', () => {
    const feed = [
      message({ kind: 'text', text: 'Done.' }),
      message({ role: 'system', kind: 'status', text: 'In progress → Needs review' }),
      call('Bash', { command: 'git log' }),
    ]

    expect(liveLine(feed, 'compact').activity).toBe('Bash · git log')
  })

  it('counts every line, so the same tool twice still blinks twice', () => {
    const feed = [
      message({ kind: 'text', text: 'Running it again.' }),
      call('Bash', { command: 'npm test' }),
      call('Bash', { command: 'npm test' }),
    ]

    // Same words, different count — the view keys its animation on the number, and an
    // element that is not replaced never replays the flash.
    const line = liveLine(feed, 'compact')
    expect(line.activity).toBe('Bash · npm test')
    expect(line.seq).toBe(3)
  })

  it('flattens a payload onto one line and clips it', () => {
    // A tool result is many lines and a traceback is more. Wrapped, they would push the
    // token count beside them out of the box before CSS had a single line to truncate.
    const traceback = `Traceback (most recent call last):\n  File "a.py", line 1\n${'x'.repeat(200)}`
    const activity = liveLine([message({ kind: 'tool_result', text: traceback })], 'compact')
      .activity

    expect(activity).not.toContain('\n')
    expect(activity.startsWith('Traceback (most recent call last): File "a.py", line 1 x')).toBe(
      true,
    )
    expect(activity).toHaveLength(161)
    expect(activity.endsWith('…')).toBe(true)
  })

  it('says nothing about a message that carries no text', () => {
    // A `doc` entry is `text`-bearing and a heartbeat frame is not; an empty line would
    // blank the tail rather than leave the last real one standing.
    const feed = [
      message({ kind: 'doc', text: 'Wrote document qa/notes.md' }),
      message({ role: 'system', kind: 'system', text: '' }),
    ]

    expect(liveLine(feed, 'compact').activity).toBe('Wrote document qa/notes.md')
  })

  it('sums what the model wrote, at every level', () => {
    // One accounting per assistant message, so these add up — a turn that answered and then
    // called two tools is one API response, and the backend puts its usage on one event.
    const feed = [spent(1200), call('Bash', { command: 'npm test' }), spent(340)]

    for (const level of ['compact', 'advanced', 'verbose'] as const) {
      expect(liveLine(feed, level).tokens).toBe(1540)
    }
  })

  it('reconciles streamed assistant output with the result instead of counting it twice', () => {
    const feed = [spent(10), spent(20), spent(30, { role: 'system', kind: 'result' })]
    for (const level of ['compact', 'advanced', 'verbose'] as const) {
      expect(liveLine(feed, level).tokens).toBe(30)
    }
    expect(liveLine([spent(10), spent(40, { role: 'system', kind: 'result' })], 'compact').tokens)
      .toBe(40)
  })

  it('closes continued segments separately before summing their output', () => {
    const feed = [spent(10), spent(20),
      message({ role: 'system', kind: 'result', data: { continued: true, usage: { output_tokens: 30 } } }),
      message({ role: 'system', kind: 'continued' }),
      spent(5), spent(7), spent(12, { role: 'system', kind: 'result' })]
    expect(liveLine(feed, 'compact').tokens).toBe(42)
  })

  it('counts Codex turn deltas once when the run result repeats their total', () => {
    const turn = (output: number) => spent(output, { role: 'system', kind: 'system', text: 'turn.completed' })
    const feed = [turn(10), turn(20)]
    expect(liveLine(feed, 'compact').tokens).toBe(30)
    expect(liveLine([...feed, spent(30, { role: 'system', kind: 'result' })], 'compact').tokens).toBe(30)
  })

  it('retains output totals for older transcripts that only recorded results', () => {
    const feed = [spent(30, { role: 'system', kind: 'result' }),
      spent(12, { role: 'system', kind: 'result' })]
    expect(liveLine(feed, 'compact').tokens).toBe(42)
  })

  it('counts nothing for a transcript written before the backend recorded any', () => {
    // Zero is what makes the view say nothing at all, rather than "0 tokens" about a run
    // whose runtime never reported a figure.
    const feed = [
      message({ kind: 'text', text: 'Working.' }),
      message({ kind: 'text', text: 'Still working.', data: { usage: {} } }),
      message({ kind: 'text', text: 'Done.', data: { usage: { output_tokens: 'lots' } } }),
    ]

    expect(liveLine(feed, 'compact').tokens).toBe(0)
  })

  it('words history from its data, the way its row does, and not from the stored English', () => {
    const status = (data: Record<string, unknown>, text = 'in_progress → needs_review — submitted') =>
      message({ role: 'system', kind: 'status', text, data })
    const edited = (data: Record<string, unknown>) =>
      message({ role: 'system', kind: 'edited', text: 'Edited brief, title', data })

    const line = (entry: AgentMessage) => liveLine([entry], 'compact').activity
    expect(line(status({ from: 'in_progress', to: 'needs_review', reason: 'submitted' }))).toBe(
      'In progress → Needs review · submitted',
    )
    expect(line(status({ from: null, to: 'todo', reason: '' }, 'created → todo'))).toBe('Todo · created')
    expect(line(edited({ fields: { title: {}, brief: {} } }))).toBe('Edited brief, title')
    // An entry from before either carried its data keeps the words it was stored with.
    expect(line(status({}))).toBe('in_progress → needs_review — submitted')
    expect(line(edited({}))).toBe('Edited brief, title')
  })
})

describe('a writing call', () => {
  it('is recognised by its own name, MCP server or not', () => {
    expect(isFileEdit(call('Edit', { file_path: 'a.ts' }))).toBe(true)
    expect(isFileEdit(call('mcp__voxarium_studio__write_doc', { path: 'qa/notes.md' }))).toBe(true)
    expect(isFileEdit(call('Read', { file_path: 'a.ts' }))).toBe(false)
    // A shell command may have written anything at all; claiming it wrote a file is a guess.
    expect(isFileEdit(call('Bash', { command: 'sed -i s/a/b/ a.ts' }))).toBe(false)
  })

  it('reports the size the backend counted, and nothing when nobody counted', () => {
    expect(editStat(call('Edit', {}, { edit: { insertions: 12, deletions: 3 } }))).toEqual({
      insertions: 12,
      deletions: 3,
    })
    // Both sides of an edit reach the transcript clipped to 500 characters, so a figure
    // derived here would be a guess — an older transcript shows the edit and no numbers.
    expect(editStat(call('Edit', { old_string: 'a', new_string: 'b' }))).toBeNull()
    expect(editStat(call('Edit', {}, { edit: { insertions: 12 } }))).toBeNull()
  })
})


it('shows current thinking in the counter, replaces it with final usage, and hides telemetry', () => {
  const prior = message({ data: { usage: { output_tokens: 10 } } })
  const counter = message({ role: 'system', kind: 'system', text: 'thinking_tokens', data: { estimated_tokens: 25 } })
  expect(liveLine([prior, counter], 'advanced').tokens).toBe(35)
  expect(transcriptRows([prior, counter], 'verbose').map((row) => row.message.id)).toEqual([prior.id])
  const answer = message({ data: { usage: { output_tokens: 40 } } })
  expect(liveLine([prior, counter, answer], 'advanced').tokens).toBe(50)
})


it('upserts newer state and ignores old replay without reordering ordinary messages', () => {
  const first = message({ id: 'state_counter', created_at: '2026-10-29T10:00:00', data: { estimated_tokens: 5 } })
  const next = message({ id: first.id, created_at: '2026-10-29T10:00:02', data: { estimated_tokens: 9 } })
  const ordinary = message({ created_at: '2026-10-29T10:00:01' })
  const feed = upsertTranscript([first, ordinary], next)
  expect(feed).toEqual([ordinary, next])
  expect(upsertTranscript(feed, first)).toBe(feed)
  expect(upsertTranscript(feed, ordinary)).toBe(feed)
})
