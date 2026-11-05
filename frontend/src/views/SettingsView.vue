<script setup lang="ts">
/**
 * Theme and project settings (spec §7).
 *
 * Everything here writes straight to `voxarium_studio/config.yaml` through the settings
 * endpoint — the theme travels with the project, not with the browser. The one exception is
 * the interface language, which is the reader's and stays in this browser (`@/i18n`).
 */
import {
  AlertTriangle,
  ArrowUpCircle,
  CheckCircle2,
  FolderOpen,
  Loader2,
  Plus,
  RefreshCw,
  X,
} from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { api } from '@/api/client'
import type { ThemeConfig } from '@/api/types'
import StartupSettings from '@/components/settings/StartupSettings.vue'
import CommitDescriptionSettings from '@/components/settings/CommitDescriptionSettings.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import SegmentedControl from '@/components/ui/SegmentedControl.vue'
import { LOCALES, locale, setLocale, t, type Locale, type MessageKey } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import {
  ACCENTS,
  CANVAS_GRIDS,
  FONTS,
  MODES,
  MONO_FONTS,
  NODE_TINT,
  SIDEBARS,
  STATUS_PALETTES,
  TEXT_SIZE,
  exportTheme,
  parseImportedTheme,
} from '@/lib/appearance'
import { formatTime, statusColor, statusLabel } from '@/lib/status'
import { useSessionStore } from '@/stores/session'
import { useThemeStore } from '@/stores/theme'
import { useUpdatesStore } from '@/stores/updates'

function openProjectMenu() { window.dispatchEvent(new Event('studio:open-project')) }
const session = useSessionStore()
const theme = useThemeStore()
const updates = useUpdatesStore()

const saveError = ref<string | null>(null)
const savingOther = ref(false)

const config = computed(() => session.project?.config ?? null)

/**
 * The page's sections, in the order they are drawn — the list beside the content that says
 * where a setting lives and takes the reader there. Nine panels are several screens of
 * scrolling, and the only way to find "Updates" used to be to scroll past the other eight.
 * Titles are keys, read while rendering, so the list follows the interface language.
 */
const SECTIONS: readonly { id: string; titleKey: MessageKey }[] = [
  { id: 'settings-startup', titleKey: 'settings.startup.title' },
  { id: 'settings-language', titleKey: 'settings.language.title' },
  { id: 'settings-appearance', titleKey: 'settings.appearance.title' },
  { id: 'settings-explorer', titleKey: 'settings.explorer.title' },
  { id: 'settings-execution', titleKey: 'settings.execution.title' },
  { id: 'settings-agents', titleKey: 'settings.agents.title' },
  { id: 'settings-tasks', titleKey: 'settings.tasks.title' },
  { id: 'settings-project', titleKey: 'settings.project.title' },
  { id: 'settings-updates', titleKey: 'settings.updates.title' },
]

const scroller = ref<HTMLElement | null>(null)
const pageEnd = ref<HTMLElement | null>(null)
const currentSection = ref(SECTIONS[0].id)

/**
 * Which section the reader is in: the first one crossing a band near the top of the page. An
 * observer rather than a scroll listener — it reports a section entering or leaving the band
 * and nothing in between. The last sections are short and can never reach that band, so the
 * end of the page is watched as well and, once on screen, names the last one.
 */
let sectionSpy: IntersectionObserver | null = null
let endSpy: IntersectionObserver | null = null
const inBand = new Set<string>()
let atEnd = false

function resolveSection() {
  if (atEnd) {
    currentSection.value = SECTIONS[SECTIONS.length - 1].id
    return
  }
  const first = SECTIONS.find((section) => inBand.has(section.id))
  if (first) currentSection.value = first.id
}

function stopSpying() {
  sectionSpy?.disconnect()
  endSpy?.disconnect()
  sectionSpy = endSpy = null
  inBand.clear()
  atEnd = false
}

function spyOnSections() {
  stopSpying()
  const root = scroller.value
  if (!root) return
  sectionSpy = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) inBand.add(entry.target.id)
        else inBand.delete(entry.target.id)
      }
      resolveSection()
    },
    { root, rootMargin: '-8% 0px -62% 0px' },
  )
  for (const section of SECTIONS) {
    const element = document.getElementById(section.id)
    if (element) sectionSpy.observe(element)
  }
  if (!pageEnd.value) return
  endSpy = new IntersectionObserver(
    ([entry]) => {
      // A page short enough to show whole is at its end and its start at once; the start wins.
      atEnd = entry.isIntersecting && root.scrollTop > 0
      resolveSection()
    },
    { root },
  )
  endSpy.observe(pageEnd.value)
}

// The sections exist only once the config has arrived, which may be after the page mounts —
// hence both: an `immediate` watcher would run during setup, before there is a DOM to observe.
onMounted(() => {
  if (config.value) spyOnSections()
})
watch(
  () => config.value !== null,
  (ready) => (ready ? spyOnSections() : stopSpying()),
  { flush: 'post' },
)
onBeforeUnmount(stopSpying)

function goToSection(id: string) {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  document
    .getElementById(id)
    ?.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' })
}

