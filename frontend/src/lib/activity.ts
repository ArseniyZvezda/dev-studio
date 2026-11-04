/**
 * A tool call in one line (spec §10.4).
 *
 * The transcript and the board card ask the same question of a call — which tool, on
 * what — so the answer lives here rather than twice: `TaskDetail` renders it as a timeline
 * row, and `tailLine` (`lib/transcript.ts`) as the last line that both the dialog and a
 * running card show. Being plain data, it is also testable without a DOM.
 */

import type { AgentMessage } from '@/api/types'
import { t } from '@/i18n'

/** The fields a line is built from — an `AgentMessage`, or the event that will become one. */
export type ActivityMessage = Pick<AgentMessage, 'role' | 'kind' | 'text' | 'data'>

/**
 * Which tool was called. MCP tools arrive as `mcp__<server>__<tool>`, and that prefix is
 * the useful half — "which server is this agent talking to" is the question a transcript
 * full of `mcp__voxarium_studio__create_task` cannot answer at a glance. The server name may itself
 * contain `_`, so the split is on the `__` separators, not on the first underscore.
 *
 * A name is data and stays as the runtime sent it; only the word standing in for a missing
 * one is the interface's, and follows its language.
 */
export function toolName(message: ActivityMessage): string {
  const raw = typeof message.data?.name === 'string' ? message.data.name : message.text
  if (!raw) return t('taskDetail.activity.unnamedTool')
  if (!raw.startsWith('mcp__')) return raw
  const [server, ...rest] = raw.slice(5).split('__')
  return rest.length ? `mcp · ${server} · ${rest.join('__')}` : `mcp · ${server}`
}

/** Argument keys that identify a call; the first one present is what the line shows. */
const TOOL_SUBJECT_KEYS = [
  'file_path',
  'path',
  'notebook_path',
  'command',
  'pattern',
  'url',
  'query',
  'title',
  'description',
  'prompt',
  'subagent_type',
  'skill',
  'name',
]

/**
 * One argument, one line. The backend already truncates each value to 500 characters
 * (`_summarise_tool_input`); a transcript line wants far less than that, and newlines in a
 * command would break the row's alignment, so it is flattened as well.
 */
export function toolSubject(message: ActivityMessage): string {
  const input = message.data?.input
  if (!input || typeof input !== 'object') return ''
  const values = input as Record<string, unknown>
  const usable = (key: string) => typeof values[key] === 'string' && values[key] !== ''
  const key = TOOL_SUBJECT_KEYS.find(usable) ?? Object.keys(values).find(usable)
  if (!key) return ''
  return oneLine(values[key], 120)
}

/**
 * One line, however many the value had: a row truncates in CSS, but not across newlines.
 *
 * Exported because the transcript's tail (`lib/transcript.ts`) flattens the same way and for
 * the same reason — it is one row of a flex line, and a payload with newlines in it would
 * otherwise push the tokens beside it off the bottom of the box.
 */
export function oneLine(value: unknown, limit: number): string {
  const text = String(value ?? '')
    .replace(/\s+/g, ' ')
    .trim()
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}
