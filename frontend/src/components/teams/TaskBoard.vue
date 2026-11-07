<script setup lang="ts">
/**
 * Task board grouped by status (spec §10.4).
 *
 * The board is editable in place, the way a board is expected to be: drag a card to another
 * column to move it, double-click its title to rename it, and reach the rest — assignee,
 * status, deletion — from the card's own menu. Everything goes through one `PATCH`, so a
 * move and a rename are the same operation with different keys, and each one leaves an entry
 * in the task's history.
 *
 * Five columns for seven statuses. `pending`, `needs_review` and `blocked` share one —
 * **Waiting** — because all three mean the same thing to the person reading the board: the
 * task is not being worked on and it is not finished, and somebody has to do something
 * about it. What that something is differs, so a card in that column carries its own status
 * badge where a card in a single-status column does not; the column already says it there.
 * Dropping a card into Waiting releases it to `pending` — the one status of the three a
 * person moves a card into by hand — while the card menu still offers every status.
 *
 * **Archived** is the sixth column and is drawn only when asked for. A task in Done can be
 * archived from its card: it leaves the board and keeps counting toward the cost, which is
 * the whole difference between this and deleting it (§10.4). The archive is reached from the
 * page header, and a card there is brought back by its own button or by dragging it out.
 *
 * Mutations are called on the store directly rather than emitted upward, as `TaskDetail`
 * does: the page owns which task is *open*, but not what is done to a card.
 *
 * The card is a `div` with a button role rather than a real `<button>`: it has to contain
 * the rename input and the actions menu, and nesting either inside a button is invalid
 * markup that browsers resolve by swallowing the inner element's clicks.
 */
import {
  Archive,
  ArchiveRestore,
  Bot,
  GripVertical,
  Link2,
  MoreHorizontal,
  Trash2,
  User,
} from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { TaskStatus, TaskUpdate, TeamTask } from '@/api/types'
import MemberMark from '@/components/teams/MemberMark.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import TokenUsageChip from '@/components/ui/TokenUsageChip.vue'
import { t } from '@/i18n'
import { isSuccessful, statusColor, statusLabel } from '@/lib/status'
import { tokenDirections } from '@/lib/token-directions'
import { resultTypeColor, resultTypeLabel } from '@/lib/task-result'
import { useTeamsStore } from '@/stores/teams'

const props = defineProps<{
  /** Every task on the team, archived ones included — the board decides what to draw. */
  tasks: TeamTask[]
  selectedId: string | null
  /** Draw the Archived column after Done. Off by default: that is what archiving is for. */
  showArchived?: boolean
}>()
const emit = defineEmits<{ select: [string] }>()

const teams = useTeamsStore()
const usageById = computed(() => new Map(props.tasks.map(task => [task.id, tokenDirections({ ...task }, task.total_tokens)])))

/** Puts the caret in the rename box exactly once, when it appears. A template ref inside
 *  `v-for` would arrive as an array, and a function ref would re-select on every keystroke. */
const vSelectOnMount = {
  mounted: (element: HTMLInputElement) => element.select(),
}

interface Column {
  key: string
  /** Read when the header renders — a getter below, so it follows the interface language. */
  label: string
  /** The statuses this column holds. Empty for the archive, which is keyed on a flag. */
  statuses: TaskStatus[]
  /** Where a card dropped here goes. Null for the archive, which sets `archived` instead. */
  dropStatus: TaskStatus | null
  /** What the column means, on the header — the two on the left are easy to mix up. */
  hint: string
}

