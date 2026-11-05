<script setup lang="ts">
/** Shared session defaults and prompts live beside the team graph (§10.2, §10.3). */
import { computed, ref, watch } from 'vue'

import { api } from '@/api/client'
import type { TeamsConfig } from '@/api/types'
import AgentPrompts from '@/components/settings/AgentPrompts.vue'
import { t } from '@/i18n'
import { useSessionStore } from '@/stores/session'
import { useTeamsStore } from '@/stores/teams'

const props = defineProps<{ editing: boolean }>()
const session = useSessionStore()
const teams = useTeamsStore()
const config = computed(() => session.project?.config.teams)
const permissionMode = ref('bypassPermissions')
const maxTurns = ref(40)
const saving = ref(false)
const promptsSaving = ref(false)
const saveError = ref<string | null>(null)
const invalidTurns = computed(() => !Number.isInteger(maxTurns.value) || maxTurns.value < 1 || maxTurns.value > 1000)

// Prompt saves also replace the config object; only changed session defaults reseed these
// fields so an unrelated save cannot erase a max-turns draft the user is still editing.
watch([() => config.value?.permission_mode, () => config.value?.max_turns], ([mode, turns]) => {
  permissionMode.value = mode ?? 'bypassPermissions'
  maxTurns.value = turns ?? 40
}, { immediate: true })

async function save(patch: Pick<Partial<TeamsConfig>, 'permission_mode' | 'max_turns'>) {
  if (!props.editing || saving.value || promptsSaving.value || !config.value) return
  if (patch.max_turns !== undefined && invalidTurns.value) return
  saving.value = true
  saveError.value = null
  try {
    const updated = await api.updateSettings({ teams: patch })
    if (session.project) session.project = { ...session.project, config: updated }
  } catch (cause) {
    saveError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <header class="vxs-pane shrink-0 border-b border-pane px-pane py-2">
      <h2 class="text-title font-medium">{{ t('teams.settings.title') }}</h2>
    </header>
    <div class="min-h-0 flex-1 space-y-5 overflow-y-auto p-pane">
      <p class="text-small text-faint">{{ t(editing ? 'teams.settings.description' : 'teams.settings.lockedHint') }}</p>
      <section class="space-y-3">
        <fieldset :disabled="!editing || saving || promptsSaving || !config" class="space-y-3">
          <label class="block">
            <span class="mb-1 block text-small text-muted">{{ t('teams.editor.permissionMode') }}</span>
            <select
              v-model="permissionMode"
              class="w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent disabled:opacity-60"
              @change="save({ permission_mode: permissionMode })"
            >
              <option v-for="mode in teams.options?.permission_modes ?? [permissionMode]" :key="mode" :value="mode">{{ mode }}</option>
            </select>
          </label>
          <label class="block">
            <span class="mb-1 block text-small text-muted">{{ t('teams.editor.maxTurns') }}</span>
            <input
              v-model.number="maxTurns"
              type="number"
              min="1"
              max="1000"
              step="1"
              class="w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent disabled:opacity-60"
              :aria-invalid="invalidTurns"
              @change="save({ max_turns: maxTurns })"
            />
          </label>
        </fieldset>
        <p v-if="invalidTurns" class="text-small text-warning" role="alert">{{ t('teams.settings.invalidTurns') }}</p>
        <p v-if="saveError" class="text-small text-danger" role="alert">{{ saveError }}</p>
        <button
          v-if="saveError"
          class="rounded-md border border-line px-3 py-1 hover:bg-hover disabled:opacity-50"
          :disabled="!editing || saving || promptsSaving || invalidTurns"
          @click="save({ permission_mode: permissionMode, max_turns: maxTurns })"
        >{{ t('common.retry') }}</button>
        <p v-if="saving" class="text-small text-faint" role="status">{{ t('common.saving') }}</p>
      </section>
      <AgentPrompts compact :disabled="!editing || saving" @saving="promptsSaving = $event" />
    </div>
  </div>
</template>
