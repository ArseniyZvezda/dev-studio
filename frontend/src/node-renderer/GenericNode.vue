<script setup lang="ts">
/**
 * The default renderer for every node type (spec §9.3).
 *
 * Everything it draws comes from the type's `NodeTypeSchema`, so a plugin gets a
 * working, correctly-coloured node on the canvas without shipping a single line of
 * frontend code. The category accent arrives as an inline `--node-accent` custom
 * property rather than a generated class, which is what lets an arbitrary plugin colour
 * work at runtime (spec §7, level 3).
 *
 * The chrome itself — border, header, status strip, port rows — is the shared `.vxs-node`
 * family in `styles/base.css`, not scoped CSS here, so a plugin's own node component wears
 * exactly the same one. Which of the four channels says what is documented there.
 *
 * **Ports are named, not counted.** They used to be bare handles spread down the node's
 * side, identified only by a tooltip, which was survivable while a node type's pins were
 * fixed by its author. They are not fixed any more — any input can be pinned per node — so
 * a column of anonymous dots would be asking the user to hover each one to find out what
 * they connected.
 */
import { Handle, Position } from '@vue-flow/core'
import { EyeOff, PauseCircle, Repeat } from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'

import type { NodeProgress, NodeStatus, NodeTypeSchema, PortSchema } from '@/api/types'
import { t } from '@/i18n'
import { isPinned } from '@/lib/graph'
import { resolveIcon } from '@/lib/icons'
import { formatProgress, isRunning, isSuccessful, statusColor, statusLabel } from '@/lib/status'

const props = defineProps<{
  data: {
    schema: NodeTypeSchema | null
    label: string
    typeId: string
    accent: string
    pins?: Record<string, boolean>
    connected?: string[]
    pauseOnComplete?: boolean
    status?: NodeStatus
    error?: string | null
    fromCache?: boolean
    progress?: NodeProgress | null
    disabled?: boolean
  }
  selected?: boolean
}>()

const icon = computed(() => resolveIcon(props.data.schema?.icon))

/**
 * The inputs that get a handle: whatever this node pins, plus anything already connected.
 *
 * The second half is what keeps a hand-edited pipeline honest. An edge into a port nobody
 * pinned is a statement the YAML is allowed to make, and Vue Flow anchors an edge to a
 * mounted handle or to nothing at all — so the handle follows the edge rather than the
 * other way round.
 */
const connectableInputs = computed<PortSchema[]>(() => {
  const connected = new Set(props.data.connected ?? [])
  return (props.data.schema?.inputs ?? []).filter(
    (port) => connected.has(port.id) || isPinned(port, props.data.pins),
  )
})
const outputs = computed<PortSchema[]>(() => props.data.schema?.outputs ?? [])

const status = computed(() => props.data.status ?? null)
const running = computed(() => isRunning(status.value))

/**
 * The node succeeded while the canvas was on screen, so its strip glows once
 * (`vxs-node-settle`, §7). This component outlives a status change — run status is painted onto
 * the node's existing `data` — which is what lets it see the change arrive. A node that had no
 * status a moment ago was not watched finishing: that is a finished run from History being
 * painted onto the canvas, and it glows nowhere.
 */
const settling = ref(false)

watch(status, (now, before) => {
  settling.value = before !== null && isSuccessful(now) && !isSuccessful(before)
})

function settled(event: AnimationEvent): void {
  if (event.animationName === 'vxs-node-settle') settling.value = false
}

/**
 * Run status has its own channel — the strip and the chip both read this — because the
 * border belongs to selection alone. A node reused from cache is a permanent status, and
 * painting it onto the border left the user with no way to see what they had clicked.
 */
const statusTone = computed(() => (status.value ? statusColor(status.value) : 'transparent'))

/** The chip carries what the row of status icons used to, as its tooltip. */
const statusTitle = computed(() => {
  if (props.data.error) return props.data.error
  if (status.value === 'paused') return t('pipelines.node.pausedHint')
  if (props.data.fromCache) return t('pipelines.node.cachedHint')
  return undefined
})

