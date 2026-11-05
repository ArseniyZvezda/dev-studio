<script setup lang="ts">
/**
 * Per-member settings (spec §10.2).
 *
 * The roster lives in `voxarium_studio/teams/*.yaml` and every save writes it back, so this panel
 * is an editor for a git-tracked file rather than for hidden state — that is why it edits a
 * local draft and sends only the fields that actually changed: an unrelated field someone
 * hand-edited in the YAML meanwhile is left alone instead of being overwritten with what
 * this form last loaded.
 *
 * Saving is automatic, on a short debounce, because the panel is only reachable with the
 * chart in edit mode and everything else in that mode already commits on the gesture —
 * drawing a line, dropping a node. A Save button here would have been the one change on the
 * page you could lose by clicking away. The debounce is what keeps a typed name from being
 * one request per keystroke; the draft is not re-seeded while it is pending, or the
 * round-trip would fight the typing.
 *
 * The agent block is hidden for `kind: human`: a person has no session, model or tool list,
 * and showing eight settings that do nothing is worse than showing none. Skills go with it.
 */
import { Bot, GraduationCap, Trash2, User, X } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { MemberKind, MemberUpdate, TeamMember } from '@/api/types'
import MemberIconPicker from '@/components/teams/MemberIconPicker.vue'
import MemberMark from '@/components/teams/MemberMark.vue'
import MemberSelect from '@/components/teams/MemberSelect.vue'
import { t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { skillChips, skillOptions } from '@/lib/skills'
import { useSkillsStore } from '@/stores/skills'
import { useTeamsStore } from '@/stores/teams'

const props = defineProps<{ member: TeamMember }>()
const emit = defineEmits<{ close: [] }>()

const teams = useTeamsStore()
const skillsStore = useSkillsStore()

// The picker offers what the folder holds now — a skill created on the Skills page a moment ago
// included — and follows the folder while the rail is open.
onMounted(() => {
  skillsStore.subscribe()
  void skillsStore.load()
})

interface Draft {
  name: string
  role_title: string
  kind: MemberKind
  manager_id: string | null
  responsibility: string
  can_assign_to: string[]
  tags: string
  agent_backend: string
  agent: string
  model: string
  effort: string | null
  allowed_tools: string[]
  skills: string[]
  max_turns: number
  permission_mode: string
  isolate_worktree: boolean
  solo_mode: boolean
  avatar_color: string | null
  avatar_icon: string | null
}

function draftOf(member: TeamMember): Draft {
  return {
    name: member.name,
    role_title: member.role_title,
    kind: member.kind,
    manager_id: member.manager_id,
    responsibility: member.responsibility,
    can_assign_to: [...(member.can_assign_to ?? [])],
    tags: (member.tags ?? []).join(', '),
    agent_backend: member.agent_backend,
    agent: member.agent ?? '',
    model: member.model,
    effort: member.effort,
    allowed_tools: [...(member.allowed_tools ?? [])],
    skills: [...(member.skills ?? [])],
    max_turns: member.max_turns,
    permission_mode: member.permission_mode,
    isolate_worktree: member.isolate_worktree,
    solo_mode: member.solo_mode,
    avatar_color: member.avatar_color,
    avatar_icon: member.avatar_icon,
  }
}

const draft = ref<Draft>(draftOf(props.member))
const confirmingRemoval = ref(false)
const saving = ref(false)
/** The last save was refused (a circular reporting line, an unknown backend). Nothing is
 *  retried until the user changes something, so the footer must not claim to be saving. */
const failed = ref(false)

const isAgent = computed(() => draft.value.kind === 'agent')
const isProducer = computed(() => props.member.builtin === 'producer')
/** The draft as a thing `MemberMark` can draw, so the header previews an unsaved choice. */
const look = computed(() => ({
  name: draft.value.name,
  kind: draft.value.kind,
  avatar_color: draft.value.avatar_color,
  avatar_icon: draft.value.avatar_icon,
}))
const options = computed(() => teams.options)

/**
 * The name the Studio would write this member under, mirroring `slug_for` in
 * `teams/agents.py` — which is the source of truth. Mirrored rather than fetched because
 * this only fills a placeholder and a hint: the name that actually runs is chosen backend
 * side and recorded on the task, so a drift here misleads nobody about what happened.
 */
const generatedAgentName = computed(
  () =>
    draft.value.name
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '') || props.member.id,
)

