<script setup lang="ts">
/**
 * What each release of the Studio changed (§12.1) — `CHANGELOG.md`, read-only.
 *
 * It belongs beside the update button and the version in the rail: those two say *which*
 * build this is and whether there is a newer one, and this is the third question nobody
 * could answer from inside the app — what came with it.
 *
 * **Read-only, and not by hiding a control.** The file is the Studio's own, not a document
 * of the managed project, so there is nothing here that could write it: no editor is
 * mounted, and the API surface is a single GET. It is rendered by `MarkdownText`, which
 * maps Markdown onto vnodes rather than producing an HTML string — the same renderer the
 * task transcript uses, in its document variant, since a changelog is made of headings that
 * mean what they say.
 */
import { AlertTriangle, Loader2, ScrollText, X } from 'lucide-vue-next'
import { onMounted, ref } from 'vue'

import { api } from '@/api/client'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import MarkdownText from '@/components/ui/MarkdownText.vue'
import { t } from '@/i18n'
import { useSessionStore } from '@/stores/session'

const emit = defineEmits<{ close: [] }>()

const session = useSessionStore()

const text = ref('')
const path = ref('')
const error = ref<string | null>(null)
const loading = ref(true)

// Fetched on open rather than held in a store: it changes once per release, and the dialog
// is opened about as often — there is nothing here worth keeping in memory between visits.
onMounted(async () => {
  try {
    const doc = await api.changelog()
    text.value = doc.text
    path.value = doc.path
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <ModalDialog
    :label="t('project.changelog.title')"
    panel-class="h-[85vh] w-[52rem]"
    @close="emit('close')"
  >
    <header class="flex shrink-0 items-center gap-2 border-b border-line px-4 py-3">
      <ScrollText class="size-4 shrink-0 text-accent" />
      <div class="min-w-0 flex-1">
        <h2 class="font-medium">{{ t('project.changelog.title') }}</h2>
        <!-- The build this changelog belongs to, in the same words the rail's footer uses:
             the file describes releases, and which one is running is the missing half. -->
        <p class="truncate text-small text-faint" :title="path">
          <template v-if="session.project?.version">
            {{ t('project.changelog.thisBuild', { version: session.project.version.display }) }}
          </template>
          <template v-else>{{ t('app.name') }}</template>
        </p>
      </div>
      <button
        class="grid size-6 shrink-0 place-items-center rounded-md text-faint hover:bg-hover hover:text-primary"
        :title="t('common.close')"
        @click="emit('close')"
      >
        <X class="size-4" />
      </button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto px-6 py-4">
      <p v-if="loading" class="flex items-center gap-2 text-faint">
        <Loader2 class="size-4 animate-spin" />
        {{ t('project.changelog.reading') }}
      </p>
      <p v-else-if="error" class="flex items-start gap-2 text-warning">
        <AlertTriangle class="mt-0.5 size-4 shrink-0" />
        <span class="break-words">{{ error }}</span>
      </p>
      <MarkdownText v-else :text="text" variant="document" />
    </div>
  </ModalDialog>
</template>
