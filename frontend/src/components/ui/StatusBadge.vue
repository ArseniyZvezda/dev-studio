<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { isActive, isRunning, isSuccessful, statusColor, statusLabel } from '@/lib/status'

const props = defineProps<{
  status: string
  dotOnly?: boolean
  /** No pulse, for a badge that names a column rather than a task's own live state. */
  quiet?: boolean
}>()

const color = computed(() => statusColor(props.status))
const active = computed(() => !props.quiet && isActive(props.status) && props.status !== 'pending')

/**
 * Work in flight rather than merely unfinished: the pill's edge breathes the running amber
 * (§7, the motion section's indication part). A paused run keeps its pulsing dot and gets no
 * edge — it is waiting on somebody, and nothing about it is moving.
 */
const live = computed(() => !props.quiet && !props.dotOnly && isRunning(props.status))

/**
 * The status arrived at success while the pill was on screen: its edge flashes green once and
 * settles. Only a change watched here counts — a pill mounted on a status that was already done
 * says nothing — and the class goes again at `animationend`, so the next arrival can replay it.
 */
const settling = ref(false)

watch(
  () => props.status,
  (now, before) => {
    settling.value =
      !props.quiet && !props.dotOnly && isSuccessful(now) && !isSuccessful(before)
  },
)
</script>

<template>
  <!-- `vxs-badge` is the outlined pill (`base.css`): the word is grey and the status colour is
       on the dot alone, because the dot is the indicator and indication is where the palette
       spends its hue (§7). -->
  <span
    class="vxs-badge inline-flex items-center gap-1.5 rounded-pill px-2 py-0.5 text-small font-medium text-muted"
    :class="{
      'vxs-badge-bare': dotOnly,
      'vxs-badge-live': live,
      'vxs-badge-settle': settling && !live,
    }"
    @animationend.self="settling = false"
  >
    <span
      class="size-1.5 shrink-0 rounded-full"
      :class="{ 'vxs-dot-live': active }"
      :style="{ background: color }"
    />
    <template v-if="!dotOnly">{{ statusLabel(status) }}</template>
  </span>
</template>
