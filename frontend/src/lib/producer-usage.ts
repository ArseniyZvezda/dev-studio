/** Session spending follows the selected Producer conversation, across all its turns (§10.11). */
import type { AgentChat, AgentChatMessage, AgentMessage, TeamTask } from '@/api/types'
import { chatUsage } from './chat-usage'
import { taskUsage } from '@/lib/task-usage'
import { tokenCount, type TokenDirections } from '@/lib/token-directions'

export interface ProducerUsage extends TokenDirections {
  costUsd: number | null
  costMessages: AgentMessage[]
}

function addKnown(left: number | null, right: number | null): number | null {
  return left === null || right === null ? null : left + right
}

export function producerSessionUsage(chat: AgentChat, messages: AgentChatMessage[], tasks: TeamTask[]): ProducerUsage {
  const { inputTokens, outputTokens, cacheReadTokens, costUsd, costMessages } = chatUsage(chat, messages)
  const total = { inputTokens, outputTokens, cacheReadTokens, costUsd, costMessages }
  // Include completed/archived/cancelled work too. Subagent spending is already in each task.
  for (const task of tasks) {
    if (task.producer_chat_id !== chat.id) continue
    const usage = taskUsage({ ...task, messages: [] })
    total.inputTokens = addKnown(total.inputTokens, usage.inputTokens)
    total.outputTokens = addKnown(total.outputTokens, usage.outputTokens)
    total.cacheReadTokens = addKnown(total.cacheReadTokens, usage.cacheReadTokens)
    total.costUsd = addKnown(total.costUsd, tokenCount(task.total_cost_usd))
  }
  return total
}