// `todo` first and Waiting beside it: that boundary is the one the user crosses by hand,
// and crossing it is what releases work to the agents (§10.4). `cancelled` is deliberately
// last and usually empty — it exists so cancelled work does not silently vanish.
//
// The words are getters rather than strings, as `choice()` in `lib/appearance.ts` is: this
// table is built once, when the module loads, and a string resolved then would stay in the
// language the page opened in. A column holding one status is named by that status's word.
const COLUMNS: Column[] = [
  {
    key: 'todo',
    get label() {
      return statusLabel('todo')
    },
    statuses: ['todo'],
    dropStatus: 'todo',
    get hint() {
      return t('tasks.board.columnHint.todo')
    },
  },
  {
    key: 'waiting',
    get label() {
      return t('tasks.board.waiting')
    },
    statuses: ['pending', 'needs_review', 'blocked'],
    dropStatus: 'pending',
    get hint() {
      return t('tasks.board.columnHint.waiting')
    },
  },
  {
    key: 'in_progress',
    get label() {
      return statusLabel('in_progress')
    },
    statuses: ['in_progress'],
    dropStatus: 'in_progress',
    get hint() {
      return t('tasks.board.columnHint.in_progress')
    },
  },
  {
    key: 'done',
    get label() {
      return statusLabel('done')
    },
    statuses: ['done'],
    dropStatus: 'done',
    get hint() {
      return t('tasks.board.columnHint.done')
    },
  },
  {
    key: 'cancelled',
    get label() {
      return statusLabel('cancelled')
    },
    statuses: ['cancelled'],
    dropStatus: 'cancelled',
    get hint() {
      return t('tasks.board.columnHint.cancelled')
    },
  },
]

const ARCHIVE: Column = {
  key: 'archived',
  get label() {
    return t('tasks.archived')
  },
  statuses: [],
  dropStatus: null,
  get hint() {
    return t('tasks.board.columnHint.archived')
  },
}

/** Every status, for the card menu — which still offers the ones the columns fold together. */
const ALL_STATUSES: TaskStatus[] = [
  'todo',
  'pending',
  'in_progress',
  'needs_review',
  'done',
  'blocked',
  'cancelled',
]

const grouped = computed(() => {
  const columns = COLUMNS.map((column) => ({
    column,
    tasks: props.tasks.filter(
      (task) => !task.archived && column.statuses.includes(task.status),
    ),
  })).filter((entry) => entry.tasks.length || entry.column.key !== 'cancelled')
  if (props.showArchived) {
    columns.push({ column: ARCHIVE, tasks: props.tasks.filter((task) => task.archived) })
  }
  return columns
})

/** A column that folds several statuses shows each card's own on the card. */
function isMixed(column: Column): boolean {
  return column.statuses.length > 1
}

/** The card being dragged, and the column under the pointer — both purely visual state. */
const draggingId = ref<string | null>(null)
const dropTarget = ref<string | null>(null)

const menuFor = ref<string | null>(null)
const confirmingRemoval = ref<string | null>(null)
const renamingId = ref<string | null>(null)
const renameDraft = ref('')

/**
 * A running task is not moved out from under its session — the backend refuses it, and
 * greying the card here says so before the drop rather than after.
 */
function isLocked(task: TeamTask): boolean {
  return task.is_running === true
}

/**
 * The line under the card's assignee while a session is live, and the counter behind it.
 *
 * It is the task dialog's last line (`tailLine`, see the store's `activity`): the newest
 * message of the whole feed, whatever its kind — a tool result, a sub-agent frame, a note —
 * rather than only the calls, so the card and the open transcript say the same thing.
 *
 * Split into two calls returning plain values rather than one returning an object, because
 * the template needs both without a non-null assertion — and read only while the task is
 * actually running: the store keeps the last line until the status moves, and a card that
 * has finished should not still be claiming to be mid-tool-call.
 */
function activityText(task: TeamTask): string {
  return task.is_running ? (teams.activity[task.id]?.text ?? '') : ''
}

/** Changes on every update, which is what re-mounts the span and re-fires its pulse. */
function activitySeq(task: TeamTask): number {
  return teams.activity[task.id]?.seq ?? 0
}

/** Delegated work this session is still waiting on — 0 for a task that is not running. */
function subagentCount(task: TeamTask): number {
  return task.is_running ? (teams.runningSubagents[task.id] ?? 0) : 0
}

