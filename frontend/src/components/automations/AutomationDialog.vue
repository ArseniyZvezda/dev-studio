<script setup lang="ts">
/**
 * One automation, being written (§6.5): a trigger on the left of the arrow, an action on the
 * right, and the sentence they make over the buttons — the same sentence the card will show.
 *
 * The form holds a **draft**, seeded once from the automation it was opened for, and Save is
 * the only thing that sends it. No autosave here, unlike the member editor: a half-written
 * automation is a live rule, and "run this pipeline whenever ./ changes" must not start
 * existing while its author is still typing the folder's name.
 *
 * **When is two picks**, in the order the sentence reads: the category as a segmented control
 * (Task · Directory · Pipeline), then the event within it. Switching kind or category is
 * `triggerSwitcher`'s — a kind comes back as it was left, one never visited starts blank. The
 * action half swaps in a blank of the new kind (`blankAction`); the kinds share no fields.
 */
import { ArrowRight } from 'lucide-vue-next'
import { computed, nextTick, onMounted, reactive, ref } from 'vue'

import type {
  Automation,
  AutomationActionKind,
  AutomationInput,
  AutomationPluginAction,
  AutomationTriggerKind,
  PipelineSummary,
} from '@/api/types'
import SegmentedControl from '@/components/ui/SegmentedControl.vue'
import { t } from '@/i18n'
import { choice } from '@/lib/appearance'
import {
  ACTION_KINDS,
  ACTION_LABELS,
  CATEGORY_LABELS,
  CATEGORY_OF,
  CHANGE_KINDS,
  EVENT_LABELS,
  PLACEHOLDERS,
  TASK_STATUSES,
  TRIGGER_CATEGORIES,
  blankAction,
  blankAutomation,
  changeLabel,
  displayPath,
  normalisePath,
  problemWith,
  sentence,
  triggerKindsOf,
  triggerSwitcher,
  type TriggerCategory,
} from '@/lib/automations'
import { statusLabel } from '@/lib/status'

const props = defineProps<{
  /** The automation being edited, or `null` for a new one. */
  automation: Automation | null
  pipelines: readonly PipelineSummary[]
  pluginActions: readonly AutomationPluginAction[]
  saving: boolean
  /** Why the last save was refused, as the backend said it. */
  error: string | null
}>()
const emit = defineEmits<{ save: [AutomationInput]; close: [] }>()

function seed(): AutomationInput {
  if (!props.automation) return blankAutomation()
  const { name, enabled, trigger, action } = props.automation
  // A deep copy: the draft is edited in place, and the store's object must not be.
  const copy: AutomationInput = JSON.parse(JSON.stringify({ name, enabled, trigger, action }))
  // The file holds `models`; the field shows what a person would type.
  if (copy.trigger.kind === 'path.changed') copy.trigger.path = displayPath(copy.trigger.path)
  return copy
}

const draft = reactive<AutomationInput>(seed())
const nameField = ref<HTMLInputElement | null>(null)

onMounted(async () => {
  await nextTick()
  nameField.value?.focus()
})

const catalogue = computed(() => ({ pipelines: props.pipelines, pluginActions: props.pluginActions }))

const switcher = triggerSwitcher()
/** Labels are getters (`choice`), so the control follows a language switch. */
const categoryChoices = TRIGGER_CATEGORIES.map((category) => choice(category, CATEGORY_LABELS[category]))
const category = computed(() => CATEGORY_OF[draft.trigger.kind])
const events = computed(() => triggerKindsOf(category.value))

function setCategory(next: TriggerCategory): void {
  draft.trigger = switcher.toCategory(draft.trigger, next)
}

function setTriggerKind(kind: AutomationTriggerKind): void {
  draft.trigger = switcher.toKind(draft.trigger, kind)
}

function setActionKind(kind: AutomationActionKind): void {
  if (draft.action.kind !== kind) draft.action = blankAction(kind, props.pipelines, props.pluginActions)
}

/** The plugin action picker speaks one value, `plugin.action`, for the two fields it sets. */
const pluginChoice = computed({
  get: () => (draft.action.kind === 'plugin.action' ? `${draft.action.plugin_id}.${draft.action.action_id}` : ''),
  set: (value: string) => {
    const offered = props.pluginActions.find((entry) => `${entry.plugin_id}.${entry.action_id}` === value)
    if (offered && draft.action.kind === 'plugin.action') {
      draft.action.plugin_id = offered.plugin_id
      draft.action.action_id = offered.action_id
    }
  },
})

const pluginDescription = computed(
  () =>
    props.pluginActions.find((entry) => `${entry.plugin_id}.${entry.action_id}` === pluginChoice.value)
      ?.description ?? null,
)

/** The pipeline the draft names when the project has none by that id — deleted since. */
const missingPipeline = computed(() => {
  if (draft.action.kind !== 'pipeline.run') return null
  const id = draft.action.pipeline_id
  return id && !props.pipelines.some((pipeline) => pipeline.id === id) ? id : null
})

