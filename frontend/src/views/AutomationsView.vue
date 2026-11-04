<script setup lang="ts">
/**
 * Automations (§6.5): what the project does by itself — one trigger, one action each.
 *
 * The page edits `voxarium_studio/automations.yaml`, which is committed with the project, so what is
 * set up here is what a colleague's Studio does too. Two consequences shape it, both borrowed
 * from the MCP page, whose file is somebody else's as well: an entry the Studio cannot read
 * is *listed* rather than hidden — it runs nothing, and every save keeps it — and a file that
 * will not parse withholds the form rather than letting it write over whatever is in there.
 *
 * Each card reads as the sentence its author would say: **On task → Status changed to: Needs
 * review → Send notification**. The words come from `lib/automations.ts`, which the dialog's
 * summary line uses too, so an automation looks the same being written and written.
 *
 * Notifications are the one thing here that is the reader's and not the project's: whether
 * this browser also raises the system's own banner is kept in `localStorage`.
 */
import {
  AlertTriangle,
  ArrowRight,
  BellRing,
  Pencil,
  Play,
  Plus,
  Power,
  Trash2,
  Zap,
} from 'lucide-vue-next'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '@/api/client'
import type { Automation, AutomationInput, AutomationRun, PipelineSummary } from '@/api/types'
import AutomationDialog from '@/components/automations/AutomationDialog.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { formatDate, t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { runBadge, runCause, runDetail, sentence } from '@/lib/automations'
import { parseUtc } from '@/lib/status'
import { useAutomationsStore } from '@/stores/automations'
import { useNotificationsStore } from '@/stores/notifications'

const store = useAutomationsStore()
const notifications = useNotificationsStore()
const router = useRouter()

const pipelines = ref<PipelineSummary[]>([])
/** The automation in the dialog; `'new'` for one that does not exist yet, `null` when shut. */
const editing = ref<Automation | 'new' | null>(null)
const saveError = ref<string | null>(null)
const confirmingDelete = ref<string | null>(null)

const overview = computed(() => store.overview)
const catalogue = computed(() => ({
  pipelines: pipelines.value,
  pluginActions: overview.value?.plugin_actions ?? [],
}))

onMounted(async () => {
  store.subscribe()
  // The pipelines are only names for the pickers and the sentences; a failure to list them
  // costs the names, which fall back to ids, and must not cost the page.
  void api
    .pipelines()
    .then((result) => (pipelines.value = result))
    .catch(() => undefined)
  await store.load()
})

function openDialog(target: Automation | 'new'): void {
  saveError.value = null
  store.error = null
  editing.value = target
}

async function save(input: AutomationInput): Promise<void> {
  const target = editing.value
  if (target === null) return
  const saved = target === 'new' ? await store.create(input) : await store.update(target.id, input)
  if (saved) editing.value = null
  else {
    // Said in the dialog, where the form that caused it is — not in a banner behind it.
    saveError.value = store.error
    store.error = null
  }
}

async function remove(automation: Automation): Promise<void> {
  if (confirmingDelete.value !== automation.id) {
    confirmingDelete.value = automation.id
    return
  }
  confirmingDelete.value = null
  await store.remove(automation.id)
}

