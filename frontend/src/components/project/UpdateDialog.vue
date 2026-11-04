<script setup lang="ts">
/**
 * An update happening (§12.1).
 *
 * The rail's button starts the update and opens this, so what it mostly does is *report*:
 * which release, which command is running, and every line that command printed. That is the
 * whole reason it is a dialog rather than a toast — a pull and a rebuild take a minute, and
 * when one fails the only useful thing on screen is the tool's own message.
 *
 * The two moments it asks something are the refusals. The backend answers 409 when the
 * Studio's checkout has uncommitted changes, or when a pipeline run or agent session would
 * be killed by the restart; both arrive as text with **Update anyway** beside them, so
 * nothing is overruled without being read first.
 *
 * It refuses to close once the server is on its way out — `close` is simply not forwarded —
 * because there is nothing behind it to use and the page reloads itself when the new one
 * answers.
 */
import { AlertTriangle, CheckCircle2, Loader2, RefreshCw, X } from 'lucide-vue-next'
import { computed, nextTick, ref, watch } from 'vue'

import ModalDialog from '@/components/ui/ModalDialog.vue'
import { t } from '@/i18n'
import { useUpdatesStore } from '@/stores/updates'

const emit = defineEmits<{ close: [] }>()

const updates = useUpdatesStore()

const transcript = ref<HTMLElement | null>(null)

const status = computed(() => updates.status)
const failed = computed(() => updates.state === 'failed')
const restarting = computed(() => updates.state === 'restarting')

/** Nothing may close the dialog once this process's own shutdown is under way. */
const locked = computed(() => updates.running || updates.waiting !== null)

const heading = computed(() => {
  const from = status.value?.current
  const to = status.value?.latest
  if (!to) return t('updates.dialog.update')
  return from
    ? t('updates.dialog.updatingFromTo', { from, to })
    : t('updates.dialog.updatingTo', { to })
})

const subtitle = computed(() => {
  if (updates.desktop) {
    if (restarting.value) return t('updates.desktop.restarting')
    if (failed.value) return t('updates.dialog.stopped')
    if (updates.downloaded) return t('updates.desktop.ready')
    if (updates.running) return t('updates.desktop.downloading', { percent: updates.progress })
    if (updates.state === 'checking') return t('updates.desktop.checking')
    return t('updates.desktop.intro')
  }
  if (updates.waiting) return updates.waiting
  if (failed.value) return t('updates.dialog.stopped')
  if (restarting.value) return t('updates.dialog.restarting')
  if (updates.state === 'updating') return status.value?.step ?? t('updates.dialog.working')
  if (updates.refusal) return t('updates.dialog.heldBack')
  return t('updates.dialog.intro')
})

// A transcript that does not follow the command is one nobody reads while it runs.
watch(
  () => updates.log.length,
  async () => {
    await nextTick()
    const element = transcript.value
    if (element) element.scrollTop = element.scrollHeight
  },
)

function close(): void {
  if (!locked.value) emit('close')
}
</script>

