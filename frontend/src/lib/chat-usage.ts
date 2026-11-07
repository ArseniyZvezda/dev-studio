/** §10.9: latest request occupancy and lifetime spending of one conversation. */
import type { AgentChat, AgentChatMessage } from '@/api/types'
import { taskUsage } from './task-usage'
import { tokenCount } from './token-directions'

export function chatUsage(chat: AgentChat, messages: AgentChatMessage[]) {
  const transcript = messages.map(message => ({ ...message, task_id: message.chat_id }))
  const usage = taskUsage({ ...chat, messages: transcript })
  return { ...usage, costUsd: tokenCount(chat.total_cost_usd), costMessages: transcript }
}
