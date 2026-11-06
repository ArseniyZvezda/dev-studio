<script setup lang="ts">
import { computed } from 'vue'

import { t } from '@/i18n'
import { EFFORT_SOLVER_DIMENSIONS, effortSolverResult } from '@/lib/effort-solver'

const props = defineProps<{ data: Record<string, unknown> }>()
const result = computed(() => effortSolverResult(props.data))
</script>

<template>
  <section
    v-if="result"
    class="rounded-lg border border-violet-500/40 bg-violet-500/10 px-3 py-2 text-small"
    :aria-label="t('effortSolver.name')"
  >
    <dl class="grid grid-cols-2 gap-x-4 gap-y-1">
      <div v-for="dimension in EFFORT_SOLVER_DIMENSIONS" :key="dimension" class="flex items-baseline justify-between gap-2">
        <dt class="text-muted">{{ t(`effortSolver.dimensions.${dimension}`) }}</dt>
        <dd class="font-mono font-medium tabular-nums">{{ result.scores[dimension] }}</dd>
      </div>
      <div class="col-span-2 mt-1 flex items-baseline justify-between border-t border-violet-500/25 pt-2">
        <dt class="font-medium">{{ t('chat.producer.effort') }}</dt>
        <dd class="font-semibold">{{ t(`effortSolver.levels.${result.effort}`) }}</dd>
      </div>
    </dl>
  </section>
</template>
