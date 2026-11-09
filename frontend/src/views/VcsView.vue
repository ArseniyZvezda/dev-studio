<script setup lang="ts">
/**
 * Version control (spec §9.6).
 *
 * The Studio touches a VCS in three places — it says what the project is under, isolates
 * parallel agent work (§10.5), and commits generated assets from a pipeline — and all
 * three go through `VcsBackend`. So this page is deliberately written about *a* version
 * control system rather than about git: nothing here parses one tool's status letters,
 * and every VCS entity is listed with its own settings, independently of plugins.
 *
 * Two shapes of state, kept apart on purpose. What the working copy holds lives in the
 * store, because the Explorer colours its tree from the same answer; what the *form*
 * holds is local, so a refused write leaves the user's typing where they left it instead
 * of silently reverting to what is stored.
 *
 * **This page does not list changed files.** It used to, split into staged and unstaged,
 * and that list was a second answer to a question the Explorer already answers on the
 * files themselves (§6.2) — in another vocabulary, on another page, about paths the user
 * cannot click through to. The tree is where a file is looked at, so the tree is where its
 * state belongs; what is left here is what is *about the system*: which one is driving,
 * where it sits, a commit, and a diff.
 */
import {
  FileDiff,
  GitBranch,
  GitCommitVertical,
  RefreshCw,
  TriangleAlert,
} from 'lucide-vue-next'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { ApiError, api } from '@/api/client'
import type { VcsBackendOption } from '@/api/types'
import CodeCopyButton from '@/components/ui/CodeCopyButton.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import { t, type MessageKey } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { useVcsStore } from '@/stores/vcs'

const vcs = useVcsStore()

/** The radio's own value, so a refused write does not move the selection back. */
const choice = ref('auto')
/** Drafts stay local so saving one card cannot erase edits on another (§9.6). */
const settings = reactive<Record<string, string>>({})
const formError = ref<string | null>(null)
const savingBackend = ref<string | null>(null)

const message = ref('')
const addUntracked = ref(false)
const committing = ref(false)
const commitError = ref<string | null>(null)
const commitNote = ref<string | null>(null)

const diffOpen = ref(false)
const diffText = ref('')
const diffLoading = ref(false)
const diffError = ref<string | null>(null)

const active = computed(() => vcs.overview?.active ?? null)
const backends = computed<VcsBackendOption[]>(() => vcs.overview?.backends ?? [])
const configured = computed(() => vcs.overview?.configured ?? 'auto')
const branch = computed(() => vcs.status?.branch ?? vcs.overview?.branch ?? null)
const revision = computed(() => vcs.status?.revision ?? vcs.overview?.revision ?? null)

/** Where the active backend came from — the whole point of the resolution order (§9.6). */
const chosenBy = computed(() =>
  configured.value === 'auto'
    ? t('vcs.active.detected')
    : t('vcs.active.namedInConfig', { id: configured.value }),
)

/** What the flag actually costs Teams, which is the only place it is felt (§10.5). */
const isolation = computed(() =>
  active.value?.supports_isolation ? t('vcs.active.isolated') : t('vcs.active.shared'),
)

/**
 * How much a commit would record. The number, not the names — the names are in the
 * Explorer, and a button that says "commit everything" has to say how much everything is.
 */
const pending = computed(() => t('vcs.commit.pending', { count: vcs.changeCount }))

/**
 * What a backend's second line says about it. One message per combination rather than
 * fragments joined with `·`, so a translation can order each line as a whole.
 */
function backendLine(backend: VcsBackendOption): MessageKey {
  return backend.supports_isolation ? 'vcs.choose.builtInIsolates' : 'vcs.choose.builtIn'
}

const fieldLabels: Record<string, MessageKey> = {
  executable: 'vcs.fields.executable',
  workspace: 'vcs.fields.workspace',
  port: 'vcs.fields.port',
  user: 'vcs.fields.user',
  client: 'vcs.fields.client',
  charset: 'vcs.fields.charset',
}

