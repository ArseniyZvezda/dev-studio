<script setup lang="ts">
/**
 * One member on the org canvas.
 *
 * Four handles, and the pairing is what carries the meaning: the top and bottom ones are
 * the **management vertical** (`manager_id`), the side ones are a **sideways route**
 * (`can_assign_to`, §10.4). Drawing a connection is the edit — that is why the handles are
 * labelled and why the two kinds cannot be joined to each other.
 *
 * `data.editing` is the chart's lock. The handles and the settings button are the affordances
 * that *change* the team, so they are hidden while it is on rather than merely inert — an
 * offered handle that refuses to connect is worse than no handle. They are hidden, not
 * removed: an edge needs its handle mounted to have something to anchor to.
 *
 * Buttons carry `nodrag` so pressing one does not start a canvas drag instead.
 *
 * An agent's skills (§10.10) are a row of their own under the name: what the member has been
 * equipped with is read off the chart as often as who they report to. Read from
 * `data.member`, which the chart's watcher repaints, so giving a skill in the rail shows here
 * without rebuilding the node; the skills store only decides whether a name is still a skill.
 */
import { GraduationCap, SlidersHorizontal } from 'lucide-vue-next'
import { Handle, Position } from '@vue-flow/core'
import { computed } from 'vue'

import type { TeamMember } from '@/api/types'
import MemberMark from '@/components/teams/MemberMark.vue'
import { t } from '@/i18n'
import { skillChips } from '@/lib/skills'
import { statusColor } from '@/lib/status'
import { useSkillsStore } from '@/stores/skills'

const props = defineProps<{
  data: {
    member: TeamMember
    /** Painted on by the chart's watcher, so a task count never rebuilds the node. */
    active: number
    review: number
    waiting: number
    routesToAnyone: boolean
    /** The chart's lock, painted on by its watcher so toggling it never rebuilds a node. */
    editing: boolean
    onConfigure: (memberId: string) => void
  }
  selected?: boolean
}>()

const member = computed(() => props.data.member)
const isHuman = computed(() => member.value.kind === 'human')
const avatar = computed(() => member.value.avatar_color ?? 'var(--vxs-accent)')

/** Message keys rather than words, resolved in the template, so the chips follow the language. */
const badges = computed(() =>
  (
    [
      {
        count: props.data.waiting,
        status: 'pending',
        label: 'teams.node.inbox',
        hint: 'teams.node.inboxHint',
      },
      {
        count: props.data.active,
        status: 'in_progress',
        label: 'teams.node.active',
        hint: 'teams.node.activeHint',
      },
      {
        count: props.data.review,
        status: 'needs_review',
        label: 'teams.node.review',
        hint: 'teams.node.reviewHint',
      },
    ] as const
  ).filter((badge) => badge.count > 0),
)

const skillsStore = useSkillsStore()

/** How many skills fit on a 220px card before the rest become "+N"; the tooltip names them all. */
const SHOWN_SKILLS = 3

/** A person has no session to tell, so a list kept from when they were an agent is not drawn. */
const skills = computed(() =>
  isHuman.value ? [] : skillChips(member.value.skills ?? [], skillsStore.skills),
)
const shownSkills = computed(() => skills.value.slice(0, SHOWN_SKILLS))
const skillsTitle = computed(() =>
  skills.value
    .map((chip) =>
      chip.missing
        ? t('teams.node.skillMissing', { name: chip.name })
        : chip.skill?.description
          ? `${chip.name} — ${chip.skill.description}`
          : chip.name,
    )
    .join('\n'),
)
</script>

