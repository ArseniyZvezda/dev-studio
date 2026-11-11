<script setup lang="ts">
/**
 * Import into project memory what the agent runtimes remember on their own (§10.12).
 *
 * Claude Code, Codex and Qwen Code each keep a memory in the user's profile that the other two
 * never see. This lists what each has — for this project, and Codex's one handbook for all of
 * them — as the entries they would become, and writes the chosen ones. The backend reads the
 * files again on import, so what is saved is the runtime's text, not what this dialog showed.
 *
 * Nothing is taken silently: an entry whose name is already used by other text is left
 * unticked, and ticking it is what agrees to replace it.
 */
import { AlertTriangle, ChevronRight, Download, X } from 'lucide-vue-next'
import { computed, onMounted, reactive, ref } from 'vue'

import { api } from '@/api/client'
import type { AgentMemoryCandidate, AgentMemoryImportResult, AgentMemorySource } from '@/api/types'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import { t } from '@/i18n'
import { defaultImportSelection } from '@/lib/memory'
import { skillBackendLabel } from '@/lib/skills'

defineProps<{ folder: string }>()
const emit = defineEmits<{ close: []; imported: [result: AgentMemoryImportResult] }>()

const sources = ref<AgentMemorySource[] | null>(null)
const error = ref<string | null>(null)
const busy = ref(false)
const selected = reactive(new Set<string>())
const expanded = reactive(new Set<string>())
/** What the last import left out, by candidate id — shown on the row it concerns. */
const skipped = ref<AgentMemoryImportResult['skipped']>([])

const candidates = computed(() => (sources.value ?? []).flatMap((source) => source.candidates))
const chosen = computed(() => candidates.value.filter((item) => selected.has(item.id)))
const replacing = computed(() => chosen.value.filter((item) => item.state === 'differs').length)

async function load(preselect: boolean): Promise<void> {
  try {
    const result = await api.agentMemory()
    sources.value = result.sources
    const known = new Set(result.sources.flatMap((source) => source.candidates.map((item) => item.id)))
    for (const id of [...selected]) if (!known.has(id)) selected.delete(id)
    if (preselect) for (const id of defaultImportSelection(result.sources)) selected.add(id)
    error.value = null
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  }
}

onMounted(() => void load(true))

function selectable(item: AgentMemoryCandidate): boolean {
  return item.state !== 'same'
}

function toggle(item: AgentMemoryCandidate): void {
  if (selected.has(item.id)) selected.delete(item.id)
  else selected.add(item.id)
}

function toggleText(id: string): void {
  if (expanded.has(id)) expanded.delete(id)
  else expanded.add(id)
}

/** New ones only: replacing an entry is agreed to one at a time, never by "select all". */
function fresh(source: AgentMemorySource): AgentMemoryCandidate[] {
  return source.candidates.filter((item) => item.state === 'new')
}

function allSelected(source: AgentMemorySource): boolean {
  const items = fresh(source)
  return items.length > 0 && items.every((item) => selected.has(item.id))
}

function toggleAll(source: AgentMemorySource): void {
  const on = !allSelected(source)
  for (const item of fresh(source)) {
    if (on) selected.add(item.id)
    else selected.delete(item.id)
  }
}

function skipReason(item: AgentMemoryCandidate): string | null {
  const entry = skipped.value.find((row) => row.id === item.id)
  if (!entry || entry.reason === 'same') return null
  if (entry.reason === 'error') return entry.error ?? t('memory.import.skipped.error')
  return t(`memory.import.skipped.${entry.reason}`)
}

