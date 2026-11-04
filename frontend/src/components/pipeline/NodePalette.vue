<script setup lang="ts">
/**
 * Searchable node palette, grouped by category (spec §6.3).
 *
 * Always on screen: adding a node is the reason the editor is open, and a palette behind a
 * toolbar button is a click and a guess in front of every one. It carries no width of its
 * own — the page owns the layout, and the page is what has to give the pixels up.
 */
import { Search } from 'lucide-vue-next'
import { computed, ref } from 'vue'

import type { NodeTypeSchema } from '@/api/types'
import { t } from '@/i18n'
import { resolveIcon } from '@/lib/icons'
import { usePipelinesStore } from '@/stores/pipelines'

const emit = defineEmits<{ add: [NodeTypeSchema] }>()

const pipelines = usePipelinesStore()
const query = ref('')

const grouped = computed(() => {
  const needle = query.value.trim().toLowerCase()
  const groups = new Map<string, NodeTypeSchema[]>()
  for (const schema of pipelines.nodeTypes) {
    const haystack = `${schema.name} ${schema.id} ${schema.category} ${schema.description}`
    if (needle && !haystack.toLowerCase().includes(needle)) continue
    const list = groups.get(schema.category) ?? []
    list.push(schema)
    groups.set(schema.category, list)
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))
})
</script>

<template>
  <div class="flex h-full shrink-0 flex-col overflow-hidden bg-surface">
    <header class="flex items-center gap-2 border-b border-line px-2 py-2">
      <div class="relative flex-1">
        <Search class="absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-faint" />
        <input
          v-model="query"
          :placeholder="t('pipelines.palette.search')"
          class="w-full rounded-md border border-line bg-inset py-1 pr-2 pl-7 outline-none focus:border-accent"
        />
      </div>
    </header>

    <div class="min-h-0 flex-1 overflow-auto p-2">
      <section v-for="[category, schemas] in grouped" :key="category" class="mb-3">
        <h3 class="mb-1 px-1 text-small font-semibold tracking-wide text-faint uppercase">
          {{ category }}
        </h3>
        <button
          v-for="schema in schemas"
          :key="schema.id"
          class="vxs-row flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-hover"
          :title="schema.description || schema.id"
          @click="emit('add', schema)"
        >
          <component
            :is="resolveIcon(schema.icon)"
            class="size-4 shrink-0"
            :style="{ color: pipelines.colorFor(schema.id) }"
          />
          <span class="min-w-0 flex-1">
            <span class="block truncate">{{ schema.name }}</span>
            <span class="block truncate font-mono text-[10px] text-faint">{{ schema.id }}</span>
          </span>
        </button>
      </section>

      <p v-if="!grouped.length" class="px-1 py-2 text-faint">
        {{ t('pipelines.palette.noMatch', { query }) }}
      </p>
    </div>
  </div>
</template>