<template>
  <div
    class="member"
    :class="{ 'member-selected': selected, 'member-locked': !data.editing }"
    :style="{ '--member-accent': avatar }"
  >
    <Handle
      id="manager"
      type="target"
      :position="Position.Top"
      :title="t('teams.node.handleManager')"
    />
    <Handle
      id="route-in"
      type="target"
      :position="Position.Left"
      class="route-handle"
      :title="t('teams.node.handleRouteIn')"
    />

    <div class="flex items-center gap-2 px-2 py-1.5">
      <!-- The member's icon where they have chosen one, their initials where they have not
           — one rule, drawn here and on every card by `MemberMark`. -->
      <MemberMark :member="member" variant="badge" />

      <span class="min-w-0 flex-1">
        <span class="block truncate font-medium">{{ member.name }}</span>
        <span class="block truncate text-small text-faint">
          {{ member.role_title || (isHuman ? t('teams.kind.person') : t('teams.kind.agent')) }}
        </span>
      </span>

      <button
        v-if="data.editing"
        class="nodrag grid size-6 shrink-0 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
        :title="t('teams.node.settingsFor', { name: member.name })"
        @click.stop="data.onConfigure(member.id)"
      >
        <SlidersHorizontal class="size-3.5" />
      </button>
    </div>

    <!-- Only when there are any, like the task load below: an agent with no skills is the
         default, and a row saying so on every card would be noise. -->
    <!-- Wrapped rather than cut: a skill's name is the whole of what the chip says, and two
         names sharing one line of a 220px card were each cut down to a few letters. -->
    <div
      v-if="skills.length"
      class="flex items-start gap-1.5 border-t border-line-subtle px-2 py-1"
      :title="skillsTitle"
    >
      <GraduationCap class="mt-1 size-3 shrink-0" style="color: var(--vxs-nav-skills)" />
      <span class="flex min-w-0 flex-wrap items-center gap-1">
        <span
          v-for="chip in shownSkills"
          :key="chip.name"
          class="max-w-full truncate rounded-sm bg-inset px-1.5 py-0.5 font-mono text-small"
          :class="chip.missing ? 'text-warning line-through' : 'text-muted'"
        >
          {{ chip.name }}
        </span>
        <span v-if="skills.length > SHOWN_SKILLS" class="text-small text-faint">
          +{{ skills.length - SHOWN_SKILLS }}
        </span>
      </span>
    </div>

    <!-- Task load only, and only when there is any: assigning work happens on the Tasks
         page, so this row is read-only context and an empty bar is just a bar. -->
    <div
      v-if="badges.length || data.routesToAnyone"
      class="flex items-center gap-1 border-t border-line-subtle px-2 py-1"
    >
      <span
        v-for="badge in badges"
        :key="badge.label"
        class="shrink-0 rounded-sm px-1.5 py-0.5 text-small"
        :style="{
          color: statusColor(badge.status),
          background: `color-mix(in oklab, ${statusColor(badge.status)} 14%, transparent)`,
        }"
        :title="t(badge.hint, { count: badge.count })"
      >
        {{ t(badge.label, { count: badge.count }) }}
      </span>
      <span
        v-if="data.routesToAnyone"
        class="shrink-0 rounded-sm border border-dashed border-line px-1.5 py-0.5 text-small text-faint"
        :title="t('teams.node.anyoneTitle')"
      >
        {{ t('teams.node.anyone') }}
      </span>
    </div>

    <Handle
      id="reports"
      type="source"
      :position="Position.Bottom"
      :title="t('teams.node.handleReports')"
    />
    <Handle
      id="route-out"
      type="source"
      :position="Position.Right"
      class="route-handle"
      :title="t('teams.node.handleRouteOut')"
    />
  </div>
</template>

<style scoped>
.member {
  width: 220px;
  border: 2px solid var(--vxs-border);
  border-left: 3px solid var(--member-accent);
  border-radius: var(--vxs-radius-lg);
  background: var(--vxs-bg-surface);
  box-shadow: var(--vxs-shadow-md);
  overflow: hidden;
}

.member-selected {
  border-color: var(--vxs-accent);
}

/* Locked: no handles to aim at, and the card does not claim to be draggable. Hidden rather
   than unmounted so the edges keep their anchor points. */
/* `visibility`, not `opacity`: it keeps the layout box Vue Flow measures to anchor an edge,
   while taking the handle out of the accessibility tree — a locked chart should not offer
   "drop a manager's line here" to a screen reader either. */
.member-locked :deep(.vue-flow__handle) {
  visibility: hidden;
  pointer-events: none;
}

.member-locked {
  cursor: default;
}

/* Sideways routes are a different relationship, so their handles read differently from
   the vertical ones — square and hollow against the vertical's filled dot. */
.route-handle {
  border-radius: 2px;
  background: var(--vxs-bg-surface);
  border: 1px solid var(--vxs-accent);
}
</style>
