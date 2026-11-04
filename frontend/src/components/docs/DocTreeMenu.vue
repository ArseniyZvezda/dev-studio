<script setup lang="ts">
/**
 * The context menu of a row in the Documents tree (block-editor spec §5.2). A folder's creates a
 * document in it, renames it, deletes it, and picks its colour and its icon; a document's deletes
 * it.
 *
 * **One menu for the whole tree**, opened at the pointer with the row as its subject — the
 * pattern of the Explorer's `FileContextMenu`, for its reason: `DocTree` is recursive, and a menu
 * mounted per row would be a menu per row.
 *
 * New document, Rename and Delete are actions, and each hands over to its dialog — nothing is
 * created, renamed or removed from the menu itself. A colour or an icon is a *choice*: it applies
 * at once, the row behind the menu changes with it, and the menu stays open so both can be picked
 * in one visit — the current one marked, `Escape` or a click outside to leave. The icons are
 * drawn in the folder's colour, so once one is picked the grid previews the row.
 *
 * Keyboard: the arrows walk the entries, and inside a group laid out as a grid `ArrowUp` and
 * `ArrowDown` move by a row of it rather than one cell (§10).
 */
import { Ban, FilePlus2, Folder, Pencil, Trash2 } from 'lucide-vue-next'
import { computed, nextTick, onMounted, ref } from 'vue'

import type { DocFolderStyle } from '@/api/types'
import { t } from '@/i18n'
import {
  FOLDER_COLORS,
  FOLDER_ICONS,
  folderColorLabel,
  folderColorValue,
  folderIcon,
  type FolderColor,
} from '@/lib/folder-style'
import type { DocTreeNode } from '@/stores/docs'

const props = defineProps<{
  /** Viewport coordinates of the right-click. */
  at: { x: number; y: number }
  /** The row — a folder or a document, its path relative to the docs root. */
  node: DocTreeNode
  /** How the folder looks now; `null` for the plain folder, and for a document. */
  current: DocFolderStyle | null
}>()

const emit = defineEmits<{
  close: []
  create: []
  rename: []
  delete: []
  restyle: [patch: { color?: string | null; icon?: string | null }]
}>()

/** Icons per row, which is also how far `ArrowUp` and `ArrowDown` move inside the grid. */
const ICON_COLUMNS = 8

const root = ref<HTMLElement | null>(null)
/** Placed at the pointer first, then pulled back inside the window once its size is known. */
const position = ref({ top: props.at.y, left: props.at.x })

/** What the row says: a folder's name, a document's title. */
const name = computed(() =>
  props.node.isFolder ? props.node.name : (props.node.entry?.title ?? props.node.name),
)
const color = computed(() => props.current?.color ?? null)
const iconName = computed(() => folderIcon(props.current?.icon)?.name ?? null)
/**
 * What the grid draws its icons in: the folder's colour, so a pick previews the row. With no
 * colour it is the secondary text rather than the accent a plain folder wears — the accent is
 * whatever the project picked, and a pale one on a light theme leaves forty icons to squint at.
 */
const preview = computed(() => folderColorValue(color.value) ?? 'var(--vxs-text-secondary)')

onMounted(async () => {
  await nextTick()
  const menu = root.value
  if (!menu) return
  const { width, height } = menu.getBoundingClientRect()
  const margin = 8
  position.value = {
    top: Math.max(margin, Math.min(props.at.y, window.innerHeight - height - margin)),
    left: Math.max(margin, Math.min(props.at.x, window.innerWidth - width - margin)),
  }
  menu.querySelector<HTMLElement>('[data-menu-entry]')?.focus()
})

function pickColor(next: FolderColor | null): void {
  if (next !== color.value) emit('restyle', { color: next })
}