async function run(): Promise<void> {
  if (!chosen.value.length || busy.value) return
  busy.value = true
  try {
    const result = await api.importAgentMemory(
      chosen.value.map((item) => ({ id: item.id, replace: item.state === 'differs' })),
    )
    emit('imported', result)
    const problems = result.skipped.filter((row) => row.reason !== 'same')
    if (!problems.length) {
      emit('close')
      return
    }
    // Something was left out: stay, with the list as it now is and the reason on each row.
    skipped.value = problems
    selected.clear()
    await load(false)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <ModalDialog :label="t('memory.import.title')" panel-class="w-[46rem]" @close="emit('close')">
    <header class="flex shrink-0 items-center gap-2 border-b border-line px-4 py-3">
      <Download class="size-4 shrink-0 text-accent" />
      <h2 class="min-w-0 flex-1 font-medium">{{ t('memory.import.title') }}</h2>
      <button
        class="grid size-6 shrink-0 place-items-center rounded-md text-faint hover:bg-hover hover:text-primary"
        :title="t('common.close')"
        :aria-label="t('common.close')"
        @click="emit('close')"
      >
        <X class="size-4" />
      </button>
    </header>

    <div class="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
      <p class="text-muted">{{ t('memory.import.intro') }}</p>
      <p class="flex items-start gap-1.5 text-small text-faint">
        <AlertTriangle class="mt-0.5 size-3.5 shrink-0" />
        {{ t('memory.import.shared', { folder }) }}
      </p>

      <p v-if="error" class="rounded-md bg-danger-soft px-2 py-1 text-danger">{{ error }}</p>
      <p v-if="skipped.length" class="rounded-md bg-warning-soft px-2 py-1 text-small text-warning">
        {{ t('memory.import.skipped.summary', { count: skipped.length }) }}
      </p>

      <LoadingBars v-if="!sources && !error" />

      <section v-for="source in sources ?? []" :key="source.backend">
        <div class="mb-1.5 flex items-center gap-2">
          <h3 class="font-medium">{{ skillBackendLabel(source.backend) }}</h3>
          <span v-if="source.candidates.length" class="text-small text-faint">
            {{ t('memory.import.found', { count: source.candidates.length }) }}
          </span>
          <span class="flex-1" />
          <button
            v-if="fresh(source).length"
            class="text-small text-muted hover:text-primary"
            @click="toggleAll(source)"
          >
            {{ allSelected(source) ? t('memory.import.selectNone') : t('memory.import.selectAll') }}
          </button>
        </div>

        <div v-if="!source.candidates.length" class="rounded-lg border border-line bg-inset p-3 text-small text-faint">
          <p>{{ t('memory.import.nothing') }}</p>
          <p v-for="place in source.locations" :key="place" class="font-mono">{{ place }}</p>
        </div>

        <ul v-else class="divide-y divide-line rounded-lg border border-line bg-surface">
          <li v-for="item in source.candidates" :key="item.id" class="p-2.5">
            <div class="flex items-start gap-2.5">
              <input
                type="checkbox"
                class="mt-1 accent-[var(--vxs-accent)]"
                :checked="selected.has(item.id)"
                :disabled="!selectable(item) || busy"
                :aria-label="item.name"
                @change="toggle(item)"
              />
              <div class="min-w-0 flex-1">
                <p class="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span class="import-mono font-medium" :class="!selectable(item) && 'text-faint'">{{ item.name }}</span>
                  <span v-if="item.kind" class="rounded bg-inset px-1.5 text-small text-muted">{{ item.kind }}</span>
                  <span v-if="item.scope === 'global'" class="rounded bg-inset px-1.5 text-small text-muted">
                    {{ item.related ? t('memory.import.globalRelated') : t('memory.import.global') }}
                  </span>
                  <span v-if="item.state === 'same'" class="text-small text-faint">{{ t('memory.import.same') }}</span>
                  <span v-else-if="item.state === 'differs'" class="text-small text-warning">
                    {{ selected.has(item.id) ? t('memory.import.replaces') : t('memory.import.taken') }}
                  </span>
                </p>
                <p class="text-muted" :class="!selectable(item) && 'opacity-60'">{{ item.description }}</p>
                <p v-if="skipReason(item)" class="mt-0.5 flex items-start gap-1.5 text-small text-warning">
                  <AlertTriangle class="mt-0.5 size-3.5 shrink-0" /> {{ skipReason(item) }}
                </p>
                <p class="mt-0.5 flex flex-wrap items-center gap-x-2 text-small text-faint">
                  <button
                    v-if="item.content"
                    class="inline-flex items-center gap-0.5 text-muted hover:text-primary"
                    :aria-expanded="expanded.has(item.id)"
                    @click="toggleText(item.id)"
                  >
                    <ChevronRight class="size-3.5 transition-transform" :class="expanded.has(item.id) && 'rotate-90'" />
                    {{ t('memory.import.text') }}
                  </button>
                  <span class="truncate font-mono" :title="item.source">{{ item.source }}</span>
                  <span v-if="item.truncated" class="text-warning">{{ t('memory.import.truncated') }}</span>
                </p>
                <pre
                  v-if="item.content && expanded.has(item.id)"
                  class="import-mono mt-1.5 max-h-56 overflow-auto whitespace-pre-wrap rounded-md border border-line bg-inset p-2"
                >{{ item.content }}</pre>
              </div>
            </div>
          </li>
        </ul>
        <p v-if="source.truncated" class="mt-1 text-small text-warning">{{ t('memory.import.listCut') }}</p>
      </section>
    </div>

    <footer class="flex shrink-0 items-center gap-2 border-t border-line px-4 py-3">
      <p class="min-w-0 flex-1 text-small" :class="replacing ? 'text-warning' : 'text-faint'">
        <template v-if="replacing">{{ t('memory.import.replacing', { count: replacing }) }}</template>
        <template v-else-if="sources && !candidates.length">{{ t('memory.import.empty') }}</template>
      </p>
      <button type="button" class="rounded-md border border-line px-3 py-1 hover:bg-hover" @click="emit('close')">
        {{ t('common.cancel') }}
      </button>
      <button
        type="button"
        class="vxs-btn-primary px-3 py-1 disabled:opacity-50"
        :disabled="!chosen.length || busy"
        @click="run"
      >
        {{ t('memory.import.confirm', { count: chosen.length }) }}
      </button>
    </footer>
  </ModalDialog>
</template>

<style scoped>
/* Same reason as MemoryView's `.memory-mono`: an unlayered `font: inherit` outranks `font-mono`. */
.import-mono {
  font-family: var(--vxs-font-mono);
}

pre.import-mono {
  font-size: 12px;
}
</style>
