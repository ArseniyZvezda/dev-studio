<script setup lang="ts">
/**
 * The task dialog's right-hand column (§10.4): what the session is equipped with and what it
 * has done, beside the transcript a reviewer reads with it.
 *
 * Sub-agents first — how many are running and how the rest ended — then the skills and MCP
 * servers the session was handed, then the files it touched (the default slot, `TaskChanges`,
 * which keeps its own scroller and commit draft under the rest). The lists are folded from
 * the transcript (`lib/session-status.ts`), so they read the same for Claude Code, Codex and
 * Qwen Code, and follow a live run over the socket like the transcript does.
 */
import { GraduationCap, Plug } from 'lucide-vue-next'
import { computed } from 'vue'

import SubagentSummary from '@/components/teams/SubagentSummary.vue'
import { t, type MessageKey } from '@/i18n'
import { sessionMcpServers, sessionSkills, type SessionMcpServer, type SkillState } from '@/lib/session-status'
import type { Subagent } from '@/lib/subagents'
import type { TranscriptMessage } from '@/lib/transcript'

const props = defineProps<{ messages: TranscriptMessage[]; subagents: Subagent[] }>()

const skills = computed(() => sessionSkills(props.messages))
const servers = computed(() => sessionMcpServers(props.messages))

const SKILL_STATES: Record<SkillState, MessageKey> = {
  used: 'taskDetail.status.skill.used',
  loaded: 'taskDetail.status.skill.loaded',
  offered: 'taskDetail.status.skill.offered',
  missing: 'taskDetail.status.skill.missing',
}

const SKILL_HINTS: Record<SkillState, MessageKey> = {
  used: 'taskDetail.status.skill.usedHint',
  loaded: 'taskDetail.status.skill.loadedHint',
  offered: 'taskDetail.status.skill.offeredHint',
  missing: 'taskDetail.status.skill.missingHint',
}

/** The runtime's words for a server's state; one this build does not know is shown as sent. */
const SERVER_STATES: Record<string, MessageKey> = {
  connected: 'taskDetail.status.mcp.connected',
  failed: 'taskDetail.status.mcp.failed',
  pending: 'taskDetail.status.mcp.pending',
  'needs-auth': 'taskDetail.status.mcp.needsAuth',
  disabled: 'taskDetail.status.mcp.disabled',
}

function serverState(server: SessionMcpServer): string {
  if (!server.status) return t('taskDetail.status.mcp.configured')
  const key = SERVER_STATES[server.status]
  return key ? t(key) : server.status
}

function serverDot(server: SessionMcpServer): string {
  if (server.status === 'connected') return 'bg-success'
  if (server.status === 'failed') return 'bg-danger'
  if (server.status) return 'bg-warning'
  // No state reported (Codex): a server that answered calls is evidently up.
  return server.calls ? 'bg-success' : 'bg-line-strong'
}
</script>

<template>
  <section class="flex h-full min-h-0 w-full flex-col bg-canvas" data-session-status>
    <header class="shrink-0 border-b border-line px-3 py-2">
      <span class="truncate text-small font-semibold tracking-wide text-faint uppercase">
        {{ t('taskDetail.status.title') }}
      </span>
    </header>

    <!-- Capped and scrolling on its own: the files below are the list a reviewer works
         through, and twenty skills must not push them out of the column. -->
    <div class="max-h-[45%] shrink-0 space-y-3 overflow-y-auto border-b border-line px-3 py-2">
      <SubagentSummary :entries="subagents" />

      <div>
        <p class="flex items-baseline gap-1.5 text-small font-medium text-faint">
          <GraduationCap class="size-3 shrink-0 translate-y-0.5" :style="{ color: 'var(--vxs-nav-skills)' }" />
          <span>{{ t('taskDetail.status.skills') }}</span>
          <span class="font-mono">{{ skills.length }}</span>
        </p>
        <p v-if="!skills.length" class="pl-4.5 text-small text-faint">{{ t('taskDetail.status.noSkills') }}</p>
        <p
          v-for="skill in skills"
          :key="skill.name"
          class="flex items-baseline gap-2 py-0.5 pl-4.5"
          :title="skill.name === skill.label ? t(SKILL_HINTS[skill.state]) : `${skill.name} · ${t(SKILL_HINTS[skill.state])}`"
        >
          <span
            class="min-w-0 flex-1 truncate"
            :class="skill.state === 'missing' ? 'text-faint line-through' : skill.state === 'offered' ? 'text-muted' : ''"
          >
            {{ skill.label }}
          </span>
          <span
            class="shrink-0 text-small"
            :class="skill.state === 'used' ? 'text-success' : skill.state === 'missing' ? 'text-warning' : 'text-faint'"
          >
            {{ t(SKILL_STATES[skill.state]) }}
          </span>
        </p>
      </div>

      <div>
        <p class="flex items-baseline gap-1.5 text-small font-medium text-faint">
          <Plug class="size-3 shrink-0 translate-y-0.5" />
          <span>{{ t('taskDetail.status.mcp.title') }}</span>
          <span class="font-mono">{{ servers.length }}</span>
        </p>
        <p v-if="!servers.length" class="pl-4.5 text-small text-faint">{{ t('taskDetail.status.noMcp') }}</p>
        <p
          v-for="server in servers"
          :key="server.name"
          class="flex items-center gap-2 py-0.5 pl-4.5"
          :title="server.calls ? t('taskDetail.status.mcp.calls', { count: server.calls }) : serverState(server)"
        >
          <span class="size-1.5 shrink-0 rounded-full" :class="serverDot(server)" />
          <span class="min-w-0 flex-1 truncate font-mono text-small">{{ server.name }}</span>
          <span v-if="server.calls" class="shrink-0 font-mono text-small text-faint">{{ server.calls }}</span>
          <span
            class="shrink-0 text-small"
            :class="server.status === 'failed' ? 'text-danger' : 'text-faint'"
          >
            {{ serverState(server) }}
          </span>
        </p>
      </div>
    </div>

    <div class="min-h-0 flex-1">
      <slot />
    </div>
  </section>
</template>