const managerOptions = computed(() => teams.managerCandidates(props.member.id))

/** Everyone else, annotated with whether the vertical already reaches them. */
const peers = computed(() =>
  teams.members
    .filter((candidate) => candidate.id !== props.member.id)
    .map((candidate) => ({
      member: candidate,
      viaVertical:
        candidate.manager_id === props.member.id ||
        candidate.id === draft.value.manager_id,
    })),
)

const anyoneAllowed = computed(() => draft.value.can_assign_to.includes('*'))

/**
 * The model picker is a list of choices plus an escape hatch, not a whitelist: the backend
 * passes `model` to the CLI verbatim, so a model released after this build has to remain
 * typeable. `CUSTOM_MODEL` is the sentinel for that row.
 */
const CUSTOM_MODEL = '__custom__'

/** The current backend's models only: a Codex member is offered Codex's catalogue, not
 *  Claude's aliases, and an entry that names no backend is for everyone. */
const modelChoices = computed(() =>
  (options.value?.suggested_models ?? []).filter(
    (entry) => !entry.backend || entry.backend === draft.value.agent_backend,
  ),
)

/** Whether this backend runs a member as a named agent definition (`claude --agent`). */
const backendNamesAgents = computed(
  () => options.value?.agent_backends?.[draft.value.agent_backend]?.named_agents !== false,
)

/** What choosing a backend other than Claude Code means for the fields around it. */
const backendHint = computed(() => {
  if (draft.value.agent_backend === 'codex') return t('teams.editor.codexHint')
  if (draft.value.agent_backend === 'qwen') return t('teams.editor.qwenHint')
  return ''
})

/** Grouped in the order the backend sent them, so aliases stay above the pinned ids. */
const modelGroups = computed(() => {
  const groups = new Map<string, typeof modelChoices.value>()
  for (const entry of modelChoices.value) {
    const existing = groups.get(entry.group)
    if (existing) existing.push(entry)
    else groups.set(entry.group, [entry])
  }
  return [...groups.entries()]
})

/** Set only by choosing "Something else"; a model that is simply unlisted implies it. */
const forcedCustomModel = ref(false)
const usingCustomModel = computed(
  () => forcedCustomModel.value || !modelChoices.value.some((m) => m.id === draft.value.model),
)

function pickModel(value: string): void {
  if (value === CUSTOM_MODEL) {
    forcedCustomModel.value = true
    return
  }
  forcedCustomModel.value = false
  draft.value.model = value
}

/** What the chosen model means, when the catalogue says something worth repeating. */
const modelNote = computed(
  () => modelChoices.value.find((entry) => entry.id === draft.value.model)?.note ?? '',
)

/** The installed SDK's levels, plus whatever this member already has if it is not among
 *  them — a picker that silently drops a stored value would clear it on the next save. */
const effortLevels = computed(() => {
  const levels = options.value?.effort_levels ?? []
  const current = draft.value.effort
  return current && !levels.includes(current) ? [...levels, current] : levels
})

/** Tools the member names that are not in the curated list — kept editable as free text. */
const extraTools = ref(
  props.member.allowed_tools
    .filter((tool) => !(teams.options?.common_tools ?? []).includes(tool))
    .join(', '),
)

function splitList(value: string): string[] {
  return value
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean)
}

function toggle(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((entry) => entry !== value) : [...list, value]
}

function toggleAnyone(): void {
  draft.value.can_assign_to = anyoneAllowed.value ? [] : ['*']
}

