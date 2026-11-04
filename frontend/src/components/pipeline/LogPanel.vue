<script setup lang="ts">
/** Bottom log dock: stdout/stderr streamed per node while a run is in flight. */
import { ChevronDown } from 'lucide-vue-next'
import { computed, nextTick, ref, watch } from 'vue'

import { t } from '@/i18n'
import { statusColor } from '@/lib/status'
import { useRunsStore } from '@/stores/runs'

const props = defineProps<{ nodeLabels: Record<string, string> }>()

const runs = useRunsStore()
const collapsed = ref(false)
const scroller = ref<HTMLElement | null>(null)
const follow = ref(true)

/**
 * A node the user clicked, versus the one the run is currently on. Kept apart because
 * a single field cannot tell its own automatic writes from the user's — auto-follow
 * would latch onto the first node that ran and never advance again.
 */
const pinnedNode = ref<string | null>(null)
const followedNode = ref<string | null>(null)

/** Nodes that have produced output, plus whichever one is currently running. */
const nodeIds = computed(() => {
  const ids = new Set(Object.keys(runs.logs))
  for (const [id, status] of Object.entries(runs.nodeStatus)) {
    if (status !== 'pending') ids.add(id)
  }
  return [...ids]
})

const selected = computed(
  () => pinnedNode.value ?? followedNode.value ?? nodeIds.value[0] ?? null,
)
const lines = computed(() => (selected.value ? (runs.logs[selected.value] ?? []) : []))

// Follow the tail unless the user has scrolled up to read something.
watch(
  () => lines.value.length,
  async () => {
    if (!follow.value || collapsed.value) return
    await nextTick()
    const element = scroller.value
    if (element) element.scrollTop = element.scrollHeight
  },
)

function onScroll() {
  const element = scroller.value
  if (!element) return
  follow.value = element.scrollHeight - element.scrollTop - element.clientHeight < 40
}

// Follow the run node by node, unless the user has pinned one.
watch(
  () => runs.nodeStatus,
  (statuses) => {
    if (pinnedNode.value) return
    const running = Object.entries(statuses).find(([, status]) => status === 'running')
    if (running) followedNode.value = running[0]
  },
  { deep: true },
)

// A new run releases the pin, so the panel follows the fresh run rather than staying on
// whichever node was being read from the previous one.
watch(
  () => runs.trackedRunId,
  () => {
    pinnedNode.value = null
    followedNode.value = null
    follow.value = true
  },
)

function label(nodeId: string): string {
  return props.nodeLabels[nodeId] ?? nodeId
}

async function selectNode(nodeId: string) {
  pinnedNode.value = nodeId
  // Historical runs have no buffered lines in memory — pull the file from the backend.
  if (!runs.logs[nodeId]?.length) await runs.fetchNodeLog(nodeId)
}
</script>

<template>
  <section
    class="flex shrink-0 flex-col border-t border-line bg-surface"
    :style="{ height: collapsed ? 'auto' : '220px' }"
  >
    <header class="flex items-center gap-1 border-b border-line px-2 py-1">
      <span class="mr-2 shrink-0 font-medium">{{ t('pipelines.logs.title') }}</span>
      <!-- One line of tabs that scrolls sideways. A pipeline has as many tabs as nodes, and
           squeezed into the header's width they broke their labels over two lines and pushed
           the fold button off the edge. No scrollbar: at this height it would be the strip. -->
      <div class="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto [scrollbar-width:none]!">
        <button
          v-for="nodeId in nodeIds"
          :key="nodeId"
          class="flex shrink-0 items-center gap-1.5 rounded-sm px-2 py-0.5 whitespace-nowrap"
          :class="
            selected === nodeId
              ? 'bg-accent-soft text-primary'
              : 'text-muted hover:bg-hover hover:text-primary'
          "
          @click="selectNode(nodeId)"
        >
          <span
            class="size-1.5 shrink-0 rounded-full"
            :style="{ background: statusColor(runs.nodeStatus[nodeId] ?? 'pending') }"
          />
          {{ label(nodeId) }}
        </button>
      </div>
      <button
        class="grid size-6 shrink-0 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
        @click="collapsed = !collapsed"
      >
        <ChevronDown class="vxs-chevron size-3.5" :class="collapsed ? 'rotate-180' : ''" />
      </button>
    </header>

    <div
      v-if="!collapsed"
      ref="scroller"
      class="min-h-0 flex-1 overflow-auto bg-inset p-2 font-mono text-[12px] leading-relaxed"
      @scroll="onScroll"
    >
      <p v-if="!lines.length" class="text-faint">
        {{ nodeIds.length ? t('pipelines.logs.noOutput') : t('pipelines.logs.runToSee') }}
      </p>
      <p
        v-for="(entry, index) in lines"
        :key="index"
        class="whitespace-pre-wrap"
        :class="entry.stream === 'stderr' ? 'text-danger' : 'text-secondary'"
      >{{ entry.line }}</p>
    </div>
  </section>
</template>
