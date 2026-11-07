<script setup lang="ts">
/** §10.4: one chip for input, output, cache reads and cost across task surfaces. */
import { ArrowDown, ArrowUp, Cpu, DollarSign } from 'lucide-vue-next'
import { computed } from 'vue'

import type { AgentMessage } from '@/api/types'
import { t } from '@/i18n'
import { formatTokens } from '@/lib/status'
import { taskCost } from '@/lib/task-telemetry'

const props = defineProps<{
  inputTokens: number | null
  outputTokens: number | null
  cachedTokens: number | null
  costUsd: number | null
  costMessages?: AgentMessage[]
}>()

const metrics = computed(() => ([
  { icon: ArrowUp, count: props.inputTokens, label: 'tokenUsage.sentValue', hint: 'tokenUsage.sentHint' },
  { icon: ArrowDown, count: props.outputTokens, label: 'tokenUsage.receivedValue', hint: 'tokenUsage.receivedHint' },
  { icon: Cpu, count: props.cachedTokens, label: 'tokenUsage.cachedValue', hint: 'tokenUsage.cachedHint' },
] as const).map(metric => {
  const known = metric.count !== null && Number.isFinite(metric.count) && metric.count >= 0
  const value = known ? formatTokens(metric.count) : '—'
  const label = t(metric.label, { tokens: value })
  return {
    key: metric.label,
    icon: metric.icon,
    value,
    label,
    title: `${label}. ${known ? t(metric.hint) : t('tokenUsage.unavailable')}`,
  }
}))
const cost = computed(() => {
  const known = props.costUsd !== null && Number.isFinite(props.costUsd) && props.costUsd >= 0
  const precise = known ? taskCost(props.costUsd!, props.costMessages ?? []) : '—'
  return {
    value: precise === '—' ? '—' : props.costUsd!.toFixed(1),
    // Keep exact amounts and estimate/partial markers available without widening the chip.
    label: t('tokenUsage.costValue', { cost: precise }),
    title: `${t('tokenUsage.costValue', { cost: precise })}. ${t(precise === '—' ? 'tokenUsage.costUnavailable' : 'tokenUsage.costHint')}`,
  }
})
const label = computed(() => [...metrics.value.map(metric => metric.label), cost.value.label].join(' · '))
</script>

<template>
  <span
    class="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-pill border border-line-subtle bg-inset/60 px-1 py-0.5 text-small text-muted"
    role="group"
    :aria-label="label"
  >
    <span
      v-for="metric in metrics"
      :key="metric.key"
      class="inline-flex items-center gap-0.5"
      :title="metric.title"
      :aria-label="metric.label"
    >
      <component :is="metric.icon" class="size-3 shrink-0 text-faint" aria-hidden="true" />
      <span class="font-mono tabular-nums">{{ metric.value }}</span>
    </span>
    <span class="inline-flex items-center gap-0.5" :title="cost.title" :aria-label="cost.label">
      <DollarSign class="size-3 shrink-0 text-faint" aria-hidden="true" />
      <span class="font-mono tabular-nums">{{ cost.value }}</span>
    </span>
  </span>
</template>
