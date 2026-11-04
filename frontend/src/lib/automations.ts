/**
 * The Automations page's data half (§6.5): how an automation reads as a sentence, what a blank
 * form holds, how a path is spelled, and how a notification words itself when its author left
 * the texts empty. No Vue and no DOM, so it runs under vitest's node environment.
 *
 * An automation is one trigger and one action, and the page writes both as the user said them
 * — "On task → Status changed to: Needs review → Send notification" — so the parts are
 * produced here once and the card, the dialog's summary and the log all read alike.
 */

import type {
  Automation,
  AutomationAction,
  AutomationActionKind,
  AutomationInput,
  AutomationNotification,
  AutomationPluginAction,
  AutomationRun,
  AutomationTrigger,
  AutomationTriggerKind,
  PipelineSummary,
  TaskStatus,
} from '@/api/types'
import { t, type MessageKey } from '@/i18n'
import { parseUtc, statusLabel } from '@/lib/status'

/** The board's statuses in the board's order — what "changed to" may name. */
export const TASK_STATUSES: readonly TaskStatus[] = [
  'todo',
  'pending',
  'in_progress',
  'needs_review',
  'blocked',
  'done',
  'cancelled',
]

export const CHANGE_KINDS = ['added', 'modified', 'deleted'] as const

export const TRIGGER_KINDS: readonly AutomationTriggerKind[] = [
  'task.status_changed',
  'task.created',
  'path.changed',
  'run.finished',
]

export const ACTION_KINDS: readonly AutomationActionKind[] = ['notify', 'pipeline.run', 'plugin.action']

/**
 * What a trigger watches. The dialog's When is two picks, in the order the sentence reads —
 * the category, then the event within it: Task → Status changed, Pipeline → Run finished.
 * The file stores the kind alone (`task.status_changed`) and the category is read off it here,
 * so the split is presentation and the YAML keeps its shape.
 */
export type TriggerCategory = 'task' | 'directory' | 'pipeline'

export const TRIGGER_CATEGORIES: readonly TriggerCategory[] = ['task', 'directory', 'pipeline']

/** Every kind belongs to exactly one category. */
export const CATEGORY_OF: Record<AutomationTriggerKind, TriggerCategory> = {
  'task.status_changed': 'task',
  'task.created': 'task',
  'path.changed': 'directory',
  'run.finished': 'pipeline',
}

/** A category's events, in `TRIGGER_KINDS` order — the first is what picking the category lands on. */
export function triggerKindsOf(category: TriggerCategory): AutomationTriggerKind[] {
  return TRIGGER_KINDS.filter((kind) => CATEGORY_OF[kind] === category)
}

/** Label tables hold keys, never strings: `t()` has to run while rendering (§6.4). */
export const CATEGORY_LABELS: Record<TriggerCategory, MessageKey> = {
  task: 'automations.trigger.category.task',
  directory: 'automations.trigger.category.directory',
  pipeline: 'automations.trigger.category.pipeline',
}

/** The second pick, named without its category — the first pick already said it. */
export const EVENT_LABELS: Record<AutomationTriggerKind, MessageKey> = {
  'task.status_changed': 'automations.trigger.event.statusChanged',
  'task.created': 'automations.trigger.event.created',
  'path.changed': 'automations.trigger.event.filesChanged',
  'run.finished': 'automations.trigger.event.runFinished',
}

export const ACTION_LABELS: Record<AutomationActionKind, MessageKey> = {
  notify: 'automations.action.notify',
  'pipeline.run': 'automations.action.runPipeline',
  'plugin.action': 'automations.action.plugin',
}

/** The sentence's first part is the category as well: "On task". */
const SUBJECTS: Record<TriggerCategory, MessageKey> = {
  task: 'automations.subject.task',
  directory: 'automations.subject.directory',
  pipeline: 'automations.subject.pipeline',
}

const CHANGE_LABELS: Record<(typeof CHANGE_KINDS)[number], MessageKey> = {
  added: 'automations.change.added',
  modified: 'automations.change.modified',
  deleted: 'automations.change.deleted',
}

