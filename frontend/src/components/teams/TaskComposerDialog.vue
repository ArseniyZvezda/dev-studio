<script setup lang="ts">
/**
 * The task composer, as a dialog (§10.4).
 *
 * It used to be a strip that unrolled between the board's header and the columns, and that
 * was the wrong shape twice over. It pushed the board down the moment it opened, so the
 * cards you were assigning *around* jumped out from under the cursor; and being only as
 * tall as the page could spare, the brief — the field that actually decides what the agent
 * does — got three cramped rows while two `<select>`s took the top billing. Centred, the
 * form sets its own size: the title leads, the brief gets room to be written in, and the
 * board stays exactly where it was, dimmed behind.
 *
 * The form state lives here rather than in the view because it is born and dies with the
 * dialog — mounting on open is what makes "reopen" mean "start over" without a reset step.
 */
import { X } from 'lucide-vue-next'
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'

import DependencyPicker from '@/components/teams/DependencyPicker.vue'
import MemberSelect from '@/components/teams/MemberSelect.vue'
import RunOverrides from '@/components/teams/RunOverrides.vue'
import TaskWritePaths from '@/components/teams/TaskWritePaths.vue'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import AttachmentPicker from '@/components/ui/AttachmentPicker.vue'
import type { UploadedAttachment } from '@/api/client'
import { t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { statusLabel } from '@/lib/status'
import { writePathsFromText } from '@/lib/task-write-paths'
import { useTeamsStore } from '@/stores/teams'

const props = defineProps<{
  /** Who the dialog opens pointed at — the board's first member, or a deep link's target. */
  assigneeId: string
}>()
const emit = defineEmits<{ close: [] }>()

const teams = useTeamsStore()

const form = ref({
  // Nullable because the picker's value is: narrowing the actor can leave the chosen
  // assignee off the list, and an id nobody offers any more is worse than none.
  assigneeId: props.assigneeId as string | null,
  title: '',
  brief: '',
  writePaths: '',
  createdBy: null as string | null,
  dependsOn: [] as string[],
  autoStart: true,
  /** Null overrides: the task runs as its assignee does (§10.3). */
  model: null as string | null,
  backend: null as string | null,
  effort: null as string | null,
})

const attachments = ref<UploadedAttachment[]>([])
const uploadingAttachments = ref(false)

const titleInput = ref<HTMLInputElement | null>(null)

/** Assigning "as" a member is what exercises the routing rules; the default is the user,
 *  who the backend allows to assign to anyone. Anyone with a route out — reports or a
 *  `can_assign_to` entry — can stand in as the creator. */
const possibleActors = computed(() =>
  teams.members.filter((member) => teams.assignableFrom(member.id).length > 0),
)

/** Who the chosen creator is actually allowed to assign to (§10.4). */
const possibleAssignees = computed(() => teams.assignableFrom(form.value.createdBy))

const assigneeIsHuman = computed(() => teams.isHuman(form.value.assigneeId))

const canSubmit = computed(
  () => !uploadingAttachments.value && !teams.busy && !!form.value.assigneeId && !!form.value.title.trim(),
)

// The title, not the panel: this form is opened to be typed into, and the field it is
// opened *for* is the one nobody can fill in for you. `ModalDialog` leaves the focus where
// this puts it, and `Escape` still reaches it — the handler is on the panel around us.
onMounted(async () => {
  await nextTick()
  titleInput.value?.focus()
})

// A create that failed left its message on the store, and it belongs to a form that is
// no longer on screen. Clearing on unmount keeps it from reappearing over the board.
onUnmounted(() => {
  if (teams.error) teams.error = null
})

async function submit() {
  const assigneeId = form.value.assigneeId
  if (!canSubmit.value || !assigneeId) return
  try {
    await teams.createTask({
      assigneeId,
      title: form.value.title.trim(),
      brief: form.value.brief.trim(),
      writePaths: writePathsFromText(form.value.writePaths),
      attachments: attachments.value.map(entry => entry.reference),
      createdBy: form.value.createdBy,
      dependsOn: form.value.dependsOn,
      autoStart: form.value.autoStart,
      model: form.value.model,
      agent_backend: form.value.backend,
      effort: form.value.effort,
    })
    emit('close')
  } catch {
    /* the routing error is on the store, and shown in the dialog rather than behind it */
  }
}
</script>

<template>
  <ModalDialog :label="t('tasks.newTask')" panel-class="w-[38rem]" @close="emit('close')">
    <form class="flex min-h-0 flex-1 flex-col" @submit.prevent="submit">
      <header class="flex shrink-0 items-start gap-2 border-b border-line px-4 py-3">
        <div class="min-w-0 flex-1">
          <h2 class="font-medium">{{ t('tasks.newTask') }}</h2>
          <p class="mt-0.5 text-small text-faint">
            <I18nT keypath="tasks.composer.intro">
              <template #todo>
                <strong class="font-medium text-muted">{{ statusLabel('todo') }}</strong>
              </template>
              <template #pending>
                <strong class="font-medium text-muted">{{ statusLabel('pending') }}</strong>
              </template>
            </I18nT>
          </p>
        </div>
        <button
          type="button"
          class="grid size-6 shrink-0 place-items-center rounded-md text-faint hover:bg-hover hover:text-primary"
          :title="t('common.close')"
          @click="emit('close')"
        >
          <X class="size-4" />
        </button>
      </header>

      <!-- Scrolls, so a long dependency list cannot push the footer off a short window. -->
      <div class="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        <label class="block">
          <span class="mb-1 block text-small text-faint">{{ t('tasks.composer.titleField') }}</span>
          <input
            ref="titleInput"
            v-model="form.title"
            :placeholder="t('tasks.composer.titlePlaceholder')"
            class="w-full rounded-md border border-line bg-inset px-3 py-2 text-[15px] outline-none focus:border-accent"
          />
        </label>

        <div class="flex gap-3">
          <div class="min-w-0 flex-1">
            <span class="mb-1 block text-small text-faint">{{ t('tasks.composer.actingAs') }}</span>
            <MemberSelect
              v-model="form.createdBy"
              :members="possibleActors"
              :empty-label="t('tasks.composer.actingAsYou')"
              :label="t('tasks.composer.actingAs')"
              :title="t('tasks.composer.actingAsHint')"
            />
          </div>
          <div class="min-w-0 flex-1">
            <span class="mb-1 block text-small text-faint">{{ t('tasks.assignTo') }}</span>
            <MemberSelect
              v-model="form.assigneeId"
              :members="possibleAssignees"
              :label="t('tasks.assignTo')"
            />
          </div>
        </div>

        <AttachmentPicker v-model="attachments" :disabled="teams.busy" @busy="uploadingAttachments = $event">
        <label class="block">
          <span class="mb-1 block text-small text-faint">
            {{ t('tasks.composer.brief') }}
          </span>
          <textarea
            v-model="form.brief"
            rows="7"
            :placeholder="t('tasks.composer.briefPlaceholder')"
            class="w-full resize-y rounded-md border border-line bg-inset px-3 py-2 outline-none focus:border-accent"
            @keydown.ctrl.enter.prevent="submit"
            @keydown.meta.enter.prevent="submit"
          />
        </label>

        </AttachmentPicker>

        <TaskWritePaths v-model="form.writePaths" />

        <DependencyPicker
          v-model="form.dependsOn"
          :hint="t('tasks.composer.dependsOnHint')"
        />

        <!-- Both default to the assignee's, so the composer stays a title and a brief for
             anyone who has no opinion about either (§10.3). -->
        <RunOverrides
          v-model:backend="form.backend"
          v-model:model="form.model"
          v-model:effort="form.effort"
          :member="teams.memberById.get(form.assigneeId ?? '') ?? null"
          :disabled="assigneeIsHuman"
        />

        <p v-if="teams.error" class="rounded-md bg-danger-soft px-2 py-1 text-danger">
          {{ teams.error }}
        </p>
      </div>

      <footer
        class="flex shrink-0 items-center gap-3 border-t border-line bg-elevated px-4 py-3"
      >
        <label
          class="flex flex-1 items-center gap-2"
          :class="assigneeIsHuman ? 'cursor-not-allowed text-faint' : 'cursor-pointer'"
          :title="
            assigneeIsHuman
              ? t('tasks.composer.autoStartHuman')
              : t('tasks.composer.autoStartHint')
          "
        >
          <input
            v-model="form.autoStart"
            type="checkbox"
            :disabled="assigneeIsHuman"
            class="accent-[var(--vxs-accent)]"
          />
          {{ t('tasks.composer.autoStart') }}
        </label>
        <!-- The brief is a textarea, so Enter belongs to it; this is the way out of it. -->
        <kbd class="shrink-0 font-mono text-small text-faint">Ctrl+Enter</kbd>
        <button
          type="button"
          class="rounded-md border border-line px-3 py-1.5 hover:bg-hover"
          @click="emit('close')"
        >
          {{ t('common.cancel') }}
        </button>
        <button
          type="submit"
          class="vxs-btn-primary px-3 py-1.5 font-medium text-on-accent disabled:opacity-50"
          :disabled="!canSubmit"
        >
          {{ t('tasks.composer.create') }}
        </button>
      </footer>
    </form>
  </ModalDialog>
</template>
