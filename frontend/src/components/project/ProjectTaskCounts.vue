<script setup lang="ts">
import type { ProjectTaskCounts } from '@/api/types'
import { t } from '@/i18n'

defineProps<{ counts: ProjectTaskCounts | null }>()
</script>

<template>
  <span class="block text-small leading-snug tabular-nums text-muted">
    <span class="block">{{ t('project.taskCounts.inProgress', { count: counts?.in_progress ?? '—' }) }}</span>
    <span class="block">{{ t('project.taskCounts.needsReview', { count: counts?.needs_review ?? '—' }) }}</span>
  </span>
</template>
