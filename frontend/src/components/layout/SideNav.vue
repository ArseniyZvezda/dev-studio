<script setup lang="ts">
/**
 * The narrow icon+label rail from spec §6.1. Built-in sections come first; anything a
 * plugin registered is appended below a divider, so it is always obvious which parts of
 * the app are core and which arrived with a plugin.
 *
 * `theme.sidebar` decides its shape (§7): `labels` is the full rail, `icons` a 48px strip
 * whose tooltips carry the names, and `auto` that strip widening *over* the page while the
 * pointer is on it — an overlay rather than a push, so nothing else reflows on every pass.
 * All three are the same markup; the words are hidden, never removed, so the strip keeps
 * every control and every `title`.
 */
import {
  ArrowUpCircle,
  Blocks,
  Brain,
  ChartLine,
  FileText,
  FolderTree,
  FolderCog,
  GitBranch,
  GraduationCap,
  Info,
  Loader2,
  MessageCircle,
  Clapperboard,
  Plug,
  ScrollText,
  Search,
  Settings,
  SquareKanban,
  Users,
  Waypoints,
  Workflow,
  Zap,
  type LucideIcon,
} from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch, type CSSProperties } from 'vue'
import { useRoute } from 'vue-router'

import AboutDialog from '@/components/project/AboutDialog.vue'
import ChangelogDialog from '@/components/project/ChangelogDialog.vue'
import UpdateDialog from '@/components/project/UpdateDialog.vue'
import AgentBackendIcon from '@/components/ui/AgentBackendIcon.vue'
import { t, type MessageKey } from '@/i18n'
import { formatTime } from '@/lib/status'
import { pluginNavItems } from '@/plugin-runtime/loader'
import { useSearchStore } from '@/stores/search'
import { useSessionStore } from '@/stores/session'
import { useThemeStore } from '@/stores/theme'
import { useUpdatesStore } from '@/stores/updates'
import { useVcsStore, vcsIndicator } from '@/stores/vcs'

interface NavItem {
  to: string
  /**
   * The section's name as a message key, resolved while rendering: the rail outlives every
   * page, so a name read once when it mounted would stay in the language it mounted in.
   */
  labelKey: MessageKey
  icon: LucideIcon
  /**
   * The section's own icon colour, one of the `--vxs-nav-*` tokens (`tokens.css`): the rail's
   * colour lives on its icons, and the rows themselves stay grey.
   */
  tint: string
}

const session = useSessionStore()
const theme = useThemeStore()
const vcs = useVcsStore()
const updates = useUpdatesStore()
const search = useSearchStore()
const showingChangelog = ref(false)
const showingAbout = ref(false)
let statusTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  statusTimer = setInterval(() => {
    // The project menu still uses the current project's task counts (§5.1).
    void session.refreshTaskCounts()
    if (session.connection === 'open') void session.refreshStatus()
  }, 15_000)
})
onBeforeUnmount(() => clearInterval(statusTimer))
watch(() => session.connection, (connection) => {
  if (connection === 'open') void session.refreshStatus()
}, { immediate: true })

// §6.1: the backend's monochrome mark carries readiness; the whole row explains it.
const agentBackends = computed(() => (['claude_code', 'codex', 'qwen'] as const).map((id) => {
  const label = t(`nav.agentBackends.${id}`)
  const info = session.connection === 'open' ? session.status?.agent_backends[id] : undefined
  const reason = info?.reason || t(info
    ? info.available ? 'nav.agentBackends.ready' : 'nav.agentBackends.unavailable'
    : 'nav.agentBackends.unknown')
  return {
    id,
    label,
    reason,
    title: `${label}\n${reason}`,
    color: !info ? 'var(--vxs-warning)' : info.available ? 'var(--vxs-success)' : 'var(--vxs-danger)',
  }
}))

