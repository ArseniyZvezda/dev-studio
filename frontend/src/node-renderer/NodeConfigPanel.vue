<script setup lang="ts">
/**
 * The side panel that opens on node click (spec §6.3).
 *
 * Inputs that are connected on the canvas are shown as such rather than as editable
 * fields — the upstream node supplies them at run time, so letting the user type a
 * value there would be misleading.
 *
 * Every input also carries a pin toggle, which is the only place the canvas's shape can be
 * edited. A node type's `connectable` is the *default* answer, not the final one: a plugin
 * author cannot know which of their settings a particular pipeline wants to drive from
 * upstream, so the instance overrides it and only the divergences are stored.
 */
import { Pin, PinOff, Trash2 } from 'lucide-vue-next'
import { computed } from 'vue'

import type { NodeInstance, NodeTypeSchema, PipelineEdge, PortSchema } from '@/api/types'
import { t } from '@/i18n'
import { isPinned } from '@/lib/graph'
import { statusColor, statusLabel } from '@/lib/status'
import PortField from './PortField.vue'

const props = defineProps<{
  node: NodeInstance
  schema: NodeTypeSchema | null
  edges: PipelineEdge[]
  status?: string
  error?: string | null
}>()

const emit = defineEmits<{
  update: [Partial<NodeInstance>]
  remove: []
  runFrom: []
  /** A pin was folded away; the edge into it has to go with it. */
  disconnect: [portId: string]
}>()

const connectedPorts = computed(() => {
  const set = new Set<string>()
  for (const edge of props.edges) {
    if (edge.to.node === props.node.id) set.add(edge.to.port)
  }
  return set
})

function setParam(portId: string, value: unknown) {
  emit('update', { params: { ...props.node.params, [portId]: value } })
}

/** The output a loop node fans out over, named the way the Outputs list names it. */
const iteratesLabel = computed(() => {
  const port = props.schema?.outputs.find((output) => output.id === props.schema?.iterates)
  return port ? port.label || port.id : null
})

function pinned(port: PortSchema): boolean {
  return isPinned(port, props.node.pins)
}

/**
 * Toggle a pin, storing only what differs from the node type's own answer.
 *
 * Deleting the key rather than writing the schema's value back is what keeps the YAML
 * quiet — and what lets a node keep following a plugin that later changes its mind about
 * which ports are pins.
 */
function togglePin(port: PortSchema) {
  const next = !pinned(port)
  const pins = { ...props.node.pins }
  if (next === port.connectable) delete pins[port.id]
  else pins[port.id] = next
  emit('update', { pins })
  // An edge cannot outlive the handle it is anchored to.
  if (!next && connectedPorts.value.has(port.id)) emit('disconnect', port.id)
}

function pinTitle(port: PortSchema): string {
  if (!pinned(port)) return t('pipelines.config.pin.show')
  return connectedPorts.value.has(port.id)
    ? t('pipelines.config.pin.hideConnected')
    : t('pipelines.config.pin.hide')
}
</script>

