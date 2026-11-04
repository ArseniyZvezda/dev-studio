import { describe, expect, it } from 'vitest'

import type { Automation, AutomationNotification, AutomationRun, AutomationTrigger } from '@/api/types'
import { t } from '@/i18n'

import {
  ACTION_KINDS,
  CATEGORY_LABELS,
  CATEGORY_OF,
  EVENT_LABELS,
  TRIGGER_CATEGORIES,
  TRIGGER_KINDS,
  blankAction,
  blankAutomation,
  blankTrigger,
  displayPath,
  mergeRun,
  normalisePath,
  notificationText,
  problemWith,
  runBadge,
  runCause,
  runDetail,
  sentence,
  subjectText,
  triggerKindsOf,
  triggerSwitcher,
} from './automations'

const catalogue = {
  pipelines: [{ id: 'bake', name: 'pipeline 1', description: '', node_count: 1, edge_count: 0 }],
  pluginActions: [
    { plugin_id: 'graphify', action_id: 'update', label: 'Graphify Update', description: null },
  ],
}

function automation(partial: Pick<Automation, 'trigger' | 'action'>): Automation {
  return { id: 'a', name: '', enabled: true, problem: null, ...partial }
}

describe('a path', () => {
  it('is stored the way the backend stores it', () => {
    expect(normalisePath('./models/')).toBe('models')
    expect(normalisePath('Assets\\Models\\')).toBe('Assets/Models')
    expect(normalisePath('./')).toBe('')
    expect(normalisePath('  models//props  ')).toBe('models/props')
  })

  it('may not leave the project', () => {
    for (const escaping of ['../elsewhere', 'models/../../x', '/etc', 'D:/Unreal', 'D:\\Unreal']) {
      expect(normalisePath(escaping)).toBeNull()
    }
  })

  it('is shown the way a person would type it', () => {
    expect(displayPath('models')).toBe('./models/')
    expect(displayPath('')).toBe('./')
    expect(normalisePath(displayPath('Assets/Models'))).toBe('Assets/Models')
  })
})

describe('the sentence', () => {
  it('reads as the three examples the page was asked for', () => {
    expect(
      sentence(
        automation({
          trigger: { kind: 'task.status_changed', status: 'needs_review' },
          action: { kind: 'notify', title: '', message: '' },
        }),
        catalogue,
      ),
    ).toEqual(['On task', 'Status changed to: Needs review', 'Send notification'])

    expect(
      sentence(
        automation({
          trigger: { kind: 'task.status_changed', status: 'done' },
          action: { kind: 'plugin.action', plugin_id: 'graphify', action_id: 'update' },
        }),
        catalogue,
      ),
    ).toEqual(['On task', 'Status changed to: Done', 'Graphify Update'])

    expect(
      sentence(
        automation({
          trigger: { kind: 'path.changed', path: 'models', pattern: '', changes: ['modified'] },
          action: { kind: 'pipeline.run', pipeline_id: 'bake', use_cache: true },
        }),
        catalogue,
      ),
    ).toEqual(['On directory', './models/ updated', 'Execute pipeline “pipeline 1”'])
  })

  it('names what the file names when the project no longer has it', () => {
    const parts = sentence(
      automation({
        trigger: { kind: 'run.finished', pipeline_id: 'gone', outcome: 'failed' },
        action: { kind: 'plugin.action', plugin_id: 'removed', action_id: 'sync' },
      }),
      catalogue,
    )
    expect(parts).toEqual(['On pipeline', 'Run of gone failed', 'removed.sync'])
  })
})

