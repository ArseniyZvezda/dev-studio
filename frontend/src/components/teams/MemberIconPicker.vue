<script setup lang="ts">
/**
 * Choosing what a member does, from the catalogue in `lib/member-icons.ts` (§10.2).
 *
 * A button showing the current icon, and a grid that unrolls under it **in the flow** rather
 * than as a floating panel: the member editor is already a scrolling column, so a panel
 * positioned against the window would have to be re-placed on every scroll of it for no gain
 * — there is nothing here for a popover to escape from.
 *
 * The grid is grouped and the groups are headed, because the catalogue's whole point is that
 * the icons name *disciplines*: Production, Design, Writing, Art, Audio, Engineering,
 * Quality, Outreach. Somebody looking for "the artist one" reads a heading, not 58 tooltips.
 * Picking closes the grid — one choice is the whole interaction — while "No icon" leaves it
 * open, since clearing is usually a step on the way to picking something else.
 *
 * Keyboard: the arrows walk the cells, `ArrowUp`/`ArrowDown` by a row of the grid, and
 * `Escape` closes the grid without changing the choice.
 */
import { Ban, ChevronDown } from 'lucide-vue-next'
import { computed, nextTick, ref } from 'vue'

import { t } from '@/i18n'
import { MEMBER_ICON_GROUPS, memberIcon } from '@/lib/member-icons'

const props = defineProps<{
  modelValue: string | null
  /** Drawn behind the chosen icon, so the button previews what the member will look like. */
  color?: string | null
}>()
const emit = defineEmits<{ 'update:modelValue': [string | null] }>()

/** Icons per row, which is also how far `ArrowUp` and `ArrowDown` move inside a group. */
const COLUMNS = 8

const open = ref(false)
const grid = ref<HTMLElement | null>(null)

const current = computed(() => memberIcon(props.modelValue))
const swatch = computed(() => props.color ?? 'var(--vxs-accent)')

async function toggle(): Promise<void> {
  open.value = !open.value
  if (!open.value) return
  await nextTick()
  // The chosen cell, or the first one, so the keyboard starts where the eye does.
  const target =
    grid.value?.querySelector<HTMLElement>('[aria-checked="true"]') ??
    grid.value?.querySelector<HTMLElement>('[data-icon-cell]')
  target?.focus()
}

function pick(name: string | null): void {
  emit('update:modelValue', name)
  // Clearing is usually a step on the way to picking, so only a real choice closes.
  if (name !== null) open.value = false
}

/**
 * One handler rather than `@keydown.left` and friends, on `DocTreeMenu`'s pattern: Vue's
 * `.left` / `.right` modifiers are *mouse button* modifiers as well as arrow-key aliases,
 * and a control whose navigation depends on which meaning wins is one to write out by hand.
 */
const STEPS: Record<string, number> = {
  ArrowLeft: -1,
  ArrowRight: 1,
  ArrowUp: -COLUMNS,
  ArrowDown: COLUMNS,
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    open.value = false
    return
  }
  const step = STEPS[event.key]
  if (step === undefined) return
  event.preventDefault()
  const cells = [...(grid.value?.querySelectorAll<HTMLElement>('[data-icon-cell]') ?? [])]
  const from = cells.indexOf(event.target as HTMLElement)
  if (from < 0) return
  const next = Math.min(cells.length - 1, Math.max(0, from + step))
  cells[next]?.focus()
}
</script>

<template>
  <div>
    <button
      type="button"
      class="flex w-full items-center gap-2 rounded-md border border-line bg-inset px-2 py-1 text-left hover:bg-hover"
      :aria-expanded="open"
      :title="
        current
          ? t('teams.iconPicker.change', { label: current.label })
          : t('teams.iconPicker.pick')
      "
      @click="toggle"
    >
      <span
        class="grid size-6 shrink-0 place-items-center rounded-md"
        :style="current ? { background: swatch, color: 'var(--vxs-text-on-accent)' } : {}"
        :class="current ? '' : 'border border-dashed border-line text-faint'"
      >
        <component :is="current.component" v-if="current" class="size-3.5" />
        <Ban v-else class="size-3" />
      </span>
      <span class="min-w-0 flex-1 truncate" :class="current ? '' : 'text-faint'">
        {{ current?.label ?? t('teams.iconPicker.none') }}
      </span>
      <ChevronDown class="vxs-chevron size-3.5 shrink-0 text-faint" :class="open ? 'rotate-180' : ''" />
    </button>

    <div
      v-if="open"
      ref="grid"
      class="mt-1 max-h-56 space-y-2 overflow-y-auto rounded-md border border-line bg-inset p-2"
      role="listbox"
      :aria-label="t('teams.iconPicker.listLabel')"
      @keydown="onKeydown"
    >
      <button
        type="button"
        role="option"
        data-icon-cell
        class="flex w-full items-center gap-2 rounded-md px-1 py-0.5 text-small hover:bg-hover"
        :class="modelValue === null ? 'bg-active' : 'text-faint'"
        :aria-selected="modelValue === null"
        :aria-checked="modelValue === null"
        @click="pick(null)"
      >
        <Ban class="size-3.5 shrink-0" />
        {{ t('teams.iconPicker.noneOption') }}
      </button>

      <div v-for="group in MEMBER_ICON_GROUPS" :key="group.label">
        <p class="mb-1 text-small font-semibold tracking-wide text-faint uppercase">
          {{ group.label }}
        </p>
        <div class="grid grid-cols-8 gap-1">
          <button
            v-for="entry in group.icons"
            :key="entry.name"
            type="button"
            role="option"
            data-icon-cell
            class="grid size-7 place-items-center rounded-md hover:bg-hover"
            :class="modelValue === entry.name ? 'bg-active ring-1 ring-line-strong' : ''"
            :style="modelValue === entry.name ? { color: swatch } : {}"
            :aria-selected="modelValue === entry.name"
            :aria-checked="modelValue === entry.name"
            :title="entry.label"
            :aria-label="entry.label"
            @click="pick(entry.name)"
          >
            <component :is="entry.component" class="size-4" />
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
