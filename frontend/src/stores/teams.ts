import { upsertTranscript } from '@/lib/transcript'
/**
 * Team state: the org tree, the task board and the live agent transcript (spec §10.4).
 *
 * One store for two pages — Teams edits the roster, Tasks runs the board — because they
 * read the same `GET /api/teams/{id}` payload and the same `team.*` events. Splitting the
 * state would mean two subscriptions and two copies of the member map.
 *
 * On acting identity: the backend's routing rules treat a missing actor as "the user",
 * who may assign to anyone and review anything (`check_can_assign` /`check_can_review`
 * both return early when the actor is `None`). The Studio is a single-user local tool
 * with no accounts, so the UI acts as the user by default, and only names an actor when
 * someone deliberately delegates *as* a manager — which is what exercises the
 * management vertical the spec describes.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import { isActive } from '@/lib/status'
import type { LifecycleMessage } from '@/lib/subagents'
import { trackDelegated } from '@/lib/subagents'
import { tailLine } from '@/lib/transcript'
import type {
  AgentMessage,
  TaskResultType,
  MemberCreate,
  MemberOptions,
  MemberUpdate,
  TaskDetail,
  TaskUpdate,
  TeamDetail,
  TeamMember,
  TeamTask,
} from '@/api/types'
import { onEvent } from '@/ws/connection'

export interface OrgNode {
  member: TeamMember
  reports: OrgNode[]
}

/** The newest line of a task's feed, and a counter that changes with every line. */
export interface TaskActivity {
  text: string
  seq: number
}

/** Every move a task can be put through, agent or human. */
export type TaskAction =
  | 'start'
  | 'reset'
  | 'compress'
  | 'approve'
  | 'request-changes'
  | 'cancel'
  | 'claim'
  | 'complete'
  | 'note'

