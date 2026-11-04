/**
 * How much of a task's transcript is on screen, and how it is grouped (spec §10.4).
 *
 * One dial with three detents rather than a checkbox, because there were always three
 * readings of a run and only two settings for them. **Compact** is someone reading what
 * happened: a block per thing that was *said* or *written*, with the tool calls between
 * them folded away. **Advanced** is the feed itself, a row per message. **Verbose** adds the
 * bulky half back: tool results and the Studio's own system notices.
 *
 * Below Verbose, one line at the bottom carries the tail of the *whole* feed — the newest
 * thing logged, whether or not this level draws a row for it, flattened to one line and
 * flashed. It is a window onto Verbose rather than an entry in the log: nothing about it is
 * kept, and turning the dial up is what puts the same lines in the transcript for good.
 *
 * The levels nest — compact ⊂ advanced ⊂ verbose — so turning the dial up never takes
 * anything off the screen. Nothing here touches the DOM: what a level shows is a property
 * of the messages, and is checked as one.
 */

import type { AgentMessage } from '@/api/types'
import { t, type MessageKey } from '@/i18n'

/**
 * What every function here reads of a message. `AgentMessage` minus the task it belongs
 * to, so a chat's transcript (`AgentChatMessage`, keyed by chat) goes through the same
 * rows, the same tail and the same fold as a task's — one rule for both feeds.
 */
export type TranscriptMessage = Omit<AgentMessage, 'task_id'>
import { oneLine, toolName, toolSubject } from '@/lib/activity'
import { statusLabel } from '@/lib/status'
import { subagentActivity } from '@/lib/subagents'
import { resultTypeOf } from '@/lib/task-result'

/** Current telemetry replaces its row; ordinary socket replay never reorders history (§10.4). */
export function upsertTranscript<M extends TranscriptMessage>(messages: M[], message: M): M[] {
  const previous = messages.find((entry) => entry.id === message.id)
  const timestamp = (value: string) => value.replace(/(?:Z|\+00:00)$/, '')
  if (previous && (!message.id.startsWith('state_') ||
    timestamp(previous.created_at) >= timestamp(message.created_at))) return messages
  return [...messages.filter((entry) => entry.id !== message.id), message]
}

/** Quietest first — the order the picker offers them in, and the order they nest in. */
export const VERBOSITY_LEVELS = ['compact', 'advanced', 'verbose'] as const

export type Verbosity = (typeof VERBOSITY_LEVELS)[number]

/**
 * The picker's own words. Spelled out rather than capitalised in CSS: the option list is
 * drawn by the platform, which is not obliged to apply `text-transform` inside it.
 *
 * Message keys rather than the words themselves, resolved with `t()` where the picker renders,
 * so the options follow the interface language instead of the one this module loaded in.
 */
export const VERBOSITY_LABELS: Record<Verbosity, MessageKey> = {
  compact: 'taskDetail.verbosity.level.compact',
  advanced: 'taskDetail.verbosity.level.advanced',
  verbose: 'taskDetail.verbosity.level.verbose',
}

/** What each level answers, for the picker's tooltip — keys, like the labels. */
export const VERBOSITY_HINTS: Record<Verbosity, MessageKey> = {
  compact: 'taskDetail.verbosity.hint.compact',
  advanced: 'taskDetail.verbosity.hint.advanced',
  verbose: 'taskDetail.verbosity.hint.verbose',
}

/** Guards a remembered choice: a stored level from an older build may no longer exist. */
export function isVerbosity(value: unknown): value is Verbosity {
  return VERBOSITY_LEVELS.includes(value as Verbosity)
}

/**
 * History entries no level hides — they are the task's audit trail, and it carries
 * `role: "system"` like the noise does (§10.4).
 */
const HISTORY_KINDS = new Set([
  'status', 'edited', 'commit', 'file_observation_status', 'file_observation_error',
])

/**
 * The history plus the tool calls, and `continued` with them.
 *
 * A tool call is what the agent actually *did*: hidden, a run that spent ten minutes
 * reading and writing files looked like a session that went quiet after its last sentence.
 * `continued` is here for a sharper reason — it is the line that explains the empty
 * `result` beside it when a session stopped mid-work and the Studio resumed it (§10.3).
 */
