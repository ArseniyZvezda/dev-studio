<script setup lang="ts">
/** VS-149: session control (§10.8), in the header before the two 24px actions. */
import { Check, Copy, ExternalLink, Smartphone, Unplug } from 'lucide-vue-next'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { t } from '@/i18n'

const props = defineProps<{
  taskId: string
  remoteLink: string | null | undefined
  canConnect: boolean
  hint: string
  busy: boolean
}>()
const emit = defineEmits<{ toggle: [enabled: boolean] }>()
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const linkField = ref<HTMLInputElement | null>(null)
const open = ref(false)
const copied = ref(false)
let copyReset: ReturnType<typeof setTimeout> | undefined
let copyGeneration = 0

function resetCopy() {
  copyGeneration++
  clearTimeout(copyReset)
  copyReset = undefined
  copied.value = false
}

function dismiss() {
  open.value = false
  resetCopy()
}

function outside(event: Event) {
  if (event.target instanceof Node && !root.value?.contains(event.target)) dismiss()
}

function escape(event: KeyboardEvent) {
  if (!open.value || event.key !== 'Escape') return
  // Claim this press before an ancestor TaskDetail/modal can close the task itself.
  event.preventDefault()
  event.stopPropagation()
  dismiss()
  trigger.value?.focus()
}

async function copyLink() {
  const url = props.remoteLink
  if (!url) return
  const generation = ++copyGeneration
  try {
    await navigator.clipboard.writeText(url)
    if (generation !== copyGeneration) return
    copied.value = true
    clearTimeout(copyReset)
    copyReset = setTimeout(() => { copied.value = false }, 1500)
  } catch {
    if (generation !== copyGeneration) return
    linkField.value?.focus()
    linkField.value?.select()
  }
}

watch(() => props.taskId, dismiss, { flush: 'sync' })
watch(() => props.remoteLink, resetCopy, { flush: 'sync' })
onMounted(() => {
  document.addEventListener('pointerdown', outside)
  document.addEventListener('click', outside)
  document.addEventListener('keydown', escape, true)
})
onBeforeUnmount(() => {
  resetCopy()
  document.removeEventListener('pointerdown', outside)
  document.removeEventListener('click', outside)
  document.removeEventListener('keydown', escape, true)
})
</script>

<template>
  <div ref="root" class="relative shrink-0">
    <button
      ref="trigger"
      type="button"
      class="grid size-6 shrink-0 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
      :class="remoteLink ? 'text-accent' : ''"
      :title="t('taskDetail.remote.title')"
      :aria-label="t('taskDetail.remote.title')"
      aria-haspopup="menu"
      :aria-expanded="open"
      @click="open ? dismiss() : (open = true)"
    >
      <Smartphone class="size-3.5" aria-hidden="true" />
    </button>
    <div
      v-if="open"
      role="menu"
      :aria-label="t('taskDetail.remote.title')"
      class="absolute -right-16 top-full z-50 mt-1 w-64 max-w-[calc(100vw-2rem)] space-y-2 rounded-md border border-line bg-surface p-2 shadow-lg"
    >
      <button
        type="button"
        role="menuitemcheckbox"
        :aria-checked="Boolean(remoteLink)"
        :disabled="busy || (!remoteLink && !canConnect)"
        class="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left hover:bg-hover disabled:opacity-50"
        :title="remoteLink ? t('taskDetail.remote.disconnectHint') : hint"
        @click="emit('toggle', !remoteLink)"
      >
        <Unplug v-if="remoteLink" class="size-3.5" aria-hidden="true" />
        <Smartphone v-else class="size-3.5" aria-hidden="true" />
        {{ remoteLink ? t('taskDetail.remote.disconnect') : t('taskDetail.remote.connect') }}
      </button>
      <p class="px-2 text-small text-faint">{{ hint }}</p>
      <div v-if="remoteLink" class="flex min-w-0 items-center gap-1">
        <input
          ref="linkField"
          readonly
          :value="remoteLink"
          :title="remoteLink"
          :aria-label="t('taskDetail.remote.openLink')"
          class="min-w-0 flex-1 rounded-md border border-line bg-inset px-2 py-1 font-mono text-small outline-none focus:border-accent"
          @focus="linkField?.select()"
        />
        <button
          type="button"
          class="grid size-6 shrink-0 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
          :title="copied ? t('common.copied') : t('taskDetail.remote.copyLink')"
          :aria-label="copied ? t('common.copied') : t('taskDetail.remote.copyLink')"
          @click="copyLink"
        >
          <Check v-if="copied" class="size-3.5 text-success" aria-hidden="true" />
          <Copy v-else class="size-3.5" aria-hidden="true" />
        </button>
        <a
          :href="remoteLink"
          target="_blank"
          rel="noopener noreferrer"
          class="grid size-6 shrink-0 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
          :title="t('taskDetail.remote.openLink')"
          :aria-label="t('taskDetail.remote.openLink')"
        >
          <ExternalLink class="size-3.5" aria-hidden="true" />
        </a>
      </div>
    </div>
  </div>
</template>
