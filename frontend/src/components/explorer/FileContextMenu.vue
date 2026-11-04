<script lang="ts">
/** Every action the menu can ask for. Declared here so the page can type its handler. */
export type ExplorerAction =
  | 'new-file'
  | 'new-folder'
  | 'rename'
  | 'duplicate'
  | 'cut'
  | 'copy'
  | 'paste'
  | 'copy-path'
  | 'copy-relative-path'
  | 'open-in-system'
  | 'delete'
</script>

<script setup lang="ts">
/**
 * The Explorer tree's context menu (spec §6.2).
 *
 * **One menu for the whole tree**, opened at the pointer with the clicked row as its
 * subject — `FileTreeNode` is recursive, so a menu mounted per row would be a menu per
 * file in the project. It follows `components/docs/BlockMenu.vue`: teleported to the body,
 * positioned in viewport coordinates with a click-catcher behind it, arrow keys walking
 * the entries and `Escape` handing focus back.
 *
 * `entry` is `null` when the empty area below the tree was clicked, which means the
 * project root — that is the only way to create something at the top level once the tree
 * fills the pane.
 */
import { computed, nextTick, onMounted, ref } from 'vue'

import type { FileEntry } from '@/api/types'
import { t } from '@/i18n'
import type { Clipboard } from '@/stores/explorer'

interface MenuItem {
  action: ExplorerAction
  label: string
  shortcut?: string
  danger?: boolean
}

const props = defineProps<{
  /** Viewport coordinates of the right-click. */
  at: { x: number; y: number }
  /** The row the menu belongs to, or `null` for the project root. */
  entry: FileEntry | null
  /** How many rows the actions will apply to — the selection, or just this row. */
  count: number
  clipboard: Clipboard | null
  busy: boolean
}>()

const emit = defineEmits<{ close: []; run: [action: ExplorerAction] }>()

const ESTIMATED_HEIGHT = 366
const ESTIMATED_WIDTH = 224

const root = ref<HTMLElement | null>(null)

const position = computed(() => {
  const flipUp = props.at.y + ESTIMATED_HEIGHT > window.innerHeight && props.at.y > ESTIMATED_HEIGHT
  const flipLeft = props.at.x + ESTIMATED_WIDTH > window.innerWidth
  const top = flipUp ? Math.max(8, props.at.y - ESTIMATED_HEIGHT) : props.at.y
  const left = flipLeft ? Math.max(8, props.at.x - ESTIMATED_WIDTH) : props.at.x
  return {
    top: `${top}px`,
    left: `${left}px`,
    // The menu unfolds from the pointer that raised it, wherever the flip left that (§7):
    // the click in the menu's own frame, and the few pixels of travel turned round when the
    // menu sits above it.
    '--vxs-origin': `${props.at.x - left}px ${props.at.y - top}px`,
    '--vxs-arrive-y': flipUp ? '4px' : '-4px',
  }
})

/** What the menu says it is about, so no action is taken on an unnamed subject. */
const subject = computed(() => {
  if (!props.entry) return t('explorer.menu.projectRoot')
  return props.count > 1 ? t('explorer.menu.items', { count: props.count }) : props.entry.name
})

const pasteLabel = computed(() => {
  const board = props.clipboard
  if (!board) return t('explorer.menu.paste')
  const count = board.paths.length
  return board.mode === 'cut'
    ? t('explorer.menu.pasteMove', { count })
    : t('explorer.menu.pasteCopy', { count })
})

/**
 * The groups, in the order a file manager puts them. Everything past the first group needs
 * a subject, so the root menu offers creation alone.
 */
const groups = computed<MenuItem[][]>(() => {
  const creation: MenuItem[] = [
    { action: 'new-file', label: t('explorer.menu.newFile') },
    { action: 'new-folder', label: t('explorer.menu.newFolder') },
  ]
  const clipboardGroup: MenuItem[] = []
  if (props.entry) {
    clipboardGroup.push(
      { action: 'cut', label: t('explorer.menu.cut'), shortcut: 'Ctrl+X' },
      { action: 'copy', label: t('common.copy'), shortcut: 'Ctrl+C' },
    )
  }
  if (props.clipboard?.paths.length) {
    clipboardGroup.push({ action: 'paste', label: pasteLabel.value, shortcut: 'Ctrl+V' })
  }

  const edit: MenuItem[] = []
  if (props.entry && props.count === 1) {
    edit.push({ action: 'rename', label: t('explorer.menu.rename'), shortcut: 'F2' })
  }
  if (props.entry) edit.push({ action: 'duplicate', label: t('explorer.menu.duplicate') })

  // One window per invocation, so this is offered for a single subject only — a menu that
  // opened five file manager windows would be a menu nobody uses twice. The project root
  // is a legitimate subject here, unlike everywhere else in this group: "show me where
  // this project lives" is exactly the question, and the root always exists on disk.
  const paths: MenuItem[] = [
    ...(props.entry
      ? [
          { action: 'copy-path' as const, label: t('explorer.menu.copyPath') },
          { action: 'copy-relative-path' as const, label: t('explorer.menu.copyRelativePath') },
        ]
      : []),
    ...(props.count === 1 || !props.entry
      ? [{ action: 'open-in-system' as const, label: t('explorer.menu.openInSystem') }]
      : []),
  ]

  const destructive: MenuItem[] = props.entry
    ? [{ action: 'delete', label: t('explorer.menu.delete'), shortcut: 'Del', danger: true }]
    : []

  return [creation, edit, clipboardGroup, paths, destructive].filter((group) => group.length)
})

onMounted(async () => {
  await nextTick()
  root.value?.querySelector<HTMLElement>('[data-menu-entry]')?.focus()
})

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
    return
  }
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  const items = [...(root.value?.querySelectorAll<HTMLElement>('[data-menu-entry]') ?? [])]
  if (!items.length) return
  event.preventDefault()
  const index = items.indexOf(document.activeElement as HTMLElement)
  const step = event.key === 'ArrowDown' ? 1 : -1
  items[(index + step + items.length) % items.length].focus()
}
</script>

<template>
  <Teleport to="body">
    <div
      class="fixed inset-0 z-50"
      @pointerdown="emit('close')"
      @contextmenu.prevent="emit('close')"
    />

    <div
      ref="root"
      class="vxs-card fixed z-50 max-h-[80vh] w-56 overflow-y-auto rounded-md border border-line bg-elevated py-1 shadow-lg"
      :style="position"
      role="menu"
      :aria-label="t('explorer.menu.actionsFor', { subject })"
      @keydown="onKeydown"
    >
      <p class="truncate px-2 pt-0.5 pb-1 text-small text-faint" :title="subject">
        {{ subject }}
      </p>

      <div
        v-for="(group, index) in groups"
        :key="index"
        :class="index ? 'border-t border-line-subtle pt-1 mt-1' : ''"
      >
        <button
          v-for="item in group"
          :key="item.action"
          type="button"
          data-menu-entry
          role="menuitem"
          class="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left disabled:opacity-40"
          :class="item.danger ? 'text-danger hover:bg-danger-soft' : 'hover:bg-hover'"
          :disabled="busy"
          @click="emit('run', item.action)"
        >
          <span class="min-w-0 flex-1 truncate">{{ item.label }}</span>
          <kbd v-if="item.shortcut" class="shrink-0 font-mono text-[10px] text-faint">
            {{ item.shortcut }}
          </kbd>
        </button>
      </div>
    </div>
  </Teleport>
</template>
