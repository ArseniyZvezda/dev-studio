<script setup lang="ts">
import { Check, Copy, X } from 'lucide-vue-next'
import { onBeforeUnmount, ref, watch } from 'vue'

import { t } from '@/i18n'

const props = defineProps<{ text: string; inline?: boolean }>()
const state = ref<'ready' | 'copied' | 'failed'>('ready')
let resetTimer: ReturnType<typeof setTimeout> | null = null

function reset(): void {
  if (resetTimer) clearTimeout(resetTimer)
  resetTimer = null
  state.value = 'ready'
}

watch(() => props.text, reset)
onBeforeUnmount(reset)

async function copy(): Promise<void> {
  reset()
  try {
    await navigator.clipboard.writeText(props.text)
    state.value = 'copied'
  } catch {
    state.value = 'failed'
  }
  resetTimer = setTimeout(reset, 2000)
}
</script>

<template>
  <button
    type="button"
    class="code-copy-button"
    :class="{ 'code-copy-button--inline': inline }"
    contenteditable="false"
    :title="t(state === 'copied' ? 'common.copied' : state === 'failed' ? 'common.copyFailed' : 'common.copy')"
    @pointerdown.stop
    @mousedown.stop
    @click.stop="copy"
    @contextmenu.stop
  >
    <Check v-if="state === 'copied'" class="size-3.5" aria-hidden="true" />
    <X v-else-if="state === 'failed'" class="size-3.5" aria-hidden="true" />
    <Copy v-else class="size-3.5" aria-hidden="true" />
    <span>{{ t(state === 'copied' ? 'common.copied' : state === 'failed' ? 'common.copyFailed' : 'common.copy') }}</span>
  </button>
</template>