/** Which system the project is under, and whether agents get isolated checkouts (§10.5). */
const vcsSummary = computed(() => {
  const vcs = session.project?.vcs
  if (!vcs) return t('settings.project.vcsNone')
  const isolation = vcs.supports_isolation
    ? t('settings.project.vcsIsolated')
    : t('settings.project.vcsShared')
  return t('settings.project.vcsLine', {
    name: vcs.name,
    detail: vcs.available ? isolation : (vcs.reason ?? ''),
  })
})

const languageError = ref<string | null>(null)

/** Switch the interface language; only this browser remembers it, so nothing is saved. */
async function chooseLocale(next: Locale) {
  languageError.value = null
  try {
    await setLocale(next)
  } catch (cause) {
    languageError.value = cause instanceof Error ? cause.message : String(cause)
  }
}

async function setTheme(patch: Parameters<typeof theme.update>[0]) {
  saveError.value = null
  try {
    await theme.update(patch)
  } catch (cause) {
    saveError.value = cause instanceof Error ? cause.message : String(cause)
  }
}

/**
 * What `tokens.css` sets `--vxs-text-size` to when a project has not picked one, so the
 * slider can show the inherited value. Restated here rather than read off the DOM because
 * the slider has to know it even while the page is showing somebody else's override.
 */
const BASE_TEXT_SIZE = 14
const effectiveTextSize = computed(() => theme.theme.text_size ?? BASE_TEXT_SIZE)

/** The board's seven columns, as the row of dots beside the status-palette knob. */
const STATUS_PREVIEW = ['todo', 'pending', 'in_progress', 'needs_review', 'done', 'blocked', 'cancelled']

/**
 * Custom tokens are edited as rows and written back as one map. The draft is re-seeded from
 * the stored map after every save, so a row that did not make it (no `--` name, no value)
 * disappears rather than lingering as something that looks saved and is not.
 */
const tokenDraft = ref<{ name: string; value: string }[]>([])
watch(
  () => theme.theme.custom_tokens,
  (tokens) => {
    tokenDraft.value = Object.entries(tokens).map(([name, value]) => ({ name, value }))
  },
  { immediate: true, deep: true },
)

function addToken() {
  tokenDraft.value.push({ name: '--vxs-', value: '' })
}

function removeToken(index: number) {
  tokenDraft.value.splice(index, 1)
  void commitTokens()
}

async function commitTokens() {
  const tokens: Record<string, string> = {}
  for (const row of tokenDraft.value) {
    const name = row.name.trim()
    const value = row.value.trim()
    if (/^--[a-zA-Z0-9_-]+$/.test(name) && value && !/[;{}]/.test(value)) tokens[name] = value
  }
  if (JSON.stringify(tokens) === JSON.stringify(theme.theme.custom_tokens)) return
  await setTheme({ custom_tokens: tokens })
}

/**
 * Studio updates (§12.1). The rail carries the button; this is where the setting behind
 * it lives, and where the answer is when there is nothing to offer — a rail that shows
 * nothing cannot tell "up to date" from "never checked".
 */
const UPDATE_INTERVALS = computed(() => [
  { value: 15, label: t('settings.updates.every15Minutes') },
  { value: 60, label: t('settings.updates.hourly') },
  { value: 180, label: t('settings.updates.every3Hours') },
  { value: 720, label: t('settings.updates.twiceADay') },
  { value: 1440, label: t('settings.updates.daily') },
])

const updatesSummary = computed(() => {
  const info = updates.status
  if (!info) return t('settings.updates.reading')
  if (!info.supported) return info.reason ?? t('settings.updates.cannotUpdate')
  if (info.available) return t('settings.updates.available', { version: info.latest ?? '' })
  if (updates.desktop) return t(info.error ? 'updates.dialog.stopped' : !info.checked_at ? 'updates.desktop.unchecked' : 'updates.desktop.upToDate')
  if (info.current) {
    return t('settings.updates.onLatest', {
      current: info.current,
      upstream: info.upstream ?? t('settings.updates.thisBranch'),
    })
  }
  return t('settings.updates.noRelease')
})

/** What follows the build's name on its line: when it was last checked, and a dirty tree. */
const buildNotes = computed(() => {
  const notes: string[] = []
  const info = updates.status
  if (info?.checked_at) notes.push(t('settings.updates.checked', { time: formatTime(info.checked_at) }))
  if (info?.dirty) notes.push(t('settings.updates.dirty'))
  return notes
})

async function setUpdates(patch: { enabled?: boolean; check_interval_minutes?: number }) {
  savingOther.value = true
  saveError.value = null
  try {
    const updated = await api.updateSettings({ updates: patch })
    if (session.project) session.project = { ...session.project, config: updated }
    // The poller was restarted on the backend; re-read so "checked" and the interval on
    // screen are the ones now in force.
    await updates.load()
  } catch (cause) {
    saveError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    savingOther.value = false
  }
}

const fileInput = ref<HTMLInputElement | null>(null)

