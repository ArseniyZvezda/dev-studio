<script setup lang="ts">
/**
 * How a team member is drawn wherever their name appears (§10.2).
 *
 * One component rather than a rule copied into six templates: the org chart's node, the
 * member editor's header, a board card, the task dialog, the assignee picker and the
 * statistics rows all have to agree about what a member looks like, or picking an icon
 * would change some of them and not others.
 *
 * Two shapes, because there are two jobs. **`badge`** is the avatar — a filled square (an
 * agent) or circle (a person) in the member's colour, carrying their icon or, with none
 * chosen, their initials. **`inline`** is the mark that sits in front of a name in a line of
 * text: the icon in the member's colour, or — for a member with no icon — the plain dot every
 * card drew before this existed. Both occupy the same box whichever way they resolve, so a
 * roster where only half the members have picked an icon still lines up.
 */
import { Bot, User } from 'lucide-vue-next'
import { computed } from 'vue'

import { t } from '@/i18n'
import { memberIcon, type MemberLook } from '@/lib/member-icons'

const props = withDefaults(
  defineProps<{
    /** Null renders nothing at all — an assignee the roster has lost still has a name. */
    member?: MemberLook | null
    variant?: 'badge' | 'inline'
    /** `badge` only: the small Bot/User chip in the corner, saying which kind this is. */
    corner?: boolean
  }>(),
  { member: null, variant: 'inline', corner: true },
)

const isHuman = computed(() => props.member?.kind === 'human')
const color = computed(() => props.member?.avatar_color ?? 'var(--vxs-accent)')
const icon = computed(() => memberIcon(props.member?.avatar_icon))

/** Up to two initials, for a member who has chosen no icon. The badge's oldest fallback. */
const initials = computed(() =>
  (props.member?.name ?? '')
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join(''),
)

/** What the mark says when hovered — the icon's discipline, or the kind of member. */
const hint = computed(() => {
  const kind = isHuman.value ? t('teams.mark.person') : t('teams.kind.aiAgent')
  return icon.value ? `${icon.value.label} · ${kind}` : kind
})
</script>

<template>
  <span v-if="member && variant === 'badge'" class="relative shrink-0" :title="hint">
    <span
      class="grid size-7 place-items-center text-small font-semibold text-on-accent"
      :class="isHuman ? 'rounded-full' : 'rounded-md'"
      :style="{ background: color }"
    >
      <component :is="icon.component" v-if="icon" class="size-4" />
      <template v-else>{{ initials }}</template>
    </span>
    <component
      :is="isHuman ? User : Bot"
      v-if="corner"
      class="absolute -right-1 -bottom-1 size-3 rounded-full bg-surface p-px text-faint"
    />
  </span>

  <!-- A fixed 14px box whether it holds an icon or the dot, so the text after it starts in
       the same place for every member on a board. -->
  <span
    v-else-if="member"
    class="grid size-3.5 shrink-0 place-items-center"
    :style="{ color }"
    :title="hint"
  >
    <component :is="icon.component" v-if="icon" class="size-3.5" />
    <span v-else class="size-1.5 rounded-full" :style="{ background: color }" />
  </span>
</template>