function toggleChange(kind: (typeof CHANGE_KINDS)[number], on: boolean): void {
  if (draft.trigger.kind !== 'path.changed') return
  const next = new Set(draft.trigger.changes)
  if (on) next.add(kind)
  else next.delete(kind)
  draft.trigger.changes = CHANGE_KINDS.filter((entry) => next.has(entry))
}

/** What would be sent: the draft with its path in the file's spelling. */
const outgoing = computed<AutomationInput>(() => {
  const copy: AutomationInput = JSON.parse(JSON.stringify(draft))
  copy.name = copy.name.trim()
  if (copy.trigger.kind === 'path.changed') {
    copy.trigger.path = normalisePath(copy.trigger.path) ?? copy.trigger.path
    copy.trigger.pattern = copy.trigger.pattern.trim()
  }
  if (copy.trigger.kind === 'run.finished' && !copy.trigger.pipeline_id) copy.trigger.pipeline_id = null
  return copy
})

/**
 * `{task} {title} …` for the trigger chosen. Built here and not in the template: a `}}` inside
 * an interpolation ends it, wherever the brace came from.
 */
const placeholderList = computed(() =>
  PLACEHOLDERS[draft.trigger.kind].map((name) => `{${name}}`).join(' '),
)

const problem = computed(() => problemWith(draft))
const parts = computed(() => sentence(outgoing.value, catalogue.value))

function submit(): void {
  if (problem.value || props.saving) return
  emit('save', outgoing.value)
}

const field = 'w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent'
</script>

