<script setup lang="ts">
/**
 * Renaming a folder of the Documents tree, and watching its links being mended (block-editor
 * spec §5.2).
 *
 * One dialog for both halves, because they are one gesture: the name is typed, and the same
 * panel then shows what the rename did. First the folder moves; then every document whose
 * links spelled out its path is rewritten, each row ticking over as its file is written — the
 * backend streams a frame per document (`POST /api/docs/folders/rename`), so what is on screen
 * is the work as it happens, not a spinner followed by a list.
 *
 * Three moments decide what it says:
 *
 * - **Refused before anything moved** — a taken name, a character a folder cannot hold, a
 *   folder the system will not let go of. The field stays open with the reason under it, to be
 *   corrected rather than started again.
 * - **Moved, and every link mended.** The summary says how many links in how many documents.
 * - **Moved, and something did not go through** — a document that could not be written, or
 *   the stream cut off. The folder has its new name either way, so the dialog reports the
 *   failures row by row and never pretends the rename can be taken back by closing it.
 *
 * It cannot be closed while the rename runs: nothing about the rename stops with the dialog,
 * and a closed dialog would only hide which documents were still being written.
 */
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  FolderPen,
  Loader2,
  Minus,
  X,
} from 'lucide-vue-next'
import { computed, nextTick, onMounted, ref } from 'vue'

