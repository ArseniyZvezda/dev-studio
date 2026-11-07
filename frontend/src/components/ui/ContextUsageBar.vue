<script setup lang="ts">
/** §10.4: latest request occupancy, sized like the read-only Effort control (§10.3). */
import { computed, nextTick, ref, watch } from 'vue'
import { sessionContextPath } from '@/api/client'
import type { ContextTarget } from '@/api/types'
import ContextViewer from '@/components/teams/ContextViewer.vue'
import { t } from '@/i18n'
import { formatTokens } from '@/lib/status'

const props = defineProps<{ tokens: number | null; window: number | null; target: ContextTarget }>()
const open = ref(false)
const trigger = ref<HTMLButtonElement | null>(null)
watch(() => sessionContextPath(props.target), () => { open.value = false })
async function close() {
  open.value = false
  await nextTick()
  trigger.value?.focus({ preventScroll: true })
}
const fraction = computed(() => props.tokens !== null && props.window && props.window > 0
  ? Math.min(1, Math.max(0, props.tokens / props.window)) : null)
const label = computed(() => t('taskDetail.header.contextWindow', {
  tokens: props.tokens === null ? '—' : formatTokens(props.tokens),
  window: props.window === null ? '—' : formatTokens(props.window),
}))
</script>

<template>
  <button ref="trigger" type="button" class="context-usage" :class="{ 'context-usage--unknown': fraction === null }"
    :style="{ '--context-fill': `${(fraction ?? 0) * 100}%`, '--context-hue': 130 * (1 - (fraction ?? 0)) }"
    :aria-label="`${t('contextViewer.open')}: ${label}`" aria-haspopup="dialog" :aria-expanded="open"
    :title="t('contextViewer.open')" @click.stop="open = true">
    <span class="context-usage__value" aria-hidden="true">{{ label }}</span>
    <span class="context-usage__track" aria-hidden="true"><span class="context-usage__fill" /></span>
  </button>
  <ContextViewer v-if="open" :target="target" :revision="tokens" @close="close" />
</template>

<style scoped>
.context-usage {
  cursor: pointer;
  border-radius: 6px;
  padding: 2px 4px;
  display: inline-flex;
  flex: 0 0 112px;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  width: 112px;
  height: 32px;
  vertical-align: middle;
  color: hsl(var(--context-hue) 65% 57%);
}
.context-usage:hover { background: var(--vxs-bg-hover); }
.context-usage:focus-visible { outline: 2px solid currentColor; outline-offset: 3px; }
.context-usage__value {
  font-size: 11px;
  font-weight: 700;
  line-height: 13px;
  text-align: center;
  white-space: nowrap;
}
.context-usage__track {
  display: block;
  height: 7px;
  border-radius: 999px;
  background: color-mix(in oklab, currentColor 19%, transparent);
}
.context-usage__fill {
  display: block;
  width: var(--context-fill);
  height: 100%;
  border-radius: inherit;
  background: currentColor;
}
[data-theme='light'] .context-usage { color: hsl(var(--context-hue) 70% 35%); }
.context-usage.context-usage--unknown { color: var(--vxs-text-faint); }
@media (forced-colors: active) {
  .context-usage { color: CanvasText; }
  .context-usage__track { border: 1px solid CanvasText; }
  .context-usage__fill { background: Highlight; forced-color-adjust: none; }
}
</style>
