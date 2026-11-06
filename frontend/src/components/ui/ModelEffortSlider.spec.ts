import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { t } from '@/i18n'
import ModelEffortSlider from './ModelEffortSlider.vue'

interface SliderProps {
  modelValue: string | null
  levels?: string[]
  readonly?: boolean
  label?: string
  defaultLabel?: string
  defaultTitle?: string
}

interface HostNode {
  children: HostNode[]
  parent: HostNode | null
}

const node = (): HostNode => ({ children: [], parent: null })
const renderer = createRenderer<HostNode, HostNode>({
  createElement: node,
  createText: node,
  createComment: node,
  setText: () => {},
  setElementText: () => {},
  parentNode: target => target.parent,
  nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] ?? null,
  insert(target, parent, anchor) {
    target.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(target)
    else parent.children.splice(index, 0, target)
  },
  remove(target) {
    if (target.parent) target.parent.children = target.parent.children.filter(child => child !== target)
  },
  patchProp: () => {},
})

const apps: ReturnType<typeof renderer.createApp>[] = []

function mount(initial: SliderProps) {
  const props = reactive(initial)
  const changed = vi.fn()
  // Exercise the real setup and event handler without a DOM; SSR covers its markup below.
  const slider = { ...ModelEffortSlider, render: () => null } as unknown as typeof ModelEffortSlider
  const app = renderer.createApp({ render: () => h(slider, {
    ...props, 'onUpdate:modelValue': changed,
  }) })
  app.provide(ssrContextKey, {})
  app.mount(node())
  apps.push(app)
  const state = (app._instance!.subTree.component as unknown as {
    setupState: { update: (event: Event) => void }
  }).setupState
  return {
    props,
    changed,
    input: (value: string) => state.update({ target: { value } } as unknown as Event),
  }
}

function markup(props: SliderProps): Promise<string> {
  return renderToString(createSSRApp(ModelEffortSlider, { ...props }))
}

function range(html: string): string {
  const input = html.match(/<input\b[^>]*>/)?.[0]
  expect(input, 'editable effort has a native range control').toBeDefined()
  expect(input).toContain('type="range"')
  return input!
}

afterEach(() => {
  for (const app of apps.splice(0)) app.unmount()
})

describe('model effort slider', () => {
  it('exposes read-only effort as labelled information with no interactive input', async () => {
    const props = { modelValue: 'high', readonly: true }
    const html = await markup(props)
    expect(html).not.toContain('<input')
    expect(html).toContain('role="img"')
    expect(html).toContain(`aria-label="${t('teams.editor.effort')}: high"`)
    const control = mount(props)
    control.input('1')
    expect(control.changed).not.toHaveBeenCalled()
  })

  it('describes an unset member value as the model default rather than low effort', async () => {
    const html = await markup({ modelValue: null })
    expect(html).toContain(`>${t('modelEffort.default')}</span>`)
    expect(range(html)).toContain('value="0"')
    expect(range(html)).toContain(`aria-label="${t('teams.editor.effort')}"`)
    expect(range(html)).toContain(`aria-valuetext="${t('teams.editor.effortDefault')}"`)
  })

  it('keeps the short inheritance label separate from its accessible resolved value', async () => {
    const props = {
      modelValue: null, label: 'Task effort',
      defaultLabel: 'Inherited', defaultTitle: 'Same as the member (high)',
    }
    const html = await markup(props)
    expect(html).toContain('>Inherited</span>')
    expect(range(html)).toContain('aria-label="Task effort"')
    expect(range(html)).toContain('aria-valuetext="Same as the member (high)"')
    expect(range(html)).toContain('value="0"')
    expect(await markup({ ...props, readonly: true })).toContain(
      'aria-label="Task effort: Same as the member (high)"',
    )
  })

  it('emits the selected supported level and null when returning to inheritance', async () => {
    const control = mount({ modelValue: null, levels: ['low', 'high', 'max'] })
    control.input('2')
    expect(control.changed).toHaveBeenLastCalledWith('high')
    control.props.modelValue = 'high'
    await nextTick()
    const input = range(await markup(control.props))
    expect(input).toContain('value="2"')
    expect(input).toContain('max="3"')
    expect(input).toContain('aria-valuetext="high"')
    control.input('0')
    expect(control.changed).toHaveBeenLastCalledWith(null)
    expect(control.changed).toHaveBeenCalledTimes(2)
  })

  it('preserves a stored level missing from the available catalogue without emitting changes', async () => {
    const control = mount({ modelValue: 'future-level', levels: ['low', 'high'] })
    let input = range(await markup(control.props))
    expect(input).toContain('aria-valuetext="future-level"')
    expect(input).toContain('value="3"')
    expect(input).toContain('max="3"')
    expect(control.changed).not.toHaveBeenCalled()

    control.props.levels = ['low', 'future-level', 'high', 'max']
    await nextTick()
    input = range(await markup(control.props))
    expect(input).toContain('value="2"')
    expect(input).toContain('aria-valuetext="future-level"')
    expect(control.props.modelValue).toBe('future-level')
    expect(control.changed).not.toHaveBeenCalled()
    control.input('2')
    expect(control.changed).toHaveBeenCalledExactlyOnceWith('future-level')
  })

  it('keeps an unset value available while effort options are still loading', async () => {
    const control = mount({ modelValue: null, levels: [] })
    expect(range(await markup(control.props))).toContain('max="0"')
    control.props.levels = ['low', 'medium', 'high']
    await nextTick()
    expect(control.changed).not.toHaveBeenCalled()
    expect(range(await markup(control.props))).toContain('value="0"')
    control.input('1')
    expect(control.changed).toHaveBeenCalledExactlyOnceWith('low')
  })

  it('ignores out-of-range input and prevents stale editable handlers changing a read-only value', async () => {
    const control = mount({ modelValue: 'high', levels: ['low', 'high'] })
    for (const value of ['-1', '3', '1.5', 'not-a-number']) control.input(value)
    expect(control.changed).not.toHaveBeenCalled()
    control.props.readonly = true
    await nextTick()
    control.input('1')
    expect(control.changed).not.toHaveBeenCalled()
    expect(await markup(control.props)).not.toContain('<input')
  })
})