// The rail is where the build already is, so it is where "there is a newer one" belongs.
onMounted(() => void updates.load())
// And where the search field lives — shown only once a plugin has something to answer it
// with (§6.1, §9.7); the store follows `plugin.changed` from here on.
onMounted(() => void search.load())

const sidebar = computed(() => theme.theme.sidebar)

const builtinItems: NavItem[] = [
  // Explorer and Documents sit together at the top: both are the project's files — the
  // tree of everything, then the team's own writing (§6.1). The work sections follow.
  { to: '/explorer', labelKey: 'nav.section.explorer', icon: FolderTree, tint: 'var(--vxs-nav-explorer)' },
  { to: '/documents', labelKey: 'nav.section.documents', icon: FileText, tint: 'var(--vxs-nav-documents)' },
  // The whole corpus as a graph, on a page of its own: the same documents, read by their links
  // rather than by their folders. It is a peer navigation row with the Documents tint (§6.1).
  { to: '/documents/graph', labelKey: 'nav.section.documentsGraph', icon: Waypoints, tint: 'var(--vxs-nav-documents)' },
  { to: '/chats', labelKey: 'nav.section.chats', icon: MessageCircle, tint: 'var(--vxs-nav-teams)' },
  { to: '/pipelines', labelKey: 'nav.section.pipelines', icon: Workflow, tint: 'var(--vxs-nav-automations)' },
  { to: '/automations', labelKey: 'nav.section.automations', icon: Zap, tint: 'var(--vxs-nav-automations)' },
  { to: '/tasks', labelKey: 'nav.section.tasks', icon: SquareKanban, tint: 'var(--vxs-nav-tasks)' },
  { to: '/producer', labelKey: 'producerPage.title', icon: Clapperboard, tint: 'var(--vxs-nav-tasks)' },
  { to: '/teams', labelKey: 'nav.section.teams', icon: Users, tint: 'var(--vxs-nav-teams)' },
  // Under Teams because it is about the team's work: what it has cost, over time and by
  // member, archived tasks included (§10.4).
  { to: '/statistics', labelKey: 'nav.section.statistics', icon: ChartLine, tint: 'var(--vxs-nav-statistics)' },
  // The project-shaped configuration pages sit together, after the sections that are about
  // the work itself: what the project is under, what its agents know, what they can reach.
  { to: '/vcs', labelKey: 'nav.section.vcs', icon: GitBranch, tint: 'var(--vxs-nav-vcs)' },
  // What every agent is told the project has learned (§10.12) — before Skills, which are given
  // member by member.
  { to: '/memory', labelKey: 'nav.section.memory', icon: Brain, tint: 'var(--vxs-nav-memory)' },
  // Above MCP Servers: both are what an agent is equipped with, and a skill is given member by
  // member where a server is handed to every session (§10.10).
  { to: '/skills', labelKey: 'nav.section.skills', icon: GraduationCap, tint: 'var(--vxs-nav-skills)' },
  { to: '/mcp', labelKey: 'nav.section.mcp', icon: Plug, tint: 'var(--vxs-nav-mcp)' },
  { to: '/plugins', labelKey: 'nav.section.plugins', icon: Blocks, tint: 'var(--vxs-nav-plugins)' },
  { to: '/project-settings', labelKey: 'nav.section.projectSettings', icon: FolderCog, tint: 'var(--vxs-text-muted)' },
]

/**
 * The current section's mark, as one piece that travels (§7, the motion section): the row it
 * belongs to is measured the way `SegmentedControl` measures its checked option, and where it
 * sits is published on the rows' box as `--vxs-mark-x` / `--vxs-mark-y` (level 3 of the styling
 * rules), so `base.css` can carry a single mark from the section left to the one arrived at on
 * the spring — instead of one row's mark vanishing while another's appears.
 *
 * Layout values (`offsetTop` and friends), in the frame of the positioned box around the rows,
 * which scrolls with them. Only the scrolling list is measured: Settings sits in the foot and
 * keeps the mark drawn on its own row, and while it is current the travelling mark is simply not
 * there — as on a page no row names, such as a pipeline's editor. Nothing is drawn before the
 * first measurement, so the mark never slides in from the top of the rail.
 *
 * Only a change of section *travels*. Rows also change height under the mark — a font
 * arriving, the text size knob, and the `auto` strip dropping its words as the pointer leaves,
 * which makes every row shorter at once — and there the rows jump in one frame, so a mark that
 * sprang after them would trail its own row. Those measurements land with the transition off.
 */
