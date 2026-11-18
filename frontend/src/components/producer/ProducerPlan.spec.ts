import { createSSRApp, defineComponent, h, type PropType } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { describe, expect, it, vi } from 'vitest'

import type { ProducerPlanItem } from '@/api/types'
import ProducerPlan from './ProducerPlan.vue'

vi.mock('@/i18n', () => ({
  t: (key: string, values?: Record<string, unknown>) => values ? `${key}:${JSON.stringify(values)}` : key,
  hasMessage: () => false, formatDate: String, formatNumber: String,
}))

async function render(items: ProducerPlanItem[]): Promise<string> {
  const app = createSSRApp({ render: () => h(ProducerPlan, { items }) })
  // The link's destination is the router's business; here it only has to carry the task.
  app.component('RouterLink', defineComponent({
    props: { to: { type: Object as PropType<{ path: string; query: { task: string } }>, required: true } },
    setup: (props, { slots, attrs }) => () =>
      h('a', { ...attrs, href: `${props.to.path}?task=${props.to.query.task}` }, slots.default?.()),
  }))
  return renderToString(app)
}

const task = (id: string, status: string, reopens = 0) =>
  ({ id: `task_${id}`, display_id: `VS-${id}`, title: `Title ${id}`, status, reopens })

describe('ProducerPlan', () => {
  it('shows every task an item ever had, each with a dot in its status colour', async () => {
    const html = await render([
      { id: 'p1', text: 'Implement movement', done: true, tasks: [
        task('01', 'done'), task('02', 'cancelled'), task('03', 'in_progress'), task('04', 'needs_review'),
      ] },
      { id: 'p2', text: 'Verify movement', done: false, tasks: [] },
    ])
    expect(html.match(/data-plan-task(?=[ >])/g)).toHaveLength(4)
    expect(html.match(/data-plan-tasks/g)).toHaveLength(1)
    for (const [id, token] of [
      ['01', '--vxs-status-success'], ['02', '--vxs-status-cancelled'],
      ['03', '--vxs-status-running'], ['04', '--vxs-status-review'],
    ]) {
      const link = html.slice(html.indexOf(`/tasks?task=task_${id}`))
      const dot = link.slice(0, link.indexOf(`VS-${id}`))
      expect(dot).toContain(`background-color:var(${token})`)
    }
    expect(html).toContain('Verify movement')
  })

  it('marks a reopened task with its count and says so in the hint', async () => {
    const html = await render([{ id: 'p1', text: 'Implement movement', done: false, tasks: [
      task('01', 'pending', 2), task('02', 'todo'),
    ] }])
    expect(html).toContain('↻2')
    expect(html.match(/↻/g)).toHaveLength(1)
    expect(html).toContain('producerPage.planTaskReopened:{&quot;count&quot;:2}')
    expect(html).toContain('VS-01 · Title 01 · Pending')
  })

  it('renders a Plan saved before items kept ids or tasks', async () => {
    const html = await render([{ text: 'Old item', done: false }])
    expect(html).toContain('Old item')
    expect(html).not.toContain('data-plan-tasks')
  })
})
