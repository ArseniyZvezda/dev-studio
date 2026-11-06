<script setup lang="ts">
/** Shared session defaults and prompts live beside the team graph (§10.2, §10.3). */
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { api } from '@/api/client'
import type { SettingsUpdate, TeamsConfig } from '@/api/types'
import AgentPrompts from '@/components/settings/AgentPrompts.vue'
import RequiredContextEditor from '@/components/teams/RequiredContextEditor.vue'
import { t } from '@/i18n'
import { useSessionStore } from '@/stores/session'
import { useTeamsStore } from '@/stores/teams'

const session = useSessionStore()
const teams = useTeamsStore()
const config = computed(() => session.project?.config.teams)
const permissionMode = ref('bypassPermissions')
const maxTurns = ref(40)
const requiredSkills = ref<string[]>([])
const requiredDocuments = ref<string[]>([])
const saving = ref(false)
const saveError = ref<string | null>(null)
const invalidTurns = computed(() => !Number.isInteger(maxTurns.value) || maxTurns.value < 1 || maxTurns.value > 1000)
type DefaultsPatch = Pick<Partial<TeamsConfig>, 'permission_mode' | 'max_turns' | 'required_skills' | 'required_documents'>
let pending: DefaultsPatch = {}
let inFlight: DefaultsPatch = {}
let failedPatch: DefaultsPatch = {}
let saveTimer: ReturnType<typeof setTimeout> | null = null
let flushing: Promise<void> | null = null
let requestQueue: Promise<unknown> = Promise.resolve()
let seededMode = 'bypassPermissions'
let seededTurns = 40
let seededSkills: string[] = []
let seededDocuments: string[] = []

// §10.2: both rails update the same config. One request queue keeps responses ordered even
// when a prompt save, a default change, and their unmount flushes happen together.
function updateSettings(patch: SettingsUpdate) {
  const request = requestQueue.then(() => api.updateSettings(patch))
  requestQueue = request.catch(() => undefined)
  return request
}

// Prompt saves also replace the config object; only changed session defaults reseed these
// fields so an unrelated save cannot erase a max-turns draft the user is still editing.
watch([() => config.value?.permission_mode, () => config.value?.max_turns], ([mode, turns]) => {
  if (permissionMode.value === seededMode && pending.permission_mode === undefined && inFlight.permission_mode === undefined) {
    permissionMode.value = mode ?? 'bypassPermissions'
  }
  if (maxTurns.value === seededTurns && pending.max_turns === undefined && inFlight.max_turns === undefined) {
    maxTurns.value = turns ?? 40
  }
  seededMode = mode ?? 'bypassPermissions'
  seededTurns = turns ?? 40
}, { immediate: true })

watch([() => config.value?.required_skills, () => config.value?.required_documents], ([skills, documents]) => {
  if (requiredSkills.value.join('\n') === seededSkills.join('\n') && pending.required_skills === undefined && inFlight.required_skills === undefined) {
    requiredSkills.value = [...(skills ?? [])]
  }
  if (requiredDocuments.value.join('\n') === seededDocuments.join('\n') && pending.required_documents === undefined && inFlight.required_documents === undefined) {
    requiredDocuments.value = [...(documents ?? [])]
  }
  seededSkills = [...(skills ?? [])]
  seededDocuments = [...(documents ?? [])]
}, { immediate: true })

function scheduleSave(patch: DefaultsPatch, immediate = false): void {
  if (!config.value) return
  // A later edit or explicit retry includes failed fields; never claim a partial retry saved
  // the requirements that were rejected in the previous request (§10.10).
  pending = { ...failedPatch, ...pending }
  failedPatch = {}
  if (patch.permission_mode !== undefined) pending.permission_mode = patch.permission_mode
  if (patch.max_turns !== undefined) {
    if (invalidTurns.value) delete pending.max_turns
    else pending.max_turns = patch.max_turns
  }
  if (patch.required_skills !== undefined) pending.required_skills = [...patch.required_skills]
  if (patch.required_documents !== undefined) pending.required_documents = [...patch.required_documents]
  saveError.value = null
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = immediate ? null : setTimeout(() => void flush(), 600)
  if (immediate) void flush()
}

function flush(): Promise<void> {
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = null
  if (flushing) return flushing
  flushing = (async () => {
    while (Object.keys(pending).length) {
      const patch = { ...failedPatch, ...pending }
      failedPatch = {}
      pending = {}
      inFlight = patch
      saving.value = true
      try {
        const updated = await updateSettings({ teams: patch })
        if (session.project) session.project = { ...session.project, config: updated }
        saveError.value = null
      } catch (cause) {
        failedPatch = { ...failedPatch, ...patch }
        saveError.value = cause instanceof Error ? cause.message : String(cause)
      } finally {
        inFlight = {}
        saving.value = false
      }
    }
  })().finally(() => { flushing = null })
  return flushing
}

onBeforeUnmount(() => { void flush() })
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <header class="vxs-pane shrink-0 border-b border-pane px-pane py-2">
      <h2 class="text-title font-medium">{{ t('teams.settings.title') }}</h2>
    </header>
    <div class="min-h-0 flex-1 space-y-5 overflow-y-auto p-pane">
      <p class="text-small text-faint">{{ t('teams.settings.description') }}</p>
      <section class="space-y-3">
        <fieldset :disabled="!config" class="space-y-3">
          <label class="block">
            <span class="mb-1 block text-small text-muted">{{ t('teams.editor.permissionMode') }}</span>
            <select
              v-model="permissionMode"
              class="w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent disabled:opacity-60"
              @change="scheduleSave({ permission_mode: permissionMode }, true)"
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
              @input="scheduleSave({ max_turns: maxTurns })"
            />
          </label>
        </fieldset>
        <p v-if="invalidTurns" class="text-small text-warning" role="alert">{{ t('teams.settings.invalidTurns') }}</p>
        <p v-if="saveError" class="text-small text-danger" role="alert">{{ saveError }}</p>
        <button
          v-if="saveError"
          class="rounded-md border border-line px-3 py-1 hover:bg-hover disabled:opacity-50"
          :disabled="saving || invalidTurns"
          @click="scheduleSave({ permission_mode: permissionMode, max_turns: maxTurns, required_skills: requiredSkills, required_documents: requiredDocuments }, true)"
        >{{ t('common.retry') }}</button>
        <p v-if="saving" class="text-small text-faint" role="status">{{ t('common.saving') }}</p>
      </section>
      <RequiredContextEditor
        :skills="requiredSkills"
        :documents="requiredDocuments"
        shared
        :disabled="!config"
        @update:skills="requiredSkills = $event; scheduleSave({ required_skills: $event }, true)"
        @update:documents="requiredDocuments = $event; scheduleSave({ required_documents: $event }, true)"
      />
      <AgentPrompts compact autosave :save-settings="updateSettings" />
    </div>
  </div>
</template>
