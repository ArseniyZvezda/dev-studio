<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '@/api/client'
import type { ApiBackendConnection } from '@/api/types'
import { apiConnections, loadApiConnections } from '@/lib/api-backends'
import { t } from '@/i18n'
import MemberIconPicker from '@/components/teams/MemberIconPicker.vue'
import AgentBackendIcon from '@/components/ui/AgentBackendIcon.vue'

const draft = ref<(Omit<ApiBackendConnection, 'has_api_key'> & { api_key: string; has_api_key?: boolean }) | null>(null)
const busy = ref(false)
const error = ref('')
const removeTarget = ref<string | null>(null)
const clearKey = ref(false)
onMounted(() => loadApiConnections(true))
function edit(item?: ApiBackendConnection) {
  error.value = ''
  clearKey.value = false
  draft.value = item ? { ...item, api_key: '' } : {
    id: crypto.randomUUID(), name: '', base_url: 'https://api.openai.com/v1', model: '', context_window: null, icon: null, api_key: '',
  }
}
async function save() {
  if (!draft.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    await api.saveApiBackend({ ...draft.value,
      api_key: clearKey.value ? '' : draft.value.api_key || undefined })
    await loadApiConnections(true)
    draft.value = null
  } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause) }
  finally { busy.value = false }
}
async function remove(id: string) {
  busy.value = true
  error.value = ''
  try {
    await api.deleteApiBackend(id)
    await loadApiConnections(true)
    removeTarget.value = null
    if (draft.value?.id === id) draft.value = null
  } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause) }
  finally { busy.value = false }
}
</script>

<template>
  <h2 class="mb-2 font-medium">{{ t('settings.apiBackends.title') }}</h2>
  <div class="vxs-card space-y-4 rounded-lg border border-card bg-surface p-4 shadow-card">
    <p class="text-small text-faint">{{ t('settings.apiBackends.hint') }}</p>
    <div v-for="item in apiConnections" :key="item.id" class="flex items-center gap-3">
      <AgentBackendIcon :backend="`openai_api:${item.id}`" class="size-5 shrink-0" />
      <div class="min-w-0 flex-1"><p class="truncate">{{ item.name }}</p><p class="truncate text-small text-faint">{{ item.model }} · {{ item.base_url }}</p></div>
      <button type="button" class="vxs-btn px-2 py-1" :disabled="busy" @click="edit(item)">{{ t('common.edit') }}</button>
      <button v-if="removeTarget !== item.id" type="button" class="vxs-btn px-2 py-1" :disabled="busy" @click="removeTarget = item.id">{{ t('common.delete') }}</button>
      <template v-else>
        <span class="text-small text-warning">{{ t('settings.apiBackends.deleteHint') }}</span>
        <button type="button" class="vxs-btn px-2 py-1 text-warning" :disabled="busy" @click="remove(item.id)">{{ t('common.delete') }}</button>
        <button type="button" class="vxs-btn px-2 py-1" :disabled="busy" @click="removeTarget = null">{{ t('common.cancel') }}</button>
      </template>
    </div>
    <button v-if="!draft" type="button" class="vxs-btn-primary px-3 py-1.5" @click="edit()">{{ t('settings.apiBackends.add') }}</button>
    <form v-if="draft" class="space-y-3 rounded-lg border border-line p-3" @submit.prevent="save">
      <label class="block"><span class="mb-1 block text-muted">{{ t('settings.apiBackends.name') }}</span>
        <input v-model="draft.name" required maxlength="120" class="w-full rounded-md border border-line bg-inset px-2 py-1" :disabled="busy" /></label>
      <label class="block"><span class="mb-1 block text-muted">{{ t('settings.apiBackends.url') }}</span>
        <input v-model="draft.base_url" required type="url" class="w-full rounded-md border border-line bg-inset px-2 py-1 font-mono" :disabled="busy" /></label>
      <label class="block"><span class="mb-1 block text-muted">{{ t('settings.apiBackends.key') }}</span>
        <input v-model="draft.api_key" type="password" autocomplete="new-password" class="w-full rounded-md border border-line bg-inset px-2 py-1 font-mono" :disabled="busy || clearKey" />
        <span class="text-small text-faint">{{ t(draft.has_api_key ? 'settings.apiBackends.keySaved' : 'settings.apiBackends.keyOptional') }}</span></label>
      <label v-if="draft.has_api_key" class="flex items-center gap-2"><input v-model="clearKey" type="checkbox" :disabled="busy" />{{ t('settings.apiBackends.clearKey') }}</label>
      <label class="block"><span class="mb-1 block text-muted">{{ t('settings.apiBackends.model') }}</span>
        <input v-model="draft.model" required maxlength="200" class="w-full rounded-md border border-line bg-inset px-2 py-1 font-mono" :disabled="busy" /></label>
      <label class="block"><span class="mb-1 block text-muted">{{ t('settings.apiBackends.contextWindow') }}</span>
        <input type="number" min="1024" max="100000000" step="1" :value="draft.context_window ?? ''"
          class="w-full rounded-md border border-line bg-inset px-2 py-1 font-mono" :disabled="busy"
          @input="draft.context_window = ($event.target as HTMLInputElement).value === '' ? null : Number(($event.target as HTMLInputElement).value)" />
        <span class="text-small text-faint">{{ t('settings.apiBackends.contextWindowHint') }}</span></label>
      <div><span class="mb-1 block text-muted">{{ t('settings.apiBackends.icon') }}</span><MemberIconPicker v-model="draft.icon" /></div>
      <div class="flex gap-2">
        <button type="submit" class="vxs-btn-primary px-3 py-1.5" :disabled="busy">{{ t(busy ? 'common.saving' : 'common.save') }}</button>
        <button type="button" class="vxs-btn px-3 py-1.5" :disabled="busy" @click="draft = null">{{ t('common.cancel') }}</button>
      </div>
    </form>
    <p v-if="error" role="alert" class="text-small text-warning">{{ error }}</p>
  </div>
</template>