function pickIcon(next: string | null): void {
  if (next !== iconName.value) emit('restyle', { icon: next })
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
    return
  }
  const entries = [...(root.value?.querySelectorAll<HTMLElement>('[data-menu-entry]') ?? [])]
  const index = entries.indexOf(document.activeElement as HTMLElement)
  if (!entries.length) return

  let next: number | null = null
  if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = entries.length - 1
  else if (event.key === 'ArrowRight') next = index + 1
  else if (event.key === 'ArrowLeft') next = index - 1
  else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    const step = event.key === 'ArrowDown' ? 1 : -1
    next = index + step
    // Inside a grid, a row at a time; past its first or last row, out of the grid altogether —
    // the colour strip is one row, so `ArrowDown` from any swatch reaches the icons.
    const group = entries[index]?.closest<HTMLElement>('[data-menu-columns]')
    if (group) {
      const members = entries.filter((entry) => group.contains(entry))
      const target = index + step * Number(group.dataset.menuColumns)
      if (members.includes(entries[target])) next = target
      else {
        next = step > 0
          ? entries.indexOf(members[members.length - 1]) + 1
          : entries.indexOf(members[0]) - 1
      }
    }
  } else {
    return
  }
  event.preventDefault()
  entries[(next + entries.length) % entries.length]?.focus()
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
      class="vxs-card fixed z-50 rounded-md border border-line bg-elevated p-1.5 shadow-lg"
      :class="node.isFolder ? 'w-64' : 'w-56'"
      :style="{
        top: `${position.top}px`,
        left: `${position.left}px`,
        // Unfolds from the pointer that raised it, even once pulled back inside the window (§7).
        '--vxs-origin': `${at.x - position.left}px ${at.y - position.top}px`,
      }"
      role="menu"
      :aria-label="
        node.isFolder ? t('docs.menu.folderLabel', { name }) : t('docs.menu.documentLabel', { name })
      "
      @keydown="onKeydown"
    >
      <p class="truncate px-1.5 pb-1 text-small text-faint" :title="node.path">{{ name }}</p>

      <template v-if="node.isFolder">
        <button
          type="button"
          role="menuitem"
          data-menu-entry
          class="flex w-full items-center gap-2 rounded-sm px-1.5 py-1 text-left hover:bg-hover"
          @click="emit('create')"
        >
          <FilePlus2 class="size-3.5 shrink-0 text-faint" />
          <span class="min-w-0 flex-1 truncate">{{ t('docs.menu.newDocument') }}</span>
        </button>
        <button
          type="button"
          role="menuitem"
          data-menu-entry
          class="flex w-full items-center gap-2 rounded-sm px-1.5 py-1 text-left hover:bg-hover"
          @click="emit('rename')"
        >
          <Pencil class="size-3.5 shrink-0 text-faint" />
          <span class="min-w-0 flex-1 truncate">{{ t('docs.menu.rename') }}</span>
        </button>
      </template>
      <!-- The colour is on the icon and the label, like the other entries' icons: `base.css`
           gives every `<button>` an unlayered `color: inherit`, which outranks a utility on it. -->
      <button
        type="button"
        role="menuitem"
        data-menu-entry
        class="flex w-full items-center gap-2 rounded-sm px-1.5 py-1 text-left hover:bg-danger-soft"
        @click="emit('delete')"
      >
        <Trash2 class="size-3.5 shrink-0 text-danger" />
        <span class="min-w-0 flex-1 truncate text-danger">{{ t('docs.menu.delete') }}</span>
      </button>

      <template v-if="node.isFolder">
        <div
          class="mt-1.5 border-t border-line-subtle pt-1.5"
          role="group"
          :aria-label="t('docs.menu.colour')"
        >
          <p class="px-1.5 pb-1 text-small text-faint">{{ t('docs.menu.colour') }}</p>
          <div
            class="flex items-center justify-between px-1"
            :data-menu-columns="FOLDER_COLORS.length + 1"
          >
            <button
              type="button"
              role="menuitemradio"
              data-menu-entry
              class="grid size-4.5 place-items-center rounded-pill border border-line bg-inset text-muted ring-offset-1 ring-offset-transparent hover:ring-2 hover:ring-line-strong"
              :class="color === null && 'ring-2 ring-primary'"
              :aria-checked="color === null"
              :title="t('docs.menu.noColour')"
              :aria-label="t('docs.menu.noColour')"
              @click="pickColor(null)"
            >
              <Ban class="size-3" />
            </button>
            <button
              v-for="choice in FOLDER_COLORS"
              :key="choice"
              type="button"
              role="menuitemradio"
              data-menu-entry
              class="size-4.5 rounded-pill ring-offset-1 ring-offset-transparent hover:ring-2 hover:ring-line-strong"
              :class="color === choice && 'ring-2 ring-primary'"
              :style="{ background: folderColorValue(choice) ?? undefined }"
              :aria-checked="color === choice"
              :title="folderColorLabel(choice)"
              :aria-label="folderColorLabel(choice)"
              @click="pickColor(choice)"
            />
          </div>
        </div>

        <div
          class="mt-1.5 border-t border-line-subtle pt-1.5"
          role="group"
          :aria-label="t('docs.menu.icon')"
        >
          <p class="px-1.5 pb-1 text-small text-faint">{{ t('docs.menu.icon') }}</p>
          <div
            class="grid grid-cols-8 gap-0.5"
            :data-menu-columns="ICON_COLUMNS"
            :style="{ color: preview }"
          >
            <button
              type="button"
              role="menuitemradio"
              data-menu-entry
              class="grid size-7 place-items-center rounded-md hover:bg-hover"
              :class="iconName === null && 'bg-active ring-1 ring-line-strong'"
              :aria-checked="iconName === null"
              :title="t('docs.menu.plainFolder')"
              :aria-label="t('docs.menu.plainFolder')"
              @click="pickIcon(null)"
            >
              <Folder class="size-4" />
            </button>
            <button
              v-for="icon in FOLDER_ICONS"
              :key="icon.name"
              type="button"
              role="menuitemradio"
              data-menu-entry
              class="grid size-7 place-items-center rounded-md hover:bg-hover"
              :class="iconName === icon.name && 'bg-active ring-1 ring-line-strong'"
              :aria-checked="iconName === icon.name"
              :title="icon.label"
              :aria-label="icon.label"
              @click="pickIcon(icon.name)"
            >
              <component :is="icon.component" class="size-4" />
            </button>
          </div>
        </div>
      </template>
    </div>
  </Teleport>
</template>
