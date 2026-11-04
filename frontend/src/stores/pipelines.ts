/**
 * Pipeline definitions and the node-type registry that drives the palette and the
 * schema-driven node renderer (spec §8.1, §9.3).
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type {
  NodeTypeSchema,
  PipelineDefinition,
  PipelineIssue,
  PipelineSummary,
} from '@/api/types'
import { onEvent } from '@/ws/connection'

/** Stable fallback colours per category, so a plugin that sets none still reads clearly. */
const CATEGORY_PALETTE = [
  '#7c5cff',
  '#39c5cf',
  '#3fb950',
  '#d29922',
  '#db61a2',
  '#58a6ff',
  '#f85149',
]

export const usePipelinesStore = defineStore('pipelines', () => {
  const nodeTypes = ref<NodeTypeSchema[]>([])
  const categories = ref<Record<string, string[]>>({})
  const summaries = ref<PipelineSummary[]>([])
  const issues = ref<PipelineIssue[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  let subscribed = false

  const nodeTypeById = computed(() => {
    const map = new Map<string, NodeTypeSchema>()
    for (const type of nodeTypes.value) map.set(type.id, type)
    return map
  })

  /** Colour for a node type: explicit `color` wins, else a stable per-category hue. */
  const categoryColor = computed(() => {
    const names = Object.keys(categories.value).sort()
    const map = new Map<string, string>()
    names.forEach((name, index) => map.set(name, CATEGORY_PALETTE[index % CATEGORY_PALETTE.length]!))
    return map
  })

  function colorFor(typeId: string): string {
    const schema = nodeTypeById.value.get(typeId)
    if (!schema) return 'var(--vxs-text-faint)'
    return schema.color ?? categoryColor.value.get(schema.category) ?? 'var(--vxs-accent)'
  }

  async function loadRegistry(): Promise<void> {
    const response = await api.nodeTypes()
    nodeTypes.value = response.node_types
    categories.value = response.categories
  }

  async function loadList(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const [, list] = await Promise.all([loadRegistry(), api.pipelines()])
      summaries.value = list
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
    } finally {
      loading.value = false
    }
  }

  async function load(id: string): Promise<PipelineDefinition> {
    if (!nodeTypes.value.length) await loadRegistry()
    const detail = await api.pipeline(id)
    issues.value = detail.issues
    return detail.pipeline
  }

  async function save(definition: PipelineDefinition): Promise<void> {
    const detail = await api.savePipeline(definition.id, definition)
    issues.value = detail.issues
  }

  async function create(name: string): Promise<PipelineDefinition> {
    const id = name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '')
    const definition: PipelineDefinition = {
      id: id || `pipeline-${Date.now()}`,
      name,
      description: '',
      version: '1.0.0',
      nodes: [],
      edges: [],
      viewport: {},
    }
    const created = await api.createPipeline(definition)
    await loadList()
    return created
  }

  async function remove(id: string): Promise<void> {
    await api.deletePipeline(id)
    await loadList()
  }

  /** Keep the palette in step when a plugin is enabled or disabled elsewhere. */
  function subscribe(): void {
    if (subscribed) return
    subscribed = true
    onEvent('plugin.changed', () => {
      void loadRegistry()
    })
  }

  return {
    nodeTypes,
    categories,
    summaries,
    issues,
    loading,
    error,
    nodeTypeById,
    colorFor,
    loadRegistry,
    loadList,
    load,
    save,
    create,
    remove,
    subscribe,
  }
})
