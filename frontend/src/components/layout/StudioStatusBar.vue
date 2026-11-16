<script setup lang="ts">
/** Project-local readiness and connection indicators (§6.1), shared across routes. */
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import AgentBackendIcon from '@/components/ui/AgentBackendIcon.vue'
import { t } from '@/i18n'
import { formatTime } from '@/lib/status'
import { useSessionStore } from '@/stores/session'
import { useVcsStore, vcsIndicator } from '@/stores/vcs'

const session = useSessionStore()
const vcs = useVcsStore()
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
      {{ backend.label }}<span class="sr-only">{{ backend.reason }}</span>
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
.connection { margin-inline-start: auto; }
.connection .vxs-dot-live { animation-duration: 2s; }
@media (max-width: 600px) {
  .studio-statusbar { column-gap: 12px; }
  .connection { margin-inline-start: 0; }
}
</style>