const route = useRoute()
const rows = ref<HTMLElement | null>(null)
const mark = ref<CSSProperties | null>(null)

function measureMark(travel = false): void {
  const current = rows.value?.querySelector<HTMLElement>('.nav-link-active')
  mark.value = current
    ? {
        '--vxs-mark-x': `${current.offsetLeft}px`,
        '--vxs-mark-y': `${Math.round(current.offsetTop + current.offsetHeight / 2)}px`,
        transition: travel ? undefined : 'none',
      }
    : null
}

// The active class follows the route in the render after it changes.
watch(
  () => route.path,
  () => void nextTick(() => measureMark(true)),
)

// A plugin's rows arriving, or the rail changing shape, moves rows without a new section.
watch(
  () => [pluginNavItems.length, sidebar.value],
  () => void nextTick(() => measureMark()),
)

let rowsObserver: ResizeObserver | null = null

onMounted(() => {
  measureMark()
  // The rows' box changes height whenever a row does.
  if (typeof ResizeObserver === 'undefined' || !rows.value) return
  rowsObserver = new ResizeObserver(() => measureMark())
  rowsObserver.observe(rows.value)
})

onBeforeUnmount(() => rowsObserver?.disconnect())

const connectionTitle = computed(() => {
  switch (session.connection) {
    case 'open':
      return t('nav.connection.live')
    case 'connecting':
      return t('nav.connection.connecting')
    default:
      return t('nav.connection.disconnected')
  }
})

/** The state itself, as the one word the footer row has room for. */
const connectionLabel = computed(() => {
  switch (session.connection) {
    case 'open':
      return t('nav.connection.state.open')
    case 'connecting':
      return t('nav.connection.state.connecting')
    default:
      return t('nav.connection.state.closed')
  }
})

const connectionColor = computed(() => {
  switch (session.connection) {
    case 'open':
      return 'var(--vxs-success)'
    case 'connecting':
      return 'var(--vxs-warning)'
    default:
      return 'var(--vxs-danger)'
  }
})

/**
 * Which build is running, next to the live indicator: those two answer the same question
 * — "is what I am looking at the thing I just changed?" — and a status light alone cannot.
 * The label is the build's tag or its commit, composed by the backend; the star is the
 * only thing added here, because a tree with uncommitted changes is not described by
 * either. Everything the label leaves out is in the tooltip.
 */
const build = computed(() => session.project?.version ?? null)

const buildLabel = computed(() => {
  if (!build.value) return ''
  return build.value.dirty ? `${build.value.display}*` : build.value.display
})

const buildTitle = computed(() => {
  const info = build.value
  if (!info) return ''
  const lines = [`${t('app.name')} ${info.display}`]
  if (info.tag) lines.push(t('nav.build.tag', { tag: info.tag }))
  if (info.commit) {
    lines.push(
      info.branch
        ? t('nav.build.commitOnBranch', { commit: info.commit, branch: info.branch })
        : t('nav.build.commit', { commit: info.commit }),
    )
  } else {
    lines.push(t('nav.build.noCommit'))
  }
  if (info.committed_at) {
    lines.push(t('nav.build.committed', { time: formatTime(info.committed_at) }))
  }
  if (info.dirty) lines.push(t('nav.build.dirty'))
  return lines.join('\n')
})