/** Only what changed, so a concurrent hand-edit elsewhere in the file survives. */
const patch = computed<MemberUpdate>(() => {
  const current = draft.value
  const original = props.member
  const result: MemberUpdate = {}

  if (current.name.trim() !== original.name) result.name = current.name.trim()
  if (current.role_title.trim() !== original.role_title)
    result.role_title = current.role_title.trim()
  if (current.kind !== original.kind) result.kind = current.kind
  if (current.manager_id !== original.manager_id) result.manager_id = current.manager_id
  // §10.11: the built-in Producer's responsibility is owned by its Settings prompt.
  if (!isProducer.value && current.responsibility !== original.responsibility)
    result.responsibility = current.responsibility
  if (current.avatar_color !== original.avatar_color) result.avatar_color = current.avatar_color
  if (current.avatar_icon !== original.avatar_icon) result.avatar_icon = current.avatar_icon

  const tags = splitList(current.tags)
  if (tags.join('\0') !== (original.tags ?? []).join('\0')) result.tags = tags

  const routes = [...current.can_assign_to].sort()
  if (routes.join('\0') !== [...(original.can_assign_to ?? [])].sort().join('\0'))
    result.can_assign_to = current.can_assign_to

  if (current.agent_backend !== original.agent_backend)
    result.agent_backend = current.agent_backend
  // Blank means "the agent the Studio writes from this member", which the backend stores
  // as null — so an empty field and a cleared one are the same edit.
  if ((current.agent.trim() || null) !== (original.agent || null))
    result.agent = current.agent.trim() || null
  if (current.model.trim() !== original.model) result.model = current.model.trim()
  // Null and "" both mean "the model decides"; only a real change is worth a request.
  if ((current.effort || null) !== (original.effort || null)) result.effort = current.effort || null
  if (current.permission_mode !== original.permission_mode)
    result.permission_mode = current.permission_mode
  if (Number(current.max_turns) !== original.max_turns) result.max_turns = Number(current.max_turns)
  if (current.isolate_worktree !== original.isolate_worktree)
    result.isolate_worktree = current.isolate_worktree
  if (current.solo_mode !== original.solo_mode) result.solo_mode = current.solo_mode

  const tools = [...new Set([...current.allowed_tools, ...splitList(extraTools.value)])]
  if (tools.join('\0') !== (original.allowed_tools ?? []).join('\0'))
    result.allowed_tools = tools

  // The whole list, in order: it is what the prompt lists, and the backend checks only the
  // names being added, so a skill gone from the folder can still be taken off beside it.
  if (current.skills.join('\n') !== (original.skills ?? []).join('\n'))
    result.skills = current.skills

  return result
})

/** The member's skills against the folder — a name the folder lost is marked, not dropped. */
const chips = computed(() => skillChips(draft.value.skills, skillsStore.skills))
const addableSkills = computed(() => skillOptions(skillsStore.skills ?? [], draft.value.skills))
const skillsFolder = computed(() => skillsStore.overview?.path ?? 'voxarium_studio/skills')

function addSkill(event: Event): void {
  // An action rather than a field, like the dependency picker: the placeholder comes straight
  // back, so a second skill is the same gesture as the first.
  const select = event.target as HTMLSelectElement
  const name = select.value
  select.value = ''
  if (name && !draft.value.skills.includes(name)) draft.value.skills = [...draft.value.skills, name]
}

function dropSkill(name: string): void {
  draft.value.skills = draft.value.skills.filter((entry) => entry !== name)
}

const dirty = computed(() => Object.keys(patch.value).length > 0)
const valid = computed(() => draft.value.name.trim().length > 0)

/** Long enough that typing a role title is one save, short enough to feel immediate. */
const SAVE_DELAY = 600

let timer: ReturnType<typeof setTimeout> | null = null
/** The pending save, snapshotted with the id it belongs to: the panel can be pointed at
 *  someone else before the timer fires, and a patch must never land on whoever came next. */
let pending: { memberId: string; patch: MemberUpdate } | null = null
/** Saves run one at a time. Each ends in a `refreshTeam`, so two in flight together can
 *  land out of order and leave the store showing the older of the two. */
let queue: Promise<unknown> = Promise.resolve()
let running = 0

function cancelPending(): void {
  if (timer) clearTimeout(timer)
  timer = null
  pending = null
}