describe('When', () => {
  it('is a category, then an event within it', () => {
    expect(TRIGGER_CATEGORIES.map((category) => t(CATEGORY_LABELS[category]))).toEqual([
      'Task',
      'Directory',
      'Pipeline',
    ])
    expect(triggerKindsOf('task').map((kind) => t(EVENT_LABELS[kind]))).toEqual([
      'Status changed',
      'Created',
    ])
    expect(triggerKindsOf('pipeline').map((kind) => t(EVENT_LABELS[kind]))).toEqual(['Run finished'])
  })

  it('puts every kind in exactly one category, and names the sentence after it', () => {
    const listed = TRIGGER_CATEGORIES.flatMap((category) => triggerKindsOf(category))
    expect([...listed].sort()).toEqual([...TRIGGER_KINDS].sort())
    for (const kind of TRIGGER_KINDS) {
      expect(triggerKindsOf(CATEGORY_OF[kind])).toContain(kind)
    }
    expect(subjectText(blankTrigger('run.finished'))).toBe('On pipeline')
  })

  it('lands on the first event of a category never visited', () => {
    const switcher = triggerSwitcher()
    const trigger = switcher.toCategory(blankTrigger('path.changed'), 'task')
    expect(trigger).toEqual(blankTrigger('task.status_changed'))
  })

  it('gives a kind back as it was left when the author clicks away and back', () => {
    const switcher = triggerSwitcher()
    let trigger: AutomationTrigger = blankTrigger('path.changed')
    // Edited in place, the way the dialog's fields write to the draft.
    if (trigger.kind === 'path.changed') trigger.path = './models/'
    trigger = switcher.toCategory(trigger, 'pipeline')
    expect(trigger.kind).toBe('run.finished')
    trigger = switcher.toCategory(trigger, 'directory')
    expect(trigger).toMatchObject({ kind: 'path.changed', path: './models/' })
  })

  it('returns to the event a category was left on, not its first', () => {
    const switcher = triggerSwitcher()
    let trigger = switcher.toKind(blankTrigger('task.status_changed'), 'task.created')
    trigger = switcher.toCategory(trigger, 'directory')
    trigger = switcher.toCategory(trigger, 'task')
    expect(trigger.kind).toBe('task.created')
    // …and the other event of that category still holds what it held.
    trigger = switcher.toKind(trigger, 'task.status_changed')
    expect(trigger).toEqual(blankTrigger('task.status_changed'))
  })

  it('changes nothing when the choice is the one already made', () => {
    const switcher = triggerSwitcher()
    const trigger = blankTrigger('run.finished')
    expect(switcher.toCategory(trigger, 'pipeline')).toBe(trigger)
    expect(switcher.toKind(trigger, 'run.finished')).toBe(trigger)
  })
})

describe('the form', () => {
  it('starts every kind from a blank that can be told apart from a mistake', () => {
    for (const kind of TRIGGER_KINDS) expect(blankTrigger(kind).kind).toBe(kind)
    for (const kind of ACTION_KINDS) expect(blankAction(kind).kind).toBe(kind)
    expect(problemWith(blankAutomation())).toBeNull()
  })

  it('offers the first pipeline and the first plugin action there is', () => {
    expect(blankAction('pipeline.run', catalogue.pipelines)).toMatchObject({ pipeline_id: 'bake' })
    expect(blankAction('plugin.action', [], catalogue.pluginActions)).toMatchObject({
      plugin_id: 'graphify',
      action_id: 'update',
    })
  })

  it('says what is missing before anything is sent', () => {
    const draft = blankAutomation()
    draft.trigger = { kind: 'path.changed', path: '../out', pattern: '', changes: ['added'] }
    expect(problemWith(draft)).toBe('automations.form.pathOutside')
    draft.trigger = { kind: 'path.changed', path: './models/', pattern: '', changes: [] }
    expect(problemWith(draft)).toBe('automations.form.noChanges')
    draft.trigger = blankTrigger('task.created')
    draft.action = blankAction('pipeline.run')
    expect(problemWith(draft)).toBe('automations.form.noPipeline')
    draft.action = blankAction('plugin.action')
    expect(problemWith(draft)).toBe('automations.form.noPluginAction')
  })
})

describe('a notification', () => {
  const base: AutomationNotification = {
    automation_id: 'a',
    automation_name: 'Review ping',
    title: '',
    message: '',
    trigger: 'task.status_changed',
    values: { task: 'DP-04', title: 'Dash ability', status: 'needs_review', previous: 'in_progress' },
    route: '/tasks?task=task_1',
  }

  it('words itself when its author left the texts empty', () => {
    expect(notificationText(base)).toEqual({ title: 'DP-04 → Needs review', message: 'Dash ability' })
  })

  it("keeps the author's words, each on its own", () => {
    expect(notificationText({ ...base, title: 'Look at DP-04' })).toEqual({
      title: 'Look at DP-04',
      message: 'Dash ability',
    })
  })

  it('counts files rather than listing two hundred of them', () => {
    const changed = { ...base, trigger: 'path.changed' as const }
    expect(
      notificationText({ ...changed, values: { folder: './models/', count: '1', path: 'models/a.fbx' } }),
    ).toEqual({ title: './models/ changed', message: 'models/a.fbx' })
    expect(
      notificationText({ ...changed, values: { folder: './models/', count: '3', path: 'models/a.fbx' } }),
    ).toEqual({ title: './models/ changed', message: '3 files' })
  })

  it('says how a run ended', () => {
    const run = { ...base, trigger: 'run.finished' as const }
    expect(notificationText({ ...run, values: { pipeline: 'Bake', outcome: 'failed', error: 'boom' } })).toEqual({
      title: 'Bake failed',
      message: 'boom',
    })
    expect(notificationText({ ...run, values: { pipeline: 'Bake', outcome: 'cancelled' } }).title).toBe(
      'Bake was cancelled',
    )
  })

  it('has something to say for Run now, which nothing triggered', () => {
    expect(notificationText({ ...base, trigger: 'manual', values: {} })).toEqual({
      title: 'Review ping',
      message: 'A test run from the Automations page',
    })
  })
})

