<script setup lang="ts">
/** Dropdown of recent runs for the open pipeline (spec §6.3 toolbar). */
import { onMounted, onBeforeUnmount } from 'vue'

import type { Run } from '@/api/types'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { t } from '@/i18n'
import { formatDuration, formatTime } from '@/lib/status'

defineProps<{ runs: Run[] }>()
const emit = defineEmits<{ select: [string]; close: [] }>()

function onKey(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <!-- Click-catcher, so clicking anywhere else dismisses the dropdown. -->
  <div class="fixed inset-0 z-40" @click="emit('close')" />

  <div
    class="absolute top-full right-0 z-50 mt-1 max-h-80 w-72 overflow-auto rounded-md border border-line bg-elevated shadow-lg"
  >
    <p v-if="!runs.length" class="p-3 text-faint">{{ t('pipelines.history.empty') }}</p>

    <button
      v-for="run in runs"
      :key="run.id"
      class="flex w-full items-center gap-2 border-b border-line-subtle px-3 py-2 text-left last:border-0 hover:bg-hover"
      @click="emit('select', run.id)"
    >
      <StatusBadge :status="run.status" dot-only />
      <span class="min-w-0 flex-1">
        <span class="block truncate">{{ formatTime(run.created_at) }}</span>
        <span class="block truncate text-small text-faint">
          {{ formatDuration(run.started_at, run.finished_at) }}
          <template v-if="run.start_node_id">
            · {{ t('pipelines.history.fromNode', { node: run.start_node_id }) }}
          </template>
        </span>
      </span>
    </button>
  </div>
</template>