const fieldHints: Record<string, MessageKey> = {
  executable: 'vcs.choose.executableHint',
  workspace: 'vcs.fields.workspaceHint',
  port: 'vcs.fields.portHint',
  user: 'vcs.fields.userHint',
  client: 'vcs.fields.clientHint',
  charset: 'vcs.fields.charsetHint',
}

onMounted(async () => {
  // The tree colours itself from the same status, so the subscription is shared.
  vcs.subscribe()
  await vcs.load()
})

// The overview arrives before the potentially slow workspace status. Seed before the
// cards render so saving during that read cannot clear configured values (§9.6).
watch(() => vcs.overview, () => seed(), { immediate: true })

/** `ApiError.detail` is the backend's own sentence; anything else falls back to its message. */
function reason(cause: unknown): string {
  if (cause instanceof ApiError) return cause.detail
  return cause instanceof Error ? cause.message : String(cause)
}

function seed(): void {
  const data = vcs.overview
  if (!data) return
  choice.value = data.configured
  for (const backend of data.backends) {
    for (const field of backend.settings_fields) {
      const key = `${backend.id}.${field.key}`
      if (!(key in settings)) settings[key] = backend.settings[field.key] ?? ''
    }
  }
}

async function choose(value: string): Promise<void> {
  choice.value = value
  formError.value = null
  try {
    const next = await vcs.updateSettings({ backend: value })
    choice.value = next.configured
    await vcs.refresh()
  } catch (cause) {
    formError.value = reason(cause)
  }
}

async function saveSettings(backend: VcsBackendOption): Promise<void> {
  if (vcs.saving) return
  formError.value = null
  savingBackend.value = backend.id
  try {
    // An empty field clears the override and restores the CLI's native defaults.
    const next = await vcs.updateSettings({
      settings: {
        [backend.id]: Object.fromEntries(
          backend.settings_fields.map((field) => [field.key, settings[`${backend.id}.${field.key}`]?.trim() ?? '']),
        ),
      },
    })
    const saved = next.backends.find((candidate) => candidate.id === backend.id)
    if (saved) {
      for (const field of saved.settings_fields) {
        settings[`${saved.id}.${field.key}`] = saved.settings[field.key] ?? ''
      }
    }
    await vcs.refresh()
  } catch (cause) {
    formError.value = reason(cause)
  } finally {
    savingBackend.value = null
  }
}

async function refreshAll(): Promise<void> {
  await vcs.load()
  if (diffOpen.value) await loadDiff()
}

async function commit(): Promise<void> {
  const text = message.value.trim()
  if (!text) {
    commitError.value = t('vcs.commit.needsMessage')
    return
  }
  committing.value = true
  commitError.value = null
  commitNote.value = null
  try {
    const result = await api.vcsCommit({ message: text, add_untracked: addUntracked.value })
    if (result.committed) {
      const { revision, branch } = result
      const count = result.files.length
      if (revision && branch) {
        commitNote.value = t('vcs.commit.committedAsOn', { count, revision, branch })
      } else if (revision) {
        commitNote.value = t('vcs.commit.committedAs', { count, revision })
      } else if (branch) {
        commitNote.value = t('vcs.commit.committedOn', { count, branch })
      } else {
        commitNote.value = t('vcs.commit.committed', { count })
      }
      message.value = ''
    } else {
      // Not a failure: `committed: false` means there was nothing to commit (§9.6).
      commitNote.value = t('vcs.commit.nothing')
    }
    await vcs.refresh()
    if (diffOpen.value) await loadDiff()
  } catch (cause) {
    commitError.value = reason(cause)
  } finally {
    committing.value = false
  }
}

/**
 * The whole working copy. `GET /api/vcs/diff` still takes a path — pipeline nodes and a
 * plugin backend narrow it — but nothing on this page names one any more: the per-file
 * entry point was a button on each row of the list that is gone.
 */
