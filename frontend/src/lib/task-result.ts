/**
 * How a task's work ended, as said by whoever did it when the card reached review (§10.4).
 *
 * Three answers, one colour each, so a reviewer's queue is read by colour before the words:
 * `done` is green (approve it), `question` is blue (decide something), `error` is red (it
 * could not be done). The vocabulary is the backend's `TaskResultType`; a value this build
 * does not know is drawn as nothing rather than as a guess.
 */
import type { TaskResultType } from '@/api/types'
import { t } from '@/i18n'
import type { TranscriptMessage } from '@/lib/transcript'

export const RESULT_TYPES: readonly TaskResultType[] = ['done', 'question', 'error']

const RESULT_TOKENS: Record<TaskResultType, string> = {
  done: '--vxs-success',
  question: '--vxs-info',
  error: '--vxs-danger',
}

export function isResultType(value: unknown): value is TaskResultType {
  return typeof value === 'string' && (RESULT_TYPES as readonly string[]).includes(value)
}

/** The colour a result type is drawn in — a `var()` the status colours already resolve. */
export function resultTypeColor(type: TaskResultType): string {
  return `var(${RESULT_TOKENS[type]})`
}

export function resultTypeLabel(type: TaskResultType): string {
  return t(`resultType.${type}`)
}

/** One line under the choice, for the form a person submits with. */
export function resultTypeHint(type: TaskResultType): string {
  return t(`resultType.hint.${type}`)
}

/**
 * The type a transcript entry carries — a `summary` written by `finish_task` or by a
 * person's Submit — or null for every other entry, including a summary from before the
 * type existed.
 */
export function resultTypeOf(message: Pick<TranscriptMessage, 'kind' | 'data'>): TaskResultType | null {
  if (message.kind !== 'summary') return null
  const value = message.data?.result_type
  return isResultType(value) ? value : null
}