/**
 * Cards that reached Done while the board was on screen, so each can flash its edge green once
 * as it lands in the column (§7, the motion section's indication part).
 *
 * Decided from the statuses this board last saw rather than from the card: a card that moves
 * column is a new element in another `<section>`, so nothing on it could remember what it was.
 * A task the board has never seen is not an arrival — opening the page on forty finished tasks
 * announces none of them — and `animationend` takes the card out of the set again.
 */
const settling = ref<Set<string>>(new Set())
let lastSeen = new Map<string, TaskStatus>()

watch(
  () => props.tasks,
  (tasks) => {
    const arrived = tasks.filter((task) => {
      const before = lastSeen.get(task.id)
      return before !== undefined && isSuccessful(task.status) && !isSuccessful(before)
    })
    lastSeen = new Map(tasks.map((task) => [task.id, task.status]))
    if (arrived.length) {
      settling.value = new Set([...settling.value, ...arrived.map((task) => task.id)])
    }
  },
  { immediate: true },
)

function settled(taskId: string, event: AnimationEvent): void {
  if (event.target !== event.currentTarget || !settling.value.has(taskId)) return
  const next = new Set(settling.value)
  next.delete(taskId)
  settling.value = next
}

/**
 * Whether the columns animate their cards yet (`:css` on each column's `TransitionGroup`).
 *
 * Not until the board has drawn tasks once. The page mounts the board while the first read is
 * still on its way, so the tasks arriving would otherwise be forty cards entering at once — and
 * that is the page loading, which the columns already announce as its bands, not forty tasks
 * moving. The same rule as `settling` above: motion is for a change seen on screen. Flipped a
 * tick after the tasks land, so the render that draws them is still the plain one.
 */
const tracking = ref(false)

watch(
  () => props.tasks.length > 0,
  (drawn) => {
    if (drawn && !tracking.value) void nextTick(() => (tracking.value = true))
  },
  { immediate: true },
)

/**
 * Hold a leaving card at the width it had.
 *
 * It fades out of the flow (`position: absolute`, the motion section of `base.css`) so the
 * cards under it have a gap to close, and an absolutely placed box with no width shrinks to
 * its content. Measured here rather than written into the CSS as the column's padding, which
 * is this template's to change.
 */
function pinLeaving(element: Element): void {
  const card = element as HTMLElement
  card.style.width = `${card.offsetWidth}px`
}

function startDrag(event: DragEvent, task: TeamTask): void {
  if (isLocked(task)) {
    event.preventDefault()
    return
  }
  draggingId.value = task.id
  menuFor.value = null
  // `move` is what gives the cursor the right affordance; the payload itself is unused,
  // since the id is already in component state.
  event.dataTransfer?.setData('text/plain', task.id)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}

function endDrag(): void {
  draggingId.value = null
  dropTarget.value = null
}

/**
 * Whether the card in hand may land in a column. Only the archive refuses anything: a card
 * that is not in Done cannot be archived, and the backend would say so — but a drop that
 * is refused after the fact reads as a bug, where a cursor that says "no" reads as a rule.
 */
function canDropOn(column: Column): boolean {
  const task = props.tasks.find((candidate) => candidate.id === draggingId.value)
  if (!task) return false
  if (column.dropStatus === null) return task.status === 'done'
  return true
}

/** `dragover` has to be cancelled for a drop to be allowed at all, so this is the gate. */
function overColumn(event: DragEvent, column: Column): void {
  if (!canDropOn(column)) return
  event.preventDefault()
  dropTarget.value = column.key
}

/** Ignore the `dragleave` that fires on every child element the pointer crosses. */
function leaveColumn(event: DragEvent, column: Column): void {
  const element = event.currentTarget as HTMLElement
  if (element.contains(event.relatedTarget as Node | null)) return
  if (dropTarget.value === column.key) dropTarget.value = null
}

