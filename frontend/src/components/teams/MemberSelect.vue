<script setup lang="ts">
/**
 * Picking a member — the assignee of a task, who is acting, who somebody reports to (§10.4).
 *
 * A listbox rather than a `<select>`, and for one reason: an `<option>` may hold text and
 * nothing else, so a native picker cannot show the icon that says what a member *does* —
 * which is the whole point of having one. Everything else about it is built to be as close
 * to the control it replaces as a hand-written one can be: the arrows walk the rows, `Enter`
 * and `Space` take the highlighted one, `Escape` closes without choosing, `Home`/`End` reach
 * the ends, and typing letters jumps to a name starting with them.
 *
 * The list is teleported to `<body>` and placed against the button's own rectangle, because
 * every caller sits inside a scrolling panel that would otherwise clip it; it flips above the
 * button when there is no room under it. It closes on a scroll rather than following one —
 * the anchor can be inside a pane, a dialog and the page at once, and a list that tracked all
 * three would be chasing three scrollers to stay where it already was.
 *
 * `Escape` is registered with the surrounding `ModalDialog` (`lib/dialog.ts`), so closing the
 * list does not also close the dialog it was opened in.
 */
import { Check, ChevronDown } from 'lucide-vue-next'
import { computed, inject, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import type { TeamMember } from '@/api/types'
import MemberMark from '@/components/teams/MemberMark.vue'
import { t } from '@/i18n'
import { dialogEscape } from '@/lib/dialog'

const props = withDefaults(
  defineProps<{
    /** The chosen member's id; `null` is the "nobody" row, offered only with `emptyLabel`. */
    modelValue: string | null
    members: TeamMember[]
    /** The row that means nobody — "You (no restrictions)", "Nobody — top of the team". */
    emptyLabel?: string
    /** Shown when the value names nobody and there is no empty row: an id off the roster. */
    placeholder?: string
    disabled?: boolean
    title?: string
    label?: string
  }>(),
  { emptyLabel: '', disabled: false, title: '' },
)
const emit = defineEmits<{ 'update:modelValue': [string | null] }>()

/**
 * The placeholder's and the label's defaults, resolved while rendering rather than given to
 * `withDefaults`, which would keep them in the language the component was created in.
 */
const placeholderText = computed(() => props.placeholder ?? t('teams.select.placeholder'))
const labelText = computed(() => props.label ?? t('teams.select.label'))

/** How far from the bottom of the window the list would rather not open downwards. */
const MAX_LIST_HEIGHT = 280

const open = ref(false)
const active = ref(-1)
const anchor = ref<HTMLElement | null>(null)
const list = ref<HTMLElement | null>(null)
const position = ref({ top: 0, left: 0, width: 0, upwards: false })

const selected = computed(
  () => props.members.find((member) => member.id === props.modelValue) ?? null,
)

/** Every row, the "nobody" one included, so the keyboard walks one flat list. */
const rows = computed(() => [
  ...(props.emptyLabel ? [{ id: null as string | null, member: null as TeamMember | null }] : []),
  ...props.members.map((member) => ({ id: member.id as string | null, member })),
])

const registerEscape = inject(dialogEscape, null)
registerEscape?.(() => {
  if (!open.value) return false
  close()
  return true
})

function place(): void {
  const box = anchor.value?.getBoundingClientRect()
  if (!box) return
  const below = window.innerHeight - box.bottom
  const upwards = below < MAX_LIST_HEIGHT && box.top > below
  position.value = {
    top: upwards ? box.top - 4 : box.bottom + 4,
    left: box.left,
    width: box.width,
    upwards,
  }
}

async function toggle(): Promise<void> {
  if (props.disabled) return
  if (open.value) return close()
  place()
  active.value = Math.max(
    0,
    rows.value.findIndex((row) => row.id === props.modelValue),
  )
  open.value = true
  await nextTick()
  scrollActiveIntoView()
}

function close(): void {
  open.value = false
  anchor.value?.focus()
}

function choose(id: string | null): void {
  emit('update:modelValue', id)
  close()
}

function scrollActiveIntoView(): void {
  list.value
    ?.querySelectorAll<HTMLElement>('[data-row]')
    [active.value]?.scrollIntoView({ block: 'nearest' })
}

async function step(delta: number): Promise<void> {
  if (!open.value) return toggle()
  const count = rows.value.length
  if (!count) return
  active.value = (active.value + delta + count) % count
  await nextTick()
  scrollActiveIntoView()
}

/** Type a few letters to reach a name, the way a native picker does. */
let typed = ''
let typedAt = 0

async function typeahead(key: string): Promise<void> {
  const now = Date.now()
  typed = now - typedAt > 800 ? key : typed + key
  typedAt = now
  const at = rows.value.findIndex((row) =>
    (row.member?.name ?? props.emptyLabel).toLowerCase().startsWith(typed),
  )
  if (at < 0) return
  if (!open.value) return choose(rows.value[at]!.id)
  active.value = at
  await nextTick()
  scrollActiveIntoView()
}

function onKeydown(event: KeyboardEvent): void {
  if (props.disabled) return
  switch (event.key) {
    case 'ArrowDown':
      event.preventDefault()
      void step(1)
      return
    case 'ArrowUp':
      event.preventDefault()
      void step(-1)
      return
    case 'Home':
      if (!open.value) return
      event.preventDefault()
      active.value = 0
      void nextTick(scrollActiveIntoView)
      return
    case 'End':
      if (!open.value) return
      event.preventDefault()
      active.value = rows.value.length - 1
      void nextTick(scrollActiveIntoView)
      return
    case 'Enter':
    case ' ':
      event.preventDefault()
      if (!open.value) return void toggle()
      choose(rows.value[active.value]?.id ?? null)
      return
    case 'Escape':
      if (!open.value) return
      // Stopped, or the dialog around this closes over the list that just took the key.
      event.preventDefault()
      event.stopPropagation()
      close()
      return
    case 'Tab':
      if (open.value) open.value = false
      return
  }
  if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
    event.preventDefault()
    void typeahead(event.key.toLowerCase())
  }
}

