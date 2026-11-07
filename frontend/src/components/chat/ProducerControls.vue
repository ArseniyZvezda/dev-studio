<script setup lang="ts">
/** Shared controls for the Producer page, popup and Chats page (§10.11). */
import { Clapperboard, Pause, Play, Square } from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'
import type { ProducerEffortMode, ProducerPolicy } from '@/api/types'
import TokenUsageChip from '@/components/ui/TokenUsageChip.vue'
import { t } from '@/i18n'
import { producerSessionUsage } from '@/lib/producer-usage'
import { useChatStore } from '@/stores/chat'
import { useTeamsStore } from '@/stores/teams'

withDefaults(defineProps<{ toolbar?: boolean }>(), { toolbar: false })
const chat = useChatStore()
const teams = useTeamsStore()
const current = computed(() => chat.view === 'chat' ? chat.activeChat : null)
const usage = computed(() => {
  if (!current.value) return null
  const team = teams.teams.find(team => team.members.some(member => member.id === current.value!.member_id))
  return producerSessionUsage(current.value, chat.activeMessages, team?.tasks ?? [])
})
const member = computed(() => teams.teams.flatMap(team => team.members).find(member => member.id ===
  (chat.view === 'new' ? chat.newMemberId : current.value?.member_id)))
const active = computed(() => current.value?.producer_mode === true)
const started = computed(() => current.value?.producer_started === true)
const paused = computed(() => started.value && current.value?.producer_paused === true)
const state = computed(() => active.value && !started.value ? 'not_started' : current.value?.producer_state ?? 'discussing')
const plan = computed(() => current.value?.producer_plan ?? [])
const newPolicy = ref<ProducerPolicy>('careful')
const policy = computed(() => current.value?.producer_policy ?? newPolicy.value)
const newEffortMode = ref<ProducerEffortMode>('agent')
const effortMode = computed(() => current.value?.producer_effort_mode ?? newEffortMode.value)
const estimate = ref<number | string>(150)
const estimateValid = computed(() => typeof estimate.value === 'number' && Number.isFinite(estimate.value) && estimate.value > 0)
const estimateDirty = computed(() => !!current.value && estimate.value !== (current.value.estimate_task_cost_usd ?? 150))
watch([() => chat.view, () => current.value?.id, () => current.value?.estimate_task_cost_usd, () => chat.newMemberId], () => {
  estimate.value = current.value?.estimate_task_cost_usd ?? 150
}, { immediate: true })
watch(() => [chat.view, chat.newMemberId], () => {
  newPolicy.value = 'careful'
  newEffortMode.value = 'agent'
})
function changePolicy(value: ProducerPolicy) {
  if (current.value) void chat.setProducerPolicy(value)
  else newPolicy.value = value
}
function changeEffortMode(value: ProducerEffortMode) {
  if (current.value) void chat.setProducerEffortMode(value)
  else newEffortMode.value = value
}
function saveEstimate() {
  if (estimateValid.value && estimateDirty.value) void chat.setProducerEstimateTaskCost(Number(estimate.value))
}
</script>