async function dropOn(column: Column): Promise<void> {
  const taskId = draggingId.value
  endDrag()
  if (!taskId) return
  const task = props.tasks.find((candidate) => candidate.id === taskId)
  if (!task) return

  if (column.dropStatus === null) {
    // Into the archive. Refused for anything but Done before the drag got here.
    if (!task.archived) await teams.updateTask(taskId, { archived: true })
    return
  }
  if (task.archived) {
    // Out of the archive, into whichever column: back on the board first, moved if the
    // column is not Done. The backend would clear the flag on a move off Done anyway;
    // sending it makes the drop into Done itself an edit rather than a no-op.
    const patch: TaskUpdate = { archived: false }
    if (task.status !== column.dropStatus) patch.status = column.dropStatus
    await teams.updateTask(taskId, patch)
    return
  }
  if (column.statuses.includes(task.status)) return
  await teams.updateTask(taskId, { status: column.dropStatus })
}

async function move(task: TeamTask, status: TaskStatus): Promise<void> {
  menuFor.value = null
  if (task.status === status) return
  await teams.updateTask(task.id, { status })
}

/** The card's own button: off the board and still on the books, or back again (§10.4). */
async function setArchived(task: TeamTask, archived: boolean): Promise<void> {
  menuFor.value = null
  if (task.archived === archived) return
  await teams.updateTask(task.id, { archived })
}

async function reassign(task: TeamTask, assigneeId: string): Promise<void> {
  menuFor.value = null
  if (assigneeId === task.assignee_id) return
  await teams.updateTask(task.id, { assignee_id: assigneeId })
}

async function remove(taskId: string): Promise<void> {
  confirmingRemoval.value = null
  menuFor.value = null
  await teams.deleteTask(taskId)
}

function startRename(task: TeamTask): void {
  renamingId.value = task.id
  renameDraft.value = task.title
}

async function commitRename(task: TeamTask): Promise<void> {
  // Enter commits and then blurs, which would otherwise send the same patch twice.
  if (renamingId.value !== task.id) return
  const title = renameDraft.value.trim()
  renamingId.value = null
  if (!title || title === task.title) return
  await teams.updateTask(task.id, { title })
}

/** `w-52`, which is what the menu is placed against its button's right edge with. */
const MENU_WIDTH = 208
/** What the menu is assumed to need below its button before it is moved up to fit. */
const MENU_HEIGHT = 340

/**
 * Where the open menu is drawn, in viewport pixels.
 *
 * The menu is teleported to `<body>` and positioned from its button, rather than sitting
 * inside the card at `absolute` coordinates as it used to. The glass cards of the deleted
 * Frutiger look made that necessary — a card that is a stacking context paints the menu under
 * the next card unless the whole card is raised — and it stays the right place: outside the
 * card it is a floating panel like any other, over everything, and no card can clip it.
 */
const menuAt = ref({ top: 0, left: 0, origin: 'top right' })

function toggleMenu(taskId: string, event: MouseEvent): void {
  confirmingRemoval.value = null
  if (menuFor.value === taskId) {
    menuFor.value = null
    return
  }
  const anchor = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const top = Math.max(8, Math.min(anchor.bottom + 4, window.innerHeight - 8 - MENU_HEIGHT))
  const left = Math.max(8, anchor.right - MENU_WIDTH)
  menuAt.value = {
    top,
    left,
    // The menu unfolds from its button (`--vxs-origin`, §7): the button's centre in the menu's
    // own frame, which is its top-right corner until the menu had to move up to fit.
    origin: `${Math.round(anchor.left + anchor.width / 2 - left)}px ${Math.round(anchor.top + anchor.height / 2 - top)}px`,
  }
  menuFor.value = taskId
}

function closeMenu(): void {
  if (menuFor.value === null) return
  menuFor.value = null
  confirmingRemoval.value = null
}