/**
 * Desktop keeps the original update entry reachable for manual checks (§12.1).
 * Source checkouts offer it when a release is available or an update is running.
 */
const showUpdate = computed(() => updates.desktop || updates.available || updates.running)

const updateLabel = computed(() =>
  updates.running ? t('nav.update.updating') : updates.available ? updates.label : t('nav.update.label'),
)

const updateTitle = computed(() => {
  if (updates.running) return t('nav.update.updatingTitle')
  if (updates.desktop && !updates.available) return t('settings.updates.checkNow')
  const info = updates.status
  const lines = [t('nav.update.available', { version: info?.latest ?? '' }).trim()]
  if (info?.current) lines.push(t('nav.update.current', { version: info.current }))
  if (info?.upstream) lines.push(t('nav.update.upstream', { upstream: info.upstream }))
  if (info?.dirty) lines.push(t('nav.update.dirty'))
  lines.push(t('nav.update.click'))
  return lines.join('\n')
})

/** In the strip the words are gone, so the tooltip has to say everything the row did. */
const footTitle = computed(() =>
  [connectionTitle.value, buildTitle.value].filter(Boolean).join('\n\n'),
)

/**
 * Which system the project is under, and whether its tool answers (§9.6). The session's
 * answer is what `/api/project` said at load; the vcs store's overview supersedes it once
 * the Version Control page has read or changed the backend, because that page is the one
 * place the answer can change without a reload. The position comes from whichever of the
 * two reads has happened, so the tooltip names the branch without a request of its own.
 */
const vcsLine = computed(() =>
  vcsIndicator(vcs.overview ? vcs.overview.active : session.project?.vcs, {
    branch: vcs.status?.branch ?? vcs.overview?.branch,
    revision: vcs.status?.revision ?? vcs.overview?.revision,
  }),
)
</script>

