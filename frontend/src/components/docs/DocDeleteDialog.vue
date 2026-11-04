<script setup lang="ts">
/**
 * Deleting a document or a folder of the Documents tree, once what goes has been said
 * (block-editor spec §5.2).
 *
 * What goes is named rather than counted, as the Explorer's confirmation names it: a folder lists
 * every document inside it, because "Delete 12 documents?" is not something anyone can agree to.
 * Pictures and anything else in the folder go with it and are said to, but not listed — the tree
 * never showed them either. The list follows the listing while the dialog is open, so a document
 * an agent writes into the folder meanwhile is on it before the button is pressed.
 *
 * The panel takes focus, not the Delete button, so a stray `Enter` cannot do the one thing here
 * that has no undo. A refusal — a folder something holds open, a file that would not go — is shown
 * in the dialog, which stays open; it cannot be closed while the delete is on its way.
 */
import { Loader2, Trash2, X } from 'lucide-vue-next'
import { computed, ref } from 'vue'

import type { DocEntry } from '@/api/types'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import { t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { useDocsStore } from '@/stores/docs'

const props = defineProps<{
  /** The document or the folder, relative to the docs root. */
  path: string
  folder: boolean
}>()

const emit = defineEmits<{ close: [] }>()

const docs = useDocsStore()

const busy = ref(false)
const refusal = ref<string | null>(null)

const name = computed(() => props.path.split('/').pop() ?? props.path)
const listed = computed(() =>
  docs.entries.filter((entry) =>
    props.folder ? entry.path.startsWith(`${props.path}/`) : entry.path === props.path,
  ),
)
/**
 * What the dialog said would go, held from the click on: the store re-reads the listing before
 * the delete resolves, and the dialog would otherwise read "0 documents" for its last frames.
 */
const held = ref<DocEntry[] | null>(null)
const documents = computed(() => held.value ?? listed.value)
/** A document is named by its title, as its row is. */
const subject = computed(() => (props.folder ? name.value : (documents.value[0]?.title ?? name.value)))

function close(): void {
  if (!busy.value) emit('close')
}

async function confirm(): Promise<void> {
  if (busy.value) return
  busy.value = true
  refusal.value = null
  held.value = listed.value
  try {
    if (props.folder) await docs.deleteFolder(props.path)
    else await docs.remove(props.path)
  } catch (cause) {
    refusal.value = cause instanceof Error ? cause.message : String(cause)
    held.value = null
    busy.value = false
    return
  }
  emit('close')
}
</script>

<template>
  <ModalDialog
    :label="folder ? t('docs.delete.folderTitle') : t('docs.delete.documentTitle')"
    panel-class="w-[32rem] h-auto"
    @close="close"
  >
    <header class="flex items-center gap-2 border-b border-line px-4 py-3">
      <Loader2 v-if="busy" class="size-4 shrink-0 animate-spin text-danger" />
      <Trash2 v-else class="size-4 shrink-0 text-danger" />
      <div class="min-w-0 flex-1">
        <h2 class="truncate font-medium">
          {{ folder ? t('docs.delete.folderTitle') : t('docs.delete.documentTitle') }}
        </h2>
        <p class="truncate text-small text-muted" :title="`voxarium_studio/docs/${path}`">
          voxarium_studio/docs/{{ path }}
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

    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <p v-if="folder">
        <I18nT keypath="docs.delete.folderBody" :params="{ count: documents.length }">
          <template #name><span class="font-medium">{{ subject }}</span></template>
        </I18nT>
      </p>
      <p v-else>
        <I18nT keypath="docs.delete.documentBody">
          <template #name><span class="font-medium">{{ subject }}</span></template>
        </I18nT>
      </p>

      <ul
        v-if="folder && documents.length"
        class="mt-2 max-h-60 overflow-y-auto rounded-md border border-line-subtle"
      >
        <li
          v-for="document in documents"
          :key="document.path"
          class="border-b border-line-subtle px-3 py-1.5 last:border-b-0"
        >
          <p class="truncate">{{ document.title }}</p>
          <p class="truncate font-mono text-small text-faint" :title="document.path">
            {{ document.path.slice(path.length + 1) }}
          </p>
        </li>
      </ul>

      <p class="mt-3 text-small text-faint">
        {{ folder ? t('docs.delete.linksFolder') : t('docs.delete.linksDocument') }}
      </p>
      <p v-if="refusal" class="mt-3 break-words text-danger">{{ refusal }}</p>
    </div>

    <footer class="flex justify-end gap-2 border-t border-line px-4 py-3">
      <button
        type="button"
        class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
        :disabled="busy"
        @click="close"
      >
        {{ t('common.cancel') }}
      </button>
      <button
        type="button"
        class="rounded-md bg-danger px-3 py-1.5 font-medium disabled:opacity-50"
        :disabled="busy"
        @click="confirm"
      >
        <!-- On the label, not the button: `base.css` gives a `<button>` an unlayered
             `color: inherit`, which outranks a colour utility on it. -->
        <span class="text-on-accent">{{ busy ? t('common.deleting') : t('common.delete') }}</span>
      </button>
    </footer>
  </ModalDialog>
</template>
