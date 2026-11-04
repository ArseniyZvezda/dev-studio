/** A task's REST snapshot plus messages received while it was loading (§10.11). */
import type { AgentMessage } from '@/api/types'
import { transcriptRows, upsertTranscript, type TranscriptRow } from '@/lib/transcript'

const timestamp = (message: AgentMessage) => message.created_at.replace(/(?:Z|\+00:00)$/, '')

/** Keep closing-summary projections and expose interrupted runs in the task strip (§10.11). */
export function taskConversationRows(messages: AgentMessage[]): TranscriptRow<AgentMessage>[] {
  const shown = new Map(transcriptRows(messages, 'advanced').map(row => [row.key, row]))
  return messages.flatMap(message => {
    const row = shown.get(message.id)
    if (row) return [row]
    return ['error', 'cancelled'].includes(message.kind) ? [{ key: message.id, message }] : []
  })
}

export function mergeTaskConversation(snapshot: AgentMessage[], incoming: AgentMessage[]): AgentMessage[] {
  const merged = incoming.reduce((messages, message) => upsertTranscript(messages, message), snapshot)
  // Compaction replaces the captured history; neither socket replay nor an older fetch
  // may put it back. The summary has the timestamp of the final replaced entry (§10.4).
  const summary = merged.reduce<AgentMessage | undefined>((latest, message) =>
    ['compressed summary', 'context_summary'].includes(message.kind) &&
    (!latest || timestamp(message) >= timestamp(latest)) ? message : latest, undefined)
  const visible = summary
    ? [summary, ...merged.filter(message => message.id !== summary.id && timestamp(message) > timestamp(summary))]
    : merged
  return [...visible].sort((a, b) => timestamp(a).localeCompare(timestamp(b)))
}