async function loadDiff(): Promise<void> {
  diffLoading.value = true
  diffError.value = null
  try {
    const result = await api.vcsDiff()
    diffText.value = result.diff
  } catch (cause) {
    diffText.value = ''
    diffError.value = reason(cause)
  } finally {
    diffLoading.value = false
  }
}

async function showDiff(): Promise<void> {
  diffOpen.value = true
  await loadDiff()
}
</script>

<template>
  <div class="h-full overflow-auto">
    <header class="vxs-pane border-b border-pane bg-surface px-pane py-2">
      <h1 class="text-title font-medium">{{ t('vcs.page.title') }}</h1>
      <p class="truncate text-small text-faint">
        {{ vcs.status?.root ?? active?.root ?? t('vcs.page.noWorkspace') }}
      </p>
    </header>

    <p v-if="vcs.error" class="bg-danger-soft px-pane py-1 text-danger">{{ vcs.error }}</p>
    <p v-if="formError" class="bg-danger-soft px-pane py-1 text-danger">{{ formError }}</p>

    <!-- Each section is one of the page's bands (`vxs-enter`, §7), arriving top-down. -->
    <div class="max-w-3xl space-y-6 p-pane">
      <LoadingBars v-if="vcs.loading && !vcs.overview" />
      <!-- Nothing to draw the page from. The banner above says why; inventing a "no
           version control detected" out of a failed read would be a different claim. -->
      <p v-else-if="!vcs.overview" class="text-muted">
        {{ t('vcs.page.unreadable') }}
      </p>

      <template v-else>
        <!-- Which system, and what follows from it -->
        <section class="vxs-enter">
          <div class="mb-2 flex items-center justify-between">
            <h2 class="font-medium">{{ t('vcs.active.title') }}</h2>
            <button
              class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
              :disabled="vcs.loading"
              @click="refreshAll"
            >
              <RefreshCw class="size-3.5" :class="{ 'animate-spin': vcs.loading }" />
              {{ vcs.loading ? t('vcs.active.refreshing') : t('common.refresh') }}
            </button>
          </div>

          <div class="rounded-lg border border-line bg-surface p-3">
            <template v-if="active">
              <p class="flex flex-wrap items-center gap-2">
                <GitBranch class="size-4 shrink-0 text-accent" />
                <span class="font-medium">{{ active.name }}</span>
                <span class="rounded-sm bg-inset px-1.5 font-mono text-small text-faint">
                  {{ active.id }}
                </span>
                <span class="text-small text-faint">{{ chosenBy }}</span>
              </p>

              <dl class="mt-2 grid grid-cols-[8rem_1fr] gap-1">
                <dt class="text-muted">{{ t('vcs.active.workspace') }}</dt>
                <dd class="font-mono break-all">{{ active.root }}</dd>
                <dt class="text-muted">{{ t('vcs.active.branch') }}</dt>
                <dd class="font-mono">{{ branch ?? '—' }}</dd>
                <dt class="text-muted">{{ t('vcs.active.revision') }}</dt>
                <dd class="font-mono">{{ revision ?? '—' }}</dd>
                <dt class="text-muted">{{ t('vcs.active.isolation') }}</dt>
                <dd>
                  {{
                    active.supports_isolation ? t('vcs.active.supported') : t('vcs.active.notSupported')
                  }}
                </dd>
              </dl>

              <p class="mt-2 text-small text-faint">{{ isolation }}</p>

              <p v-if="!active.available" class="mt-2 flex items-center gap-2 text-danger">
                <TriangleAlert class="size-4 shrink-0" />
                {{ active.reason }}
              </p>
            </template>

            <template v-else-if="configured === 'none'">
              <p class="font-medium">{{ t('vcs.active.offTitle') }}</p>
              <p class="mt-1 text-muted">{{ t('vcs.active.offBody') }}</p>
            </template>

            <template v-else>
              <p class="font-medium">{{ t('vcs.active.noneTitle') }}</p>
              <p class="mt-1 text-muted">{{ t('vcs.active.noneBody') }}</p>
            </template>
          </div>
        </section>

        <!-- Choosing one -->
        <section class="vxs-enter">
          <h2 class="mb-2 font-medium">{{ t('vcs.choose.title') }}</h2>
          <p class="mb-3 text-small text-muted">{{ t('vcs.choose.intro') }}</p>
          <div class="space-y-2">
            <label
              class="flex cursor-pointer items-start gap-3 rounded-lg border border-line bg-surface p-3"
            >
              <input
                type="radio"
                name="vcs-backend"
                class="mt-1 accent-[var(--vxs-accent)]"
                :checked="choice === 'auto'"
                :disabled="vcs.saving"
                @change="choose('auto')"
              />
              <span>
                {{ t('vcs.choose.auto') }}
                <span class="block text-small text-faint">{{ t('vcs.choose.autoHint') }}</span>
              </span>
            </label>

            <div
              v-for="backend in backends"
              :key="backend.id"
              class="rounded-lg border border-line bg-surface p-3"
              :class="{ 'border-accent': backend.active }"
            >
              <label class="flex cursor-pointer items-start gap-3">
                <input
                  type="radio"
                  name="vcs-backend"
                  class="mt-1 accent-[var(--vxs-accent)]"
                  :checked="choice === backend.id"
                  :disabled="vcs.saving"
                  @change="choose(backend.id)"
                />
                <span class="min-w-0 flex-1">
                  <span class="flex flex-wrap items-center gap-2">
                    <span class="font-medium">{{ backend.name }}</span>
                    <span class="rounded-sm bg-inset px-1.5 font-mono text-small text-faint">
                      {{ backend.id }}
                    </span>
                    <span v-if="backend.active" class="text-small text-success">
                      {{ t('vcs.choose.active') }}
                    </span>
                    <span v-else-if="backend.detected" class="text-small text-faint">
                      {{ t('vcs.choose.claims') }}
                    </span>
                  </span>
                  <span class="block text-small text-faint">
                    <I18nT :keypath="backendLine(backend)">
                      <template #program>
                        <span class="font-mono">{{ backend.program }}</span>
                      </template>
                    </I18nT>
                  </span>
                  <!-- Kept on the list rather than hidden: "why is Plastic not offered" is
                       exactly the question this page exists to answer. -->
                  <span v-if="!backend.available" class="mt-1 flex items-center gap-1.5 text-danger">
                    <TriangleAlert class="size-3.5 shrink-0" />
                    {{ backend.reason }}
                  </span>
                </span>
              </label>

              <form class="mt-3 space-y-3 pl-7" @submit.prevent="saveSettings(backend)">
                <div v-for="field in backend.settings_fields" :key="field.key">
                  <label
                    :for="`vcs-${backend.id}-${field.key}`"
                    class="mb-1 block text-small text-muted"
                  >
                    {{ fieldLabels[field.key] ? t(fieldLabels[field.key]!) : field.key }}
                  </label>
                  <input
                    :id="`vcs-${backend.id}-${field.key}`"
                    v-model="settings[`${backend.id}.${field.key}`]"
                    type="text"
                    class="w-full min-w-0 rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent disabled:opacity-50"
                    :placeholder="field.default ?? (field.key === 'executable' ? backend.program : '')"
                    :aria-describedby="fieldHints[field.key] ? `vcs-${backend.id}-${field.key}-hint` : undefined"
                    :disabled="vcs.saving"
                    spellcheck="false"
                    autocomplete="off"
                  />
                  <p
                    v-if="fieldHints[field.key]"
                    :id="`vcs-${backend.id}-${field.key}-hint`"
                    class="mt-1 text-small text-faint"
                  >
                    {{ t(fieldHints[field.key]!, { program: backend.program }) }}
                  </p>
                </div>
                <p v-if="backend.id === 'perforce'" class="text-small text-muted">
                  {{ t('vcs.fields.perforceAuth') }}
                </p>
                <button
                  type="submit"
                  class="shrink-0 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                  :disabled="vcs.saving"
                >
                  {{ savingBackend === backend.id ? t('common.saving') : t('common.save') }}
                </button>
              </form>
            </div>

            <label
              class="flex cursor-pointer items-start gap-3 rounded-lg border border-line bg-surface p-3"
            >
              <input
                type="radio"
                name="vcs-backend"
                class="mt-1 accent-[var(--vxs-accent)]"
                :checked="choice === 'none'"
                :disabled="vcs.saving"
                @change="choose('none')"
              />
              <span>
                {{ t('vcs.choose.none') }}
                <span class="block text-small text-faint">{{ t('vcs.choose.noneHint') }}</span>
              </span>
            </label>
          </div>
        </section>

        <template v-if="vcs.backendId">
          <!-- Committing -->
          <section class="vxs-enter">
            <h2 class="mb-2 font-medium">{{ t('vcs.commit.title') }}</h2>
            <div class="space-y-3 rounded-lg border border-line bg-surface p-3">
              <!-- Which files those are is the Explorer's answer, on the files themselves
                   (§6.2). This says how many, because "commit everything" has to. -->
              <p class="text-small text-faint">
                <template v-if="vcs.changeCount">{{ pending }}</template>
                <template v-else>{{ t('vcs.commit.clean') }}</template>
              </p>

              <textarea
                v-model="message"
                rows="3"
                :placeholder="t('vcs.commit.placeholder')"
                class="w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
              />

              <label class="flex cursor-pointer items-start gap-2">
                <input
                  v-model="addUntracked"
                  type="checkbox"
                  class="mt-1 accent-[var(--vxs-accent)]"
                />
                <span>
                  {{ t('vcs.commit.untracked') }}
                  <span class="block text-small text-faint">
                    {{ t('vcs.commit.untrackedHint') }}
                  </span>
                </span>
              </label>

              <div class="flex items-center gap-2">
                <button
                  class="inline-flex items-center gap-1.5 rounded-md border border-line bg-accent-soft px-3 py-1 hover:bg-hover disabled:opacity-50"
                  :disabled="committing || !message.trim()"
                  @click="commit"
                >
                  <GitCommitVertical class="size-3.5" />
                  {{ committing ? t('vcs.commit.committing') : t('vcs.commit.commitAll') }}
                </button>
                <span class="text-small text-faint">{{ t('vcs.commit.wholeCopy') }}</span>
              </div>

              <p v-if="commitError" class="text-danger">{{ commitError }}</p>
              <p v-else-if="commitNote" class="rounded-md bg-inset p-2 text-muted">
                {{ commitNote }}
              </p>
            </div>
          </section>

          <!-- The diff -->
          <section class="vxs-enter">
            <div class="mb-2 flex items-center gap-2">
              <h2 class="font-medium">{{ t('vcs.diff.title') }}</h2>
              <span v-if="diffOpen" class="min-w-0 truncate font-mono text-small text-faint">
                {{ t('vcs.diff.scope') }}
              </span>
              <button
                class="ml-auto inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                :disabled="diffLoading"
                @click="diffOpen ? (diffOpen = false) : showDiff()"
              >
                <FileDiff class="size-3.5" />
                {{ diffOpen ? t('vcs.diff.hide') : t('vcs.diff.show') }}
              </button>
            </div>

            <div v-if="diffOpen" class="rounded-lg border border-line bg-inset">
              <p v-if="diffLoading" class="p-3 text-muted">{{ t('vcs.diff.reading') }}</p>
              <p v-else-if="diffError" class="p-3 text-danger">{{ diffError }}</p>
              <p v-else-if="!diffText.trim()" class="p-3 text-muted">{{ t('vcs.diff.empty') }}</p>
              <div v-else class="code-copy-block">
                <pre class="max-h-96 overflow-auto p-3 font-mono text-[12px] leading-snug">{{ diffText }}</pre>
                <CodeCopyButton :text="diffText" />
              </div>
            </div>
          </section>
        </template>
      </template>
    </div>
  </div>
</template>
