/** Producer chat task status and desktop attention (spec §10.11). */
import type { AgentChat, TeamTask } from '@/api/types'

/** The workspace follows the Producer's team, including review and blocked conversations (§10.11). */
export function producerActiveTasks(tasks: TeamTask[], teamId: string): TeamTask[] {
  return tasks.filter(task => task.team_id === teamId && !task.archived && (
    ['pending', 'in_progress', 'needs_review', 'blocked'].includes(task.status) ||
    (task.status === 'todo' && !!task.producer_approved)
  )).sort((a, b) => Number(b.status === 'in_progress') - Number(a.status === 'in_progress'))
}

/** The footer follows the chat's team, not whichever board happens to be selected (§10.11). */
export function producerChatTasks(chat: AgentChat, tasks: TeamTask[], teamId: string) {
  const board = tasks.filter(task => task.team_id === teamId)
  const watched = new Set(chat.producer_watch?.map(task => task.id) ?? [])
  return {
    running: board.filter(task => task.status === 'in_progress'),
    waiting: board.filter(task => watched.has(task.id)),
  }
}

/** Global attention is independent of the visible conversation or unread messages. */
export function producerNeedsReply(chats: AgentChat[]): boolean {
  return chats.some(chat => chat.producer_mode && !chat.producer_paused && !chat.is_running &&
    (chat.producer_state === 'awaiting_user' || chat.producer_state === 'awaiting_approval' ||
      chat.producer_state === 'discussing')) // Legacy saved conversations already awaiting input.
}
