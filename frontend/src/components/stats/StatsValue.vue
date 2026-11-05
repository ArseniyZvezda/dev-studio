<script setup lang="ts">
import { LoaderCircle } from 'lucide-vue-next'

import { t } from '@/i18n'

defineProps<{ value: string | null; loading: boolean }>()
</script>

<template>
  <span class="inline-flex min-w-0 items-center gap-2" :aria-busy="loading">
    <span v-if="value === null && loading" class="vxs-skeleton inline-block h-[1em] w-20" aria-hidden="true" />
    <span v-else>{{ value ?? '—' }}</span>
    <LoaderCircle v-if="loading && value !== null" class="size-3 shrink-0 animate-spin" aria-hidden="true" />
    <span v-if="loading" class="sr-only">{{ t('common.loading') }}</span>
  </span>
</template>
