<script setup lang="ts">
import { PlugZap } from 'lucide-vue-next'

import { t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
</script>

<template>
  <div class="grid h-full place-items-center p-8 text-center">
    <div class="max-w-md">
      <PlugZap class="mx-auto mb-3 size-8 text-faint" />
      <h1 class="mb-1 font-semibold">{{ t('plugins.missingPage.title') }}</h1>
      <p class="text-muted">
        <I18nT keypath="plugins.missingPage.body">
          <template #dir><code class="font-mono">frontend.dist</code></template>
        </I18nT>
      </p>
    </div>
  </div>
</template>
