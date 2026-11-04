<script setup lang="ts">
/**
 * The block's context menu (block-editor spec §4.5).
 *
 * Renders whatever `menuFor` handed it — the component knows about *kinds* of control, never
 * about kinds of block. Adding a property to a block type is a change to `block-menu.ts`
 * alone, which is what stops this file from growing a branch per node type.
 *
 * §10: `Escape` closes it and focus goes back to the editor, arrows walk the entries, and
 * every entry is reachable without a mouse.
 */
import { ChevronRight } from 'lucide-vue-next'
import { computed, nextTick, ref, watch } from 'vue'

import { t } from '@/i18n'
import type { BlockMenuModel, MenuAction } from '@/lib/editor/block-menu'
import { searchLanguages } from '@/lib/editor/languages'
import type { TransformTarget } from '@/lib/editor/transform'

const props = defineProps<{
  model: BlockMenuModel
  /** Viewport coordinates of the click or of the handle. */
  at: { x: number; y: number }
}>()

const emit = defineEmits<{ close: []; transform: [target: TransformTarget] }>()

const ESTIMATED_HEIGHT = 380
const ESTIMATED_WIDTH = 232

const root = ref<HTMLElement | null>(null)
const submenuOpen = ref(false)
const languageQuery = ref('')

const entries = computed<MenuAction[]>(() => [...props.model.clipboard, ...props.model.actions])

const position = computed(() => {
  const flipUp = props.at.y + ESTIMATED_HEIGHT > window.innerHeight && props.at.y > ESTIMATED_HEIGHT
  const flipLeft = props.at.x + ESTIMATED_WIDTH > window.innerWidth
  return {
    top: flipUp ? `${Math.max(8, props.at.y - ESTIMATED_HEIGHT)}px` : `${props.at.y}px`,
    left: flipLeft ? `${Math.max(8, props.at.x - ESTIMATED_WIDTH)}px` : `${props.at.x}px`,
  }
})

const languages = computed(() => searchLanguages(languageQuery.value))

watch(
  () => props.model.row.pos,
  async () => {
    submenuOpen.value = false
    languageQuery.value = ''
    await nextTick()
    root.value?.querySelector<HTMLElement>('[data-menu-entry]')?.focus()
  },
  { immediate: true },
)

/** Arrow keys walk the focusable entries; `Escape` gives the editor its focus back (§10). */
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
    return
  }
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  const focusable = [...(root.value?.querySelectorAll<HTMLElement>('[data-menu-entry]') ?? [])]
  if (!focusable.length) return
  event.preventDefault()
  const index = focusable.indexOf(document.activeElement as HTMLElement)
  const step = event.key === 'ArrowDown' ? 1 : -1
  focusable[(index + step + focusable.length) % focusable.length].focus()
}

function run(action: MenuAction) {
  action.run()
  emit('close')
}
</script>

<template>
  <Teleport to="body">
    <!-- Click-catcher, so a click anywhere else dismisses the menu. -->
    <div class="block-menu-backdrop" @pointerdown="emit('close')" @contextmenu.prevent="emit('close')" />

    <div
      ref="root"
      class="block-menu"
      :style="position"
      role="menu"
      :aria-label="model.title"
      @keydown="onKeydown"
    >
      <p class="block-menu-title">{{ model.title }}</p>

      <!-- §4.6. Absent entirely for a table, a divider or an unsupported block. -->
      <div v-if="model.transforms.length" class="block-menu-section">
        <button
          type="button"
          data-menu-entry
          class="block-menu-item"
          role="menuitem"
          :aria-expanded="submenuOpen"
          @click="submenuOpen = !submenuOpen"
        >
          <span class="block-menu-label">{{ t('editor.blockMenu.turnInto') }}</span>
          <ChevronRight class="block-menu-chevron" :class="submenuOpen && 'is-open'" />
        </button>

        <div v-if="submenuOpen" class="block-menu-submenu">
          <button
            v-for="entry in model.transforms"
            :key="entry.target"
            type="button"
            data-menu-entry
            class="block-menu-item"
            :class="entry.current && 'is-current'"
            role="menuitemradio"
            :aria-checked="entry.current"
            @click="((emit('transform', entry.target)), emit('close'))"
          >
            <span class="block-menu-label">{{ entry.label }}</span>
          </button>
        </div>
      </div>

      <!-- §4.5: only what Markdown can express. Edited here, never in a dialog. -->
      <div v-if="model.properties.length" class="block-menu-section">
        <div v-for="property in model.properties" :key="property.id" class="block-menu-property">
          <span class="block-menu-property-label">{{ property.label }}</span>

          <select
            v-if="property.kind === 'select'"
            data-menu-entry
            class="block-menu-control"
            :value="property.value"
            @change="property.set(($event.target as HTMLSelectElement).value)"
          >
            <option v-for="option in property.options" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>

          <template v-else-if="property.kind === 'search'">
            <input
              v-model="languageQuery"
              data-menu-entry
              class="block-menu-control"
              :placeholder="property.options.find((o) => o.value === property.value)?.label ?? t('common.search')"
            />
            <ul class="block-menu-options">
              <li v-for="option in languages" :key="option.value">
                <button
                  type="button"
                  class="block-menu-option"
                  :class="option.value === property.value && 'is-current'"
                  @click="((property.set(option.value)), emit('close'))"
                >
                  {{ option.label }}
                </button>
              </li>
            </ul>
          </template>

          <input
            v-else-if="property.kind === 'number'"
            type="number"
            data-menu-entry
            class="block-menu-control"
            :value="property.value"
            :min="property.min"
            @change="property.set(Number(($event.target as HTMLInputElement).value))"
          />

          <input
            v-else-if="property.kind === 'text'"
            data-menu-entry
            class="block-menu-control"
            :value="property.value"
            :placeholder="property.placeholder"
            @change="property.set(($event.target as HTMLInputElement).value)"
          />

          <label v-else-if="property.kind === 'switch'" class="block-menu-switch">
            <input
              type="checkbox"
              data-menu-entry
              :checked="property.value"
              @change="property.set(($event.target as HTMLInputElement).checked)"
            />
          </label>

          <div v-else class="block-menu-buttons">
            <button
              v-for="button in property.buttons"
              :key="button.id"
              type="button"
              data-menu-entry
              class="block-menu-small"
              @click="((button.run()), emit('close'))"
            >
              {{ button.label }}
            </button>
          </div>
        </div>
      </div>

      <div class="block-menu-section">
        <button
          v-for="action in entries"
          :key="action.id"
          type="button"
          data-menu-entry
          class="block-menu-item"
          :class="action.danger && 'is-danger'"
          role="menuitem"
          @click="run(action)"
        >
          <span class="block-menu-label">{{ action.label }}</span>
          <kbd v-if="action.shortcut" class="block-menu-shortcut">{{ action.shortcut }}</kbd>
        </button>
      </div>
    </div>
  </Teleport>
</template>
