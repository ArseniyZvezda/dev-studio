<script setup lang="ts">
/**
 * The "Waits for" editor — one removable chip per dependency, plus a picker to add another.
 *
 * A `<select multiple>` was what this used to be, and it was wrong twice. Removing an entry
 * needed a ctrl-click nobody discovers, and a single stray click *added* one; worse, the
 * options are the unfinished tasks, so a dependency that reached `done` had no `<option>` to
 * be selected against and simply disappeared from the control while still holding the task
 * back. A chip is rendered from the value itself, so what is on the task is always what is
 * on screen, and each one carries its own way out.
 *
 * Shared by the composer and the task rail because both edit the same field and neither is
 * the natural home for the rules about it (§10.4).
 */
import { X } from 'lucide-vue-next'
import { computed } from 'vue'

import { t } from '@/i18n'
import { useTeamsStore } from '@/stores/teams'

const props = defineProps<{
  modelValue: string[]
  /** The task being edited, so it is never offered as a dependency of itself. */
  excludeId?: string | null
  /** Appended to the label — what the dependencies will actually do, where that differs. */
  hint?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [string[]] }>()

const teams = useTeamsStore()

/**
 * Resolved against the whole board rather than against the candidate list: a dependency
 * that is finished, or one on a task this list would not offer, still has to be shown and
 * still has to be removable. The id stands in when the task is gone entirely.
 */
const chips = computed(() =>
  props.modelValue.map((id) => {
    const found = teams.tasks.find((candidate) => candidate.id === id)
    return {
      id,
      // Blank for a task that is gone entirely — the opaque id is then the only name it
      // has left, and it is already standing in for the title below.
      displayId: found?.display_id ?? '',
      title: found?.title ?? id,
      done: found?.status === 'done',
    }
  }),
)

/** Anything unfinished makes a sensible dependency; itself and finished ones do not. */
const candidates = computed(() =>
  teams.tasks.filter(
    (candidate) =>
      candidate.id !== props.excludeId &&
      candidate.status !== 'done' &&
      candidate.status !== 'cancelled' &&
      !props.modelValue.includes(candidate.id),
  ),
)

function drop(id: string): void {
  emit(
    'update:modelValue',
    props.modelValue.filter((entry) => entry !== id),
  )
}

function add(event: Event): void {
  // The picker is an action, not a field: it shows its placeholder again straight away, so
  // adding a second dependency is the same gesture as adding the first.
  const select = event.target as HTMLSelectElement
  const id = select.value
  select.value = ''
  if (id && !props.modelValue.includes(id)) emit('update:modelValue', [...props.modelValue, id])
}
</script>

<template>
  <div v-if="chips.length || candidates.length">
    <span class="mb-1 block text-small text-faint">
      {{
        hint ? t('tasks.dependencies.labelWithHint', { hint }) : t('tasks.dependencies.label')
      }}
    </span>

    <ul v-if="chips.length" class="mb-1 flex flex-wrap gap-1">
      <li
        v-for="chip in chips"
        :key="chip.id"
        class="flex min-w-0 items-center gap-1 rounded-md border border-line bg-inset py-0.5 pr-0.5 pl-2"
      >
        <span v-if="chip.displayId" class="shrink-0 font-mono text-small text-faint">
          {{ chip.displayId }}
        </span>
        <span class="truncate" :class="chip.done ? 'text-faint line-through' : ''">
          {{ chip.title }}
        </span>
        <button
          type="button"
          class="grid size-4 shrink-0 place-items-center rounded-sm text-faint hover:bg-hover hover:text-danger"
          :title="t('tasks.dependencies.remove', { title: chip.title })"
          @click="drop(chip.id)"
        >
          <X class="size-3" />
        </button>
      </li>
    </ul>

    <select
      v-if="candidates.length"
      class="w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
      @change="add"
    >
      <option value="">{{ t('tasks.dependencies.add') }}</option>
      <option v-for="candidate in candidates" :key="candidate.id" :value="candidate.id">
        {{
          t('tasks.dependencies.option', {
            id: candidate.display_id,
            title: candidate.title,
            assignee:
              teams.memberById.get(candidate.assignee_id)?.name ?? candidate.assignee_id,
          })
        }}
      </option>
    </select>
  </div>
</template>