<template>
  <!-- In `auto` the rail's slot in the layout stays 48px wide and the rail itself is
       positioned over the page, so widening it moves nothing else. -->
  <div class="relative shrink-0" :class="sidebar === 'auto' ? 'w-12' : 'w-rail'">
    <nav
      class="vxs-pane vxs-rail rail flex h-full pt-2 flex-col border-r border-pane bg-surface"
      :class="`rail-${sidebar}`"
    >
      <!-- The search field, under the project it searches. A button drawn as a field rather
           than an input: the rail is 48px wide in two of its three shapes, and the panel it
           opens carries the real input at any width. -->
      <button
        v-if="search.available"
        class="rail-search mx-2 mb-1 flex items-center gap-2 rounded-md border border-line bg-inset px-2 py-1.5 text-left text-muted hover:border-accent hover:text-primary"
        :title="t('nav.search.title')"
        @click="search.show()"
      >
        <Search class="size-4 shrink-0" />
        <span class="rail-label truncate">{{ t('nav.search.label') }}</span>
        <kbd class="rail-label ml-auto shrink-0 font-mono text-[10px] text-faint">Ctrl K</kbd>
      </button>

      <div class="rail-scroll flex-1 overflow-x-hidden overflow-y-auto px-2">
        <!-- One positioned box around the rows: the frame the travelling mark is measured and
             drawn in (`measureMark`), and it scrolls with them. -->
        <div ref="rows" class="rail-rows relative" :data-mark="mark ? '' : undefined">
          <RouterLink
            v-for="item in builtinItems"
            :key="item.to"
            :to="item.to"
            class="nav-link"
            active-class="nav-link-active"
            :title="t(item.labelKey)"
          >
            <component :is="item.icon" class="size-4 shrink-0" :style="{ color: item.tint }" />
            <span class="rail-label truncate">{{ t(item.labelKey) }}</span>
          </RouterLink>

          <template v-if="pluginNavItems.length">
            <div class="rail-rule my-2" />
            <p class="rail-label px-2 pb-1 text-small tracking-wide text-faint uppercase">
              {{ t('nav.pluginPages') }}
            </p>
            <RouterLink
              v-for="item in pluginNavItems"
              :key="item.route"
              :to="item.route"
              class="nav-link"
              active-class="nav-link-active"
              :title="item.title"
            >
              <Blocks class="size-4 shrink-0" style="color: var(--vxs-nav-plugin-page)" />
              <span class="rail-label truncate">{{ item.title }}</span>
            </RouterLink>
          </template>

          <span v-if="mark" class="rail-mark" :style="mark" aria-hidden="true" />
        </div>
      </div>

      <div class="rail-footer p-2">
        <!-- The original update position; desktop also uses it for manual checks. -->
        <button
          v-if="showUpdate"
          class="nav-link nav-link-update w-full"
          :title="updateTitle"
          :disabled="updates.busy"
          @click="updates.running ? (updates.dialogOpen = true) : updates.startFromButton()"
        >
          <Loader2 v-if="updates.running" class="size-4 shrink-0 animate-spin" />
          <ArrowUpCircle v-else class="size-4 shrink-0" />
          <span class="rail-label truncate">{{ updateLabel }}</span>
        </button>
        <!-- Between the update button and Settings, because the three are one thought: is
             there a newer Studio, what came with this one, and where its knobs are. Unlike
             the button above it this is always here — a build with nothing to update to
             still has a history worth reading. -->
        <button
          class="nav-link w-full"
          :title="t('nav.changelog.title')"
          @click="showingChangelog = true"
        >
          <ScrollText class="size-4 shrink-0" />
          <span class="rail-label truncate">{{ t('nav.changelog.label') }}</span>
        </button>
        <RouterLink
          to="/settings"
          class="nav-link"
          active-class="nav-link-active"
          :title="t('nav.section.settings')"
        >
          <Settings class="size-4 shrink-0" />
          <span class="rail-label">{{ t('nav.section.settings') }}</span>
        </RouterLink>
        <!-- Last, under Settings: who makes the Studio and on what terms. -->
        <button class="nav-link w-full" :title="t('nav.about.title')" @click="showingAbout = true">
          <Info class="size-4 shrink-0" />
          <span class="rail-label truncate">{{ t('nav.about.label') }}</span>
        </button>
        <div class="rail-status">
          <div
            v-for="backend in agentBackends"
            :key="backend.id"
            class="rail-foot flex items-center gap-1.5"
            :title="backend.title"
          >
            <AgentBackendIcon :backend="backend.id" class="size-3 shrink-0" :style="{ color: backend.color }" />
            <span class="rail-label truncate text-small">{{ backend.label }}</span>
            <span class="sr-only">{{ backend.reason }}</span>
          </div>
          <!-- Which version control the project is under, in the same shape as the row below
               it: a dot for the state, a word for the system. It links to the page that can
               change either, and its tooltip carries what the strip hides. -->
          <RouterLink to="/vcs" class="rail-foot flex items-center gap-1.5" :title="vcsLine.title">
            <span class="size-2 shrink-0 rounded-full" :style="{ background: vcsLine.color }" />
            <span class="rail-label truncate text-small">{{ t('nav.vcs', { name: vcsLine.label }) }}</span>
          </RouterLink>
          <div class="rail-foot flex items-center gap-1.5" :title="footTitle">
            <!-- The one dot in the rail that breathes, and only while events are arriving: a
                 connection that is retrying is not alive, and its colour already says so. -->
            <span
              class="rail-live size-2 shrink-0 rounded-full"
              :class="{ 'vxs-dot-live': session.connection === 'open' }"
              :style="{ background: connectionColor }"
            />
            <!-- The rail is 192px wide and "connecting" plus a version does not fit in it.
                 The word is the one that gives way: it is transient, its tooltip spells it
                 out, and the dot beside it already carries the same state in colour. -->
            <span class="rail-label truncate text-small">{{ connectionLabel }}</span>
            <!-- A commit is always seven characters; a tag is whatever someone typed, so it
                 is capped rather than allowed to push the row past the rail's width. -->
            <span
              v-if="buildLabel"
              class="rail-build rail-label ml-auto max-w-28 shrink-0 truncate font-mono text-[10px]"
              >{{ buildLabel }}</span
            >
          </div>
        </div>
      </div>
    </nav>
  </div>

  <ChangelogDialog v-if="showingChangelog" @close="showingChangelog = false" />
  <AboutDialog v-if="showingAbout" @close="showingAbout = false" />
  <UpdateDialog v-if="updates.dialogOpen" @close="updates.dialogOpen = false" />
