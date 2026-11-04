<script setup lang="ts">
/** Shared project-local uploads for briefs and messages (§10.4, §10.9). */
import { onUnmounted, ref } from 'vue'
import { Paperclip, X } from 'lucide-vue-next'
import { attachmentUrl, uploadAttachment, type UploadedAttachment } from '@/api/client'
import { t } from '@/i18n'

const props = defineProps<{ modelValue: UploadedAttachment[]; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [UploadedAttachment[]]; busy: [boolean] }>()
const input = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const error = ref('')
let disposed = false
onUnmounted(() => { disposed = true; emit('busy', false) })
async function add(files: File[]) {
  if (props.disabled || uploading.value || !files.length) return
  uploading.value = true
  emit('busy', true)
  error.value = ''
  const added: UploadedAttachment[] = []
  try {
    for (const file of files) {
      if (disposed) break
      if (file.size > 20 * 1024 * 1024) throw new Error(t('attachments.tooLarge'))
      added.push(await uploadAttachment(file))
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    if (added.length && !disposed) emit('update:modelValue', [...props.modelValue, ...added])
    uploading.value = false
    if (!disposed) emit('busy', false)
  }
}
function picked(event: Event) {
  const element = event.target as HTMLInputElement
  void add(Array.from(element.files ?? []))
  element.value = ''
}
function pasted(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.files ?? [])
  if (files.length) { event.preventDefault(); void add(files) }
}
function dropped(event: DragEvent) {
  if (!event.dataTransfer?.files.length) return
  event.preventDefault()
  void add(Array.from(event.dataTransfer.files))
}
</script>

<template>
  <div class="space-y-2" @dragover.prevent @drop="dropped" @paste="pasted">
    <slot />
    <div class="flex flex-wrap items-center gap-2">
      <input ref="input" type="file" multiple hidden @change="picked" />
      <button type="button" class="flex items-center gap-1 rounded-md border border-line px-2 py-1 text-small hover:bg-hover disabled:opacity-50" :disabled="disabled || uploading" @click="input?.click()">
        <Paperclip class="size-4" />{{ t('attachments.add') }}
      </button>
      <span class="text-small text-faint">{{ t(uploading ? 'attachments.uploading' : 'attachments.hint') }}</span>
    </div>
    <div v-if="modelValue.length" class="flex flex-wrap gap-2">
      <div v-for="entry in modelValue" :key="entry.reference" class="flex max-w-60 items-center gap-2 rounded-md border border-line p-2">
        <a :href="attachmentUrl(entry.reference)" target="_blank" rel="noopener" class="min-w-0 text-small">
          <img v-if="entry.image" :src="attachmentUrl(entry.reference)" :alt="entry.name" class="mb-1 max-h-24 max-w-40 rounded object-contain" />
          <span class="block truncate">{{ entry.name }}</span>
        </a>
        <button type="button" :disabled="disabled || uploading" :aria-label="t('attachments.remove', { name: entry.name })" @click="emit('update:modelValue', modelValue.filter(item => item.reference !== entry.reference))"><X class="size-4" /></button>
      </div>
    </div>
    <p v-if="error" role="alert" class="text-small text-danger">{{ error }}</p>
  </div>
</template>