/** The whole theme as a file — the same shape config.yaml holds, so it can be pasted there too. */
function downloadTheme() {
  const blob = new Blob([exportTheme(theme.theme)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${session.projectName || 'voxarium_studio'}-theme.json`
  anchor.click()
  URL.revokeObjectURL(url)
}

/** Read a theme file back; whatever it holds is checked field by field before it is applied. */
async function importTheme(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  saveError.value = null
  try {
    await theme.update(parseImportedTheme(await file.text()))
  } catch (cause) {
    saveError.value = cause instanceof Error ? cause.message : String(cause)
  }
}

async function setShowDataDir(value: boolean) {
  savingOther.value = true
  saveError.value = null
  try {
    const updated = await api.updateSettings({ show_data_dir: value })
    if (session.project) session.project = { ...session.project, config: updated }
  } catch (cause) {
    saveError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    savingOther.value = false
  }
}

async function setNodeCache(value: boolean) {
  savingOther.value = true
  saveError.value = null
  try {
    const updated = await api.updateSettings({ node_cache_enabled: value })
    if (session.project) session.project = { ...session.project, config: updated }
  } catch (cause) {
    saveError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    savingOther.value = false
  }
}

/**
 * Where agent sessions start (§10.5). Edited as text with an explicit Save rather than
 * saved on every keystroke, because a half-typed path names a directory that does not
 * exist and the backend refuses those.
 */
const savedLaunchRoot = computed(() => config.value?.teams.launch_root ?? '.')
const launchRoot = ref(savedLaunchRoot.value)
watch(savedLaunchRoot, (value) => {
  launchRoot.value = value
})
const launchRootDirty = computed(() => (launchRoot.value.trim() || '.') !== savedLaunchRoot.value)

/**
 * What the path will mean, resolved against the project root. An approximation of the
 * backend's own `resolve()` — no symlinks, no case folding — but it answers the question
 * the setting actually raises, which is where `../` lands.
 */
const resolvedLaunchRoot = computed(() => {
  const root = session.project?.root ?? ''
  const value = launchRoot.value.trim() || '.'
  if (!root) return ''
  if (/^([a-zA-Z]:[\\/]|[\\/])/.test(value)) return value
  const sep = root.includes('\\') ? '\\' : '/'
  const parts = root.split(/[\\/]/)
  for (const segment of value.split(/[\\/]/)) {
    if (!segment || segment === '.') continue
    if (segment === '..') {
      if (parts.length > 1) parts.pop()
    } else {
      parts.push(segment)
    }
  }
  // A lone `D:` is a drive, not a directory — the trailing separator is what makes it one.
  if (parts.length === 1) return parts[0] + sep
  return parts.join(sep)
})

/** True once the path leaves the project, which is what turns isolated checkouts off. */
const launchRootOutside = computed(() => {
  const root = session.project?.root
  if (!root) return false
  const resolved = resolvedLaunchRoot.value
  const normalise = (value: string) => value.replace(/[\\/]+$/, '').toLowerCase()
  const base = normalise(root)
  const target = normalise(resolved)
  return target !== base && !target.startsWith(base + (root.includes('\\') ? '\\' : '/'))
})

async function saveLaunchRoot() {
  savingOther.value = true
  saveError.value = null
  try {
    const updated = await api.updateSettings({
      teams: { launch_root: launchRoot.value.trim() || '.' },
    })
    if (session.project) session.project = { ...session.project, config: updated }
  } catch (cause) {
    saveError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    savingOther.value = false
  }
}

/**
 * How many agent sessions — task runs and chats — hold a slot at once (`concurrency.agents`,
 * §8.2). Saved explicitly like the launch root, and applied by the backend without a
 * restart: raising it starts whatever is queued behind the old limit on the spot.
 */
const MAX_PARALLEL_AGENTS = 16
const savedParallelAgents = computed(() => config.value?.concurrency.agents ?? 2)
const parallelAgents = ref(savedParallelAgents.value)
watch(savedParallelAgents, (value) => {
  parallelAgents.value = value
})
const parallelAgentsValid = computed(
  () =>
    Number.isInteger(parallelAgents.value) &&
    parallelAgents.value >= 1 &&
    parallelAgents.value <= MAX_PARALLEL_AGENTS,
)
const parallelAgentsDirty = computed(() => parallelAgents.value !== savedParallelAgents.value)

async function saveParallelAgents() {
  if (!parallelAgentsValid.value || !parallelAgentsDirty.value) return
  savingOther.value = true
  saveError.value = null
  try {
    const updated = await api.updateSettings({ concurrency: { agents: parallelAgents.value } })
    if (session.project) {
      session.project = {
        ...session.project,
        config: updated,
        // The Execution section lists the engine's live limits, which this save just moved.
        concurrency_limits: { ...session.project.concurrency_limits, agent: updated.concurrency.agents },
      }
    }
  } catch (cause) {
    saveError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    savingOther.value = false
  }
}

/**
 * The code every task id is displayed with — `VS` in `VS-01` (§10.2).
 *
 * Edited with an explicit Save for the reason the launch root is: half a code is a valid
 * string the backend would happily store, and every task on the board would change its
 * name on each keystroke. Cleared, it falls back to a code derived from the project's
 * name, which is why the field can be emptied at all.
 */
const savedProjectCode = computed(() => config.value?.project_code ?? '')
const projectCode = ref(savedProjectCode.value)
watch(savedProjectCode, (value) => {
  projectCode.value = value
})
const projectCodeDirty = computed(
  () => projectCode.value.trim().toUpperCase() !== savedProjectCode.value,
)

/** What the first task will read as, so the effect of the field is visible before saving. */
const sampleTaskId = computed(() => {
  const typed = projectCode.value.trim().toUpperCase()
  if (typed) return `${typed}-01`
  // Mirrors the backend's `derive_project_code`: initials for a multi-word name, the
  // first three letters otherwise. Only a preview — the backend is what decides.
  const words = (session.project?.name ?? '').match(/[A-Za-z0-9]+/g) ?? []
  if (!words.length) return 'TSK-01'
  const derived = (words.length > 1 ? words.map((w) => w[0]).slice(0, 4).join('') : words[0]!.slice(0, 3))
    .toUpperCase()
  return `${/[A-Z]/.test(derived) ? derived : 'TSK'}-01`
})

async function saveProjectCode() {
  savingOther.value = true
  saveError.value = null
  try {
    const updated = await api.updateSettings({ project_code: projectCode.value.trim() })
    if (session.project) session.project = { ...session.project, config: updated }
  } catch (cause) {
    saveError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    savingOther.value = false
  }
}

async function clearAssetCache() {
  if (!window.confirm(t('settings.execution.assetCacheConfirm'))) return
  savingOther.value = true
  try {
    const result = await api.clearAssetCache()
    saveError.value = t('settings.execution.assetCacheCleared', {
      count: result.removed, size: (result.bytes / 1048576).toFixed(1), failed: result.failed,
    })
  } catch (error) {
    saveError.value = String(error)
  } finally {
    savingOther.value = false
  }
}

async function clearCache() {
  savingOther.value = true
  try {
    const result = await api.clearNodeCache()
    saveError.value = t('settings.execution.cacheCleared', { count: result.removed })
  } finally {
    savingOther.value = false
  }
}
</script>

<template>
  <div ref="scroller" class="h-full overflow-auto">
    <header class="vxs-pane border-b border-pane bg-surface px-pane py-2">
      <h1 class="text-title font-medium">{{ t('settings.title') }}</h1>
      <p class="truncate text-small text-faint">{{ session.project?.data_dir }}/config.yaml</p>
    </header>

    <EmptyState v-if="!config" :title="t('settings.unavailable')" />

    <!-- The sections in a column, and beside it — where the page is wide enough to have the
         room — the list of them. A container query rather than a breakpoint: what matters is
         the width this page was given, which the rail and the window both change. Each
         section is one of the page's bands (`vxs-enter`, §7), arriving top-down. -->
    <div v-else class="@container p-pane">
      <div class="flex items-start gap-10">
        <div class="max-w-2xl min-w-0 flex-1 space-y-6">
          <p v-if="saveError" class="rounded-md bg-inset p-2 text-muted">{{ saveError }}</p>

          <section id="settings-startup" class="vxs-enter scroll-mt-4"><StartupSettings /></section>
          <section id="settings-language" class="vxs-enter scroll-mt-4">
            <h2 class="mb-2 font-medium">{{ t('settings.language.title') }}</h2>

            <div class="vxs-card rounded-lg border border-card bg-surface p-4 shadow-card">
              <div class="flex items-start gap-3">
                <span class="w-28 shrink-0 pt-1 text-muted">{{ t('settings.language.label') }}</span>
                <div class="min-w-0 flex-1 space-y-1.5">
                  <SegmentedControl
                    :model-value="locale"
                    :options="LOCALES"
                    @update:model-value="chooseLocale"
                  />
                  <p class="text-small text-faint">
                    <I18nT keypath="settings.language.hint">
                      <template #file><code class="font-mono">config.yaml</code></template>
                    </I18nT>
                  </p>
                  <p v-if="languageError" class="text-small text-warning">{{ languageError }}</p>
                </div>
              </div>
            </div>
          </section>

          <section id="settings-appearance" class="vxs-enter scroll-mt-4">
            <h2 class="mb-2 font-medium">{{ t('settings.appearance.title') }}</h2>

            <div class="vxs-card space-y-4 rounded-lg border border-card bg-surface p-4 shadow-card">
              <div class="flex items-start gap-3">
                <span class="w-28 shrink-0 pt-1 text-muted">{{ t('settings.appearance.mode') }}</span>
                <SegmentedControl
                  :model-value="theme.theme.mode"
                  :options="MODES"
                  @update:model-value="setTheme({ mode: $event })"
                />
              </div>

              <div class="flex items-start gap-3">
                <span class="w-28 shrink-0 pt-1 text-muted">{{ t('settings.appearance.accent') }}</span>
                <div class="flex items-center gap-2">
                  <button
                    v-for="accent in ACCENTS"
                    :key="accent"
                    type="button"
                    class="size-6 rounded-full border-2"
                    :style="{
                      background: accent,
                      borderColor: theme.theme.accent === accent ? 'var(--vxs-text-primary)' : 'transparent',
                    }"
                    :title="accent"
                    @click="setTheme({ accent })"
                  />
                  <input
                    type="color"
                    :value="theme.theme.accent"
                    class="size-6 cursor-pointer rounded-full border-0 bg-transparent p-0"
                    :title="t('settings.appearance.customAccent')"
                    @change="setTheme({ accent: ($event.target as HTMLInputElement).value })"
                  />
                </div>
              </div>

              <div class="flex items-start gap-3">
                <span class="w-28 shrink-0 pt-1 text-muted">{{ t('settings.appearance.textSize') }}</span>
                <div class="min-w-0 flex-1 space-y-1.5">
                  <div class="flex items-center gap-3">
                    <input
                      type="range"
                      :min="TEXT_SIZE.min"
                      :max="TEXT_SIZE.max"
                      :value="effectiveTextSize"
                      class="w-56 accent-[var(--vxs-accent)]"
                      @change="setTheme({ text_size: Number(($event.target as HTMLInputElement).value) })"
                    />
                    <span class="tabular-nums">
                      {{ t('settings.appearance.pixels', { size: effectiveTextSize }) }}
                    </span>
                  </div>
                  <p class="text-small text-faint">
                    <template v-if="theme.theme.text_size === null">
                      {{ t('settings.appearance.textSizeInherited') }}
                    </template>
                    <template v-else>
                      {{ t('settings.appearance.textSizePicked') }}
                      <button
                        type="button"
                        class="underline hover:text-primary"
                        @click="setTheme({ text_size: null })"
                      >
                        {{ t('settings.appearance.useDefaultSize') }}
                      </button>
                    </template>
                  </p>
                </div>
              </div>

              <div class="flex items-start gap-3">
                <span class="w-28 shrink-0 pt-1 text-muted">{{ t('settings.appearance.fonts') }}</span>
                <div class="flex flex-wrap gap-3">
                  <label class="flex flex-col gap-1">
                    <span class="text-small text-faint">{{ t('settings.appearance.fontInterface') }}</span>
                    <select
                      :value="theme.theme.font"
                      class="rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
                      @change="
                        setTheme({ font: ($event.target as HTMLSelectElement).value as ThemeConfig['font'] })
                      "
                    >
                      <option v-for="face in FONTS" :key="face.value" :value="face.value">
                        {{ face.label }}
                      </option>
                    </select>
                  </label>
                  <label class="flex flex-col gap-1">
                    <span class="text-small text-faint">{{ t('settings.appearance.fontCode') }}</span>
                    <select
                      :value="theme.theme.mono_font"
                      class="rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
                      @change="
                        setTheme({
                          mono_font: ($event.target as HTMLSelectElement).value as ThemeConfig['mono_font'],
                        })
                      "
                    >
                      <option v-for="face in MONO_FONTS" :key="face.value" :value="face.value">
                        {{ face.label }}
                      </option>
                    </select>
                  </label>
                </div>
              </div>

              <div class="flex items-start gap-3">
                <span class="w-28 shrink-0 pt-1 text-muted">{{ t('settings.appearance.sidebar') }}</span>
                <div class="min-w-0 flex-1 space-y-1.5">
                  <SegmentedControl
                    :model-value="theme.theme.sidebar"
                    :options="SIDEBARS"
                    @update:model-value="setTheme({ sidebar: $event })"
                  />
                  <p class="text-small text-faint">{{ t('settings.appearance.sidebarHint') }}</p>
                </div>
              </div>

              <div class="flex items-start gap-3">
                <span class="w-28 shrink-0 pt-1 text-muted">{{ t('settings.appearance.canvas') }}</span>
                <div class="flex flex-wrap items-end gap-4">
                  <label class="flex flex-col gap-1">
                    <span class="text-small text-faint">{{ t('settings.appearance.grid') }}</span>
                    <SegmentedControl
                      :model-value="theme.theme.canvas_grid"
                      :options="CANVAS_GRIDS"
                      @update:model-value="setTheme({ canvas_grid: $event })"
                    />
                  </label>
                  <label class="flex flex-col gap-1">
                    <span class="text-small text-faint">{{ t('settings.appearance.nodeTint') }}</span>
                    <span class="flex items-center gap-2">
                      <input
                        type="range"
                        :min="NODE_TINT.min"
                        :max="NODE_TINT.max"
                        :value="theme.theme.node_tint"
                        class="w-36 accent-[var(--vxs-accent)]"
                        @change="setTheme({ node_tint: Number(($event.target as HTMLInputElement).value) })"
                      />
                      <span class="tabular-nums">{{ theme.theme.node_tint }}%</span>
                    </span>
                  </label>
                </div>
              </div>

              <div class="flex items-start gap-3">
                <span class="w-28 shrink-0 pt-1 text-muted">{{ t('settings.appearance.statusColours') }}</span>
                <div class="min-w-0 flex-1 space-y-1.5">
                  <div class="flex flex-wrap items-center gap-3">
                    <SegmentedControl
                      :model-value="theme.theme.status_palette"
                      :options="STATUS_PALETTES"
                      @update:model-value="setTheme({ status_palette: $event })"
                    />
                    <span class="flex items-center gap-1.5">
                      <span
                        v-for="status in STATUS_PREVIEW"
                        :key="status"
                        class="block size-2.5 rounded-full"
                        :style="{ background: statusColor(status) }"
                        :title="statusLabel(status)"
                      />
                    </span>
                  </div>
                  <p class="text-small text-faint">{{ t('settings.appearance.statusColoursHint') }}</p>
                </div>
              </div>

              <div class="flex items-start gap-3">
                <span class="w-28 shrink-0 pt-1 text-muted">{{ t('settings.appearance.customTokens') }}</span>
                <div class="min-w-0 flex-1 space-y-2">
                  <div v-for="(row, index) in tokenDraft" :key="index" class="flex gap-1.5">
                    <input
                      v-model="row.name"
                      class="min-w-0 flex-1 rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
                      placeholder="--vxs-bg-surface"
                      spellcheck="false"
                      @blur="commitTokens"
                      @keyup.enter="commitTokens"
                    />
                    <input
                      v-model="row.value"
                      class="min-w-0 flex-1 rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
                      placeholder="#101114"
                      spellcheck="false"
                      @blur="commitTokens"
                      @keyup.enter="commitTokens"
                    />
                    <button
                      type="button"
                      class="grid size-7 shrink-0 place-items-center rounded-md text-faint hover:bg-hover hover:text-primary"
                      :title="t('settings.appearance.removeToken')"
                      @click="removeToken(index)"
                    >
                      <X class="size-3.5" />
                    </button>
                  </div>
                  <div class="flex flex-wrap gap-2">
                    <button
                      type="button"
                      class="flex items-center gap-1 rounded-md border border-dashed border-line-strong px-3 py-1 text-muted hover:bg-hover hover:text-primary"
                      @click="addToken"
                    >
                      <Plus class="size-3.5" /> {{ t('settings.appearance.addToken') }}
                    </button>
                    <button
                      type="button"
                      class="rounded-md border border-line px-3 py-1 hover:bg-hover"
                      @click="downloadTheme"
                    >
                      {{ t('settings.appearance.exportTheme') }}
                    </button>
                    <button
                      type="button"
                      class="rounded-md border border-line px-3 py-1 hover:bg-hover"
                      @click="fileInput?.click()"
                    >
                      {{ t('settings.appearance.importTheme') }}
                    </button>
                    <input
                      ref="fileInput"
                      type="file"
                      accept="application/json,.json"
                      class="hidden"
                      @change="importTheme"
                    />
                  </div>
                  <p class="text-small text-faint">
                    <I18nT keypath="settings.appearance.customTokensHint">
                      <template #file><code class="font-mono">config.yaml</code></template>
                    </I18nT>
                  </p>
                </div>
              </div>
            </div>
          </section>

          <section id="settings-explorer" class="vxs-enter scroll-mt-4">
            <h2 class="mb-2 font-medium">{{ t('settings.explorer.title') }}</h2>
            <label class="vxs-card flex cursor-pointer items-center gap-2 rounded-lg border border-card bg-surface p-4 shadow-card">
              <input
                type="checkbox"
                :checked="config.explorer.show_data_dir"
                :disabled="savingOther"
                class="accent-[var(--vxs-accent)]"
                @change="setShowDataDir(($event.target as HTMLInputElement).checked)"
              />
              <span>
                <I18nT keypath="settings.explorer.showDataDir">
                  <template #dir><code class="font-mono">voxarium_studio/</code></template>
                </I18nT>
                <span class="block text-small text-faint">
                  {{ t('settings.explorer.showDataDirHint') }}
                </span>
              </span>
            </label>
          </section>

          <section id="settings-execution" class="vxs-enter scroll-mt-4">
            <h2 class="mb-2 font-medium">{{ t('settings.execution.title') }}</h2>
            <div class="vxs-card space-y-3 rounded-lg border border-card bg-surface p-4 shadow-card">
              <label class="flex cursor-pointer items-center gap-2">
                <input
                  type="checkbox"
                  :checked="config.node_cache_enabled"
                  :disabled="savingOther"
                  class="accent-[var(--vxs-accent)]"
                  @change="setNodeCache(($event.target as HTMLInputElement).checked)"
                />
                <span>
                  {{ t('settings.execution.reuseCache') }}
                  <span class="block text-small text-faint">
                    {{ t('settings.execution.reuseCacheHint') }}
                  </span>
                </span>
              </label>

              <button
                class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
                :disabled="savingOther"
                @click="clearCache"
              >
                {{ t('settings.execution.clearCache') }}
              </button>

              <button
                class="rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
                :disabled="savingOther"
                @click="clearAssetCache"
              >
                {{ t('settings.execution.clearAssetCache') }}
              </button>
              <p class="text-small text-faint">{{ t('settings.execution.assetCacheHint') }}</p>

              <dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-small text-faint">
                <template v-for="(limit, name) in session.project?.concurrency_limits ?? {}" :key="name">
                  <dt>{{ t('settings.execution.concurrentJobs', { name }) }}</dt>
                  <dd>{{ limit }}</dd>
                </template>
              </dl>
              <p class="text-small text-faint">
                <I18nT keypath="settings.execution.concurrencyHint">
                  <template #file><code class="font-mono">config.yaml</code></template>
                </I18nT>
              </p>
            </div>
          </section>

          <section id="settings-agents" class="vxs-enter scroll-mt-4">
            <h2 class="mb-2 font-medium">{{ t('settings.agents.title') }}</h2>
            <div class="vxs-card space-y-3 rounded-lg border border-card bg-surface p-4 shadow-card">
              <div>
                <label class="block">
                  <span class="mb-1 block text-muted">{{ t('settings.agents.parallel') }}</span>
                  <div class="flex gap-2">
                    <input
                      v-model.number="parallelAgents"
                      type="number"
                      min="1"
                      :max="MAX_PARALLEL_AGENTS"
                      step="1"
                      class="w-20 min-w-0 rounded-md border bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
                      :class="parallelAgentsValid ? 'border-line' : 'border-danger'"
                      @keyup.enter="saveParallelAgents"
                    />
                    <button
                      class="shrink-0 rounded-md border border-line px-3 py-1 hover:bg-hover disabled:opacity-50"
                      :disabled="savingOther || !parallelAgentsDirty || !parallelAgentsValid"
                      @click="saveParallelAgents"
                    >
                      {{ t('common.save') }}
                    </button>
                  </div>
                </label>
                <p class="mt-1 text-small text-faint">{{ t('settings.agents.parallelHint') }}</p>
              </div>

              <div>
                <label class="block">
                  <span class="mb-1 block text-muted">{{ t('settings.agents.launchRoot') }}</span>
                  <div class="flex gap-2">
                    <input
                      v-model="launchRoot"
                      class="min-w-0 flex-1 rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
                      placeholder="."
                      spellcheck="false"
                      @keyup.enter="saveLaunchRoot"
                    />
                    <button
                      class="shrink-0 rounded-md border border-line px-3 py-1 hover:bg-hover disabled:opacity-50"
                      :disabled="savingOther || !launchRootDirty"
                      @click="saveLaunchRoot"
                    >
                      {{ t('common.save') }}
                    </button>
                  </div>
                </label>
                <p class="mt-1 text-small text-faint">
                  <I18nT keypath="settings.agents.launchRootHint">
                    <template #here><code class="font-mono">.</code></template>
                    <template #up><code class="font-mono">../</code></template>
                  </I18nT>
                </p>
                <dl class="mt-2 grid grid-cols-[8rem_1fr] gap-1 text-small">
                  <dt class="text-muted">{{ t('settings.agents.startIn') }}</dt>
                  <dd class="font-mono break-all" :class="launchRootDirty ? 'text-warning' : ''">
                    {{
                      launchRootDirty
                        ? t('settings.unsavedValue', { value: resolvedLaunchRoot })
                        : resolvedLaunchRoot
                    }}
                  </dd>
                </dl>
              </div>

              <p v-if="launchRootOutside" class="rounded-md bg-warning-soft p-2 text-small text-warning">
                {{ t('settings.agents.outside') }}
              </p>
              <CommitDescriptionSettings />
            </div>
          </section>

          <section id="settings-tasks" class="vxs-enter scroll-mt-4">
            <h2 class="mb-2 font-medium">{{ t('settings.tasks.title') }}</h2>
            <div class="vxs-card space-y-3 rounded-lg border border-card bg-surface p-4 shadow-card">
              <div>
                <label class="block">
                  <span class="mb-1 block text-muted">{{ t('settings.tasks.projectCode') }}</span>
                  <div class="flex gap-2">
                    <input
                      v-model="projectCode"
                      maxlength="6"
                      class="w-28 min-w-0 rounded-md border border-line bg-inset px-2 py-1 font-mono uppercase outline-none focus:border-accent"
                      :placeholder="sampleTaskId.split('-')[0]"
                      spellcheck="false"
                      @keyup.enter="saveProjectCode"
                    />
                    <button
                      class="shrink-0 rounded-md border border-line px-3 py-1 hover:bg-hover disabled:opacity-50"
                      :disabled="savingOther || !projectCodeDirty"
                      @click="saveProjectCode"
                    >
                      {{ t('common.save') }}
                    </button>
                  </div>
                </label>
                <p class="mt-1 text-small text-faint">{{ t('settings.tasks.projectCodeHint') }}</p>
                <dl class="mt-2 grid grid-cols-[8rem_1fr] gap-1 text-small">
                  <dt class="text-muted">{{ t('settings.tasks.readAs') }}</dt>
                  <dd class="font-mono" :class="projectCodeDirty ? 'text-warning' : ''">
                    {{
                      projectCodeDirty ? t('settings.unsavedValue', { value: sampleTaskId }) : sampleTaskId
                    }}
                  </dd>
                </dl>
                <p class="mt-1 text-small text-faint">{{ t('settings.tasks.renamesNothing') }}</p>
              </div>
            </div>
          </section>

          <section id="settings-project" class="vxs-enter scroll-mt-4">
            <h2 class="mb-2 font-medium">{{ t('settings.project.title') }}</h2>
            <div class="vxs-card space-y-3 rounded-lg border border-card bg-surface p-4 shadow-card">
              <dl class="grid grid-cols-[8rem_1fr] gap-1">
                <dt class="text-muted">{{ t('settings.project.root') }}</dt>
                <dd class="font-mono break-all">{{ session.project?.root }}</dd>
                <dt class="text-muted">{{ t('settings.project.versionControl') }}</dt>
                <dd>{{ vcsSummary }}</dd>
                <dt class="text-muted">{{ t('settings.project.authentication') }}</dt>
                <dd>
                  {{
                    session.project?.auth_required
                      ? t('settings.project.tokenRequired')
                      : t('settings.project.open')
                  }}
                </dd>
              </dl>

              <button
                class="flex items-center gap-2 rounded-md border border-line px-3 py-1.5 hover:bg-hover"
                @click="openProjectMenu"
              >
                <FolderOpen class="size-4" />
                {{ t('settings.project.openAnother') }}
              </button>
              <p class="text-small text-faint">{{ t('settings.project.openAnotherHint') }}</p>
            </div>
          </section>

          <section id="settings-updates" class="vxs-enter scroll-mt-4">
            <h2 class="mb-2 font-medium">{{ t('settings.updates.title') }}</h2>
            <div class="vxs-card space-y-3 rounded-lg border border-card bg-surface p-4 shadow-card">
              <div class="flex items-start gap-2">
                <Loader2 v-if="updates.running" class="mt-0.5 size-4 shrink-0 animate-spin text-accent" />
                <ArrowUpCircle
                  v-else-if="updates.available"
                  class="mt-0.5 size-4 shrink-0 text-accent"
                />
                <AlertTriangle
                  v-else-if="updates.status && !updates.status.supported"
                  class="mt-0.5 size-4 shrink-0 text-faint"
                />
                <CheckCircle2 v-else class="mt-0.5 size-4 shrink-0 text-success" />
                <div class="min-w-0 flex-1">
                  <p>{{ updatesSummary }}</p>
                  <p class="text-small text-faint">
                    <I18nT keypath="settings.updates.thisBuild">
                      <template #version>
                        <span class="font-mono">{{ session.project?.version?.display }}</span>
                      </template>
                    </I18nT>
                    <!-- The separator is literal text in the loop, not part of the message: a
                         block holding nothing but an interpolation loses the whitespace around it,
                         which ran the notes into the version. -->
                    <template v-for="note in buildNotes" :key="note"> · {{ note }}</template>
                  </p>
                  <p v-if="updates.status?.error" class="text-small text-warning">
                    {{ updates.status.error }}
                  </p>
                </div>
              </div>

              <div v-if="updates.status?.supported" class="flex flex-wrap gap-2">
                <button
                  v-if="updates.available || updates.running"
                  class="vxs-btn-primary flex items-center gap-2 px-3 py-1.5"
                  @click="updates.running ? (updates.dialogOpen = true) : updates.startFromButton()"
                >
                  <ArrowUpCircle class="size-4" />
                  {{ updates.running ? t('settings.updates.updating') : updates.label }}
                </button>
                <button
                  class="flex items-center gap-2 rounded-md border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
                  :disabled="updates.busy || updates.running"
                  @click="updates.check()"
                >
                  <RefreshCw class="size-4" :class="{ 'animate-spin': updates.busy }" />
                  {{ t('settings.updates.checkNow') }}
                </button>
              </div>

              <label v-if="!updates.desktop" class="flex cursor-pointer items-center gap-2">
                <input
                  type="checkbox"
                  :checked="config.updates.enabled"
                  :disabled="savingOther"
                  class="accent-[var(--vxs-accent)]"
                  @change="setUpdates({ enabled: ($event.target as HTMLInputElement).checked })"
                />
                <span>
                  {{ t('settings.updates.automatic') }}
                  <span class="block text-small text-faint">
                    {{ t('settings.updates.automaticHint') }}
                  </span>
                </span>
              </label>

              <label v-if="!updates.desktop" class="block">
                <span class="mb-1 block text-muted">{{ t('settings.updates.howOften') }}</span>
                <select
                  class="rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent disabled:opacity-50"
                  :value="config.updates.check_interval_minutes"
                  :disabled="savingOther || !config.updates.enabled"
                  @change="
                    setUpdates({
                      check_interval_minutes: Number(($event.target as HTMLSelectElement).value),
                    })
                  "
                >
                  <option v-for="option in UPDATE_INTERVALS" :key="option.value" :value="option.value">
                    {{ option.label }}
                  </option>
                </select>
              </label>

              <p class="text-small text-faint">
                <span v-if="updates.desktop">{{ t('updates.desktop.schedule') }}</span>
                <I18nT v-else keypath="settings.updates.howItWorks">
                  <template #command><code class="font-mono">git pull</code></template>
                </I18nT>
              </p>
            </div>
          </section>
        </div>

        <nav
          class="sticky top-pane hidden w-44 shrink-0 @4xl:block"
          :aria-label="t('settings.onThisPage')"
        >
          <p class="mb-2 text-small tracking-wide text-faint uppercase">
            {{ t('settings.onThisPage') }}
          </p>
          <!-- One line down the list's edge, and the current section's stretch of it in the
               accent: the rail's mark, at the size of a page. -->
          <ul class="border-l border-line">
            <li v-for="section in SECTIONS" :key="section.id">
              <button
                type="button"
                class="-ml-px block w-full border-l py-1 pl-3 text-left"
                :class="
                  currentSection === section.id
                    ? 'border-accent text-primary'
                    : 'border-transparent text-muted hover:text-primary'
                "
                :aria-current="currentSection === section.id ? 'location' : undefined"
                @click="goToSection(section.id)"
              >
                {{ t(section.titleKey) }}
              </button>
            </li>
          </ul>
        </nav>
      </div>

      <!-- What the section list watches to know the page has been read to its end. -->
      <div ref="pageEnd" class="h-px" aria-hidden="true" />
    </div>

  </div>
</template>