function schedule(): void {
  if (!dirty.value || !valid.value) {
    // Nothing to send, or nothing sendable: an empty name is refused by the backend, so
    // hold the edit here rather than spending a round-trip to be told so mid-word.
    cancelPending()
    return
  }
  if (timer) clearTimeout(timer)
  failed.value = false
  pending = { memberId: props.member.id, patch: patch.value }
  timer = setTimeout(() => void flush(), SAVE_DELAY)
}

async function flush(): Promise<void> {
  if (timer) clearTimeout(timer)
  timer = null
  const job = pending
  pending = null
  if (!job) return
  running += 1
  saving.value = true
  const attempt = queue.then(() => teams.saveMember(job.memberId, job.patch))
  queue = attempt
  try {
    failed.value = (await attempt) === false
  } finally {
    running -= 1
    if (running === 0) saving.value = false
  }
}

watch([draft, extraTools], schedule, { deep: true })

watch(
  () => props.member,
  (member, previous) => {
    if (member.id !== previous.id) {
      // Start the outgoing member's save before re-seeding — `flush` takes its snapshot
      // synchronously, so it still carries the id it was built for — and deliberately do
      // not await it. Suspending here lets a second switch seed the draft first and then
      // be overwritten by this one, which writes one member's settings onto another.
      void flush()
      confirmingRemoval.value = false
      forcedCustomModel.value = false
      extraTools.value = member.allowed_tools
        .filter((tool) => !(teams.options?.common_tools ?? []).includes(tool))
        .join(', ')
      draft.value = draftOf(member)
      return
    }
    // Same member, changed underneath — a line drawn on the canvas moves `manager_id`.
    // Take the new values unless a save of our own is in flight. `dirty` is not the test
    // for that: an external change is *precisely* what makes the draft differ from the
    // member, so using it kept the stale value and wrote it back on the next keystroke.
    if (!pending && !saving.value) draft.value = draftOf(member)
  },
)

// A closing panel still owes whatever the debounce is holding.
onBeforeUnmount(() => {
  void flush()
})

async function remove() {
  // Whatever is queued is about to be a patch against a row that no longer exists.
  cancelPending()
  const removed = await teams.removeMember(props.member.id)
  if (removed) emit('close')
  else confirmingRemoval.value = false
}
</script>

