<script setup lang="ts">
/** The same write-list contract in task creation and editing (§10.5). */
import { t } from '@/i18n'

const model = defineModel<string>({ required: true })
defineProps<{ disabled?: boolean }>()
</script>

<template>
  <label class="block">
    <span class="mb-1 block text-small text-faint">{{ t('tasks.writePaths.label') }}</span>
    <textarea
      v-model="model"
      rows="3"
      :disabled="disabled"
      :placeholder="t('tasks.writePaths.placeholder')"
      class="w-full resize-y rounded-md border border-line bg-inset px-2 py-1 font-mono text-small outline-none focus:border-accent disabled:cursor-not-allowed disabled:opacity-50"
      spellcheck="false"
    />
    <span class="mt-1 block text-small text-faint">
      {{ disabled ? t('tasks.writePaths.runningHint') : t('tasks.writePaths.hint') }}
    </span>
  </label>
</template>