<template>
  <div class="flex h-full flex-col bg-surface">
    <header class="border-b border-line px-pane py-2">
      <div class="flex items-center gap-2">
        <input
          :value="node.label ?? schema?.name ?? node.type"
          class="min-w-0 flex-1 rounded-sm bg-transparent font-medium outline-none hover:bg-hover focus:bg-inset"
          @change="emit('update', { label: ($event.target as HTMLInputElement).value })"
        />
        <button
          class="grid size-6 shrink-0 place-items-center rounded-sm text-faint hover:bg-danger-soft hover:text-danger"
          :title="t('pipelines.config.deleteNode')"
          @click="emit('remove')"
        >
          <Trash2 class="size-3.5" />
        </button>
      </div>
      <p class="truncate font-mono text-small text-faint">{{ node.type }}</p>
      <p
        v-if="status"
        class="mt-1 text-small font-medium"
        :style="{ color: statusColor(status) }"
      >
        {{ statusLabel(status) }}
      </p>
    </header>

    <div class="min-h-0 flex-1 space-y-4 overflow-auto p-pane">
      <p v-if="error" class="rounded-md bg-danger-soft p-2 font-mono text-small text-danger">
        {{ error }}
      </p>

      <p v-if="!schema" class="text-danger">
        {{ t('pipelines.config.notRegistered') }}
      </p>

      <template v-else>
        <p v-if="schema.description" class="text-muted">{{ schema.description }}</p>

        <section class="space-y-2">
          <label class="flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              :checked="node.disabled"
              class="accent-[var(--vxs-accent)]"
              @change="emit('update', { disabled: ($event.target as HTMLInputElement).checked })"
            />
            <span>{{ t('pipelines.config.skip') }}</span>
          </label>

          <label class="flex cursor-pointer items-start gap-2">
            <input
              type="checkbox"
              :checked="node.pause_on_complete"
              class="mt-0.5 accent-[var(--vxs-accent)]"
              @change="
                emit('update', {
                  pause_on_complete: ($event.target as HTMLInputElement).checked,
                })
              "
            />
            <span>
              {{ t('pipelines.config.pauseOnComplete') }}
              <span class="block text-small text-faint">
                {{ t('pipelines.config.pauseOnCompleteHint') }}
              </span>
            </span>
          </label>
        </section>

        <section v-if="schema.inputs.length" class="space-y-3">
          <h3 class="text-small font-semibold tracking-wide text-faint uppercase">
            {{ t('pipelines.config.inputs') }}
          </h3>
          <template v-for="port in schema.inputs" :key="port.id">
            <div v-if="connectedPorts.has(port.id)" class="rounded-md border border-line-subtle p-2">
              <div class="flex items-baseline gap-1">
                <p class="font-medium">{{ port.label || port.id }}</p>
                <button
                  class="ml-auto grid size-5 shrink-0 place-items-center rounded-sm text-accent hover:bg-hover"
                  :title="pinTitle(port)"
                  @click="togglePin(port)"
                >
                  <Pin class="size-3" />
                </button>
              </div>
              <p class="text-small text-info">{{ t('pipelines.config.connected') }}</p>
            </div>
            <PortField
              v-else
              :port="port"
              :model-value="node.params[port.id] ?? port.default"
              @update:model-value="setParam(port.id, $event)"
            >
              <template #actions>
                <button
                  class="grid size-5 shrink-0 place-items-center rounded-sm hover:bg-hover"
                  :class="pinned(port) ? 'text-accent' : 'text-faint'"
                  :title="pinTitle(port)"
                  @click.stop.prevent="togglePin(port)"
                >
                  <component :is="pinned(port) ? Pin : PinOff" class="size-3" />
                </button>
              </template>
            </PortField>
          </template>
        </section>

        <section v-if="schema.outputs.length">
          <h3 class="mb-2 text-small font-semibold tracking-wide text-faint uppercase">
            {{ t('pipelines.config.outputs') }}
          </h3>
          <ul class="space-y-1">
            <li
              v-for="port in schema.outputs"
              :key="port.id"
              class="flex items-baseline justify-between gap-2 rounded-sm bg-inset px-2 py-1"
            >
              <span>{{ port.label || port.id }}</span>
              <span class="font-mono text-[10px] text-faint">{{ port.type }}</span>
            </li>
          </ul>
        </section>

        <section class="text-small text-faint">
          <h3 class="mb-1 font-semibold tracking-wide uppercase">
            {{ t('pipelines.config.execution') }}
          </h3>
          <p>{{ schema.execution.kind }} · {{ schema.execution.resource_class }}</p>
          <p class="font-mono break-all">{{ schema.execution.entry }}</p>
          <p v-if="schema.iterates">
            {{ t('pipelines.config.iterates', { item: iteratesLabel ?? '' }) }}
          </p>
          <p v-if="!schema.execution.cacheable">{{ t('pipelines.config.neverCached') }}</p>
        </section>
      </template>
    </div>

    <footer class="border-t border-line p-2">
      <button
        class="w-full rounded-md border border-line px-3 py-1.5 hover:bg-hover"
        @click="emit('runFrom')"
      >
        {{ t('pipelines.config.runFromHere') }}
      </button>
    </footer>
  </div>
</template>