<template>
  <div class="flex h-full flex-col border-l border-line bg-surface">
    <header class="flex items-center gap-2 border-b border-line px-pane py-2">
      <!-- Drawn from the draft rather than from the member, so the icon and the colour
           being chosen below are previewed here before the debounce has saved either. -->
      <MemberMark :member="look" variant="badge" />
      <div class="min-w-0 flex-1">
        <p class="truncate font-medium">{{ draft.name || t('teams.editor.unnamed') }}</p>
        <p class="truncate font-mono text-small text-faint">{{ member.id }}</p>
      </div>
      <button
        class="grid size-6 shrink-0 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
        :title="t('common.close')"
        @click="emit('close')"
      >
        <X class="size-3.5" />
      </button>
    </header>

    <div class="min-h-0 flex-1 space-y-4 overflow-auto p-pane">
      <!-- Identity ------------------------------------------------------------- -->
      <section class="space-y-2">
        <p class="text-small font-semibold tracking-wide text-faint uppercase">
          {{ t('teams.editor.identity') }}
        </p>

        <div class="flex gap-2">
          <label class="flex-1">
            <span class="mb-1 block text-small text-faint">{{ t('teams.fields.name') }}</span>
            <input v-model="draft.name" class="field" />
          </label>
          <label class="flex-1">
            <span class="mb-1 block text-small text-faint">{{ t('teams.fields.role') }}</span>
            <input
              v-model="draft.role_title"
              class="field"
              :placeholder="t('teams.editor.rolePlaceholder')"
            />
          </label>
        </div>

        <div>
          <span class="mb-1 block text-small text-faint">{{ t('teams.fields.kind') }}</span>
          <div class="flex gap-2">
            <button
              v-for="kind in (['agent', 'human'] as MemberKind[])"
              :key="kind"
              :disabled="member.builtin === 'producer' && kind === 'human'"
              class="flex flex-1 items-center justify-center gap-1.5 rounded-md border px-3 py-1.5"
              :class="
                draft.kind === kind
                  ? 'border-accent bg-accent-soft'
                  : 'border-line hover:bg-hover'
              "
              @click="draft.kind = kind"
            >
              <component :is="kind === 'agent' ? Bot : User" class="size-3.5" />
              {{ kind === 'agent' ? t('teams.kind.aiAgent') : t('teams.kind.person') }}
            </button>
          </div>
          <p class="mt-1 text-small text-faint">
            {{ isAgent ? t('teams.editor.agentKindHint') : t('teams.editor.personKindHint') }}
          </p>
        </div>

        <div class="flex items-end gap-2">
          <label class="flex-1">
            <span class="mb-1 block text-small text-faint">{{ t('teams.editor.tags') }}</span>
            <input v-model="draft.tags" class="field" placeholder="engine, ui" />
          </label>
          <label class="shrink-0">
            <span class="mb-1 block text-small text-faint">{{ t('teams.editor.colour') }}</span>
            <input
              :value="draft.avatar_color ?? '#7c5cff'"
              type="color"
              class="h-[30px] w-12 cursor-pointer rounded-md border border-line bg-inset"
              @input="draft.avatar_color = ($event.target as HTMLInputElement).value"
            />
          </label>
          <button
            v-if="draft.avatar_color"
            class="shrink-0 rounded-md border border-line px-2 py-1 text-small hover:bg-hover"
            :title="t('teams.editor.clearColourTitle')"
            @click="draft.avatar_color = null"
          >
            {{ t('teams.editor.clearColour') }}
          </button>
        </div>

        <div>
          <span class="mb-1 block text-small text-faint">{{ t('teams.editor.icon') }}</span>
          <MemberIconPicker v-model="draft.avatar_icon" :color="draft.avatar_color" />
          <p class="mt-1 text-small text-faint">
            {{ t('teams.editor.iconHint') }}
          </p>
        </div>
      </section>

      <!-- Place in the team ---------------------------------------------------- -->
      <section class="space-y-2 border-t border-line-subtle pt-3">
        <p class="text-small font-semibold tracking-wide text-faint uppercase">
          {{ t('teams.editor.placeInTeam') }}
        </p>

        <div>
          <span class="mb-1 block text-small text-faint">{{ t('teams.fields.reportsTo') }}</span>
          <MemberSelect
            v-model="draft.manager_id"
            :members="managerOptions"
            :empty-label="t('teams.editor.nobodyTop')"
            :label="t('teams.fields.reportsTo')"
          />
          <span class="mt-1 block text-small text-faint">
            {{ t('teams.editor.reportsToHint') }}
          </span>
        </div>

        <div>
          <span class="mb-1 block text-small text-faint">{{ t('teams.editor.mayAlsoAssign') }}</span>
          <label class="mb-1 flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              :checked="anyoneAllowed"
              class="accent-[var(--vxs-accent)]"
              @change="toggleAnyone"
            />
            {{ t('teams.editor.anyone') }}
          </label>
          <div v-if="!anyoneAllowed" class="space-y-1 pl-1">
            <label
              v-for="entry in peers"
              :key="entry.member.id"
              class="flex items-center gap-2"
              :class="entry.viaVertical ? 'cursor-default text-faint' : 'cursor-pointer'"
            >
              <input
                type="checkbox"
                :checked="entry.viaVertical || draft.can_assign_to.includes(entry.member.id)"
                :disabled="entry.viaVertical"
                class="accent-[var(--vxs-accent)]"
                @change="draft.can_assign_to = toggle(draft.can_assign_to, entry.member.id)"
              />
              <MemberMark :member="entry.member" />
              <span class="truncate">
                {{ entry.member.name }} — {{ entry.member.role_title }}
              </span>
              <span v-if="entry.viaVertical" class="ml-auto shrink-0 text-small">
                {{ t('teams.editor.always') }}
              </span>
            </label>
          </div>
          <p class="mt-1 text-small text-faint">
            {{ t('teams.editor.routesHint') }}
          </p>
        </div>
      </section>

      <!-- Responsibility ------------------------------------------------------- -->
      <section class="space-y-2 border-t border-line-subtle pt-3">
        <p class="text-small font-semibold tracking-wide text-faint uppercase">
          {{ t('teams.editor.responsibility') }}
        </p>
        <textarea
          v-if="isProducer"
          :value="member.responsibility"
          :aria-label="t('teams.editor.responsibility')"
          readonly
          rows="8"
          class="field resize-y text-muted"
        />
        <textarea
          v-else
          v-model="draft.responsibility"
          :aria-label="t('teams.editor.responsibility')"
          rows="4"
          class="field resize-y"
        />
        <p class="text-small text-faint">
          {{
            isProducer
              ? t('teams.editor.responsibilityProducerHint')
              : isAgent
                ? t('teams.editor.responsibilityAgentHint')
                : t('teams.editor.responsibilityPersonHint')
          }}
        </p>
      </section>

      <!-- Skills --------------------------------------------------------------- -->
      <!-- Chips from the draft, the picker from the folder (§10.10). A name the folder no longer
           has stays as a marked chip until it is taken off: the member's list still holds it,
           and quietly dropping it here would be an edit nobody made. -->
      <section v-if="isAgent" class="space-y-2 border-t border-line-subtle pt-3">
        <p class="text-small font-semibold tracking-wide text-faint uppercase">
          {{ t('teams.editor.skills') }}
        </p>

        <ul v-if="chips.length" class="flex flex-wrap gap-1">
          <li
            v-for="chip in chips"
            :key="chip.name"
            class="flex min-w-0 items-center gap-1 rounded-md border border-line bg-inset py-0.5 pr-0.5 pl-1.5"
            :title="
              chip.missing
                ? t('teams.editor.skillMissing', { folder: skillsFolder })
                : chip.skill?.description || chip.name
            "
          >
            <GraduationCap
              class="size-3 shrink-0"
              :class="chip.missing ? 'text-warning' : 'text-faint'"
            />
            <span
              class="truncate font-mono text-[12px]"
              :class="chip.missing ? 'text-warning line-through' : ''"
            >
              {{ chip.name }}
            </span>
            <button
              type="button"
              class="grid size-4 shrink-0 place-items-center rounded-sm text-faint hover:bg-hover hover:text-danger"
              :title="
                draft.name
                  ? t('teams.editor.takeOffSkill', { skill: chip.name, member: draft.name })
                  : t('teams.editor.takeOffSkillUnnamed', { skill: chip.name })
              "
              @click="dropSkill(chip.name)"
            >
              <X class="size-3" />
            </button>
          </li>
        </ul>

        <select v-if="addableSkills.length" class="field" @change="addSkill">
          <option value="">{{ t('teams.editor.addSkill') }}</option>
          <option
            v-for="skill in addableSkills"
            :key="skill.name"
            :value="skill.name"
            :title="skill.description"
          >
            {{ skill.name }}
          </option>
        </select>
        <p v-else-if="skillsStore.skills && !skillsStore.skills.length" class="text-small text-faint">
          <I18nT keypath="teams.editor.noSkills">
            <template #link>
              <RouterLink to="/skills" class="text-accent hover:underline">{{
                t('teams.editor.noSkillsLink')
              }}</RouterLink>
            </template>
          </I18nT>
        </p>

        <p class="text-small text-faint">
          {{ t('teams.editor.skillsHint', { folder: skillsFolder }) }}
        </p>
      </section>

      <!-- Agent settings ------------------------------------------------------- -->
      <section v-if="isAgent" class="space-y-2 border-t border-line-subtle pt-3">
        <p class="text-small font-semibold tracking-wide text-faint uppercase">
          {{ t('teams.editor.agentSection') }}
        </p>

        <div class="flex gap-2">
          <label class="flex-1">
            <span class="mb-1 block text-small text-faint">{{ t('teams.editor.backend') }}</span>
            <select v-model="draft.agent_backend" class="field">
              <option
                v-for="(info, name) in options?.agent_backends ?? {}"
                :key="name"
                :value="name"
              >
                {{ info.available ? name : t('teams.editor.backendUnavailable', { name }) }}
              </option>
            </select>
          </label>
          <label class="flex-1">
            <span class="mb-1 block text-small text-faint">{{ t('teams.editor.model') }}</span>
            <select
              :value="usingCustomModel ? CUSTOM_MODEL : draft.model"
              class="field"
              @change="pickModel(($event.target as HTMLSelectElement).value)"
            >
              <optgroup v-for="[group, entries] in modelGroups" :key="group" :label="group">
                <option v-for="entry in entries" :key="entry.id" :value="entry.id">
                  {{ entry.label }}
                </option>
              </optgroup>
              <option :value="CUSTOM_MODEL">{{ t('teams.editor.somethingElse') }}</option>
            </select>
          </label>
        </div>

        <label v-if="usingCustomModel" class="block">
          <span class="mb-1 block text-small text-faint">{{ t('teams.editor.modelId') }}</span>
          <input
            v-model="draft.model"
            class="field font-mono"
            :placeholder="modelChoices[0]?.id ?? 'claude-opus-5'"
          />
          <span class="mt-1 block text-small text-faint">
            {{ t('teams.editor.modelIdHint') }}
          </span>
        </label>
        <p v-else-if="modelNote" class="text-small text-faint">{{ modelNote }}</p>
        <p
          v-if="
            options?.agent_backends?.[draft.agent_backend] &&
            !options.agent_backends[draft.agent_backend]!.available
          "
          class="rounded-md bg-warning-soft p-2 text-small text-warning"
        >
          {{ options.agent_backends[draft.agent_backend]!.reason }}
        </p>
        <p v-if="backendHint" class="text-small text-faint">{{ backendHint }}</p>

        <!-- The session runs as a Claude Code agent either way (§10.3); this only chooses
             whose definition it is. Left blank the Studio writes one from this member and
             keeps it in step with the roster, which is what almost everyone wants. A backend
             without named agents (Codex) keeps the identity in the system prompt, so the
             field would choose nothing there (§10.6). -->
        <label v-if="backendNamesAgents" class="block">
          <span class="mb-1 block text-small text-faint">{{ t('teams.editor.runsAs') }}</span>
          <input
            v-model="draft.agent"
            class="field font-mono"
            :placeholder="generatedAgentName"
          />
          <span class="mt-1 block text-small text-faint">
            <I18nT v-if="draft.agent.trim()" keypath="teams.editor.runsAsNamed">
              <template #agent>
                <span class="font-mono">{{ draft.agent.trim() }}</span>
              </template>
            </I18nT>
            <I18nT v-else keypath="teams.editor.runsAsGenerated">
              <template #file>
                <span class="font-mono">.claude/agents/{{ generatedAgentName }}.md</span>
              </template>
              <template #command>
                <span class="font-mono">claude --agent {{ generatedAgentName }}</span>
              </template>
            </I18nT>
          </span>
        </label>

        <div class="flex gap-2">
          <label class="flex-1">
            <span class="mb-1 block text-small text-faint">{{ t('teams.editor.permissionMode') }}</span>
            <select v-model="draft.permission_mode" class="field">
              <option v-for="mode in options?.permission_modes ?? []" :key="mode" :value="mode">
                {{ mode }}
              </option>
            </select>
          </label>
          <label class="w-28 shrink-0">
            <span class="mb-1 block text-small text-faint">{{ t('teams.editor.maxTurns') }}</span>
            <input v-model.number="draft.max_turns" type="number" min="1" max="1000" class="field" />
          </label>
        </div>

        <label class="block">
          <span class="mb-1 block text-small text-faint">{{ t('teams.editor.effort') }}</span>
          <select
            :value="draft.effort ?? ''"
            class="field"
            @change="draft.effort = ($event.target as HTMLSelectElement).value || null"
          >
            <option value="">{{ t('teams.editor.effortDefault') }}</option>
            <option v-for="level in effortLevels" :key="level" :value="level">{{ level }}</option>
          </select>
          <span class="mt-1 block text-small text-faint">
            {{ t('teams.editor.effortHint') }}
          </span>
        </label>

        <label class="flex cursor-pointer items-start gap-2">
          <input
            v-model="draft.solo_mode"
            type="checkbox"
            class="mt-0.5 accent-[var(--vxs-accent)]"
          />
          <span>
            {{ t('teams.editor.soloMode') }}
            <span class="block text-small text-faint">
              {{ t('teams.editor.soloModeHint') }}
            </span>
          </span>
        </label>

        <label class="flex cursor-pointer items-start gap-2">
          <input
            v-model="draft.isolate_worktree"
            type="checkbox"
            class="mt-0.5 accent-[var(--vxs-accent)]"
          />
          <span>
            {{ t('teams.editor.isolate') }}
            <span class="block text-small text-faint">
              {{ t('teams.editor.isolateHint') }}
            </span>
          </span>
        </label>

        <div>
          <span class="mb-1 block text-small text-faint">{{ t('teams.editor.tools') }}</span>
          <div class="grid grid-cols-2 gap-x-3 gap-y-1">
            <label
              v-for="tool in options?.common_tools ?? []"
              :key="tool"
              class="flex cursor-pointer items-center gap-2"
            >
              <input
                type="checkbox"
                :checked="draft.allowed_tools.includes(tool)"
                class="accent-[var(--vxs-accent)]"
                @change="draft.allowed_tools = toggle(draft.allowed_tools, tool)"
              />
              <span class="truncate font-mono text-[12px]">{{ tool }}</span>
            </label>
          </div>
          <input
            v-model="extraTools"
            class="field mt-2"
            :placeholder="t('teams.editor.otherTools')"
          />
          <p class="mt-1 text-small text-faint">
            {{
              t('teams.editor.studioTools', {
                prefix: options?.studio_tool_prefix ?? 'mcp__voxarium_studio__',
              })
            }}
          </p>
        </div>
      </section>
    </div>

    <footer class="space-y-2 border-t border-line p-2">
      <p v-if="teams.error" class="rounded-md bg-danger-soft p-2 text-small text-danger">
        {{ teams.error }}
      </p>

      <p v-if="confirmingRemoval" class="rounded-md bg-inset p-2 text-small">
        {{ t('teams.editor.confirmRemove', { name: member.name }) }}
      </p>

      <div class="flex items-center gap-2">
        <p
          class="flex-1 truncate text-small"
          :class="!valid || failed ? 'text-warning' : 'text-faint'"
        >
          <template v-if="!valid">{{ t('teams.editor.nameNeeded') }}</template>
          <template v-else-if="failed">{{ t('teams.editor.notSaved') }}</template>
          <template v-else-if="saving || dirty">{{ t('common.saving') }}</template>
          <!-- Not naming the file: a team's id and its filename are independent (see
               `TeamStore.file_for`), so the panel would be guessing. -->
          <template v-else>{{ t('teams.editor.savedToYaml') }}</template>
        </p>
        <button
          v-if="!confirmingRemoval && !member.builtin"
          class="grid place-items-center rounded-md border border-line px-2 py-1.5 text-danger hover:bg-danger-soft"
          :title="t('teams.editor.removeTitle')"
          @click="confirmingRemoval = true"
        >
          <Trash2 class="size-3.5" />
        </button>
        <template v-else-if="confirmingRemoval && !member.builtin">
          <button
            class="rounded-md bg-danger px-3 py-1.5 font-medium text-on-accent disabled:opacity-50"
            :disabled="teams.busy"
            @click="remove"
          >
            {{ t('common.remove') }}
          </button>
          <button
            class="rounded-md border border-line px-3 py-1.5 hover:bg-hover"
            @click="confirmingRemoval = false"
          >
            {{ t('teams.editor.keep') }}
          </button>
        </template>
      </div>
      <p v-if="member.builtin === 'producer'" class="text-small text-faint">{{ t('teams.editor.builtinProducer') }}</p>
    </footer>
  </div>
</template>

<style scoped>
/* One shared input skin: this form has a dozen fields, and repeating the utility string on
   each is what makes them drift apart. Values come from the tokens in `styles/tokens.css`,
   never literals (§7). */
.field {
  width: 100%;
  border-radius: var(--vxs-radius-md);
  border: 1px solid var(--vxs-border);
  background: var(--vxs-bg-inset);
  padding: 4px var(--vxs-space-2);
  outline: none;
}

.field:focus {
  border-color: var(--vxs-accent);
}
</style>