export function changeLabel(kind: (typeof CHANGE_KINDS)[number]): string {
  return t(CHANGE_LABELS[kind])
}

/** A trigger with every field at its default — what picking a kind in the dialog starts from. */
export function blankTrigger(kind: AutomationTriggerKind): AutomationTrigger {
  switch (kind) {
    case 'task.status_changed':
      return { kind, status: 'needs_review' }
    case 'task.created':
      return { kind }
    case 'path.changed':
      return { kind, path: '', pattern: '', changes: [...CHANGE_KINDS] }
    case 'run.finished':
      return { kind, pipeline_id: null, outcome: 'any' }
  }
}

/**
 * The When picker's memory, for one dialog's life: what each trigger kind held when the author
 * switched away from it, and the kind each category was last left on. The categories sit side
 * by side as one control, so clicking through them to see what each offers is the expected
 * gesture — and it must not cost the folder already typed. A kind never visited starts blank.
 */
export function triggerSwitcher() {
  const held = new Map<AutomationTriggerKind, AutomationTrigger>()
  const lastKind = new Map<TriggerCategory, AutomationTriggerKind>()

  /** Leave `current` for `kind`, and answer what `kind` held before — or a blank. */
  function toKind(current: AutomationTrigger, kind: AutomationTriggerKind): AutomationTrigger {
    if (current.kind === kind) return current
    held.set(current.kind, current)
    lastKind.set(CATEGORY_OF[current.kind], current.kind)
    return held.get(kind) ?? blankTrigger(kind)
  }

  /** Leave `current` for another category, landing on the event it was last left on. */
  function toCategory(current: AutomationTrigger, category: TriggerCategory): AutomationTrigger {
    if (CATEGORY_OF[current.kind] === category) return current
    return toKind(current, lastKind.get(category) ?? triggerKindsOf(category)[0])
  }

  return { toKind, toCategory }
}

export function blankAction(
  kind: AutomationActionKind,
  pipelines: readonly PipelineSummary[] = [],
  offered: readonly AutomationPluginAction[] = [],
): AutomationAction {
  switch (kind) {
    case 'notify':
      return { kind, title: '', message: '' }
    case 'pipeline.run':
      return { kind, pipeline_id: pipelines[0]?.id ?? '', use_cache: true }
    case 'plugin.action':
      return { kind, plugin_id: offered[0]?.plugin_id ?? '', action_id: offered[0]?.action_id ?? '' }
  }
}

export function blankAutomation(): AutomationInput {
  return {
    name: '',
    enabled: true,
    trigger: blankTrigger('task.status_changed'),
    action: blankAction('notify'),
  }
}

/**
 * `./models/` → `models`, the spelling the file holds — or `null` for something that is not a
 * path inside the project. The backend applies the same rule (`automations/schema.py`) and
 * answers 422 with a validation dump; checking here first is what lets the dialog say it in
 * a sentence, under the field.
 */
export function normalisePath(value: string): string | null {
  const text = value.trim().replace(/\\/g, '/')
  if (/^[A-Za-z]:/.test(text) || text.startsWith('/')) return null
  const parts = text.split('/').filter((part) => part !== '' && part !== '.')
  if (parts.includes('..')) return null
  return parts.join('/')
}

/** How the page shows a stored path: `models` → `./models/`, the whole project → `./`. */
export function displayPath(path: string): string {
  return path ? `./${path}/` : './'
}

/** Why this input cannot be saved, as a message key — or `null` when it can. */
export function problemWith(input: AutomationInput): MessageKey | null {
  const { trigger, action } = input
  if (trigger.kind === 'path.changed') {
    if (normalisePath(trigger.path) === null) return 'automations.form.pathOutside'
    if (!trigger.changes.length) return 'automations.form.noChanges'
  }
  if (action.kind === 'pipeline.run' && !action.pipeline_id) return 'automations.form.noPipeline'
  if (action.kind === 'plugin.action' && !(action.plugin_id && action.action_id)) {
    return 'automations.form.noPluginAction'
  }
  return null
}

