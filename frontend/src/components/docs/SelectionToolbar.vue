<script setup lang="ts">
/**
 * Formatting for the selected text (block-editor spec §4.4).
 *
 * `mousedown.prevent` on every control is what keeps the selection alive: without it the
 * press moves focus, the selection collapses, and the button applies its mark to nothing.
 */
import type { Editor } from '@tiptap/core'
import { Ban, Bold, ChevronDown, Code, Italic, Link2, Palette, Strikethrough } from 'lucide-vue-next'
import { nextTick, ref, watch } from 'vue'

import { t, type MessageKey } from '@/i18n'
import { textColorLabel } from '@/lib/editor/text-color'
import type { ToolbarPosition } from '@/lib/editor/use-selection-toolbar'
import { sameColor, TEXT_COLOR_CHOICES, type TextColor } from '@/lib/md'

const props = defineProps<{
  editor: Editor
  position: ToolbarPosition
  editingLink: boolean
  linkDraft: string
  pickingColor: boolean
}>()

const emit = defineEmits<{
  'update:linkDraft': [value: string]
  startLink: []
  applyLink: []
  cancelLink: []
  startColor: []
  applyColor: [color: TextColor | null]
  cancelColor: []
  /** §4.4 lists "Turn into" for the whole block; the block's own menu is where it lives. */
  turnInto: [at: { x: number; y: number }]
}>()

const input = ref<HTMLInputElement | null>(null)

watch(
  () => props.editingLink,
  async (editing) => {
    if (!editing) return
    await nextTick()
    input.value?.focus()
    input.value?.select()
  },
)

/** The word the file would carry, or `null` where the selection has no colour. */
function activeColor(): string | null {
  const color = props.editor.getAttributes('textColor').color
  return typeof color === 'string' && color ? color : null
}

/**
 * The colour, as an inline custom property.
 *
 * The toolbar is teleported to `<body>`, so it is outside the `.markdown-body` subtree the
 * colour classes are scoped to — and scoping them any wider would put ten single-word class
 * names into the app's global namespace. This is the third level of `.claude/rules/styling.md`:
 * a colour known only at runtime, scoped to one element by a custom property.
 */
function swatch(color: string): Record<string, string> {
  return { '--vxs-md-color': `var(--vxs-md-${color === 'grey' ? 'gray' : color})` }
}

/** Titles as keys, resolved in the template, so they follow the language on screen. */
const MARKS: { name: string; icon: typeof Bold; title: MessageKey; run: () => void }[] = [
  { name: 'bold', icon: Bold, title: 'editor.toolbar.bold', run: () => props.editor.chain().focus().toggleBold().run() },
  { name: 'italic', icon: Italic, title: 'editor.toolbar.italic', run: () => props.editor.chain().focus().toggleItalic().run() },
  { name: 'strike', icon: Strikethrough, title: 'editor.toolbar.strike', run: () => props.editor.chain().focus().toggleStrike().run() },
  { name: 'code', icon: Code, title: 'editor.toolbar.code', run: () => props.editor.chain().focus().toggleCode().run() },
]
</script>

<template>
  <Teleport to="body">
    <div
      class="selection-toolbar"
      :style="{ top: `${position.top}px`, left: `${position.left}px` }"
      role="toolbar"
      :aria-label="t('editor.toolbar.label')"
    >
      <form v-if="editingLink" class="selection-toolbar-link" @submit.prevent="emit('applyLink')">
        <input
          ref="input"
          :value="linkDraft"
          placeholder="https://"
          class="selection-toolbar-input"
          @input="emit('update:linkDraft', ($event.target as HTMLInputElement).value)"
          @keydown.esc.prevent="emit('cancelLink')"
        />
        <button type="submit" class="selection-toolbar-apply">{{ t('common.apply') }}</button>
      </form>

      <!-- The swatches take the toolbar over, as the link field above does: the strip is
           narrower than the field, and a popover would need its own dismissal rules. -->
      <div
        v-else-if="pickingColor"
        class="selection-toolbar-colors"
        role="group"
        :aria-label="t('editor.toolbar.textColor')"
        @keydown.esc.prevent="emit('cancelColor')"
      >
        <button
          type="button"
          class="selection-toolbar-swatch selection-toolbar-swatch-none"
          :class="!activeColor() && 'is-active'"
          :title="t('editor.toolbar.noColor')"
          @mousedown.prevent="emit('applyColor', null)"
        >
          <Ban class="size-3" />
        </button>
        <button
          v-for="color in TEXT_COLOR_CHOICES"
          :key="color"
          type="button"
          class="selection-toolbar-swatch"
          :class="sameColor(activeColor(), color) && 'is-active'"
          :style="swatch(color)"
          :title="textColorLabel(color)"
          @mousedown.prevent="emit('applyColor', color)"
        />
      </div>

      <template v-else>
        <button
          v-for="mark in MARKS"
          :key="mark.name"
          type="button"
          class="selection-toolbar-button"
          :class="editor.isActive(mark.name) && 'is-active'"
          :title="t(mark.title)"
          @mousedown.prevent="mark.run()"
        >
          <component :is="mark.icon" class="size-3.5" />
        </button>
        <button
          type="button"
          class="selection-toolbar-button"
          :class="editor.isActive('link') && 'is-active'"
          :title="t('editor.toolbar.link')"
          @mousedown.prevent="emit('startLink')"
        >
          <Link2 class="size-3.5" />
        </button>
        <button
          type="button"
          class="selection-toolbar-button selection-toolbar-color"
          :class="activeColor() && 'is-active'"
          :style="activeColor() ? swatch(activeColor()!) : undefined"
          :title="t('editor.toolbar.textColor')"
          @mousedown.prevent="emit('startColor')"
        >
          <Palette class="size-3.5" />
        </button>

        <span class="selection-toolbar-divider" />

        <button
          type="button"
          class="selection-toolbar-turn"
          :title="t('editor.toolbar.turnIntoHint')"
          @mousedown.prevent="
            emit('turnInto', {
              x: ($event.currentTarget as HTMLElement).getBoundingClientRect().left,
              y: ($event.currentTarget as HTMLElement).getBoundingClientRect().bottom + 6,
            })
          "
        >
          {{ t('editor.blockMenu.turnInto') }}
          <ChevronDown class="size-3" />
        </button>
      </template>
    </div>
  </Teleport>
</template>