const ALWAYS_KINDS = new Set([...HISTORY_KINDS, 'tool_use', 'continued'])

/** Rows drawn as a timeline line rather than a card: nobody is speaking in them. */
export const TIMELINE_KINDS = new Set([...HISTORY_KINDS, 'continued'])

/**
 * Entries whose text is *prose*, and therefore Markdown — an agent writes headings, lists
 * and fenced code into most answers, and reading the asterisks was worse than reading the
 * sentences plainly.
 *
 * Everything else keeps `whitespace-pre-wrap`: a tool result and a traceback are payloads,
 * where a `*` is a character and column alignment carries meaning Markdown would eat.
 */
export const PROSE_KINDS = new Set(['text', 'thinking', 'result', 'note', 'summary', 'review', 'context_summary', 'compressed summary'])

/**
 * What opens a block in Compact: someone speaking in their own words.
 *
 * `review` and `summary` are on the list for a reason beyond symmetry with `note` — a level
 * that did not lead with them would leave a person's words nowhere in the transcript but
 * the tail, which is one line and keeps nothing.
 *
 * A file edit used to open a block too (with its `+4 −1` beside the path), and since
 * 2026-10-21 it folds away with the reads around it: the changed-files column beside the
 * transcript lists every file a task wrote *with* those counts, so the row in the feed
 * said the same thing a second time, once per edit, between the sentences a reader at
 * Compact came for. Advanced still draws each one.
 */
const LEAD_KINDS = new Set(['text', 'note', 'review', 'summary', 'context_summary', 'compressed summary'])

/**
 * Tools whose call means "this file now differs" — the same set the changes column matches
 * on (`teams/changes.py`), so the two agree about what counts as writing. Matched on the
 * last segment of the name, which is what makes an MCP tool recognisable as itself.
 */
const WRITING_TOOLS = new Set(['edit', 'write', 'multiedit', 'notebookedit', 'write_doc'])

/** Lines a writing call added and removed, as the backend counted them at capture time. */
export interface EditStat {
  insertions: number
  deletions: number
}

/** One row of the transcript, at whichever level is showing. */
export interface TranscriptRow<M extends TranscriptMessage = TranscriptMessage> {
  key: string
  message: M
}

/**
 * The last line of the transcript: what was logged most recently, and what the run has
 * produced.
 *
 * One line, at the bottom — which is the whole difference from the lines Compact used to
 * leave folded onto every block. Left behind on each one they were the tool feed again,
 * which is what Advanced is for, and they read as history: "Bash · npm test" still sitting
 * under a block the run walked away from ten minutes ago, indistinguishable from the line
 * that is actually live.
 *
 * `activity` is the tail of everything Verbose would log, and is empty *at* Verbose, where
 * the newest row is already the newest line and a copy of it under the scroller would say
 * nothing. `tokens` is every level's: what the model has *written* across this task — its
 * thinking, its answers and its tool calls. The header separately shows lifetime spending,
 * including input and cached input, as the runtime reports it.
 * `seq` counts the lines that have gone by, so the view can key the element on it: a CSS
 * animation restarts when its element does, which is what turns a changing line into a
 * blinking one, and counting rather than comparing is what makes the same tool twice blink
 * twice.
 */
export interface LiveLine {
  activity: string
  seq: number
  tokens: number
}

/** The tool's own name, MCP server stripped — mirrors `tool_name` in `teams/changes.py`. */
export function bareToolName(message: TranscriptMessage): string {
  const raw = typeof message.data?.name === 'string' ? message.data.name : message.text
  if (!raw) return ''
  return raw.startsWith('mcp__') ? (raw.split('__').pop() ?? '') : raw
}

/** Whether this call wrote a file, whatever it managed to say about how much. */
export function isFileEdit(message: TranscriptMessage): boolean {
  return message.kind === 'tool_use' && WRITING_TOOLS.has(bareToolName(message).toLowerCase())
}