<template>
  <form class="flex min-h-0 flex-1 flex-col" @submit.prevent="submit">
    <header class="shrink-0 border-b border-line px-4 py-3">
      <h2 class="font-medium">
        {{ automation ? t('automations.form.editTitle') : t('automations.form.addTitle') }}
      </h2>
    </header>

    <div class="min-h-0 flex-1 space-y-4 overflow-auto p-4">
      <label class="block">
        <span class="mb-1 block text-small text-muted">{{ t('automations.form.name') }}</span>
        <input
          ref="nameField"
          v-model="draft.name"
          :class="field"
          maxlength="120"
          :placeholder="t('automations.form.namePlaceholder')"
        />
      </label>

      <div class="grid gap-4 md:grid-cols-2">
        <!-- when -->
        <fieldset class="min-w-0 space-y-3 rounded-lg border border-line p-3">
          <legend class="px-1 text-small text-muted">{{ t('automations.form.when') }}</legend>
          <!-- What is watched, then what happened to it. The event stays a list even for a
               category with one: it is the second half of the sentence, and the next event a
               category gains lands there. -->
          <SegmentedControl
            :options="categoryChoices"
            :model-value="category"
            :aria-label="t('automations.form.category')"
            @update:model-value="setCategory"
          />
          <select
            :class="field"
            :value="draft.trigger.kind"
            :aria-label="t('automations.form.event')"
            @change="setTriggerKind(($event.target as HTMLSelectElement).value as AutomationTriggerKind)"
          >
            <option v-for="kind in events" :key="kind" :value="kind">
              {{ t(EVENT_LABELS[kind]) }}
            </option>
          </select>

          <template v-if="draft.trigger.kind === 'task.status_changed'">
            <label class="block">
              <span class="mb-1 block text-small text-muted">{{ t('automations.form.statusTo') }}</span>
              <select v-model="draft.trigger.status" :class="field">
                <option :value="null">{{ t('automations.form.anyStatus') }}</option>
                <option v-for="status in TASK_STATUSES" :key="status" :value="status">
                  {{ statusLabel(status) }}
                </option>
              </select>
            </label>
          </template>

          <template v-else-if="draft.trigger.kind === 'path.changed'">
            <label class="block">
              <span class="mb-1 block text-small text-muted">{{ t('automations.form.path') }}</span>
              <input
                v-model="draft.trigger.path"
                :class="[field, 'font-mono']"
                placeholder="./models/"
                spellcheck="false"
              />
            </label>
            <label class="block">
              <span class="mb-1 block text-small text-muted">{{ t('automations.form.pattern') }}</span>
              <input
                v-model="draft.trigger.pattern"
                :class="[field, 'font-mono']"
                placeholder="*.fbx"
                spellcheck="false"
              />
            </label>
            <div class="flex flex-wrap gap-x-4 gap-y-1">
              <label v-for="kind in CHANGE_KINDS" :key="kind" class="inline-flex items-center gap-1.5">
                <input
                  type="checkbox"
                  :checked="draft.trigger.changes.includes(kind)"
                  @change="toggleChange(kind, ($event.target as HTMLInputElement).checked)"
                />
                {{ changeLabel(kind) }}
              </label>
            </div>
            <p class="text-small text-faint">{{ t('automations.form.pathHint') }}</p>
          </template>

          <template v-else-if="draft.trigger.kind === 'run.finished'">
            <label class="block">
              <span class="mb-1 block text-small text-muted">{{ t('automations.form.pipeline') }}</span>
              <select v-model="draft.trigger.pipeline_id" :class="field">
                <option :value="null">{{ t('automations.form.anyPipeline') }}</option>
                <option v-for="pipeline in pipelines" :key="pipeline.id" :value="pipeline.id">
                  {{ pipeline.name || pipeline.id }}
                </option>
              </select>
            </label>
            <label class="block">
              <span class="mb-1 block text-small text-muted">{{ t('automations.form.outcome') }}</span>
              <select v-model="draft.trigger.outcome" :class="field">
                <option value="any">{{ t('automations.form.outcomeAny') }}</option>
                <option value="success">{{ t('automations.form.outcomeSuccess') }}</option>
                <option value="failed">{{ t('automations.form.outcomeFailed') }}</option>
              </select>
            </label>
          </template>
        </fieldset>

        <!-- do -->
        <fieldset class="min-w-0 space-y-3 rounded-lg border border-line p-3">
          <legend class="px-1 text-small text-muted">{{ t('automations.form.then') }}</legend>
          <select
            :class="field"
            :value="draft.action.kind"
            :aria-label="t('automations.form.then')"
            @change="setActionKind(($event.target as HTMLSelectElement).value as AutomationActionKind)"
          >
            <option
              v-for="kind in ACTION_KINDS"
              :key="kind"
              :value="kind"
              :disabled="kind === 'plugin.action' && !pluginActions.length"
            >
              {{ t(ACTION_LABELS[kind]) }}
            </option>
          </select>

          <template v-if="draft.action.kind === 'notify'">
            <label class="block">
              <span class="mb-1 block text-small text-muted">{{ t('automations.form.notifyTitle') }}</span>
              <input
                v-model="draft.action.title"
                :class="field"
                maxlength="120"
                :placeholder="t('automations.form.notifyAuto')"
              />
            </label>
            <label class="block">
              <span class="mb-1 block text-small text-muted">{{ t('automations.form.notifyMessage') }}</span>
              <textarea
                v-model="draft.action.message"
                :class="field"
                rows="2"
                maxlength="2000"
                :placeholder="t('automations.form.notifyAuto')"
              />
            </label>
            <p class="text-small text-faint">
              {{ t('automations.form.placeholders') }}
              <span class="font-mono">
                {{ placeholderList }}
              </span>
            </p>
          </template>

          <template v-else-if="draft.action.kind === 'pipeline.run'">
            <label class="block">
              <span class="mb-1 block text-small text-muted">{{ t('automations.form.pipeline') }}</span>
              <select v-model="draft.action.pipeline_id" :class="field">
                <option v-if="!pipelines.length" value="" disabled>
                  {{ t('automations.form.noPipelines') }}
                </option>
                <!-- A pipeline the file names and the project no longer has stays selectable,
                     so opening the dialog does not quietly point the automation elsewhere. -->
                <option v-if="missingPipeline" :value="missingPipeline">{{ missingPipeline }}</option>
                <option v-for="pipeline in pipelines" :key="pipeline.id" :value="pipeline.id">
                  {{ pipeline.name || pipeline.id }}
                </option>
              </select>
            </label>
            <label class="inline-flex items-center gap-1.5">
              <input v-model="draft.action.use_cache" type="checkbox" />
              {{ t('automations.form.useCache') }}
            </label>
            <p v-if="draft.trigger.kind === 'path.changed'" class="text-small text-faint">
              {{ t('automations.form.loopHint') }}
            </p>
          </template>

          <template v-else>
            <label class="block">
              <span class="mb-1 block text-small text-muted">{{ t('automations.form.pluginAction') }}</span>
              <select v-model="pluginChoice" :class="field">
                <option
                  v-for="offered in pluginActions"
                  :key="`${offered.plugin_id}.${offered.action_id}`"
                  :value="`${offered.plugin_id}.${offered.action_id}`"
                >
                  {{ offered.label }}
                </option>
              </select>
            </label>
            <p v-if="pluginDescription" class="text-small text-faint">{{ pluginDescription }}</p>
          </template>
        </fieldset>
      </div>

      <!-- The sentence this makes — what the card will say. -->
      <p class="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg bg-inset px-3 py-2">
        <span v-for="(part, index) in parts" :key="index" class="inline-flex min-w-0 items-center gap-2">
          <ArrowRight v-if="index" class="size-3.5 shrink-0 text-faint" />
          <span class="min-w-0 break-words" :class="index === 0 ? 'text-muted' : ''">{{ part }}</span>
        </span>
      </p>

      <label class="inline-flex items-center gap-1.5">
        <input v-model="draft.enabled" type="checkbox" />
        {{ t('automations.form.enabled') }}
      </label>
    </div>

    <footer class="flex shrink-0 items-center gap-2 border-t border-line px-4 py-3">
      <p class="min-w-0 flex-1 truncate text-small" :class="error ? 'text-danger' : 'text-faint'" :title="error ?? undefined">
        {{ error ?? (problem ? t(problem) : '') }}
      </p>
      <button type="button" class="rounded-md border border-line px-3 py-1 hover:bg-hover" @click="emit('close')">
        {{ t('common.cancel') }}
      </button>
      <button type="submit" class="vxs-btn-primary px-3 py-1 disabled:opacity-50" :disabled="!!problem || saving">
        {{ saving ? t('common.saving') : t('common.save') }}
      </button>
    </footer>
  </form>
</template>