describe('a row of the log', () => {
  function run(partial: Partial<AutomationRun>): AutomationRun {
    return {
      id: 'r',
      automation_id: 'a',
      automation_name: '',
      status: 'ok',
      cause: 'backend cause',
      detail: 'backend detail',
      data: {},
      created_at: '2026-09-18T08:00:00Z',
      ...partial,
    }
  }

  it("is worded from its data, not from the backend's English", () => {
    const review = run({
      data: { trigger: 'task.status_changed', action: 'notify', values: { task: 'DP-04', status: 'needs_review' } },
    })
    expect(runCause(review)).toBe('DP-04 → Needs review')
    expect(runDetail(review, catalogue)).toBe('Notification sent')

    const baked = run({
      data: {
        trigger: 'path.changed',
        action: 'pipeline.run',
        pipeline_id: 'bake',
        values: { count: '3', folder: './models/', path: 'models/a.fbx' },
      },
    })
    expect(runCause(baked)).toBe('3 files under ./models/')
    expect(runDetail(baked, catalogue)).toBe('“pipeline 1” finished')
    expect(runCause(run({ data: { trigger: 'manual' } }))).toBe('Run from this page')
  })

  it('shows what only the backend can say as it came', () => {
    // A plugin's answer and an error are data (§6.4); so is a row from before rows carried any.
    expect(runDetail(run({ data: { trigger: 'task.created', action: 'plugin.action' } }), catalogue)).toBe(
      'backend detail',
    )
    expect(runDetail(run({ status: 'error', data: { action: 'pipeline.run', pipeline_id: 'bake' } }), catalogue)).toBe(
      'backend detail',
    )
    expect(runCause(run({}))).toBe('backend cause')
  })

  it('names the guard that refused', () => {
    expect(runDetail(run({ status: 'skipped', data: { guard: 'chain' } }), catalogue)).toContain('chain')
    expect(runDetail(run({ status: 'skipped', data: { guard: 'flood' } }), catalogue)).toContain('too often')
  })

  it('follows a pipeline through its run', () => {
    const data = { trigger: 'task.created' as const, action: 'pipeline.run' as const, pipeline_id: 'bake' }
    expect(runDetail(run({ status: 'running', data }), catalogue)).toBe('Running “pipeline 1”')
    expect(runDetail(run({ status: 'ok', data }), catalogue)).toBe('“pipeline 1” finished')
    expect(runDetail(run({ status: 'cancelled', data }), catalogue)).toBe('“pipeline 1” was cancelled')
  })

  it("says nothing of a plugin's work until it ends, and names a Studio that stopped under it", () => {
    expect(runDetail(run({ status: 'running', detail: '', data: { action: 'plugin.action' } }), catalogue)).toBe('')
    expect(
      runDetail(run({ status: 'cancelled', data: { action: 'plugin.action', interrupted: true } }), catalogue),
    ).toBe('Interrupted — the Studio stopped before it finished')
  })

  it('wears the status pill a task or a run would', () => {
    const statuses = ['running', 'ok', 'error', 'cancelled', 'skipped'] as const
    expect(statuses.map((status) => runBadge(run({ status })))).toEqual([
      'in_progress',
      'done',
      'failed',
      'cancelled',
      'skipped',
    ])
  })
})

describe('the log as its events arrive', () => {
  function row(id: string, minute: number, status: AutomationRun['status'] = 'ok'): AutomationRun {
    return {
      id,
      automation_id: 'a',
      automation_name: '',
      status,
      cause: '',
      detail: '',
      data: {},
      created_at: `2026-09-18T10:${String(minute).padStart(2, '0')}:00Z`,
    }
  }

  it('puts a row that ended where it was, not on top', () => {
    const log = [row('b', 2, 'running'), row('a', 1)]
    expect(mergeRun(log, row('b', 2, 'ok'), 50).map((entry) => [entry.id, entry.status])).toEqual([
      ['b', 'ok'],
      ['a', 'ok'],
    ])
  })

  it('puts a row it never saw where its start puts it', () => {
    const log = [row('c', 3), row('a', 1)]
    expect(mergeRun(log, row('d', 4), 50).map((entry) => entry.id)).toEqual(['d', 'c', 'a'])
    expect(mergeRun(log, row('b', 2, 'running'), 50).map((entry) => entry.id)).toEqual(['c', 'b', 'a'])
    // Older than everything a full log holds: it would be the first to go, so it goes.
    expect(mergeRun(log, row('z', 0), 2).map((entry) => entry.id)).toEqual(['c', 'a'])
  })
})
