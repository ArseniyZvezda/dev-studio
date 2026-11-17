<script setup lang="ts">
/** Project-local readiness and connection indicators (§6.1), shared across routes. */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AgentBackendIcon from '@/components/ui/AgentBackendIcon.vue'
import { t } from '@/i18n'
import { formatTime } from '@/lib/status'
import { useSessionStore } from '@/stores/session'
import { useVcsStore, vcsIndicator } from '@/stores/vcs'
import { apiConnections } from '@/lib/api-backends'

const session = useSessionStore()
const vcs = useVcsStore()
const now = ref(Date.now())
let statusTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  statusTimer = setInterval(() => {
    now.value = Date.now()
    // The project menu still uses the current project's task counts (§5.1).
    void session.refreshTaskCounts()
    if (session.connection === 'open') void session.refreshStatus()
  }, 15_000)
})
onBeforeUnmount(() => clearInterval(statusTimer))
watch(() => session.connection, (connection) => {
  if (connection === 'open') void session.refreshStatus()
}, { immediate: true })

/** §6.1: compact quota countdown, independent of status-request success. */
function resetCountdown(reset: number | null): string | null {
  if (typeof reset !== 'number' || !Number.isFinite(reset) || reset * 1000 <= now.value) return null
  const minutes = Math.floor((reset * 1000 - now.value) / 60_000)
  if (minutes >= 1440) return t('nav.agentBackends.resetDays', {
    days: Math.floor(minutes / 1440), hours: Math.floor(minutes % 1440 / 60),
  })
  if (minutes >= 60) return t('nav.agentBackends.resetHours', {
    hours: Math.floor(minutes / 60), minutes: minutes % 60,
  })
  return minutes > 0 ? t('nav.agentBackends.resetMinutes', { minutes }) : t('nav.agentBackends.resetSoon')
}

// §6.1: the backend's monochrome mark carries readiness; the whole row explains it.
const agentBackends = computed(() => ([...(['claude_code', 'codex', 'qwen'] as const),
  ...apiConnections.value.map(c => `openai_api:${c.id}`),
]).map((id) => {
  const label = id === 'claude_code' || id === 'codex' || id === 'qwen'
    ? t(`nav.agentBackends.${id}`)
    : apiConnections.value.find(c => `openai_api:${c.id}` === id)?.name || id
  const info = session.connection === 'open' ? session.status?.agent_backends[id] : undefined
  const reason = info?.reason || t(info
    ? info.available ? 'nav.agentBackends.ready' : 'nav.agentBackends.unavailable'
    : 'nav.agentBackends.unknown')
  // §6.1: unknown quotas are not zero; only recent provider telemetry is shown.
  const limits = (info?.usage_limits ?? []).filter(limit =>
    Number.isFinite(limit.used_percent) && limit.used_percent >= 0 && limit.used_percent <= 100
    && !(typeof limit.resets_at === 'number' && Number.isFinite(limit.resets_at) && limit.resets_at >= 0 && limit.resets_at * 1000 <= now.value),
  )
  const remainingPercent = (used: number) => `${Math.round(100 - used)}%`
  const windowLabels: Record<string, string> = {
    primary: t('nav.agentBackends.windows.primary'),
    secondary: t('nav.agentBackends.windows.secondary'),
    five_hour: t('nav.agentBackends.windows.five_hour'),
    seven_day: t('nav.agentBackends.windows.seven_day'),
    seven_day_opus: t('nav.agentBackends.windows.seven_day_opus'),
    seven_day_sonnet: t('nav.agentBackends.windows.seven_day_sonnet'),
    overage: t('nav.agentBackends.windows.overage'),
  }
  const usage = limits.length ? limits.map(limit => remainingPercent(limit.used_percent)).join(' / ') : '—'
  const usageTitle = limits.length
    ? limits.map(limit => {
      const remaining = t('nav.agentBackends.limitRemaining', {
        window: windowLabels[limit.window] ?? limit.window,
        percent: remainingPercent(limit.used_percent),
      })
      const countdown = resetCountdown(limit.resets_at)
      return countdown ? `${remaining}\n${t('nav.agentBackends.resetsIn', { time: countdown })}` : remaining
    }).join('\n')
    : t('nav.agentBackends.limitsUnknown')
  return {
    id,
    label,
    reason,
    usage,
    usageTitle,
    title: `${label}\n${reason}\n${usageTitle}`,
    color: !info ? 'var(--vxs-warning)' : info.available ? 'var(--vxs-success)' : 'var(--vxs-danger)',
  }
}))

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

/** Connection and build details share a tooltip. */
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
  <footer class="studio-statusbar text-small text-secondary" :aria-label="t('nav.statusBar')">
    <div v-for="backend in agentBackends" :key="backend.id" class="status-item" :title="backend.title">
      <AgentBackendIcon :backend="backend.id" class="size-3 shrink-0" :style="{ color: backend.color }" />
      {{ backend.label }}<span class="backend-usage text-faint" :aria-label="backend.usageTitle">{{ backend.usage }}</span>
      <span class="sr-only">{{ backend.reason }}</span>
    </div>
    <RouterLink to="/vcs" class="status-item hover:text-primary" :title="vcsLine.title">
      <span class="size-2 rounded-full" :style="{ background: vcsLine.color }" />
      {{ t('nav.vcs', { name: vcsLine.label }) }}
    </RouterLink>
    <div class="status-item connection" :title="footTitle">
      <span class="size-2 rounded-full" :class="{ 'vxs-dot-live': session.connection === 'open' }" :style="{ background: connectionColor }" />
      {{ connectionLabel }}
      <span v-if="buildLabel" class="max-w-28 truncate font-mono text-[10px] text-faint">{{ buildLabel }}</span>
    </div>
  </footer>
</template>

<style scoped>
.studio-statusbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 3px 20px;
  min-height: 28px;
  padding: 4px 12px;
}
.status-item { display: flex; align-items: center; gap: 6px; white-space: nowrap; }
.backend-usage { font-variant-numeric: tabular-nums; }
.connection { margin-inline-start: auto; }
.connection .vxs-dot-live { animation-duration: 2s; }
@media (max-width: 600px) {
  .studio-statusbar { column-gap: 12px; }
  .connection { margin-inline-start: 0; }
}
</style>