</template>

<style scoped>
/* The project switcher is a raised tile rather than a row: it names what everything under
   it is scoped to, and the tile is what says it is a control and not a heading. Its mark sits
   in a well of the canvas grey. In the 48px strip the rules further down centre it and take
   its side padding, and the tile is then just the well. */
.rail-project {
  margin: 12px 8px 6px;
  padding: 7px 8px;
  border-radius: var(--vxs-radius-md);
  background: var(--vxs-bg-hover);
  box-shadow: inset 0 0 0 1px var(--vxs-accent-border);
}

.rail-project:hover {
  background: var(--vxs-bg-active);
}

.rail-project > svg:first-child {
  width: 26px;
  height: 26px;
  padding: 5px;
  border-radius: 6px;
  background: var(--vxs-bg-canvas);
  box-shadow: inset 0 0 0 1px var(--vxs-accent-border);
}

.nav-link {
  display: flex;
  align-items: center;
  gap: var(--vxs-space-2);
  padding: 7px var(--vxs-row-pad-x);
  border-radius: var(--vxs-radius-md);
  color: var(--vxs-text-muted);
}

.nav-link:hover {
  background: var(--vxs-bg-hover);
  color: var(--vxs-text-primary);
}

/* The current section: a grey pill on its own edge with a mark down its leading edge. The
   section's icon carries the colour and the mark carries the position, so the words stay
   plain — the row used to be named by accent-coloured lettering instead. */
.nav-link-active,
.nav-link-active:hover {
  position: relative;
  background: var(--vxs-accent-container);
  box-shadow: inset 0 0 0 1px var(--vxs-accent-border);
  color: var(--vxs-text-on-accent-container);
}

.nav-link-active::before,
.rail-mark {
  content: '';
  position: absolute;
  width: 2px;
  height: 16px;
  margin-top: -8px;
  border-radius: 2px;
  background: var(--vxs-text-secondary);
}

.nav-link-active::before {
  left: 0;
  top: 50%;
}

/* The list's mark travels (`measureMark`, and its move in `base.css`), so once it has been
   measured the current row there stops drawing its own. Settings, in the foot, keeps its own
   mark, and so does every row before the first measurement. Placed with `left` and `translate`
   from the row it belongs to — only the vertical part changes between rows, so only that part
   moves. After the rows in the markup and above them, or the current row's pill would paint
   over it. */
.rail-rows[data-mark] .nav-link-active::before {
  content: none;
}

.rail-mark {
  top: 0;
  left: var(--vxs-mark-x);
  z-index: 1;
  translate: 0 var(--vxs-mark-y);
  pointer-events: none;
}

/* The list says when there is more of it. With a few plugin pages installed the rows no longer
   fit a laptop's height, and the list simply stopped mid-row above the foot — which read as
   the last row being cut off rather than as a list that scrolls. Its edge fades instead, and
   only the edge there is more beyond: a scroll timeline of the list itself moves two lengths,
   so at the top only the bottom fades, at the end only the top, and a list that fits has no
   timeline, keeps both at zero and is not masked at all. The mask's black and transparent are
   coverage, not paint. Chromium's alone for now; elsewhere the list is as it was. */
@property --vxs-rail-fade-top {
  syntax: '<length>';
  inherits: false;
  initial-value: 0px;
}