export interface Catalogue {
  pipelines: readonly PipelineSummary[]
  pluginActions: readonly AutomationPluginAction[]
}

function pipelineName(id: string, catalogue: Catalogue): string {
  return catalogue.pipelines.find((pipeline) => pipeline.id === id)?.name || id
}

/** "On task", "On directory", "On pipeline" — the sentence's first part. */
export function subjectText(trigger: AutomationTrigger): string {
  return t(SUBJECTS[CATEGORY_OF[trigger.kind]])
}

/** "Status changed to: Needs review", "./models/ updated" — the second. */
export function triggerText(trigger: AutomationTrigger, catalogue: Catalogue): string {
  switch (trigger.kind) {
    case 'task.status_changed':
      return trigger.status
        ? t('automations.sentence.statusTo', { status: statusLabel(trigger.status) })
        : t('automations.sentence.statusAny')
    case 'task.created':
      return t('automations.sentence.taskCreated')
    case 'path.changed': {
      const where = trigger.pattern
        ? `${displayPath(trigger.path)}${trigger.pattern}`
        : displayPath(trigger.path)
      return t('automations.sentence.pathUpdated', { path: where })
    }
    case 'run.finished': {
      const which = trigger.pipeline_id
        ? pipelineName(trigger.pipeline_id, catalogue)
        : t('automations.sentence.anyPipeline')
      const key: MessageKey =
        trigger.outcome === 'success'
          ? 'automations.sentence.runSucceeded'
          : trigger.outcome === 'failed'
            ? 'automations.sentence.runFailed'
            : 'automations.sentence.runFinished'
      return t(key, { pipeline: which })
    }
  }
}

/** "Send notification", "Execute pipeline “Bake models”", "Graphify Update" — the third. */
export function actionText(action: AutomationAction, catalogue: Catalogue): string {
  switch (action.kind) {
    case 'notify':
      return t('automations.sentence.notify')
    case 'pipeline.run':
      return t('automations.sentence.runPipeline', {
        pipeline: pipelineName(action.pipeline_id, catalogue),
      })
    case 'plugin.action': {
      const offered = catalogue.pluginActions.find(
        (entry) => entry.plugin_id === action.plugin_id && entry.action_id === action.action_id,
      )
      // The plugin's own label is data (§6.4); an action nobody offers any more is named by
      // what the file says, which is what the problem line under it is about.
      return offered?.label ?? `${action.plugin_id}.${action.action_id}`
    }
  }
}

export function sentence(automation: Pick<Automation, 'trigger' | 'action'>, catalogue: Catalogue): string[] {
  return [
    subjectText(automation.trigger),
    triggerText(automation.trigger, catalogue),
    actionText(automation.action, catalogue),
  ]
}

/** Placeholders a notification's texts may use, by trigger — shown under the fields. */
export const PLACEHOLDERS: Record<AutomationTriggerKind, readonly string[]> = {
  'task.status_changed': ['task', 'title', 'status', 'previous', 'assignee'],
  'task.created': ['task', 'title', 'assignee'],
  'path.changed': ['path', 'paths', 'count', 'folder'],
  'run.finished': ['pipeline', 'outcome', 'error'],
}

/**
 * What set a firing off, in the reader's language — "BT-04 → Needs review". Worded from the
 * row's data; the backend's own `cause` is the fallback for a row from before it carried any.
 */
export function runCause(run: AutomationRun): string {
  const values = run.data.values ?? {}
  switch (run.data.trigger) {
    case 'manual':
      return t('automations.log.manual')
    case 'task.status_changed':
      return values.task ? `${values.task} → ${statusLabel(values.status ?? '')}` : run.cause
    case 'task.created':
      return values.task ? t('automations.notification.createdTitle', { task: values.task }) : run.cause
    case 'path.changed': {
      const count = Number(values.count ?? '1') || 1
      if (count === 1) return values.path ?? run.cause
      return t('automations.log.filesUnder', { count, folder: values.folder ?? './' })
    }
    case 'run.finished':
      return t(
        values.outcome === 'success'
          ? 'automations.notification.runSucceeded'
          : values.outcome === 'failed'
            ? 'automations.notification.runFailed'
            : 'automations.notification.runCancelled',
        { pipeline: values.pipeline ?? '' },
      )
    default:
      return run.cause
  }
}

