<script setup lang="ts">
/**
 * One row of a file tree — the Explorer's and the Documents section's alike (§6.2).
 *
 * The two trees used to draw their rows separately and had drifted apart: different
 * heights, icon sizes and indents, and only one of them showed version control status.
 * This is the one row both mount now, so a document seen under Documents and the same
 * file seen in the Explorer are the same row, marks included.
 *
 * Version control status colours the name (§6.2, §9.6), and this is the **only** place a
 * file's state is drawn — the Version Control page is about the system, not the files.
 * Colour is never the only signal: a changed file carries its status letter at the end of
 * the row, and a folder holding changes underneath carries a dot with the count in its
 * tooltip — otherwise a red row and a green one are the same row to a colour-blind reader,
 * and a change three levels down is invisible until everything is expanded.
 *
 * The vocabulary lives in `stores/vcs.ts`: a table per component is how two surfaces end
 * up disagreeing about what amber means. `vcsPath` is the entry's path in the **project's**
 * frame, which is the frame `/api/vcs/status` speaks — the Documents tree passes
 * `voxarium_studio/docs/<path>`, never the document's own path.
 *
 * Expansion, selection and loading are the caller's state, passed in: the Explorer keeps
 * them in its store so a selection survives a re-read, the Documents tree keeps a collapsed
 * set of its own. The row only draws them.
 */
import { ChevronRight, File as FileIcon, Folder, FolderOpen } from 'lucide-vue-next'
import { computed, type Component } from 'vue'

import { t } from '@/i18n'
import { VCS_MARKS, useVcsStore } from '@/stores/vcs'

const props = withDefaults(
  defineProps<{
    /** What the row says — a file name, or a document's title. */
    name: string
    isDir: boolean
    depth: number
    expanded?: boolean
    /** Part of the selection: the accent-tinted row. */
    selected?: boolean
    /** The cursor when it is not also selected — the Explorer's `selectedPath`. */
    active?: boolean
    /** Cut to the clipboard: still here, and leaving. */
    dimmed?: boolean
    /** A directory whose children are being read. */
    loading?: boolean
    /** The file's icon, by what the caller knows of it; folders draw their own. */
    icon?: Component | null
    /**
     * The icon a folder chose for itself (block-editor spec §5.2), in place of the folder
     * glyph. Its own prop because `icon` is the Explorer's guess for a *file* and arrives for
     * folders too, where it must go on meaning nothing.
     */
    folderIcon?: Component | null
    /** A CSS colour for the icon — a token such as `var(--vxs-md-green)` — or null for the default. */
    iconColor?: string | null
    /** The path `/api/vcs/status` knows this entry by, or null for a tree without marks. */
    vcsPath?: string | null
    /**
     * Reachable with Tab, with Enter and Space as a click — for a tree that has no key
     * handling of its own. The Explorer's rows leave this off: its container is the one
     * focus target there and moves a cursor between rows itself.
     */
    focusable?: boolean
    title?: string
  }>(),
  {
    expanded: false,
    selected: false,
    active: false,
    dimmed: false,
    loading: false,
    icon: null,
    folderIcon: null,
    iconColor: null,
    vcsPath: null,
    focusable: false,
    title: undefined,
  },
)

/**
 * `click` carries the event so a caller can read its modifiers — the Explorer builds a
 * range on Shift and toggles on Ctrl — and a keyboard activation arrives as the keyboard
 * event it was, which carries the same modifier fields.
 */
const emit = defineEmits<{
  click: [event: MouseEvent | KeyboardEvent]
  toggle: []
  context: [event: MouseEvent]
}>()

const vcs = useVcsStore()

const mark = computed(() => {
  if (props.vcsPath === null) return null
  const kind = vcs.kindFor(props.vcsPath)
  return kind ? VCS_MARKS[kind] : null
})

/** The quieter treatment: a folder that has changes somewhere below it, but is not one. */
const dirtyUnder = computed(
  () => props.isDir && !mark.value && props.vcsPath !== null && vcs.dirtyUnder(props.vcsPath),
)

/**
 * What the dot means, in words. A collapsed folder is the one place a tree can hide a
 * change outright, so the count is what says whether opening it is worth the click.
 */
const dirtyTitle = computed(() => {
  const count = props.vcsPath === null ? 0 : vcs.changesUnder(props.vcsPath)
  return t('ui.treeRow.changesUnder', { count })
})

/** A chosen icon stays put; the plain folder opens and closes with the row. */
const icon = computed(() => {
  if (props.isDir) return props.folderIcon ?? (props.expanded ? FolderOpen : Folder)
  return props.icon ?? FileIcon
})

/**
 * Only the icon takes a folder's colour. The *name* is the one place version control status is
 * drawn (§6.2), and a folder painted red must not read as a conflict.
 */
const iconClass = computed(() => {
  if (props.iconColor) return ''
  return props.isDir ? 'text-accent' : 'text-faint'
})

function onKeydown(event: KeyboardEvent): void {
  if (!props.focusable || (event.key !== 'Enter' && event.key !== ' ')) return
  event.preventDefault()
  emit('click', event)
}
</script>

<template>
  <!-- `vxs-row`: under the pointer the row's content nudges 2px along while its fill stays put
       (`base.css`, the motion section) — which is why the right padding is never under 2px. -->
  <div
    class="vxs-row flex cursor-default items-center gap-1 rounded-sm pr-2 select-none"
    :class="[
      selected ? 'bg-accent-soft text-primary' : 'hover:bg-hover',
      active && !selected ? 'bg-hover' : '',
      dimmed ? 'opacity-50' : '',
      focusable
        ? 'outline-none focus-visible:ring-1 focus-visible:ring-accent focus-visible:ring-inset'
        : '',
    ]"
    :style="{ paddingLeft: `${depth * 12 + 4}px`, height: 'var(--vxs-row-height)' }"
    :role="focusable ? 'button' : undefined"
    :tabindex="focusable ? 0 : undefined"
    :title="title"
    @click="emit('click', $event)"
    @keydown="onKeydown"
    @contextmenu.prevent.stop="emit('context', $event)"
  >
    <button
      v-if="isDir"
      class="grid size-4 shrink-0 place-items-center text-faint hover:text-primary"
      :tabindex="focusable ? -1 : undefined"
      @click.stop="emit('toggle')"
    >
      <!-- One chevron that turns (`vxs-chevron`, `base.css`), not two glyphs swapped. -->
      <ChevronRight class="vxs-chevron size-3.5" :class="expanded ? 'rotate-90' : ''" />
    </button>
    <span v-else class="size-4 shrink-0" />

    <component
      :is="icon"
      class="size-4 shrink-0"
      :class="iconClass"
      :style="iconColor ? { color: iconColor } : undefined"
    />
    <!-- The colour is set on the name itself, so it wins over the selected row's own. -->
    <span class="truncate" :class="mark?.class">{{ name }}</span>

    <span v-if="loading" class="ml-auto text-small text-faint">…</span>
    <span
      v-else-if="mark"
      class="ml-auto shrink-0 font-mono text-[10px]"
      :class="mark.class"
      :title="mark.label"
      >{{ mark.letter }}</span
    >
    <span
      v-else-if="dirtyUnder"
      class="ml-auto shrink-0 text-[10px] text-warning"
      :title="dirtyTitle"
      >•</span
    >
  </div>
</template>
