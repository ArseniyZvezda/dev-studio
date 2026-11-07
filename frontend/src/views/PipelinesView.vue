<script setup lang="ts">
import { Plus, Trash2, Workflow } from 'lucide-vue-next'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import EmptyState from '@/components/ui/EmptyState.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import { t } from '@/i18n'
import { usePipelinesStore } from '@/stores/pipelines'

const router = useRouter()
const pipelines = usePipelinesStore()

const creating = ref(false)
const newName = ref('')
const actionError = ref<string | null>(null)

onMounted(() => {
  pipelines.subscribe()
  void pipelines.loadList()
})

async function create() {
  const name = newName.value.trim()
  if (!name) return
  actionError.value = null
  try {
    const created = await pipelines.create(name)
    creating.value = false
    newName.value = ''
    await router.push(`/pipelines/${created.id}`)
  } catch (cause) {
    actionError.value = cause instanceof Error ? cause.message : String(cause)
  }
}

async function remove(id: string, name: string) {
  if (!window.confirm(t('pipelines.confirmDelete', { name }))) {
    return
  }
  try {
    await pipelines.remove(id)
  } catch (cause) {
    actionError.value = cause instanceof Error ? cause.message : String(cause)
  }
}
</script>

<template>
  <div class="flex h-full flex-col">
    <header class="vxs-pane flex shrink-0 items-center gap-2 border-b border-pane bg-surface px-pane py-2">
      <h1 class="flex-1 text-title font-medium">{{ t('pipelines.title') }}</h1>
      <button
        class="inline-flex items-center gap-1.5 vxs-btn-primary px-3 py-1.5 text-on-accent"
        @click="creating = true"
      >
        <Plus class="size-3.5" /> {{ t('pipelines.newPipeline') }}
      </button>
    </header>

    <p v-if="actionError" class="shrink-0 bg-danger-soft px-pane py-1 text-danger">
      {{ actionError }}
    </p>

    <form
      v-if="creating"
      class="flex shrink-0 items-center gap-2 border-b border-line bg-elevated px-pane py-2"
      @submit.prevent="create"
    >
      <input
        v-model="newName"
        autofocus
        :placeholder="t('pipelines.namePlaceholder')"
        class="flex-1 rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
      />
      <button type="submit" class="vxs-btn-primary px-3 py-1 text-on-accent">{{ t('common.create') }}</button>
      <button
        type="button"
        class="rounded-md border border-line px-3 py-1 hover:bg-hover"
        @click="creating = false"
      >
        {{ t('common.cancel') }}
      </button>
    </form>

    <div class="min-h-0 flex-1 overflow-auto">
      <!-- Bars only for a first read: the list is re-read after a delete, and swapping it for a
           placeholder there would take it off screen and bring it back in again (§7). -->
      <div v-if="pipelines.loading && !pipelines.summaries.length" class="p-pane">
        <LoadingBars />
      </div>
      <p v-else-if="pipelines.error" class="p-pane text-danger">{{ pipelines.error }}</p>

      <EmptyState
        v-else-if="!pipelines.summaries.length"
        class="vxs-enter"
        :title="t('pipelines.empty.title')"
        :hint="t('pipelines.empty.hint', { dir: '.voxarium_studio/pipelines' })"
      >
        <template #icon><Workflow class="mx-auto mb-3 size-8 text-faint" /></template>
      </EmptyState>

      <ul v-else class="vxs-enter p-pane grid gap-2" style="grid-template-columns: repeat(auto-fill, minmax(260px, 1fr))">
        <li
          v-for="pipeline in pipelines.summaries"
          :key="pipeline.id"
          class="group cursor-pointer rounded-lg border border-line bg-surface p-3 hover:border-accent-border"
          @click="router.push(`/pipelines/${pipeline.id}`)"
        >
          <div class="flex items-start gap-2">
            <Workflow class="mt-0.5 size-4 shrink-0 text-accent" />
            <div class="min-w-0 flex-1">
              <p class="truncate font-medium">{{ pipeline.name }}</p>
              <p class="truncate font-mono text-small text-faint">{{ pipeline.id }}</p>
            </div>
            <button
              class="grid size-6 shrink-0 place-items-center rounded-sm text-faint opacity-0 group-hover:opacity-100 hover:bg-danger-soft hover:text-danger"
              :title="t('common.delete')"
              @click.stop="remove(pipeline.id, pipeline.name)"
            >
              <Trash2 class="size-3.5" />
            </button>
          </div>
          <p v-if="pipeline.description" class="mt-2 line-clamp-2 text-muted">
            {{ pipeline.description }}
          </p>
          <p class="mt-2 text-small text-faint">
            {{ t('pipelines.counts.nodes', { count: pipeline.node_count }) }} ·
            {{ t('pipelines.counts.edges', { count: pipeline.edge_count }) }}
          </p>
        </li>
      </ul>
    </div>
  </div>
</template>