/** How a firing went. A plugin's answer and an error are data and are shown as they came. */
export function runDetail(run: AutomationRun, catalogue: Catalogue): string {
  if (run.status === 'skipped') {
    if (run.data.guard === 'flood') return t('automations.log.guardFlood')
    if (run.data.guard === 'chain') return t('automations.log.guardChain')
    return run.detail
  }
  if (run.data.interrupted) return t('automations.log.interrupted')
  if (run.data.action === 'pipeline.run' && run.data.pipeline_id) {
    const pipeline = pipelineName(run.data.pipeline_id, catalogue)
    if (run.status === 'running') return t('automations.log.pipelineRunning', { pipeline })
    if (run.status === 'ok') return t('automations.log.pipelineFinished', { pipeline })
    if (run.status === 'cancelled') return t('automations.log.pipelineCancelled', { pipeline })
    return run.detail
  }
  if (run.data.action === 'notify' && run.status === 'ok') return t('automations.log.notified')
  // A plugin's own words, and an error, arrive as data; a plugin's work says nothing until it
  // ends, and the status pill says it is going.
  return run.status === 'running' ? '' : run.detail
}

/**
 * The log's status in the app's one vocabulary (`lib/status.ts`), so a firing's pill reads — and
 * breathes while its work goes on — the way a task's and a run's do.
 */
export function runBadge(run: AutomationRun): string {
  switch (run.status) {
    case 'running':
      return 'in_progress'
    case 'ok':
      return 'done'
    case 'error':
      return 'failed'
    default:
      return run.status
  }
}

/**
 * The log with ``run`` in it — in place when the row is there already (its work reported, or
 * ended), otherwise where its start puts it. Newest first, at most ``max`` rows; an update for a
 * row the page never saw, its first event missed, still lands in order.
 */
export function mergeRun(runs: readonly AutomationRun[], run: AutomationRun, max: number): AutomationRun[] {
  const next = [...runs]
  const index = next.findIndex((entry) => entry.id === run.id)
  if (index >= 0) {
    next[index] = run
    return next
  }
  const started = parseUtc(run.created_at)
  const before = next.findIndex((entry) => parseUtc(entry.created_at) < started)
  next.splice(before < 0 ? next.length : before, 0, run)
  return next.slice(0, max)
}

export interface NotificationText {
  title: string
  message: string
}

/**
 * What a notification says. The automation's own texts win; one left empty is worded here, in
 * the reader's language, from what the trigger carried — the backend never writes copy (§6.4).
 */
export function notificationText(notification: AutomationNotification): NotificationText {
  const values = notification.values
  let title = ''
  let message = ''
  switch (notification.trigger) {
    case 'task.status_changed':
      title = t('automations.notification.statusTitle', {
        task: values.task ?? '',
        status: statusLabel(values.status ?? ''),
      })
      message = values.title ?? ''
      break
    case 'task.created':
      title = t('automations.notification.createdTitle', { task: values.task ?? '' })
      message = values.title ?? ''
      break
    case 'path.changed': {
      const count = Number(values.count ?? '1') || 1
      title = t('automations.notification.pathTitle', { folder: values.folder ?? './' })
      message =
        count === 1 ? (values.path ?? '') : t('automations.notification.pathCount', { count })
      break
    }
    case 'run.finished':
      title = t(
        values.outcome === 'success'
          ? 'automations.notification.runSucceeded'
          : values.outcome === 'failed'
            ? 'automations.notification.runFailed'
            : 'automations.notification.runCancelled',
        { pipeline: values.pipeline ?? '' },
      )
      message = values.error ?? ''
      break
    default:
      title = notification.automation_name || t('automations.notification.testTitle')
      message = t('automations.notification.testMessage')
  }
  return {
    title: notification.title || title,
    message: notification.message || message,
  }
}
