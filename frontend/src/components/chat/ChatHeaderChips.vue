<script setup lang="ts">
/** Shared by the popup, Chats and the pinned Producer conversation (§10.9). */
import { computed, onMounted } from 'vue'
import type { AgentChat, AgentChatMessage, TeamMember } from '@/api/types'
import ModelSelector from '@/components/ui/ModelSelector.vue'
import ContextUsageBar from '@/components/ui/ContextUsageBar.vue'
import ModelEffortSlider from '@/components/ui/ModelEffortSlider.vue'
import TokenUsageChip from '@/components/ui/TokenUsageChip.vue'
import { t } from '@/i18n'
import { chatUsage } from '@/lib/chat-usage'
import { useChatStore } from '@/stores/chat'
import { useTeamsStore } from '@/stores/teams'

const props = defineProps<{
  chat: AgentChat
  member: TeamMember
  messages: AgentChatMessage[]
  levels?: string[]
}>()
const usage = computed(() => chatUsage(props.chat, props.messages))
const chatStore = useChatStore()
const teams = useTeamsStore()
onMounted(() => void teams.loadOptions())
</script>

<template>
  <div class="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-3 pb-2 text-small text-faint" data-chat-header-chips>
    <ModelSelector class="min-w-40 max-w-64 flex-1" :backend="chat.agent_backend ?? null" :model="chat.model ?? null"
      :inherited-backend="member.agent_backend" :inherited-model="member.model" :options="teams.options"
      :disabled="chat.is_running || chatStore.busy" allow-inherit @change="(backend, model) => chatStore.setModel(backend, model)" />
    <ModelEffortSlider :model-value="member.effort" :levels="levels" readonly />
    <ContextUsageBar :target="{ chatId: chat.id }" :tokens="usage.contextTokens" :window="usage.contextWindow" />
    <span class="inline-flex max-w-full flex-wrap items-center gap-1.5">
      <span v-if="chat.producer_mode">{{ t('chat.producer.chatUsage') }}</span>
      <TokenUsageChip :input-tokens="usage.inputTokens" :output-tokens="usage.outputTokens"
        :cached-tokens="usage.cacheReadTokens" :cost-usd="usage.costUsd" :cost-messages="usage.costMessages" />
    </span>
  </div>
</template>