<template>
  <section v-if="member?.builtin === 'producer' && chat.view !== 'list'"
    class="shrink-0 border-b border-line bg-inset/50 px-3 py-2" :aria-label="t('chat.producer.label')">
    <div>
      <div class="flex flex-wrap items-center gap-2" :class="toolbar ? 'min-w-0 flex-1' : ''">
        <Clapperboard class="size-4 text-accent" />
        <span v-if="toolbar || active" class="font-medium" :class="!toolbar ? 'flex-1' : ''">{{ t('chat.producer.label') }}</span>
        <span v-if="toolbar" class="rounded-pill border border-line px-2 py-0.5 text-small text-muted" role="status">
          {{ paused ? t('chat.producer.pausedShort') : t(`chat.producer.states.${active ? state : 'off'}`) }}
        </span>
        <template v-if="active">
          <button class="vxs-btn flex items-center gap-1 border border-line rounded-md px-2 py-1 text-small hover:bg-hover disabled:opacity-50"
            :disabled="chat.busy || !started" :title="t(paused ? 'chat.producer.resumeHint' : 'chat.producer.pauseHint')"
            @click="chat.producerAction(paused ? 'resume' : 'pause')">
            <Play v-if="paused" class="size-3" /><Pause v-else class="size-3" />
            {{ t(paused ? 'chat.producer.resume' : 'chat.producer.pause') }}
          </button>
          <button v-if="!toolbar && !current?.producer_workspace" class="vxs-btn flex items-center gap-1 border border-line rounded-md px-2 py-1 text-small hover:bg-hover disabled:opacity-50"
            :disabled="chat.busy" :title="t('chat.producer.stopHint')" @click="chat.producerAction('stop')">
            <Square class="size-3" />{{ t('chat.producer.stop') }}
          </button>
        </template>
        <button v-else-if="!toolbar && !current?.producer_workspace" class="vxs-btn-primary flex items-center gap-1.5 px-3 py-1.5 disabled:opacity-50"
          :disabled="chat.busy || !!current?.is_running || !estimateValid" @click="chat.producerAction('start', policy, Number(estimate), effortMode)">
          <Play class="size-3.5" />{{ t('chat.producer.start') }}
        </button>
      </div>
      <!-- §10.11: all Producer settings share the row below its title. -->
      <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2" data-producer-settings>
        <div class="flex flex-wrap items-center gap-2">
          <span v-if="!toolbar" class="text-small text-faint">{{ t('chat.producer.policy') }}</span>
          <div class="inline-flex rounded-md border border-line p-0.5" role="group" :aria-label="t('chat.producer.policy')">
            <button v-for="choice in (['careful', 'independent'] as const)" :key="choice"
              class="rounded px-2 py-1 text-small disabled:opacity-50"
              :class="policy === choice ? 'bg-accent-soft font-medium' : 'hover:bg-hover'"
              :aria-pressed="policy === choice" :title="t(`chat.producer.policies.${choice}Hint`)"
              :disabled="chat.busy || paused || !!current?.is_running" @click="changePolicy(choice)">
              {{ t(`chat.producer.policies.${choice}`) }}
            </button>
          </div>
        </div>
        <form class="flex flex-wrap items-center gap-2" @submit.prevent="saveEstimate">
          <label class="flex items-center gap-2 text-small" :title="t('chat.producer.estimateHint')">
            <span>{{ t(toolbar ? 'chat.producer.taskCost' : 'chat.producer.estimate') }}</span>
            <span class="flex items-center gap-1 rounded-md border border-line bg-surface px-2 py-1">
              <span class="text-faint" aria-hidden="true">$</span>
              <input v-model.number="estimate" type="number" min="0.01" step="0.01" inputmode="decimal"
                class="min-w-0 bg-transparent outline-none disabled:opacity-50" :class="toolbar ? 'w-14' : 'w-20'"
                :aria-label="t('chat.producer.estimateUsd')" :aria-invalid="!estimateValid"
                :disabled="chat.busy || !!current?.is_running" />
            </span>
          </label>
          <button v-if="estimateDirty" type="submit" class="rounded-md border border-line px-2 py-1 text-small hover:bg-hover disabled:opacity-50"
            :disabled="chat.busy || !!current?.is_running || !estimateValid">{{ t('common.save') }}</button>
        </form>
        <label class="flex items-center gap-2 text-small" :title="t(`chat.producer.effortModes.${effortMode}Hint`)">
          <span>{{ t('chat.producer.effort') }}</span>
          <select :value="effortMode" :aria-label="t('chat.producer.effort')"
            class="rounded-md border border-line bg-surface px-2 py-1 outline-none focus:border-accent disabled:opacity-50"
            :disabled="chat.busy || !!current?.is_running"
            @change="changeEffortMode(($event.target as HTMLSelectElement).value as ProducerEffortMode)">
            <option v-for="choice in (['agent', 'auto'] as const)" :key="choice" :value="choice">{{ t(`chat.producer.effortModes.${choice}`) }}</option>
          </select>
        </label>
      </div>
    </div>
    <p v-if="!estimateValid" class="mt-1 text-small text-danger" role="alert">{{ t('chat.producer.estimateInvalid') }}</p>
    <div v-if="active && usage" class="mt-2 flex flex-wrap items-center gap-2" data-producer-session-usage>
      <span class="text-small text-faint">{{ t('chat.producer.sessionUsage') }}</span>
      <TokenUsageChip :input-tokens="usage.inputTokens" :output-tokens="usage.outputTokens"
        :cached-tokens="usage.cacheReadTokens" :cost-usd="usage.costUsd" :cost-messages="usage.costMessages" />
    </div>
    <template v-if="!toolbar">
      <p class="mt-1 text-small text-faint">{{ t(`chat.producer.policies.${policy}Hint`) }}</p>
      <p v-if="active" class="mt-1 text-small text-faint" role="status">{{ paused ? t('chat.producer.paused') : t(`chat.producer.states.${state}`) }}</p>
      <p v-if="!active" class="mt-1 text-small text-faint">{{ t('chat.producer.hint') }}</p>
    </template>
    <div v-if="active && plan.length" class="mt-2 rounded-md border border-line bg-surface p-2">
      <div class="max-h-48 overflow-y-auto overscroll-contain">
        <p class="whitespace-pre-wrap text-small">{{ current?.producer_plan_summary }}</p>
        <details v-for="task in plan" :key="task.id" class="mt-2 text-small">
          <summary class="cursor-pointer font-medium">{{ task.display_id }} · {{ task.title }}</summary>
          <p class="mt-1 text-faint">{{ teams.memberById.get(task.assignee_id)?.name ?? task.assignee_id }}</p>
          <p class="mt-1 whitespace-pre-wrap">{{ task.brief }}</p>
          <p v-if="task.depends_on.length" class="mt-1 text-faint">
            {{ t('chat.producer.dependencies', { tasks: task.depends_on.map(id => plan.find(item => item.id === id)?.display_id ?? id).join(', ') }) }}
          </p>
        </details>
      </div>
      <p class="mt-2 text-small text-faint">{{ t('chat.producer.approvalHint') }}</p>
      <button class="vxs-btn-primary mt-2 flex items-center gap-1.5 px-3 py-1.5 disabled:opacity-50"
        :disabled="chat.busy || paused || !!current?.is_running" @click="chat.producerAction('approve')">
        <Play class="size-3.5" />{{ t('chat.producer.approve') }}
      </button>
    </div>
  </section>
</template>
