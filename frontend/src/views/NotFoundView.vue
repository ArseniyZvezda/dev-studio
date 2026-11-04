<script setup lang="ts">
import EmptyState from '@/components/ui/EmptyState.vue'
import { t } from '@/i18n'
</script>

<template>
  <EmptyState :title="t('ui.notFound.title')" :hint="t('ui.notFound.hint')">
    <RouterLink to="/explorer" class="mt-3 inline-block text-accent hover:underline">
      {{ t('ui.notFound.back') }}
    </RouterLink>
  </EmptyState>
</template>