import type { DocFolderRenameDone, DocFolderRenameFrame, DocRelink } from '@/api/types'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import { formatNumber, t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { useDocsStore } from '@/stores/docs'

const props = defineProps<{
  /** The folder, relative to the docs root. */
  path: string
}>()

const emit = defineEmits<{ close: [] }>()

const docs = useDocsStore()

type Phase = 'name' | 'settling' | 'renaming' | 'finished'

interface Row extends DocRelink {
  status: 'pending' | 'updated' | 'unchanged' | 'failed'
  error: string | null
}

const currentName = props.path.split('/').pop() ?? props.path
const parent = props.path.includes('/') ? props.path.slice(0, props.path.lastIndexOf('/') + 1) : ''

const phase = ref<Phase>('name')
const draft = ref(currentName)
/** Why the rename was refused; shown under the field, which stays open. */
const refusal = ref<string | null>(null)
/** Set once the folder has moved — from then on nothing is "refused", only reported. */
const renamedTo = ref<string | null>(null)
const moved = ref(0)
const rows = ref<Row[]>([])
const summary = ref<DocFolderRenameDone | null>(null)
/** The rename moved the folder and then stopped short of saying it was done. */
const interrupted = ref<string | null>(null)

const input = ref<HTMLInputElement | null>(null)
const closeButton = ref<HTMLButtonElement | null>(null)

const locked = computed(() => phase.value === 'settling' || phase.value === 'renaming')
const newName = computed(() => draft.value.trim())
const canSubmit = computed(() => phase.value === 'name' && newName.value.length > 0)

const handled = computed(() => rows.value.filter((row) => row.status !== 'pending').length)
const failedRows = computed(() => rows.value.filter((row) => row.status === 'failed'))
const progress = computed(() =>
  rows.value.length ? Math.round((handled.value / rows.value.length) * 100) : 100,
)

const heading = computed(() => {
  if (phase.value === 'name' || phase.value === 'settling') return t('docs.folderRename.title')
  const to = (renamedTo.value ?? `${parent}${newName.value}`).split('/').pop() ?? ''
  return phase.value === 'finished'
    ? t('docs.folderRename.renamed', { from: currentName, to })
    : t('docs.folderRename.renaming', { from: currentName, to })
})

const subtitle = computed(() => {
  if (phase.value === 'name') return `voxarium_studio/docs/${props.path}`
  if (phase.value === 'settling') return t('docs.folderRename.settling')
  if (phase.value === 'renaming') {
    if (renamedTo.value === null) return t('docs.folderRename.moving')
    return rows.value.length
      ? t('docs.folderRename.updating', {
          handled: formatNumber(handled.value),
          count: rows.value.length,
        })
      : t('docs.folderRename.looking')
  }
  if (interrupted.value) return t('docs.folderRename.interrupted')
  if (failedRows.value.length) {
    return t('docs.folderRename.failed', { count: failedRows.value.length })
  }
  const done = summary.value
  if (!done || done.links === 0) return t('docs.folderRename.nothingLinked')
  // Two quantities, so two plurals: the verb agrees with the links, the phrase with the documents.
  return t('docs.folderRename.updatedLinks', {
    count: done.links,
    documents: t('docs.folderRename.updatedLinksIn', { count: done.updated }),
  })
})

onMounted(async () => {
  await nextTick()
  const field = input.value
  if (!field) return
  field.focus()
  field.select()
})

function close(): void {
  if (!locked.value) emit('close')
}

function onFrame(frame: DocFolderRenameFrame): void {
  if (frame.type === 'renamed') {
    renamedTo.value = frame.path
    moved.value = frame.moved
    rows.value = frame.documents.map((document) => ({ ...document, status: 'pending', error: null }))
  } else if (frame.type === 'document') {
    const row = rows.value.find((candidate) => candidate.path === frame.path)
    if (row) {
      row.status = frame.status
      row.links = frame.links
      row.error = frame.error
    }
  }
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  if (newName.value === currentName) {
    emit('close')
    return
  }
  refusal.value = null
  phase.value = 'settling'
  // A save still on its way would land after the rename — on a path that no longer exists,
  // recreating the old folder around one document.
  await docs.settle()
  if (docs.dirty) {
    phase.value = 'name'
    refusal.value = t('docs.folderRename.unsaved')
    return
  }

  phase.value = 'renaming'
  try {
    const done = await docs.renameFolder(props.path, newName.value, onFrame)
    summary.value = done
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : String(cause)
    if (renamedTo.value === null) {
      // Nothing moved: back to the field, with the reason beside it.
      phase.value = 'name'
      refusal.value = message
      await nextTick()
      input.value?.focus()
      return
    }
    interrupted.value = message
  }
  phase.value = 'finished'
  await nextTick()
  closeButton.value?.focus()
}
</script>

<template>
  <ModalDialog :label="t('docs.folderRename.title')" panel-class="w-[36rem] h-auto" @close="close">
    <header class="flex items-center gap-2 border-b border-line px-4 py-3">
      <Loader2 v-if="locked" class="size-4 shrink-0 animate-spin text-accent" />
      <AlertTriangle
        v-else-if="phase === 'finished' && (interrupted || failedRows.length)"
        class="size-4 shrink-0 text-warning"
      />
      <CheckCircle2 v-else-if="phase === 'finished'" class="size-4 shrink-0 text-success" />
      <FolderPen v-else class="size-4 shrink-0 text-accent" />
      <div class="min-w-0 flex-1">
        <h2 class="truncate font-medium">{{ heading }}</h2>
        <p class="truncate text-small text-muted" :title="subtitle">{{ subtitle }}</p>
      </div>
      <button
        type="button"
        class="rounded-md p-1 text-muted hover:bg-hover hover:text-primary disabled:opacity-40"
        :disabled="locked"
        :title="t('common.close')"
        @click="close"
      >
        <X class="size-4" />
      </button>
    </header>

    <form v-if="phase === 'name' || phase === 'settling'" class="contents" @submit.prevent="submit">
      <div class="p-4">
        <div class="flex items-center rounded-md border border-line bg-inset focus-within:border-accent">
          <span
            v-if="parent"
            class="max-w-[45%] shrink-0 truncate pl-2 font-mono text-faint"
            :title="parent"
            >{{ parent }}</span
          >
          <input
            ref="input"
            v-model="draft"
            class="min-w-0 flex-1 bg-transparent px-2 py-1 font-mono outline-none"
            :class="parent && 'pl-0'"
            :aria-label="t('docs.folderRename.nameLabel')"
            spellcheck="false"
            :disabled="phase === 'settling'"
          />
        </div>
        <p v-if="refusal" class="mt-2 break-words text-danger">{{ refusal }}</p>
        <p class="mt-2 text-small text-faint">
          <I18nT keypath="docs.folderRename.hint">
            <template #link><code class="font-mono">[[{{ path }}/…]]</code></template>
          </I18nT>
        </p>
      </div>

      <footer class="flex justify-end gap-2 border-t border-line px-4 py-3">
        <button
          type="button"
          class="rounded-md border border-line px-3 py-1.5 hover:bg-hover"
          @click="close"
        >
          {{ t('common.cancel') }}
        </button>
        <button
          type="submit"
          class="vxs-btn-primary px-3 py-1.5 font-medium text-on-accent disabled:opacity-50"
          :disabled="!canSubmit"
        >
          {{ t('common.rename') }}
        </button>
      </footer>
    </form>

    <template v-else>
      <div class="min-h-0 flex-1 overflow-y-auto p-4">
        <!-- How far along the documents are. Before the folder has moved there is nothing to
             count, and the bar sits empty rather than pretending. -->
        <div
          class="h-1.5 overflow-hidden rounded-pill bg-inset"
          role="progressbar"
          :aria-label="t('docs.folderRename.progress')"
          :aria-valuenow="renamedTo === null ? 0 : progress"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <div
            class="h-full rounded-pill bg-accent"
            :style="{ width: `${renamedTo === null ? 0 : progress}%` }"
          />
        </div>

        <p class="mt-3 text-small text-muted">
          <!-- Two interpolations, not two `<template v-if>`: the compiler drops the whitespace
               around a template holding only an interpolation, and the sentences would touch. -->
          {{ renamedTo !== null ? t('docs.folderRename.movedWith', { count: moved }) : '' }}
          {{ rows.length ? t('docs.folderRename.linking', { count: rows.length }) : '' }}
        </p>

        <ul v-if="rows.length" class="mt-2 max-h-72 overflow-y-auto rounded-md border border-line-subtle">
          <li
            v-for="row in rows"
            :key="row.path"
            class="flex items-start gap-2 border-b border-line-subtle px-3 py-2 last:border-b-0"
          >
            <Loader2 v-if="row.status === 'pending'" class="mt-0.5 size-3.5 shrink-0 animate-spin text-faint" />
            <Check v-else-if="row.status === 'updated'" class="mt-0.5 size-3.5 shrink-0 text-success" />
            <Minus v-else-if="row.status === 'unchanged'" class="mt-0.5 size-3.5 shrink-0 text-faint" />
            <AlertTriangle v-else class="mt-0.5 size-3.5 shrink-0 text-danger" />

            <div class="min-w-0 flex-1">
              <p class="truncate">{{ row.title }}</p>
              <p class="truncate font-mono text-small text-faint" :title="row.path">{{ row.path }}</p>
              <p v-if="row.error" class="mt-0.5 break-words text-small text-danger">{{ row.error }}</p>
            </div>

            <span class="shrink-0 text-small tabular-nums text-muted">
              <template v-if="row.status === 'unchanged'">{{ t('docs.folderRename.noChange') }}</template>
              <template v-else-if="row.status === 'failed'">{{ t('docs.folderRename.notUpdated') }}</template>
              <template v-else>{{ t('docs.folderRename.links', { count: row.links }) }}</template>
            </span>
          </li>
        </ul>

        <p v-if="interrupted" class="mt-3 flex items-start gap-2 text-warning">
          <AlertTriangle class="mt-0.5 size-4 shrink-0" />
          <span class="break-words">{{ interrupted }}</span>
        </p>
      </div>

      <footer class="flex justify-end gap-2 border-t border-line px-4 py-3">
        <button
          ref="closeButton"
          type="button"
          class="vxs-btn-primary px-3 py-1.5 font-medium text-on-accent disabled:opacity-50"
          :disabled="locked"
          @click="close"
        >
          {{ t('common.close') }}
        </button>
      </footer>
    </template>
  </ModalDialog>
</template>