/**
 * Whether this call went to an MCP server — `mcp__<server>__<tool>`, as the runtime names
 * them. Compact draws these as rows of their own: a call to the Studio's board or to a
 * project's own server is a step a reader follows, where a `Read` between two sentences
 * is not. The one exception is a write (`write_doc`), which is a file edit first and
 * folds like one.
 */
export function isMcpCall(message: TranscriptMessage): boolean {
  if (message.kind !== 'tool_use') return false
  const raw = typeof message.data?.name === 'string' ? message.data.name : message.text
  return typeof raw === 'string' && raw.startsWith('mcp__')
}

/**
 * Whether this call loaded a skill — the runtime's `Skill` tool, `voxarium_studio:studio` or one of
 * the project's own. Compact draws it as a row, as it does an MCP call: which instructions the
 * agent took up is a step the reader follows, and folded into the tail it was gone as soon as
 * the next `Read` landed.
 */
export function isSkillCall(message: TranscriptMessage): boolean {
  return message.kind === 'tool_use' && bareToolName(message) === 'Skill'
}

/**
 * What the call said it added and removed, or `null` when nobody counted.
 *
 * Read, never derived: the transcript stores each tool argument clipped to 500 characters,
 * so the two sides of an edit are not here to be diffed — `edit_stat` in
 * `teams/changes.py` does it at capture time, where the whole input still exists. A
 * transcript written before that existed therefore shows the edit and no figures, which is
 * the honest answer rather than a guess made from half a string.
 */
export function editStat(message: TranscriptMessage): EditStat | null {
  if (message.kind !== 'tool_use') return null
  const stat = message.data?.edit
  if (!stat || typeof stat !== 'object') return null
  const { insertions, deletions } = stat as Record<string, unknown>
  if (typeof insertions !== 'number' || typeof deletions !== 'number') return null
  return { insertions, deletions }
}

/**
 * Tokens the model produced on this turn, or 0 for a turn that reported none.
 *
 * The runtime's `usage` is attached to **one** event per assistant message
 * (`_attach_usage` in `teams/backends/claude_code.py`), which is what makes summing these
 * right: a message that answered in prose and then called two tools is one API response
 * with one accounting. A result carries the segment's total and is reconciled against
 * those messages by liveLine rather than added a second time.
 *
 * `output_tokens` is the half that counts the thinking. The prompt and the cache reads are
 * in the task header's lifetime total, and they dwarf this one without saying anything
 * about what the agent has been doing — a long session is mostly cache reads.
 */
function outputTokens(message: TranscriptMessage): number {
  if (message.kind === 'system' && message.text === 'usage_update') return 0
  const usage = message.data?.usage
  if (!usage || typeof usage !== 'object') return 0
  const value = (usage as Record<string, unknown>).output_tokens
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : 0
}

/** Whether a message survives this level. Compact and Advanced hide the same things. */
function shows(message: TranscriptMessage, verbosity: Verbosity): boolean {
  if (message.kind === 'system' && ['thinking_tokens', 'usage_update'].includes(message.text)) return false
  if (verbosity === 'verbose') return true
  return ALWAYS_KINDS.has(message.kind) || message.role !== 'system'
}

/** Whether this message starts a block of its own in Compact. */
function opensBlock(message: TranscriptMessage): boolean {
  return (
    LEAD_KINDS.has(message.kind) ||
    isSkillCall(message) ||
    (isMcpCall(message) && !isFileEdit(message))
  )
}

/**
 * The transcript as the chosen level renders it.
 *
 * Above Compact this is the filtered feed, one row per message and nothing folded. Compact
 * keeps only what is worth a block of its own — something said, a call to an MCP server, a
 * skill loaded, or an entry in the task's own history — and the work between those, file
 * edits included, is
 * drawn nowhere here: it becomes the single line `liveLine` puts at the bottom of the
 * transcript.
 */
export function transcriptRows<M extends TranscriptMessage>(
  messages: M[],
  verbosity: Verbosity,
): TranscriptRow<M>[] {
  const shown = closingSummaries(messages).filter((message) => shows(message, verbosity))
  const rows =
    verbosity === 'compact'
      ? shown.filter((message) => TIMELINE_KINDS.has(message.kind) || opensBlock(message))
      : shown
  return rows.map((message) => ({ key: message.id, message }))
}

