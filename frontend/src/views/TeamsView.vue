<script setup lang="ts">
/**
 * The team roster (spec §10.2/§10.4) — who is on the team, who reports to whom, and who may
 * hand work to whom. Work in flight lives on the Tasks page.
 *
 * The roster is drawn as an interactive graph rather than a list, because its content *is*
 * the relationships: `OrgChart` makes the connection itself the edit, so the reporting
 * vertical and the sideways `can_assign_to` routes are set by drawing them. Per-member task
 * counts ride along on the nodes — read-only context, and exactly what you want in view
 * while deciding whether to move a line.
 *
 * The page has two modes and `editing` is the switch for both halves of it: locked, the
 * chart only pans and zooms and there is nothing here that writes; editing, the roster can
 * be restructured, the settings rails are editable and changes save as you go. The default is
 * locked because reading the team is the common visit, and on a canvas the gesture for
 * reading (drag) and the gesture for restructuring (drag) are the same one.
 *
 * The roster is saved in `voxarium_studio/teams/<team>.yaml`, comments and all; shared run
 * defaults and agent prompts live in `voxarium_studio/config.yaml` (§10.2, §10.3).
 */
import { Check, Pencil, UserPlus, Users } from 'lucide-vue-next'
import { onMounted, ref, watch } from 'vue'

import EmptyState from '@/components/ui/EmptyState.vue'
import MemberEditor from '@/components/teams/MemberEditor.vue'
import OrgChart from '@/components/teams/OrgChart.vue'
import TeamSettings from '@/components/teams/TeamSettings.vue'
import type { MemberKind } from '@/api/types'
import { t } from '@/i18n'
import { useSkillsStore } from '@/stores/skills'
import { useTeamsStore } from '@/stores/teams'

const teams = useTeamsStore()
const skills = useSkillsStore()

const editing = ref(false)
const memberFormOpen = ref(false)
const newMember = ref({ name: '', roleTitle: '', kind: 'agent' as MemberKind, managerId: '' })

// Leaving edit mode puts the page back to read-only whole: the rail and the add form are
// both editors, and leaving one open over a locked canvas would offer what the mode says
// is unavailable.
watch(editing, (on) => {
  if (on) return
  memberFormOpen.value = false
  teams.editMember(null)
})

onMounted(async () => {
  teams.subscribe()
  // Beside the roster, not after it: the cards name each agent's skills either way, and the
  // folder only decides which of those names are marked as gone from it (§10.10).
  skills.subscribe()
  void skills.load()
  await teams.load()
})

function openMemberForm() {
  newMember.value = {
    name: '',
    roleTitle: '',
    kind: 'agent',
    managerId: teams.orgTree[0]?.member.id ?? '',
  }
  memberFormOpen.value = true
}

async function addMember() {
  if (!newMember.value.name.trim()) return
  const created = await teams.addMember({
    name: newMember.value.name.trim(),
    role_title: newMember.value.roleTitle.trim(),
    kind: newMember.value.kind,
    manager_id: newMember.value.managerId || null,
  })
  // `addMember` opens the editor for whoever it created, so the new row is ready to
  // configure rather than just present.
  if (created) memberFormOpen.value = false
}
</script>