/**
 * Click-away for the card menu: one listener on the document while any menu is open.
 *
 * It used to be a `position: fixed` backdrop inside the card, which was cheaper to reason
 * about — until the card became glass under the since-deleted Frutiger look: a
 * `backdrop-filter` makes its element the containing block for every fixed descendant, so
 * that backdrop shrank to the card's own box and a click anywhere else no longer closed
 * anything. A listener cannot be broken by how a card is styled. The menu and its button both
 * carry `data-task-menu`; a press that lands outside either closes the menu before the click
 * that follows can do anything. A scroll anywhere closes it too, because the menu is
 * positioned once, from where its button was. One listener per board, not per card, and
 * it goes with the board.
 */
function onDocumentPointerDown(event: PointerEvent): void {
  if (menuFor.value === null) return
  const target = event.target as Element | null
  if (target?.closest('[data-task-menu]')) return
  closeMenu()
}

onMounted(() => {
  document.addEventListener('pointerdown', onDocumentPointerDown)
  document.addEventListener('scroll', closeMenu, true)
  window.addEventListener('resize', closeMenu)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocumentPointerDown)
  document.removeEventListener('scroll', closeMenu, true)
  window.removeEventListener('resize', closeMenu)
})
</script>

<template>
  <div class="flex h-full gap-3 overflow-x-auto p-pane">
    <!-- `vxs-sheet` is the column's material (`base.css`): a step under the surface, on an
         inset edge. The drop target is an outline rather than a ring, because a ring is a
         box-shadow and the material owns the column's box-shadow. Each column is one of the
         page's bands (`vxs-enter`), so the board arrives left to right a step apart.

         A column shares the board's width with the others between two bounds, rather than
         holding one fixed width: at the fixed 256px the four columns were a few pixels wider
         than an ordinary window and Done sat behind a scrollbar, while a wide window left half
         the board empty. Below the lower bound the board scrolls sideways as it did. -->
    <section
      v-for="{ column, tasks: columnTasks } in grouped"
      :key="column.key"
      class="vxs-sheet vxs-enter flex max-w-96 min-w-56 flex-1 flex-col rounded-lg border border-column bg-surface"
      :class="dropTarget === column.key ? 'outline-1 -outline-offset-1 outline-accent' : ''"
      @dragover="overColumn($event, column)"
      @dragleave="leaveColumn($event, column)"
      @drop.prevent="dropOn(column)"
    >
      <!-- A single-status column is named by the same pill every other surface uses for
           that status — a task's badge and its column read alike (§7). A column that folds
           several is named in words, with the three dots of what it holds; the archive is
           named by its icon, because it is not a status at all. -->
      <header class="flex items-center gap-2 px-3 pt-3 pb-1" :title="column.hint">
        <StatusBadge
          v-if="column.statuses.length === 1"
          :status="column.statuses[0]!"
          quiet
        />
        <!-- The same outlined pill as `StatusBadge` draws, with the dots of every status it
             folds in place of one. -->
        <span
          v-else
          class="vxs-badge inline-flex items-center gap-1.5 rounded-pill px-2 py-0.5 text-small font-medium text-muted"
        >
          <Archive v-if="column.dropStatus === null" class="size-3" />
          <span v-else class="flex items-center gap-0.5">
            <span
              v-for="status in column.statuses"
              :key="status"
              class="size-1.5 rounded-full"
              :style="{ background: statusColor(status) }"
            />
          </span>
          {{ column.label }}
        </span>
        <span class="ml-auto text-small text-faint">{{ columnTasks.length }}</span>
      </header>

      <!-- A `TransitionGroup` (`vxs-tile-list-*`, the motion section of `base.css`): a card that
           lands in the column comes up into its place and the cards around a gap close it, so a
           status changing under the reader's eye reads as a card moving. `relative`, because
           the card that left is taken out of the flow while it fades and this is the box it is
           placed in. Nothing plays when the board first draws: a group does not animate its
           first render, `tracking` covers tasks that arrive after it, and the column itself is
           the band that arrives. -->
      <TransitionGroup
        tag="div"
        name="vxs-tile-list"
        :css="tracking"
        class="relative min-h-0 flex-1 space-y-2.5 overflow-y-auto p-2.5"
        @before-leave="pinLeaving"
      >
        <!-- `vxs-tile` is the card's material (`base.css`) — a ring over the card fill, brighter
             under the pointer — and `vxs-tile-active` edges the selected one in the accent.
             Rounded like the column itself rather than concentric with it, which on a
             10px-padded column was a 2px corner. A card whose session is live breathes the
             running amber on its edge (`vxs-tile-live`), and one that has just landed in Done
             flashes green once (`vxs-tile-settle`, see `settling`). -->
        <div
          v-for="task in columnTasks"
          :key="task.id"
          class="vxs-tile group relative rounded-lg border"
          :class="[
            selectedId === task.id ? 'vxs-tile-active' : '',
            draggingId === task.id ? 'opacity-40' : '',
            task.is_running ? 'vxs-tile-live' : settling.has(task.id) ? 'vxs-tile-settle' : '',
          ]"
          :draggable="!isLocked(task) && renamingId !== task.id"
          @dragstart="startDrag($event, task)"
          @dragend="endDrag"
          @animationend="settled(task.id, $event)"
        >
          <div
            class="cursor-pointer p-3"
            role="button"
            tabindex="0"
            @click="emit('select', task.id)"
            @keydown.enter.self="emit('select', task.id)"
          >
            <!-- The rename box alone keeps clear of the actions button in the corner:
                 renaming starts with a double-click on the card, so the pointer is on it and
                 the button is showing, and it would sit over the end of the box. -->
            <div v-if="renamingId === task.id" class="pr-4">
              <input
                v-model="renameDraft"
                v-select-on-mount
                class="w-full rounded-sm border border-accent bg-inset px-1 py-0.5 font-medium outline-none"
                @click.stop
                @keydown.enter.prevent="commitRename(task)"
                @keydown.esc.prevent="renamingId = null"
                @blur="commitRename(task)"
              />
            </div>
            <!-- The id sits above the title rather than beside it: a card is 256px wide
                 and a two-line title is normal, so sharing the line would push the id off
                 or clip the title. It is text, never an input — the number is assigned on
                 creation and no route can change it (§10.2). In a column that folds
                 several statuses the card's own badge sits on this line, because there the
                 column header cannot say which of them this card is in. The badge ends at the
                 card's 12px padding like every row under it, so the actions button, shown on
                 hover, lies over its corner — chosen over a row cut short for the button. -->
            <div v-else class="flex items-center gap-2">
              <span class="font-mono text-small text-faint">{{ task.display_id }}</span>
              <!-- How the work ended, beside the status it ended in (§10.4): the reviewer
                   reads the colour before opening the card. -->
              <span
                v-if="task.status === 'needs_review' && task.result_type"
                class="ml-auto size-2 shrink-0 rounded-full"
                :style="{ background: resultTypeColor(task.result_type) }"
                :title="resultTypeLabel(task.result_type)"
                :aria-label="resultTypeLabel(task.result_type)"
              />
              <StatusBadge
                v-if="isMixed(column)"
                :status="task.status"
                :class="task.status === 'needs_review' && task.result_type ? '' : 'ml-auto'"
                :title="statusLabel(task.status)"
              />
            </div>
            <p
              v-if="renamingId !== task.id"
              class="line-clamp-2 font-medium"
              :title="t('tasks.board.card.renameHint')"
              @dblclick.stop="startRename(task)"
            >
              {{ task.title }}
            </p>

            <p class="mt-1 flex items-center gap-1.5 text-small text-faint">
              <MemberMark :member="teams.memberById.get(task.assignee_id) ?? null" />
              <span v-if="teams.isHuman(task.assignee_id)" :title="t('tasks.board.card.person')">
                <User class="size-3 shrink-0" />
              </span>
              <span class="max-w-[50%] shrink-0 truncate">
                {{ teams.memberById.get(task.assignee_id)?.name ?? task.assignee_id }}
              </span>
              <span
                v-if="task.depends_on.length"
                class="shrink-0"
                :title="t('tasks.board.card.waitsFor', { count: task.depends_on.length })"
              >
                <Link2 class="size-3" />
              </span>
              <!-- Sub-agents and workflows this session is waiting on (§10.3). Live only,
                   like the line under this row: it says the session is held open, which is
                   the one thing a card cannot otherwise show about a run that has stopped
                   speaking for itself. -->
              <span
                v-if="subagentCount(task)"
                class="vxs-subagent-running flex shrink-0 items-center gap-0.5"
                :style="{ color: statusColor('in_progress') }"
                :title="t('tasks.board.card.subagents', { count: subagentCount(task) })"
              >
                <Bot class="size-3" />
                {{ subagentCount(task) }}
              </span>
            </p>
            <TokenUsageChip v-if="task.total_tokens > 0 || task.total_cost_usd > 0" class="mt-1"
              :input-tokens="usageById.get(task.id)?.inputTokens ?? null"
              :output-tokens="usageById.get(task.id)?.outputTokens ?? null"
              :cached-tokens="usageById.get(task.id)?.cacheReadTokens ?? null"
              :cost-usd="task.total_cost_usd" />

            <!-- The transcript's last line, drawn the way the task dialog draws it under its
                 feed: a row of its own under the assignee, like the Archive button below,
                 because squeezed between the name and the cost it was a few characters wide.
                 The span is re-keyed on the store's counter so it is a new element on every
                 line: a CSS animation restarts when the element does, and the flash is the
                 part that says the session is alive. -->
            <p v-if="activityText(task)" class="mt-1 flex text-small text-faint">
              <span
                :key="activitySeq(task)"
                class="vxs-task-activity min-w-0 truncate font-mono"
                :title="activityText(task)"
              >
                {{ activityText(task) }}
              </span>
            </p>
            <p v-if="task.error" class="mt-1 line-clamp-2 text-small text-danger">
              {{ task.error }}
            </p>

            <!-- The way off the board, on the card and only in Done (§10.4). A row of its
                 own rather than an icon in the corner: the corner is where the menu lives,
                 and this is the one action a finished card is for. The archived card
                 carries the way back in the same place. -->
            <button
              v-if="task.status === 'done' && !task.archived"
              class="mt-2 flex w-full items-center justify-center gap-1 rounded-sm border border-line-subtle py-0.5 text-small text-faint hover:bg-hover hover:text-primary disabled:opacity-50"
              :disabled="teams.busy"
              :title="t('tasks.board.card.archiveHint')"
              @click.stop="setArchived(task, true)"
            >
              <Archive class="size-3" /> {{ t('tasks.board.card.archive') }}
            </button>
            <button
              v-else-if="task.archived"
              class="mt-2 flex w-full items-center justify-center gap-1 rounded-sm border border-line-subtle py-0.5 text-small text-faint hover:bg-hover hover:text-primary disabled:opacity-50"
              :disabled="teams.busy"
              :title="t('tasks.board.card.unarchiveHint')"
              @click.stop="setArchived(task, false)"
            >
              <ArchiveRestore class="size-3" /> {{ t('tasks.board.card.unarchive') }}
            </button>
          </div>

          <span
            class="absolute top-1.5 -left-1 hidden text-faint group-hover:block"
            :class="isLocked(task) ? 'opacity-30' : 'cursor-grab'"
            :title="isLocked(task) ? t('tasks.board.card.locked') : t('tasks.board.card.drag')"
          >
            <GripVertical class="size-3" />
          </span>
          <button
            class="absolute top-1 right-1 grid size-5 place-items-center rounded-sm text-faint opacity-0 group-hover:opacity-100 hover:bg-hover hover:text-primary"
            :class="menuFor === task.id ? 'opacity-100' : ''"
            :title="t('tasks.board.card.actions')"
            data-task-menu
            @click.stop="toggleMenu(task.id, $event)"
          >
            <MoreHorizontal class="size-3.5" />
          </button>

          <!-- Teleported and placed from the button (see `menuAt`), closed by the document
               listener (`onDocumentPointerDown`) rather than by a backdrop of its own. A
               floating menu, so it wears `vxs-card` like the rest. -->
          <Teleport v-if="menuFor === task.id" to="body">
            <div
              class="vxs-card fixed z-40 max-h-[calc(100vh-16px)] w-52 overflow-y-auto rounded-md border border-line bg-surface p-1 shadow-lg"
              :style="{ top: `${menuAt.top}px`, left: `${menuAt.left}px`, '--vxs-origin': menuAt.origin }"
              data-task-menu
            >
              <p class="px-2 py-1 text-small font-semibold text-faint uppercase">
                {{ t('tasks.board.menu.moveTo') }}
              </p>
              <button
                v-for="status in ALL_STATUSES"
                :key="status"
                class="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left hover:bg-hover disabled:opacity-40"
                :disabled="teams.busy || (isLocked(task) && status !== task.status)"
                @click="move(task, status)"
              >
                <span
                  class="size-1.5 shrink-0 rounded-full"
                  :style="{ background: statusColor(status) }"
                />
                <span class="truncate">{{ statusLabel(status) }}</span>
                <span v-if="task.status === status" class="ml-auto text-small text-faint">
                  {{ t('tasks.board.menu.current') }}
                </span>
              </button>

              <p
                class="mt-1 border-t border-line-subtle px-2 pt-1.5 text-small font-semibold text-faint uppercase"
              >
                {{ t('tasks.assignTo') }}
              </p>
              <select
                :value="task.assignee_id"
                class="mb-1 w-full rounded-sm border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
                :disabled="teams.busy || isLocked(task)"
                @change="reassign(task, ($event.target as HTMLSelectElement).value)"
              >
                <option v-for="member in teams.members" :key="member.id" :value="member.id">
                  {{
                    member.kind === 'human'
                      ? t('tasks.board.menu.person', { name: member.name })
                      : member.name
                  }}
                </option>
              </select>

              <div class="border-t border-line-subtle pt-1">
                <button
                  v-if="confirmingRemoval !== task.id"
                  class="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left text-danger hover:bg-danger-soft"
                  @click="confirmingRemoval = task.id"
                >
                  <Trash2 class="size-3.5" /> {{ t('tasks.board.menu.delete') }}
                </button>
                <template v-else>
                  <p class="px-2 py-1 text-small text-faint">
                    {{ t('tasks.board.menu.deleteWarning') }}
                  </p>
                  <div class="flex gap-1 px-1 pb-1">
                    <button
                      class="flex-1 rounded-sm bg-danger px-2 py-1 font-medium text-on-accent disabled:opacity-50"
                      :disabled="teams.busy"
                      @click="remove(task.id)"
                    >
                      {{ t('common.delete') }}
                    </button>
                    <button
                      class="flex-1 rounded-sm border border-line px-2 py-1 hover:bg-hover"
                      @click="confirmingRemoval = null"
                    >
                      {{ t('tasks.board.menu.keep') }}
                    </button>
                  </div>
                </template>
              </div>
            </div>
          </Teleport>
        </div>

        <p v-if="!columnTasks.length" key="empty" class="px-1 py-2 text-small text-faint">
          {{
            draggingId
              ? canDropOn(column)
                ? t('tasks.board.dropHere')
                : t('tasks.board.onlyDone')
              : t('tasks.board.nothingHere')
          }}
        </p>
      </TransitionGroup>
    </section>
  </div>
</template>