/**
 * §10.4: finish_task reports the type before the runtime emits the closing reply.
 * Give that reply the summary styling, retaining the detailed report as ordinary prose.
 * This is a display projection, including for old runs; the stored audit trail is intact.
 * Never carry a result type across a run boundary or another person's contribution.
 */
function closingSummaries<M extends TranscriptMessage>(messages: M[]): M[] {
  const projected = [...messages]
  const omitted = new Set<number>()
  let report = -1
  let closing = -1
  const flush = () => {
    if (report >= 0 && closing >= 0) {
      const original = messages[report]!
      const last = messages[closing]!
      if (original.text.trim() === last.text.trim()) omitted.add(report)
      else projected[report] = { ...original, kind: 'text' }
      projected[closing] = {
        ...last,
        kind: 'summary',
        data: { ...last.data, result_type: original.data?.result_type },
      }
    }
    report = closing = -1
  }
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index]!
    if (message.kind === 'summary') {
      flush()
      if (message.role === 'assistant' && resultTypeOf(message)) report = index
    } else if (
      message.kind === 'status' || message.kind === 'continued' ||
      message.kind === 'result' || message.kind === 'error' ||
      message.kind === 'cancelled' || message.kind === 'result_missing' ||
      message.kind === 'note' || message.kind === 'review' ||
      message.role === 'user' || message.role === 'human' ||
      (report >= 0 && message.kind === 'text' &&
        message.member_id !== messages[report]!.member_id)
    ) {
      flush()
    } else if (report >= 0 && message.member_id === messages[report]!.member_id) {
      if (message.kind === 'text' && message.role === 'assistant' && message.text.trim()) {
        closing = index
      } else if (message.kind === 'tool_use') {
        // A reply followed by more work is commentary, not the closing summary.
        closing = -1
      }
    }
  }
  flush()
  return projected.filter((_, index) => !omitted.has(index))
}

/** As long as the board card's line: past this nobody is reading, they are glancing. */
const TAIL_LIMIT = 160

/**
 * Any message as one line, whatever the level would make of it.
 *
 * Nothing is silent here: a `tool_result`, a person's note and a `system` frame each get a
 * line, because the whole reason to look at the tail is to see what the level is *not*
 * showing. A tool call reads as its own row does, name and subject; a lifecycle frame gets
 * `subagentActivity`'s sentence rather than the bare subtype it carries as text; everything
 * else is its text.
 *
 * The board card shows the same line for a running task (the store's `activity`), one
 * message at a time off the socket, so a card and the open transcript never disagree about
 * what just happened.
 *
 * Flattened and clipped here rather than in CSS alone. A tool result is many lines and a
 * traceback is more, and both would push the token count beside them out of the box before
 * `truncate` ever saw a line to truncate.
 */
export function tailLine(message: TranscriptMessage): string {
  if (message.kind === 'system' && message.text === 'usage_update') return ''
  // History is worded from its data, as its row is — the stored text is the backend's English.
  if (message.kind === 'status') return oneLine(statusText(message), TAIL_LIMIT)
  if (message.kind === 'edited') return oneLine(editedText(message), TAIL_LIMIT)
  if (message.kind === 'commit') return oneLine(commitText(message), TAIL_LIMIT)
  if (message.kind === 'tool_use') {
    return [toolName(message), toolSubject(message)].filter(Boolean).join(' · ')
  }
  if (message.kind === 'system') {
    return oneLine(subagentActivity(message) || message.text, TAIL_LIMIT)
  }
  return oneLine(message.text, TAIL_LIMIT)
}

/**
 * The one line under the rows: the last thing logged, and the tokens behind it.
 *
 * The tail is taken over the **whole** feed, not over the rows this level draws — that is
 * what makes it worth having below Verbose, where the messages it names are the ones the
 * level is hiding: the result of the call that just ran, the frame from a sub-agent nobody
 * asked to see. It follows the log and never the blocks, so it does not go blank when the
 * agent speaks; on a live session it is whatever arrived last, and on a finished one it is
 * how the run ended. Nothing here is kept — turning the dial to Verbose is what puts these
 * lines in the transcript as rows, and at Verbose the tail is empty for exactly that reason.
 *
 * Tokens are counted at every level and over every message, since nothing about a level
 * changes what a run has cost.
 */