/** A click anywhere else closes the list — on the way down, so a pick elsewhere still lands. */
function onPointerDown(event: PointerEvent): void {
  const target = event.target as Node | null
  if (!target) return
  if (anchor.value?.contains(target) || list.value?.contains(target)) return
  open.value = false
}

/**
 * Where the list is drawn. A computed rather than an inline expression, because a template
 * cannot reach `window` — it renders against the component's own scope, so `window.innerHeight`
 * there is simply undefined and the flipped list lands at the top of the page.
 */
const listStyle = computed(() => ({
  top: position.value.upwards ? 'auto' : `${position.value.top}px`,
  bottom: position.value.upwards ? `${window.innerHeight - position.value.top}px` : 'auto',
  left: `${position.value.left}px`,
  width: `${Math.max(position.value.width, 220)}px`,
  maxHeight: `${MAX_LIST_HEIGHT}px`,
  // The list unfolds from the edge its button is on (§7): downwards from its top, or, flipped,
  // upwards from its bottom with the travel turned round.
  '--vxs-origin': position.value.upwards ? 'bottom left' : 'top left',
  '--vxs-arrive-y': position.value.upwards ? '4px' : '-4px',
}))

watch(open, (isOpen) => {
  if (isOpen) {
    window.addEventListener('pointerdown', onPointerDown as EventListener, true)
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
  } else {
    window.removeEventListener('pointerdown', onPointerDown as EventListener, true)
    window.removeEventListener('scroll', close, true)
    window.removeEventListener('resize', close)
  }
})

// A control unmounted with its list open — the dialog around it closed — must not leave its
// listeners on `window`, where the next scroll would call into a torn-down component.
onBeforeUnmount(() => {
  window.removeEventListener('pointerdown', onPointerDown as EventListener, true)
  window.removeEventListener('scroll', close, true)
  window.removeEventListener('resize', close)
})

// The chosen member can stop being offered — changing who is acting narrows the assignees —
// and a list open over rows that have gone is pointing at somebody else's row.
watch(
  () => props.members,
  () => {
    if (open.value) open.value = false
  },
)
</script>

<template>
  <div>
    <button
      ref="anchor"
      type="button"
      class="flex w-full items-center gap-2 rounded-md border border-line bg-inset px-2 py-1.5 text-left outline-none focus:border-accent disabled:cursor-not-allowed disabled:opacity-60"
      role="combobox"
      :aria-expanded="open"
      aria-haspopup="listbox"
      :aria-label="labelText"
      :disabled="disabled"
      :title="title"
      @click="toggle"
      @keydown="onKeydown"
    >
      <MemberMark :member="selected" />
      <span class="min-w-0 flex-1 truncate">
        <template v-if="selected">
          {{ selected.name }}
          <span class="text-faint">
            —
            {{
              selected.role_title ||
              (selected.kind === 'human' ? t('teams.kind.person') : t('teams.kind.agent'))
            }}
          </span>
        </template>
        <template v-else-if="emptyLabel">{{ emptyLabel }}</template>
        <span v-else class="text-faint">{{ modelValue || placeholderText }}</span>
      </span>
      <ChevronDown class="size-3.5 shrink-0 text-faint" />
    </button>

    <Teleport to="body">
      <div
        v-if="open"
        ref="list"
        class="vxs-card fixed z-[60] overflow-y-auto rounded-md border border-card bg-elevated py-1 shadow-lg"
        :style="listStyle"
        role="listbox"
        :aria-label="labelText"
        @keydown="onKeydown"
      >
        <button
          v-for="(row, index) in rows"
          :key="row.id ?? '__none__'"
          data-row
          type="button"
          role="option"
          class="flex w-full items-center gap-2 px-2 py-1 text-left"
          :class="index === active ? 'bg-hover' : ''"
          :aria-selected="row.id === modelValue"
          @mouseenter="active = index"
          @click="choose(row.id)"
        >
          <MemberMark :member="row.member" />
          <span v-if="!row.member" class="size-3.5 shrink-0" />
          <span class="min-w-0 flex-1 truncate">
            <template v-if="row.member">
              {{ row.member.name }}
              <span class="text-faint">
                —
                {{
                  row.member.role_title ||
                  (row.member.kind === 'human' ? t('teams.kind.person') : t('teams.kind.agent'))
                }}
              </span>
            </template>
            <template v-else>{{ emptyLabel }}</template>
          </span>
          <Check v-if="row.id === modelValue" class="size-3.5 shrink-0 text-accent" />
        </button>
      </div>
    </Teleport>
  </div>
</template>