export const useTeamsStore = defineStore('teams', () => {
  const teams = ref<TeamDetail[]>([])
  const activeTeamId = ref<string | null>(null)
  const openTask = ref<TaskDetail | null>(null)
  const editingMemberId = ref<string | null>(null)
  const options = ref<MemberOptions | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const busy = ref(false)
  const compressingTaskId = ref<string | null>(null)
  const cancellingCompressionTaskId = ref<string | null>(null)

  /**
   * The last line of each running task's feed, keyed by task id (§10.4).
   *
   * The same line the task dialog draws under its transcript — `tailLine`, the newest
   * message whatever its kind, as Verbose would log it — so a card and the open task never
   * disagree about what just happened. Live only, and deliberately not fetched: it exists to
   * say "this session is still moving", and a line rebuilt on load would claim activity that
   * may have stopped hours ago. A reload therefore shows nothing until the next message,
   * which for a working agent is seconds. `seq` is the half the card watches — it changes on
   * every update, so the pulse fires again even when the same line arrives twice.
   */
  const activity = ref<Record<string, TaskActivity>>({})

  /**
   * The sub-agents and workflows each running task currently has in flight (§10.3).
   *
   * Live only, for the same reason `activity` is: it answers "what is this session waiting
   * on right now", and a count rebuilt on load would describe work that died with the run
   * that started it. An open task does not read this — `lib/subagents.ts` folds the whole
   * list out of its transcript, which is durable — so this exists purely so a card on the
   * board can say a session is busy delegating without the transcript being on screen.
   *
   * Ids rather than a number: a task's completion frame can arrive twice (a notification
   * and a `task_updated` patch both report it), and counting down on each would go
   * negative on the second.
   */
  const subagentIds = ref<Record<string, string[]>>({})

  /** How many pieces of delegated work a card should say are running. */
  const runningSubagents = computed<Record<string, number>>(() =>
    Object.fromEntries(
      Object.entries(subagentIds.value).map(([taskId, ids]) => [taskId, ids.length]),
    ),
  )

  function forgetSubagents(taskId: string): void {
    if (subagentIds.value[taskId]) delete subagentIds.value[taskId]
  }

  /** Fold one lifecycle frame into the live set. Anything else passes through untouched. */
  function trackSubagents(payload: LifecycleMessage & { task_id: string }): void {
    const seen = trackDelegated(subagentIds.value[payload.task_id] ?? [], payload)
    if (seen === null) return
    if (seen.length) subagentIds.value[payload.task_id] = seen
    else forgetSubagents(payload.task_id)
  }

  let subscribed = false

  const team = computed(
    () => teams.value.find((candidate) => candidate.id === activeTeamId.value) ?? teams.value[0] ?? null,
  )
  const members = computed(() => team.value?.members ?? [])
  const tasks = computed(() => team.value?.tasks ?? [])
  const taskById = computed(() => new Map(tasks.value.map(task => [task.id, task])))

  /**
   * What the finished work has cost, in dollars — the figure over the board (§10.4).
   *
   * Only `done` counts. Work still in flight would move the total under the reader while
   * they look at it, and a cancelled task's spend bought the project nothing; what this
   * answers is "what did the results so far cost", which is the number worth a header.
   */
  const doneCost = computed(() =>
    tasks.value.reduce(
      (total, task) => (task.status === 'done' ? total + task.total_cost_usd : total),
      0,
    ),
  )

  const memberById = computed(() => {
    const map = new Map<string, TeamMember>()
    for (const member of members.value) map.set(member.id, member)
    return map
  })

  /** The management vertical, built from `manager_id` self-references. */
  const orgTree = computed<OrgNode[]>(() => {
    const nodes = new Map<string, OrgNode>()
    for (const member of members.value) nodes.set(member.id, { member, reports: [] })

    const roots: OrgNode[] = []
    for (const node of nodes.values()) {
      const parent = node.member.manager_id ? nodes.get(node.member.manager_id) : undefined
      if (parent) parent.reports.push(node)
      else roots.push(node)
    }

    const sort = (list: OrgNode[]) => {
      list.sort((a, b) => a.member.sort_order - b.member.sort_order)
      for (const node of list) sort(node.reports)
    }
    sort(roots)
    return roots
  })

  /**
   * The two halves of the board (§10.4). `tasks` is everything the team has, which is what
   * the cost figures sum; `boardTasks` is what the columns draw, and `archivedTasks` what
   * the Archived column shows when it is asked for. Archiving is a `PATCH` like any other
   * edit — the backend refuses it off Done and clears it on a move off Done.
   */
  const boardTasks = computed(() => tasks.value.filter((task) => !task.archived))
  const archivedTasks = computed(() => tasks.value.filter((task) => task.archived))
  const startableTasks = computed(() => boardTasks.value.filter((task) =>
    ['todo', 'pending'].includes(task.status) && !task.is_running &&
    memberById.value.get(task.assignee_id)?.kind === 'agent',
  ))

  function tasksFor(memberId: string): TeamTask[] {
    return tasks.value.filter((task) => task.assignee_id === memberId)
  }

  function isHuman(memberId: string | null | undefined): boolean {
    return memberById.value.get(memberId ?? '')?.kind === 'human'
  }

  /** Who a member may hand work to — the vertical plus their sideways routes (§10.4). */
  function assignableFrom(memberId: string | null): TeamMember[] {
    if (!memberId) return members.value
    const actor = memberById.value.get(memberId)
    if (!actor) return []
    const sideways = new Set(actor.can_assign_to ?? [])
    return members.value.filter(
      (candidate) =>
        candidate.id !== actor.id &&
        (candidate.manager_id === actor.id ||
          actor.manager_id === candidate.id ||
          sideways.has('*') ||
          sideways.has(candidate.id)),
    )
  }

  /** Tasks a human still has to act on: unclaimed, or claimed and unfinished. */
  function inboxFor(memberId: string): TeamTask[] {
    return tasksFor(memberId).filter((task) =>
      ['todo', 'pending', 'in_progress', 'blocked'].includes(task.status),
    )
  }

  /** Who may review a given task, for the "acting as" hint in the UI. */
  function managerOf(memberId: string): TeamMember | null {
    const member = memberById.value.get(memberId)
    if (!member?.manager_id) return null
    return memberById.value.get(member.manager_id) ?? null
  }

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      // The list endpoint carries members but not tasks, so seed with an empty board
      // and let `refreshTeam` fill in the one the user is actually looking at.
      const list = await api.teams()
      teams.value = list.map((entry) => ({ ...entry, tasks: [] }))
      if (!activeTeamId.value && teams.value.length) activeTeamId.value = teams.value[0]!.id
      await refreshTeam()
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
    } finally {
      loading.value = false
    }
  }

  async function refreshTeam(): Promise<void> {
    if (!team.value) return
    const updated = await api.team(team.value.id)
    teams.value = teams.value.map((candidate) =>
      candidate.id === updated.id ? updated : candidate,
    )
  }

  /** Board-wide release is server-owned so it also works with the browser closed (§10.4). */
  async function setAutostartTasks(enabled: boolean): Promise<boolean> {
    if (!team.value || busy.value) return false
    busy.value = true
    error.value = null
    try {
      const updated = await api.setAutostartTasks(team.value.id, enabled)
      teams.value = teams.value.map((entry) => entry.id === updated.id ? updated : entry)
      return true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      return false
    } finally {
      busy.value = false
    }
  }

  async function startAllTasks(): Promise<boolean> {
    if (!team.value || busy.value) return false
    busy.value = true
    error.value = null
    try {
      await api.startAllTasks(team.value.id)
      await refreshTeam()
      return true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      return false
    } finally {
      busy.value = false
    }
  }

  // ---------------------------------------------------------------- member editing

  const editingMember = computed(() =>
    editingMemberId.value ? (memberById.value.get(editingMemberId.value) ?? null) : null,
  )

  /**
   * Members that cannot be someone's manager: themselves, plus everyone beneath them.
   * The backend refuses a circular reporting line; this keeps the UI from offering one.
   */
  function managerCandidates(memberId: string): TeamMember[] {
    const descendants = new Set<string>([memberId])
    let grew = true
    while (grew) {
      grew = false
      for (const candidate of members.value) {
        if (
          candidate.manager_id &&
          descendants.has(candidate.manager_id) &&
          !descendants.has(candidate.id)
        ) {
          descendants.add(candidate.id)
          grew = true
        }
      }
    }
    return members.value.filter((candidate) => !descendants.has(candidate.id))
  }

  async function loadOptions(): Promise<void> {
    if (options.value) return
    try {
      options.value = await api.memberOptions()
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
    }
  }

  function editMember(memberId: string | null): void {
    editingMemberId.value = memberId
    if (memberId) void loadOptions()
  }

  /** Saving writes the team back to YAML, so `refreshTeam` also picks up the round-trip. */
  async function saveMember(memberId: string, patch: MemberUpdate): Promise<boolean> {
    if (!team.value) return false
    busy.value = true
    error.value = null
    try {
      await api.updateMember(team.value.id, memberId, patch)
      await refreshTeam()
      return true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      return false
    } finally {
      busy.value = false
    }
  }

  /**
   * Persist where the org chart's nodes sit — one request for a whole move, since a drag
   * can carry a selection and each member write rewrites the team's YAML.
   *
   * Deliberately does not raise `busy`: this fires on every drop while the user is
   * arranging the chart, and greying out the editor's buttons on each one would make the
   * page feel like it is saving something important rather than remembering a position.
   */
  async function saveLayout(
    positions: Record<string, { x: number; y: number } | null>,
  ): Promise<void> {
    if (!team.value || !Object.keys(positions).length) return
    try {
      await api.saveLayout(team.value.id, positions)
      await refreshTeam()
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
    }
  }

  async function addMember(input: MemberCreate): Promise<TeamMember | null> {
    if (!team.value) return null
    busy.value = true
    error.value = null
    try {
      const created = await api.createMember(team.value.id, input)
      await refreshTeam()
      editMember(created.id)
      return created
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      return null
    } finally {
      busy.value = false
    }
  }

  async function removeMember(memberId: string): Promise<boolean> {
    if (!team.value) return false
    busy.value = true
    error.value = null
    try {
      await api.deleteMember(team.value.id, memberId)
      if (editingMemberId.value === memberId) editingMemberId.value = null
      await refreshTeam()
      return true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      return false
    } finally {
      busy.value = false
    }
  }

  async function selectTask(taskId: string | null): Promise<void> {
    if (!taskId || !team.value) {
      openTask.value = null
      return
    }
    // The open task and the member being edited are on different pages now (Tasks and
    // Teams), each with its own rail, so neither closes the other: coming back to Teams
    // after looking at a task should find the editor exactly as it was left.
    openTask.value = await api.task(team.value.id, taskId)
  }

  /**
   * Edit a task's own fields, or move it between columns.
   *
   * One call for both because the backend takes one patch: dragging a card and renaming it
   * are the same operation with different keys, and the history entry the backend writes is
   * what makes either of them visible afterwards.
   */
  async function updateTask(
    taskId: string,
    patch: TaskUpdate,
    actorId: string | null = null,
  ): Promise<boolean> {
    if (!team.value) return false
    busy.value = true
    error.value = null
    try {
      await api.updateTask(team.value.id, taskId, { ...patch, actor_id: actorId })
      await refreshTeam()
      if (openTask.value?.id === taskId) await selectTask(taskId)
      return true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      // The card was moved optimistically by the drag; put the board back as the server
      // still sees it, or a refused move leaves the UI claiming something that never happened.
      await refreshTeam()
      return false
    } finally {
      busy.value = false
    }
  }

  async function deleteTask(taskId: string, actorId: string | null = null): Promise<boolean> {
    if (!team.value) return false
    busy.value = true
    error.value = null
    try {
      await api.deleteTask(team.value.id, taskId, actorId)
      if (openTask.value?.id === taskId) openTask.value = null
      await refreshTeam()
      return true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      return false
    } finally {
      busy.value = false
    }
  }

  async function createTask(input: {
    attachments?: string[]
    assigneeId: string
    title: string
    brief: string
    createdBy?: string | null
    dependsOn?: string[]
    autoStart: boolean
    /** Overrides for this task alone; null on either leaves it to the assignee (§10.3). */
    model?: string | null
    effort?: string | null
  }): Promise<void> {
    if (!team.value) return
    busy.value = true
    error.value = null
    try {
      await api.createTask(team.value.id, {
        assignee_id: input.assigneeId,
        title: input.title,
        brief: input.brief,
        attachments: input.attachments ?? [],
        created_by: input.createdBy ?? null,
        depends_on: input.dependsOn ?? [],
        auto_start: input.autoStart,
        model: input.model ?? null,
        effort: input.effort ?? null,
      })
      await refreshTeam()
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    } finally {
      busy.value = false
    }
  }

  /**
   * One entry point for every move on a task.
   *
   * `actorId` names who is acting. It stays null for the user (who the backend lets do
   * anything), and carries a member id when a *human member* acts as themselves — which
   * is what puts their name, rather than "You", on the transcript entry.
   */
  async function act(
    action: TaskAction,
    taskId: string,
    text = '',
    actorId: string | null = null,
    attachments: string[] = [],
    resultType: TaskResultType = 'done',
  ): Promise<boolean> {
    if (!team.value) return false
    busy.value = true
    if (action === 'compress') compressingTaskId.value = taskId
    error.value = null
    try {
      const teamId = team.value.id
      if (action === 'start') await api.startTask(teamId, taskId)
      else if (action === 'reset') await api.resetTask(teamId, taskId, actorId)
      else if (action === 'compress') await api.compressTask(teamId, taskId, actorId)
      else if (action === 'approve') await api.approveTask(teamId, taskId, text, actorId)
      else if (action === 'request-changes')
        await api.requestChanges(teamId, taskId, text, actorId, attachments)
      else if (action === 'claim') await api.claimTask(teamId, taskId, actorId)
      else if (action === 'complete')
        await api.completeTask(teamId, taskId, text, actorId, resultType)
      else if (action === 'note') await api.addNote(teamId, taskId, text, actorId)
      else await api.cancelTask(teamId, taskId)
      await refreshTeam()
      if (openTask.value?.id === taskId) await selectTask(taskId)
      return true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      return false
    } finally {
      if (action === 'compress') compressingTaskId.value = null
      busy.value = false
    }
  }

  async function cancelCompression(taskId: string): Promise<boolean> {
    if (!team.value || cancellingCompressionTaskId.value) return false
    cancellingCompressionTaskId.value = taskId
    try {
      // Compress owns `busy` until its request settles; cancellation must remain usable
      // during that request and must not release its guard prematurely (§10.4).
      await api.cancelTask(team.value.id, taskId)
      await refreshTeam()
      if (openTask.value?.id === taskId) await selectTask(taskId)
      return true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      return false
    } finally {
      cancellingCompressionTaskId.value = null
    }
  }

  /**
   * Switch Remote Control on or off for a task's running session (§10.8).
   *
   * Separate from `act` rather than a seventh `TaskAction`: it is not a move on the task —
   * the status, the transcript position and the review flow are all untouched — and it is
   * the one call whose *answer* the caller needs, since a refusal here (no claude.ai
   * subscription auth, organisation policy) is the whole information.
   */
  async function remoteControl(taskId: string, enabled: boolean): Promise<boolean> {
    if (!team.value) return false
    busy.value = true
    error.value = null
    try {
      if (enabled) await api.enableRemoteControl(team.value.id, taskId)
      else await api.disableRemoteControl(team.value.id, taskId)
      // The link lives on the task, so the rail is refetched rather than patched — same
      // reason as a status change: the backend also writes a transcript entry for it.
      if (openTask.value?.id === taskId) await selectTask(taskId)
      return true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      return false
    } finally {
      busy.value = false
    }
  }

  function subscribe(): void {
    if (subscribed) return
    subscribed = true

    onEvent('team.updated', (payload) => {
      teams.value = teams.value.map((entry) => entry.id === payload.team_id
        ? { ...entry, autostart_tasks: payload.autostart_tasks }
        : entry)
    })

    onEvent('team.task.created', () => {
      void refreshTeam()
    })

    onEvent('team.task.status_changed', (payload) => {
      // A task that is no longer running has no current action, and the last one it took
      // is history the transcript keeps. Leaving the line behind would have a finished
      // card claiming to be mid-tool-call.
      if (!isActive(payload.status)) {
        delete activity.value[payload.task_id]
        forgetSubagents(payload.task_id)
      }

      // Patch the board in place so a status change does not reshuffle the whole view,
      // then reconcile the fields the event does not carry (cost, turns, timestamps).
      teams.value = teams.value.map((candidate) =>
        candidate.id !== payload.team_id
          ? candidate
          : {
              ...candidate,
              tasks: candidate.tasks.map((task) =>
                task.id === payload.task_id
                  ? { ...task, status: payload.status, error: payload.error }
                  : task,
              ),
            },
      )
      // Refetching the open task, not patching it: the event carries the status and nothing
      // else, and the field that changes *with* the status is `result_summary`. Patching
      // alone flipped the rail to "needs review" while it still showed the summary from the
      // fetch before — on a first run, none at all; on a resume, the previous run's.
      if (openTask.value?.id === payload.task_id) {
        openTask.value = { ...openTask.value, status: payload.status, error: payload.error }
        void selectTask(payload.task_id)
      }
      void refreshTeam()
    })

    onEvent('team.task.updated', (payload) => {
      void refreshTeam()
      if (openTask.value?.id === payload.task_id) void selectTask(payload.task_id)
    })

    onEvent('team.task.deleted', (payload) => {
      delete activity.value[payload.task_id]
      forgetSubagents(payload.task_id)
      if (openTask.value?.id === payload.task_id) openTask.value = null
      void refreshTeam()
    })

    onEvent('team.member.message', (payload) => {
      const message: AgentMessage = {
        id: payload.id,
        task_id: payload.task_id,
        member_id: payload.member_id,
        role: payload.role,
        kind: payload.kind,
        text: payload.text,
        data: payload.data,
        created_at: payload.created_at,
      }

      // Every task's messages arrive here, not just the open one's: this is the only place
      // the board learns that a card it is not showing a transcript for is still moving.
      const line = tailLine(message)
      if (line) {
        const previous = activity.value[payload.task_id]
        activity.value[payload.task_id] = { text: line, seq: (previous?.seq ?? 0) + 1 }
      }
      trackSubagents(payload)

      if (openTask.value?.id !== payload.task_id) return
      openTask.value = {
        ...openTask.value,
        messages: upsertTranscript(openTask.value.messages, message),
      }
    })
  }

  return {
    teams,
    activeTeamId,
    openTask,
    editingMemberId,
    editingMember,
    options,
    loading,
    error,
    busy,
    compressingTaskId,
    cancellingCompressionTaskId,
    team,
    members,
    tasks,
    taskById,
    boardTasks,
    archivedTasks,
    startableTasks,
    setAutostartTasks,
    startAllTasks,
    doneCost,
    activity,
    runningSubagents,
    memberById,
    orgTree,
    tasksFor,
    inboxFor,
    isHuman,
    assignableFrom,
    managerCandidates,
    managerOf,
    loadOptions,
    editMember,
    saveMember,
    saveLayout,
    addMember,
    removeMember,
    load,
    refreshTeam,
    selectTask,
    createTask,
    updateTask,
    deleteTask,
    act,
    cancelCompression,
    remoteControl,
    subscribe,
  }
})