export function liveLine(messages: TranscriptMessage[], verbosity: Verbosity): LiveLine {
  const line: LiveLine = { activity: '', seq: 0, tokens: 0 }
  let thinking = 0
  let segmentOutput = 0
  for (const message of messages) {
    const output = outputTokens(message)
    if (message.kind === 'result') {
      // Each result closes one segment, including continued results. Its aggregate
      // fills gaps in older transcripts without charging the streamed output again.
      line.tokens += Math.max(segmentOutput, output)
      segmentOutput = 0
    } else if (message.role === 'assistant' ||
      (message.kind === 'system' && message.text === 'turn.completed')) {
      // Codex reports turn deltas here; Claude/Qwen attach one delta per assistant reply.
      segmentOutput += output
    }
    if (output > 0 || message.kind === 'result') thinking = 0
    if (message.kind === 'system' && message.text === 'thinking_tokens') {
      const estimate = message.data?.estimated_tokens
      thinking = typeof estimate === 'number' && Number.isFinite(estimate) ? Math.max(0, estimate) : 0
      continue
    }
    if (verbosity === 'verbose') continue
    const text = tailLine(message)
    if (!text) continue
    line.activity = text
    line.seq += 1
  }
  line.tokens += segmentOutput + thinking
  return line
}

/**
 * The reasons `_set_task_status` records, as words in the interface language. It is a closed
 * set the backend writes, not prose; a reason this table does not know is shown as it came.
 */
const HISTORY_REASONS = new Map<string, MessageKey>([
  ['approved', 'taskDetail.history.reason.approved'],
  ['changes requested', 'taskDetail.history.reason.changesRequested'],
  ['claimed', 'taskDetail.history.reason.claimed'],
  ['reset', 'taskDetail.history.reason.reset'],
  ['submitted', 'taskDetail.history.reason.submitted'],
  ['usage limit reset', 'taskDetail.history.reason.usageLimitReset'],
])

export function historyReason(reason: string): string {
  const key = HISTORY_REASONS.get(reason)
  return key ? t(key) : reason
}

/**
 * An edit entry's sentence. The backend stores it as "Edited " and the changed field names,
 * sorted (`_record_edit`), and keeps the same names in `data.fields` — so the sentence is
 * rebuilt from those in the interface language. The names themselves are the API's and stay
 * as they are; an entry without them keeps the text it was stored with.
 */
export function editedText(message: TranscriptMessage): string {
  const fields = message.data?.fields
  if (!fields || typeof fields !== 'object' || Array.isArray(fields)) return message.text
  const names = Object.keys(fields).sort()
  return names.length ? t('taskDetail.history.edited', { fields: names.join(', ') }) : message.text
}

/**
 * A commit entry as one line — `Committed 3 files as a1b2c3d on main` — worded from the
 * data the Commit button stored (§10.4). An entry without it keeps its stored text.
 */
export function commitText(message: TranscriptMessage): string {
  const files = message.data?.files
  if (!Array.isArray(files)) return message.text
  const revision = typeof message.data?.revision === 'string' ? message.data.revision : ''
  const branch = typeof message.data?.branch === 'string' ? message.data.branch : ''
  const line = t('taskDetail.history.committed', { count: files.length })
  const where = [revision, branch].filter(Boolean).join(' · ')
  return where ? `${line} · ${where}` : line
}

/**
 * A status entry as one line — `In progress → Needs review · submitted`, or `Todo · created`
 * for the entry a task is born with — worded from `data` the way the dialog's row words it.
 * An entry without that data keeps the text it was stored with.
 */
export function statusText(message: TranscriptMessage): string {
  const to = message.data?.to
  if (typeof to !== 'string') return message.text
  const from = message.data?.from
  if (typeof from !== 'string') return `${statusLabel(to)} · ${t('taskDetail.history.created')}`
  const reason = message.data?.reason
  const line = `${statusLabel(from)} → ${statusLabel(to)}`
  return typeof reason === 'string' && reason ? `${line} · ${historyReason(reason)}` : line
}