<template>
  <ModalDialog :label="t('updates.dialog.label')" panel-class="w-[46rem] h-auto" @close="close">
    <header class="flex items-center gap-2 border-b border-line px-4 py-3">
      <Loader2
        v-if="updates.running || updates.waiting"
        class="size-4 shrink-0 animate-spin text-accent"
      />
      <AlertTriangle v-else-if="failed" class="size-4 shrink-0 text-danger" />
      <CheckCircle2 v-else-if="restarting" class="size-4 shrink-0 text-success" />
      <RefreshCw v-else class="size-4 shrink-0 text-accent" />
      <div class="min-w-0 flex-1">
        <h2 class="truncate font-medium">{{ heading }}</h2>
        <p class="truncate text-small text-muted">{{ subtitle }}</p>
      </div>
      <button
        class="rounded-md p-1 text-muted hover:bg-hover hover:text-primary disabled:opacity-40"
        :disabled="locked"
        :title="t('common.close')"
        @click="close"
      >
        <X class="size-4" />
      </button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-4">
      <!-- A refusal, and the choice it exists to offer. -->
      <div v-if="updates.refusal" class="mb-3 rounded-md border border-line bg-inset p-3">
        <p class="flex items-start gap-2 text-warning">
          <AlertTriangle class="mt-0.5 size-4 shrink-0" />
          <span>{{ updates.refusal }}</span>
        </p>
        <div class="mt-3 flex gap-2">
          <button
            class="vxs-btn-primary px-3 py-1.5 disabled:opacity-50"
            :disabled="updates.busy"
            @click="updates.apply(true)"
            v-if="!updates.desktop"
          >
            {{ t('updates.dialog.updateAnyway') }}
          </button>
          <button
            class="rounded-md border border-line px-3 py-1.5 hover:bg-hover"
            @click="close"
          >
            {{ t('common.cancel') }}
          </button>
        </div>
      </div>

      <p v-if="failed && status?.error" class="mb-3 flex items-start gap-2 text-danger">
        <AlertTriangle class="mt-0.5 size-4 shrink-0" />
        <span class="break-words whitespace-pre-wrap">{{ status.error }}</span>
      </p>

      <!-- Exactly what the commands printed. Wrapped, because a git or npm error is a
           paragraph and a horizontal scroller hides the end of it. -->
      <pre
        v-if="updates.log.length"
        ref="transcript"
        class="max-h-80 overflow-y-auto rounded-md bg-inset p-3 font-mono text-small whitespace-pre-wrap text-muted"
        >{{ updates.log.join('\n') }}</pre
      >
      <div v-else-if="updates.desktop && !updates.refusal" class="space-y-3">
        <progress v-if="updates.running && !restarting" class="w-full" :value="updates.progress" max="100" :aria-label="t('updates.desktop.downloading', { percent: updates.progress })" />
        <p>{{ t(updates.downloaded ? 'updates.desktop.ready' : updates.available ? 'updates.desktop.intro' : updates.state === 'checking' ? 'updates.desktop.checking' : failed ? 'updates.dialog.stopped' : !status?.checked_at ? 'updates.desktop.unchecked' : 'updates.desktop.upToDate') }}</p>
      </div>
      <p v-else-if="!updates.refusal" class="text-muted">
        {{
          t('updates.dialog.willPull', {
            release: status?.latest ?? t('updates.dialog.newRelease'),
          })
        }}
      </p>
    </div>

    <footer class="flex items-center justify-between gap-2 border-t border-line px-4 py-3">
      <p class="truncate text-small text-faint">
        <span v-if="status?.branch">{{ status.branch }}</span>
        <span v-if="status?.upstream"> {{ t('updates.dialog.tracking', { upstream: status.upstream }) }}</span>
      </p>
      <div class="flex gap-2">
        <button v-if="updates.desktop && updates.downloaded" class="vxs-btn-primary px-3 py-1.5 disabled:opacity-50" :disabled="updates.busy || updates.running" @click="updates.installDesktop()">
          {{ t('updates.desktop.install') }}
        </button>
        <button v-else-if="updates.desktop && updates.available && !updates.running" class="vxs-btn-primary px-3 py-1.5 disabled:opacity-50" :disabled="updates.busy" @click="updates.apply()">
          {{ t('updates.desktop.download') }}
        </button>
        <button v-else-if="updates.desktop && !updates.running" class="rounded-md border border-line px-3 py-1.5 disabled:opacity-50" :disabled="updates.busy" @click="updates.check()">
          {{ t('settings.updates.checkNow') }}
        </button>
        <button
          v-if="failed && !updates.desktop"
          class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
          :disabled="updates.busy"
          @click="updates.apply(true)"
        >
          {{ t('updates.dialog.tryAgain') }}
        </button>
        <button
          class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-40"
          :disabled="locked"
          @click="close"
        >
          {{ t('common.close') }}
        </button>
      </div>
    </footer>
  </ModalDialog>
</template>