/**
 * What the node says about its own progress, drawn only while it is running.
 *
 * The status guard matters as much as the value: the store drops progress when a node
 * finishes, but a node whose run ended while this client was disconnected would keep the
 * last frame it saw, and a bar under a finished node claims work still in flight.
 */
const progress = computed(() => (running.value ? (props.data.progress ?? null) : null))
const progressText = computed(() =>
  progress.value ? formatProgress(progress.value) : { stage: '', amount: '' },
)

/**
 * A loop looks like any other node — its body is just the nodes to its right — so the one
 * thing the canvas has to say is that it repeats, and which of its outputs is the value
 * that changes each turn.
 *
 * A marker rather than a drawn region: which nodes are inside is the descendant closure,
 * and outlining that would mean re-deciding the graph's layout every time an edge moves.
 */
const iterates = computed(() => props.data.schema?.iterates ?? null)
const iteratesLabel = computed(() => {
  const port = props.data.schema?.outputs.find((output) => output.id === iterates.value)
  return port ? (port.label || port.id) : null
})
</script>

<template>
  <div
    class="vxs-node"
    :class="{
      'vxs-node-selected': selected,
      'vxs-node-running': running,
      'vxs-node-settle': settling,
      'vxs-node-disabled': data.disabled,
    }"
    :style="{ '--node-accent': data.accent, '--node-status': statusTone }"
    @animationend="settled"
  >
    <div v-if="status" class="vxs-node-status" />

    <header class="vxs-node-header">
      <component :is="icon" class="size-3.5 shrink-0" :style="{ color: 'var(--node-accent)' }" />
      <span class="truncate font-medium">{{ data.label }}</span>
      <!-- The markers sit before the chip, so a status arriving never moves them. -->
      <Repeat
        v-if="iterates"
        class="ml-auto size-3 shrink-0 text-faint"
        :title="t('pipelines.node.iterates', { item: iteratesLabel ?? '' })"
      />
      <PauseCircle
        v-if="data.pauseOnComplete"
        class="size-3 shrink-0 text-faint"
        :class="{ 'ml-auto': !iterates }"
        :title="t('pipelines.node.pausesOnComplete')"
      />
      <EyeOff
        v-if="data.disabled"
        class="size-3 shrink-0 text-faint"
        :class="{ 'ml-auto': !iterates && !data.pauseOnComplete }"
        :title="t('pipelines.node.disabled')"
      />
      <span v-if="status" class="vxs-node-chip" :title="statusTitle">
        {{ statusLabel(status) }}
      </span>
    </header>

    <div class="vxs-node-body">
      <p class="truncate text-small text-faint">
        {{ data.schema?.category ?? t('pipelines.node.unknownType') }}
      </p>
      <p v-if="!data.schema" class="truncate text-small text-danger">
        {{ t('pipelines.node.notRegistered', { type: data.typeId }) }}
      </p>
    </div>

    <div
      v-if="progress"
      class="vxs-node-progress"
      :class="{ 'vxs-node-progress-unknown': progress.percent === null }"
      :title="progressText.stage || undefined"
    >
      <div class="vxs-node-progress-track">
        <div
          class="vxs-node-progress-fill"
          :style="progress.percent === null ? undefined : { width: `${progress.percent}%` }"
        />
      </div>
      <p v-if="progressText.stage || progressText.amount" class="vxs-node-progress-text">
        <span>{{ progressText.stage }}</span>
        <span>{{ progressText.amount }}</span>
      </p>
    </div>

    <div v-if="connectableInputs.length || outputs.length" class="vxs-node-ports">
      <div
        v-for="port in connectableInputs"
        :key="`in-${port.id}`"
        class="vxs-node-port"
        :title="`${port.label || port.id} (${port.type})`"
      >
        <Handle type="target" :position="Position.Left" :id="port.id" />
        <span class="truncate">{{ port.label || port.id }}</span>
      </div>
      <div
        v-for="port in outputs"
        :key="`out-${port.id}`"
        class="vxs-node-port vxs-node-port-out"
        :title="`${port.label || port.id} (${port.type})`"
      >
        <Handle type="source" :position="Position.Right" :id="port.id" />
        <span class="truncate">{{ port.label || port.id }}</span>
      </div>
    </div>
  </div>
</template>
