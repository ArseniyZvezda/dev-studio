<script setup lang="ts">
/**
 * A new document in a folder of the Documents tree — the folder menu's "New document…"
 * (block-editor spec §5.2).
 *
 * The folder is fixed and only the name is typed, so where the document lands is the row that was
 * right-clicked, never a path retyped by hand. The folder stands in front of the field the way the
 * rename dialog shows a parent, and the line under it says what the file will be called — `.md`
 * included, which is the part nobody types.
 *
 * The field stays open when the name is refused — taken (the write is exclusive, so a document
 * already there is a 409 rather than a document overwritten), or a path the backend will not take
 * — to be corrected rather than started again.
 */
import { FilePlus2, Loader2, X } from 'lucide-vue-next'
import { computed, nextTick, onMounted, ref } from 'vue'

import ModalDialog from '@/components/ui/ModalDialog.vue'
import { t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { titleOf } from '@/lib/md/title'
import { documentPath, useDocsStore } from '@/stores/docs'

const props = defineProps<{
  /** The folder, relative to the docs root. */
  folder: string
}>()

const emit = defineEmits<{ close: []; created: [path: string] }>()

const docs = useDocsStore()

const draft = ref('')
const busy = ref(false)
/** Why the document was not created; shown under the field, which stays open. */
const refusal = ref<string | null>(null)
const input = ref<HTMLInputElement | null>(null)

const path = computed(() => documentPath(props.folder, draft.value))
const canSubmit = computed(() => !busy.value && path.value !== '')

onMounted(async () => {
  await nextTick()
  input.value?.focus()
})

function close(): void {
  if (!busy.value) emit('close')
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  busy.value = true
  refusal.value = null
  try {
    // Not opened here: the page opens it, through the path that writes the outgoing document first.
    emit('created', await docs.createDocument(path.value, { open: false }))
  } catch (cause) {
    refusal.value = cause instanceof Error ? cause.message : String(cause)
    busy.value = false
    await nextTick()
    input.value?.focus()
  }
}
</script>

<template>
  <ModalDialog :label="t('docs.newDocument')" panel-class="w-[32rem] h-auto" @close="close">
    <header class="flex items-center gap-2 border-b border-line px-4 py-3">
      <Loader2 v-if="busy" class="size-4 shrink-0 animate-spin text-accent" />
      <FilePlus2 v-else class="size-4 shrink-0 text-accent" />
      <div class="min-w-0 flex-1">
        <h2 class="truncate font-medium">{{ t('docs.newDocument') }}</h2>
        <p class="truncate text-small text-muted" :title="`voxarium_studio/docs/${folder}`">
          {{ t('docs.create.in', { path: `voxarium_studio/docs/${folder}` }) }}
        </p>
      </div>
      <button
        type="button"
        class="rounded-md p-1 text-muted hover:bg-hover hover:text-primary disabled:opacity-40"
        :disabled="busy"
        :title="t('common.close')"
        @click="close"
      >
        <X class="size-4" />
      </button>
    </header>

    <form class="contents" @submit.prevent="submit">
      <div class="p-4">
        <div class="flex items-center rounded-md border border-line bg-inset focus-within:border-accent">
          <span
            class="max-w-[45%] shrink-0 truncate pl-2 font-mono text-faint"
            :title="`${folder}/`"
            >{{ folder }}/</span
          >
          <input
            ref="input"
            v-model="draft"
            class="min-w-0 flex-1 bg-transparent py-1 pr-2 font-mono outline-none"
            :aria-label="t('docs.create.nameLabel')"
            :placeholder="t('docs.create.namePlaceholder')"
            spellcheck="false"
            :disabled="busy"
          />
        </div>
        <p v-if="refusal" class="mt-2 break-words text-danger">{{ refusal }}</p>
        <p class="mt-2 text-small text-faint">
          <I18nT v-if="path" keypath="docs.create.createdAs" :params="{ title: titleOf('', path) }">
            <template #path><code class="font-mono">{{ path }}</code></template>
          </I18nT>
          <I18nT v-else keypath="docs.create.nameHint">
            <template #ext><code class="font-mono">.md</code></template>
          </I18nT>
        </p>
      </div>

      <footer class="flex justify-end gap-2 border-t border-line px-4 py-3">
        <button
          type="button"
          class="rounded-md border border-line px-3 py-1.5 hover:bg-hover"
          :disabled="busy"
          @click="close"
        >
          {{ t('common.cancel') }}
        </button>
        <button
          type="submit"
          class="vxs-btn-primary px-3 py-1.5 font-medium text-on-accent disabled:opacity-50"
          :disabled="!canSubmit"
        >
          {{ t('common.create') }}
        </button>
      </footer>
    </form>
  </ModalDialog>
</template>