@property --vxs-rail-fade-bottom {
  syntax: '<length>';
  inherits: false;
  initial-value: 0px;
}

@supports (animation-timeline: scroll()) {
  .rail-scroll {
    mask-image: linear-gradient(
      to bottom,
      transparent,
      black var(--vxs-rail-fade-top),
      black calc(100% - var(--vxs-rail-fade-bottom)),
      transparent
    );
    animation: rail-fade linear both;
    animation-timeline: scroll(self);
  }
}

@keyframes rail-fade {
  0% {
    --vxs-rail-fade-top: 0px;
    --vxs-rail-fade-bottom: 24px;
  }
  12% {
    --vxs-rail-fade-top: 24px;
  }
  88% {
    --vxs-rail-fade-bottom: 24px;
  }
  100% {
    --vxs-rail-fade-top: 24px;
    --vxs-rail-fade-bottom: 0px;
  }
}

/* "There is a newer Studio" is the one announcement in the rail: a lifted row whose icon
   takes the green of news, because it is news and not a place. The words stay plain, like
   every other row's — the icon already says it is not a section. */
.nav-link-update,
.nav-link-update:hover {
  background: var(--vxs-bg-hover);
  color: var(--vxs-text-secondary);
}

.nav-link-update > svg {
  color: var(--vxs-nav-update);
}

/* Rules fade out at both ends instead of stopping: they separate groups of rows, and a hard
   line across the rail read as an edge of the rail itself. */
.rail-rule,
.rail-footer::before {
  height: 1px;
  background: linear-gradient(
    90deg,
    transparent,
    color-mix(in srgb, var(--vxs-text-primary) 16%, transparent) 30%,
    color-mix(in srgb, var(--vxs-text-primary) 16%, transparent) 70%,
    transparent
  );
}

.rail-footer::before {
  content: '';
  display: block;
  margin-bottom: 8px;
}

/* Agent readiness, version control and the live connection with the build share one
   inset status card at the foot of the rail (§6.1). */
.rail-status {
  margin-top: 8px;
  padding: 8px;
  border-radius: var(--vxs-radius-md);
  background: var(--vxs-bg-canvas);
  box-shadow: inset 0 0 0 1px var(--vxs-border);
}

.rail-status .rail-foot {
  padding: 6px 0 0;
  color: var(--vxs-text-secondary);
}

.rail-status .rail-foot:first-child {
  padding-top: 0;
}

.rail-status a.rail-foot:hover {
  color: var(--vxs-text-primary);
}

.rail-build {
  color: var(--vxs-text-faint);
}

/* A connection is ambient where a run is urgent, so the live dot breathes slower than work in
   flight does. */
.rail-live.vxs-dot-live {
  animation-duration: 2s;
}

/* Icons only: the strip keeps every control and drops the words; `title` carries them. */
.rail-icons .rail-label,
.rail-auto:not(:hover) .rail-label {
  display: none;
}

.rail-icons .nav-link,
.rail-icons .rail-project,
.rail-icons .rail-search,
.rail-icons .rail-foot,
.rail-auto:not(:hover) .nav-link,
.rail-auto:not(:hover) .rail-project,
.rail-auto:not(:hover) .rail-search,
.rail-auto:not(:hover) .rail-foot {
  justify-content: center;
  padding-left: 0;
  padding-right: 0;
}

/* Auto-collapse: the strip widens over the page while the pointer is on it. */
.rail-auto {
  position: absolute;
  inset: 0 auto 0 0;
  width: 48px;
  z-index: 30;
  overflow: hidden;
  transition:
    width var(--vxs-motion-fast) var(--vxs-ease-out),
    box-shadow var(--vxs-motion-fast) var(--vxs-ease-out);
}

.rail-auto:hover {
  width: var(--vxs-rail-width);
  box-shadow: var(--vxs-shadow-lg);
}
</style>