function when(run: AutomationRun): string {
  return formatDate(parseUtc(run.created_at), {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

/** A firing that started a run leads to its pipeline; nothing else in the log is a place. */
function openRun(run: AutomationRun): void {
  if (run.data.pipeline_id) void router.push(`/pipelines/${run.data.pipeline_id}`)
}

function runName(run: AutomationRun): string {
  const live = store.automations.find((entry) => entry.id === run.automation_id)
  if (live?.name || run.automation_name) return live?.name || run.automation_name
  // Unnamed: the sentence is the name — the live one, or the one the row kept from when it
  // fired if the automation has been deleted since.
  const definition = live ?? run.data.definition
  return definition ? sentence(definition, catalogue.value).slice(1).join(' → ') : run.automation_id
}
</script>

<template>
  <div class="h-full overflow-auto">
    <header class="vxs-pane flex items-center gap-3 border-b border-pane bg-surface px-pane py-2">
      <div class="min-w-0 flex-1">
        <h1 class="text-title font-medium">{{ t('automations.title') }}</h1>
        <p class="truncate text-small text-faint">
          <template v-if="overview && !overview.exists">
            {{ t('automations.notCreated', { path: overview.path }) }}
          </template>
          <template v-else>{{ overview?.path ?? 'voxarium_studio/automations.yaml' }}</template>
        </p>
      </div>
    </header>

    <p v-if="store.error" class="bg-danger-soft px-pane py-1 text-danger">{{ store.error }}</p>

    <!-- The introduction and each section are the page's bands (`vxs-enter`, §7). -->
    <div class="max-w-3xl space-y-6 p-pane">
      <LoadingBars v-if="store.loading && !overview" />

      <template v-else-if="overview">
        <p class="vxs-enter text-muted">{{ t('automations.intro') }}</p>

        <!-- A document that will not parse: no form is offered over it. -->
        <section
          v-if="overview.error"
          class="vxs-enter rounded-lg border border-danger-soft bg-danger-soft p-3"
        >
          <p class="flex items-center gap-2 font-medium text-danger">
            <AlertTriangle class="size-4 shrink-0" /> {{ t('automations.parseError.title') }}
          </p>
          <p class="mt-1 font-mono text-small text-danger">{{ overview.error }}</p>
          <p class="mt-1 text-muted">
            <I18nT keypath="automations.parseError.hint">
              <template #file><code class="font-mono">{{ overview.path }}</code></template>
            </I18nT>
          </p>
        </section>

        <section v-else class="vxs-enter">
          <div class="mb-2 flex items-center justify-between">
            <h2 class="font-medium">{{ t('automations.list.title') }}</h2>
            <button
              class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover"
              @click="openDialog('new')"
            >
              <Plus class="size-3.5" /> {{ t('automations.list.add') }}
            </button>
          </div>

          <ul class="space-y-2">
            <li
              v-for="automation in store.automations"
              :key="automation.id"
              class="rounded-lg border border-line bg-surface p-3"
              :class="automation.enabled ? '' : 'opacity-60'"
            >
              <div class="flex items-start gap-3">
                <Zap class="mt-0.5 size-5 shrink-0 text-[var(--vxs-nav-automations)]" />

                <div class="min-w-0 flex-1">
                  <p v-if="automation.name" class="flex items-baseline gap-2">
                    <span class="truncate font-medium">{{ automation.name }}</span>
                    <span v-if="!automation.enabled" class="shrink-0 text-small text-faint">
                      {{ t('automations.list.disabled') }}
                    </span>
                  </p>
                  <!-- The sentence. Named automations carry it as their second line; unnamed
                       ones have it for a name. -->
                  <p
                    class="flex flex-wrap items-center gap-x-1.5 gap-y-0.5"
                    :class="automation.name ? 'text-small text-muted' : 'font-medium'"
                  >
                    <!-- An arrow travels with the part it introduces, so a wrapped sentence
                         never leaves one dangling at the end of a line. -->
                    <span
                      v-for="(part, index) in sentence(automation, catalogue)"
                      :key="index"
                      class="inline-flex min-w-0 items-center gap-1.5"
                    >
                      <ArrowRight v-if="index" class="size-3 shrink-0 text-faint" />
                      <span class="min-w-0 break-words" :class="index === 0 ? 'text-muted' : ''">{{ part }}</span>
                    </span>
                    <span v-if="!automation.name && !automation.enabled" class="text-small font-normal text-faint">
                      · {{ t('automations.list.disabled') }}
                    </span>
                  </p>

                  <p v-if="automation.problem" class="mt-1 flex items-center gap-1.5 text-small text-danger">
                    <AlertTriangle class="size-3.5 shrink-0" />
                    {{ automation.problem }}
                  </p>
                  <p v-else class="mt-1 truncate text-small text-faint">
                    <!-- Its latest firing, and whether that one's work is still going: a graph
                         update started a minute ago is not "last ran". -->
                    <template v-if="store.lastRun.get(automation.id)">
                      {{
                        t(
                          store.lastRun.get(automation.id)!.status === 'running'
                            ? 'automations.list.running'
                            : 'automations.list.lastRun',
                          {
                            when: when(store.lastRun.get(automation.id)!),
                            cause: runCause(store.lastRun.get(automation.id)!),
                          },
                        )
                      }}
                    </template>
                    <template v-else>{{ t('automations.list.neverRan') }}</template>
                  </p>
                </div>

                <div class="flex shrink-0 gap-2">
                  <button
                    class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                    :disabled="store.busy === automation.id"
                    :title="t('automations.list.runNowTitle')"
                    @click="store.runNow(automation.id)"
                  >
                    <Play class="size-3.5" /> {{ t('automations.list.runNow') }}
                  </button>
                  <button
                    class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                    :disabled="store.busy === automation.id"
                    :title="t('common.edit')"
                    :aria-label="t('common.edit')"
                    @click="openDialog(automation)"
                  >
                    <Pencil class="size-3.5" />
                  </button>
                  <button
                    class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                    :disabled="store.busy === automation.id"
                    :title="automation.enabled ? t('common.disable') : t('common.enable')"
                    :aria-label="automation.enabled ? t('common.disable') : t('common.enable')"
                    :aria-pressed="automation.enabled"
                    @click="store.setEnabled(automation.id, !automation.enabled)"
                  >
                    <Power class="size-3.5" :class="automation.enabled ? 'text-success' : ''" />
                  </button>
                  <button
                    class="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 disabled:opacity-50"
                    :class="
                      confirmingDelete === automation.id
                        ? 'border-danger-soft bg-danger-soft text-danger'
                        : 'border-line hover:bg-hover'
                    "
                    :disabled="store.busy === automation.id"
                    :title="t('common.delete')"
                    :aria-label="t('common.delete')"
                    @click="remove(automation)"
                    @blur="confirmingDelete = null"
                  >
                    <Trash2 class="size-3.5" />
                    <span v-if="confirmingDelete === automation.id">{{ t('automations.list.confirmDelete') }}</span>
                  </button>
                </div>
              </div>
            </li>
          </ul>

          <EmptyState
            v-if="!store.automations.length"
            :title="t('automations.list.emptyTitle')"
            :hint="t('automations.list.emptyHint')"
          >
            <template #icon><Zap class="mx-auto mb-3 size-8 text-faint" /></template>
          </EmptyState>

          <!-- Entries the file has and the Studio cannot read: shown, kept by every save, and
               run by nothing. -->
          <ul v-if="overview.invalid.length" class="mt-2 space-y-1">
            <li
              v-for="entry in overview.invalid"
              :key="entry.index"
              class="rounded-md border border-line bg-inset p-2 text-small"
            >
              <span class="font-medium">{{ entry.id ?? t('automations.list.entryNumber', { index: entry.index + 1 }) }}</span>
              <span class="text-danger"> — {{ entry.error }}</span>
              <span class="text-faint"> · {{ t('automations.list.notRun') }}</span>
            </li>
          </ul>
        </section>

        <section class="vxs-enter">
          <h2 class="mb-2 font-medium">{{ t('automations.notifications.title') }}</h2>
          <div class="flex items-start gap-3 rounded-lg border border-line bg-surface p-3">
            <BellRing class="mt-0.5 size-5 shrink-0 text-faint" />
            <div class="min-w-0 flex-1">
              <p>{{ t('automations.notifications.inApp') }}</p>
              <p class="mt-1 text-small text-faint">
                <template v-if="notifications.permission === 'unsupported'">
                  {{ t('automations.notifications.unsupported') }}
                </template>
                <template v-else-if="notifications.permission === 'denied'">
                  {{ t('automations.notifications.denied') }}
                </template>
                <template v-else>{{ t('automations.notifications.desktopHint') }}</template>
              </p>
            </div>
            <label
              v-if="notifications.permission !== 'unsupported'"
              class="inline-flex shrink-0 items-center gap-1.5"
            >
              <input
                type="checkbox"
                :checked="notifications.desktop && notifications.permission === 'granted'"
                :disabled="notifications.permission === 'denied'"
                @change="
                  ($event.target as HTMLInputElement).checked
                    ? notifications.enableDesktop()
                    : notifications.setDesktop(false)
                "
              />
              {{ t('automations.notifications.desktop') }}
            </label>
          </div>
        </section>

        <section class="vxs-enter">
          <div class="mb-2 flex items-center justify-between">
            <h2 class="font-medium">{{ t('automations.log.title') }}</h2>
            <button
              v-if="store.runs.length"
              class="text-small text-muted hover:text-primary"
              @click="store.clearRuns()"
            >
              {{ t('automations.log.clear') }}
            </button>
          </div>
          <p v-if="!store.runs.length" class="text-small text-faint">{{ t('automations.log.empty') }}</p>
          <ul v-else class="divide-y divide-line rounded-lg border border-line bg-surface">
            <li v-for="run in store.runs" :key="run.id" class="px-3 py-2">
              <p class="flex items-center gap-2">
                <span class="shrink-0 font-mono text-small text-faint">{{ when(run) }}</span>
                <span class="min-w-0 flex-1 truncate">{{ runName(run) }}</span>
                <!-- The app's status pill (§7): in progress breathes until the work ends, and the
                     same row settling to done flashes once — the row is replaced in place. -->
                <StatusBadge class="shrink-0" :status="runBadge(run)" />
              </p>
              <p class="truncate text-small text-muted" :title="runDetail(run, catalogue)">
                <span class="text-faint">{{ runCause(run) }}</span>
                <template v-if="runDetail(run, catalogue)"> — </template>
                <button
                  v-if="run.data.pipeline_id"
                  class="underline decoration-dotted underline-offset-2 hover:text-primary"
                  @click="openRun(run)"
                >
                  {{ runDetail(run, catalogue) }}
                </button>
                <template v-else>{{ runDetail(run, catalogue) }}</template>
              </p>
            </li>
          </ul>
        </section>
      </template>
    </div>

    <ModalDialog
      v-if="editing !== null"
      :label="editing === 'new' ? t('automations.form.addTitle') : t('automations.form.editTitle')"
      panel-class="w-[44rem]"
      @close="editing = null"
    >
      <AutomationDialog
        :automation="editing === 'new' ? null : editing"
        :pipelines="pipelines"
        :plugin-actions="overview?.plugin_actions ?? []"
        :saving="store.busy !== null"
        :error="saveError"
        @save="save"
        @close="editing = null"
      />
    </ModalDialog>
  </div>
</template>
