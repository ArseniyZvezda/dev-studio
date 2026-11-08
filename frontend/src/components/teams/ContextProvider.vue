<script setup lang="ts">
import { computed } from 'vue'
import type { ContextInventory, ContextSource } from '@/api/types'
import AgentBackendIcon from '@/components/ui/AgentBackendIcon.vue'
import { contextSourceLabel } from '@/lib/context-source'

const props = defineProps<{ source: ContextSource; inventory: ContextInventory['origin']; compact?: boolean }>()
const label = computed(() => contextSourceLabel(props.source, props.inventory, props.compact))
</script>

<template>
  <span v-if="label" class="inline-flex max-w-full min-w-0 items-baseline gap-1 text-[11px] text-muted">
    <AgentBackendIcon v-if="source.provided_by" :backend="source.provided_by"
      class="size-3.5 shrink-0 self-start text-primary" />
    <span :class="compact ? 'truncate' : '[overflow-wrap:anywhere]'">{{ label }}</span>
  </span>
</template>