<template>
  <div class="flex h-full">
    <aside class="w-[22rem] shrink-0 border-r border-line bg-surface">
      <TeamSettings :editing="editing" />
    </aside>
    <section class="flex min-w-0 flex-1 flex-col">
      <header class="vxs-pane flex shrink-0 flex-wrap items-center gap-2 border-b border-pane px-pane py-2">
        <span class="flex-1 truncate text-title font-medium">
          {{ teams.team?.name ?? t('teams.page.fallbackTitle') }}
        </span>
        <span v-if="editing" class="order-last w-full text-small text-faint">
          {{ t('teams.page.savesAsYouGo') }}
        </span>
        <button
          v-if="editing"
          class="flex shrink-0 items-center gap-1.5 rounded-pill border border-line px-3 py-1 hover:bg-hover"
          :title="t('teams.page.addMemberTitle')"
          @click="openMemberForm()"
        >
          <UserPlus class="size-3.5" />
          {{ t('teams.page.addMember') }}
        </button>
        <button
          class="flex shrink-0 items-center gap-1.5 rounded-pill px-3 py-1 font-medium"
          :class="editing ? 'vxs-btn-primary' : 'border border-line hover:bg-hover'"
          :title="editing ? t('teams.page.lockTitle') : t('teams.page.editTitle')"
          @click="editing = !editing"
        >
          <component :is="editing ? Check : Pencil" class="size-3.5" />
          {{ editing ? t('common.done') : t('common.edit') }}
        </button>
      </header>

      <p v-if="teams.error" class="shrink-0 bg-danger-soft px-pane py-1 text-danger">
        {{ teams.error }}
      </p>

      <form
        v-if="memberFormOpen"
        class="shrink-0 border-b border-line bg-elevated p-pane"
        @submit.prevent="addMember"
      >
        <div class="flex max-w-3xl flex-wrap items-end gap-2">
          <label class="min-w-40 flex-1">
            <span class="mb-1 block text-small text-faint">{{ t('teams.fields.name') }}</span>
            <input
              v-model="newMember.name"
              :placeholder="t('teams.fields.name')"
              class="w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
            />
          </label>
          <label class="min-w-40 flex-1">
            <span class="mb-1 block text-small text-faint">{{ t('teams.fields.role') }}</span>
            <input
              v-model="newMember.roleTitle"
              :placeholder="t('teams.page.rolePlaceholder')"
              class="w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
            />
          </label>
          <label class="min-w-32">
            <span class="mb-1 block text-small text-faint">{{ t('teams.fields.kind') }}</span>
            <select
              v-model="newMember.kind"
              class="w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
            >
              <option value="agent">{{ t('teams.kind.aiAgent') }}</option>
              <option value="human">{{ t('teams.kind.person') }}</option>
            </select>
          </label>
          <label class="min-w-40">
            <span class="mb-1 block text-small text-faint">{{ t('teams.fields.reportsTo') }}</span>
            <select
              v-model="newMember.managerId"
              class="w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
            >
              <option value="">{{ t('teams.page.noManager') }}</option>
              <option v-for="member in teams.members" :key="member.id" :value="member.id">
                {{ member.name }}
              </option>
            </select>
          </label>
          <div class="flex gap-2">
            <button
              type="submit"
              class="vxs-btn-primary px-3 py-1.5 font-medium text-on-accent disabled:opacity-50"
              :disabled="teams.busy || !newMember.name.trim()"
            >
              {{ t('common.add') }}
            </button>
            <button
              type="button"
              class="rounded-md border border-line px-3 py-1.5 hover:bg-hover"
              @click="memberFormOpen = false"
            >
              {{ t('common.cancel') }}
            </button>
          </div>
        </div>
      </form>

      <!-- The chart is the page's one band (`vxs-enter`, §7): it arrives under a header that
           stays still. The member rail beside it opens on a click, not on a navigation. -->
      <div class="vxs-enter min-h-0 flex-1">
        <EmptyState
          v-if="!teams.members.length && !teams.loading"
          :title="t('teams.page.emptyTitle')"
          :hint="
            editing
              ? t('teams.page.emptyHintEditing', { dir: 'voxarium_studio/teams/' })
              : t('teams.page.emptyHintLocked', { dir: 'voxarium_studio/teams/' })
          "
        >
          <template #icon><Users class="mx-auto mb-3 size-8 text-faint" /></template>
        </EmptyState>
        <OrgChart v-else :editing="editing" @configure="teams.editMember($event)" />
      </div>
    </section>

    <!-- Gated on `editing` as well as on there being a member: `editingMemberId` lives in
         the store and outlives this page (§ two pages, one store), while `editing` is local
         and starts false on every mount. Without the first half, leaving for Tasks and
         coming back brought the rail up over a locked chart. Keeping the id is deliberate —
         pressing Edit again reopens on whoever was being configured. -->
    <aside v-if="editing && teams.editingMember" class="w-96 shrink-0">
      <MemberEditor :member="teams.editingMember" @close="teams.editMember(null)" />
    </aside>
  </div>
</template>
