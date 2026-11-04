<script setup lang="ts">
/**
 * A picture inside the editor (block-editor spec §3.4).
 *
 * Resolution is a *view* concern and never touches the node, exactly as it is for a wiki
 * link: what goes back to disk is `attrs.src`, the reference as the author wrote it, and this
 * component only decides what to fetch for it. That is what lets a document full of pictures
 * be opened, edited and saved without a single `![...]` line moving (§8.2).
 *
 * A remote picture is shown as a **link** and never fetched, which is the rule
 * `MarkdownText.vue` already follows for the task transcript: these documents are written by
 * agents, and an `<img>` would make the page call a host the *model* chose, on open, before
 * anyone had read the document it arrived in.
 *
 * **The width is the one thing edited here** (§3.4). A selected picture carries a slider over
 * its lower edge, and moving it writes `{width=…}` after the reference — through
 * `updateAttributes`, so it is an ordinary transaction: undoable (a drag is one step, since
 * ProseMirror's history groups what arrives together), marks the document dirty and is saved
 * on the debounce. The control is not part of the document: it exists only while the picture
 * is selected, and Tiptap's node view keeps ProseMirror's hands off an `<input>` and a
 * `<button>` inside it.
 */
import { NodeSelection } from '@tiptap/pm/state'
import { nodeViewProps, NodeViewWrapper } from '@tiptap/vue-3'
import { RotateCcw } from 'lucide-vue-next'
import { computed, nextTick, ref, watch } from 'vue'

import { docAssetUrl } from '@/api/client'
import { t } from '@/i18n'
import { documentPathOf } from '@/lib/editor/document-context'
import { imageLabel, isRemoteImage } from '@/lib/editor/images'
import { safeLinkHref } from '@/lib/links'
import { isImageWidth } from '@/lib/md/image-width'
import { useDocsStore } from '@/stores/docs'

// `nodeViewProps` rather than a hand-written `defineProps<…>`: an SFC's inferred props never
// structurally match Tiptap's `NodeViewProps` (see `.claude/rules/frontend-vue.md`).
const props = defineProps(nodeViewProps)

const docs = useDocsStore()

const src = computed(() => String(props.node.attrs.src ?? ''))
const title = computed(() => (props.node.attrs.title as string | null) || null)
const label = computed(() => imageLabel(src.value, props.node.attrs.alt as string | null))
const remote = computed(() => isRemoteImage(src.value))
const remoteHref = computed(() => remote.value ? safeLinkHref(src.value) : null)

/**
 * The document the reference is relative to. Read from the store rather than passed down: a
 * node view is mounted outside the page's component tree, so there is no parent to take a
 * prop from — the same reason a wiki link raises a DOM event instead of emitting one.
 */
const url = computed(() => {
  // A reader outside Documents (the graph page) names its own document on the editor.
  const document = documentPathOf(props.editor) ?? docs.open?.path
  if (!document || remote.value || !src.value) return null
  return docAssetUrl(document, src.value)
})

const failed = ref(false)
// A new reference is a new attempt: keeping the failure would leave a corrected path showing
// the old error until the document was reopened.
watch(url, () => {
  failed.value = false
})

/** The width the file names, or `null` for the picture's own size. */
const width = computed(() => {
  const value = props.node.attrs.width
  return isImageWidth(value) ? value : null
})

/** Below a thumbnail the slider stops meaning anything. */
const MIN_WIDTH = 32

const img = ref<HTMLImageElement | null>(null)
/** How wide the picture may be drawn — the block it sits in, which `max-width` caps it at. */
const room = ref(0)
/** How wide it is drawn now: where the slider starts for a picture the file gives no width. */
const drawn = ref(0)
/**
 * The slider's top, fixed when it appears rather than derived from the value: a range that grew
 * with the value moved the thumb away from the pointer on every step of a drag.
 */
const max = ref(MIN_WIDTH)

function measure(): void {
  const element = img.value
  if (!element) return
  const block = element.closest('p, h1, h2, h3, h4, h5, h6, .block-editor')
  room.value = Math.round(block?.clientWidth ?? 0)
  drawn.value = Math.round(element.getBoundingClientRect().width)
  max.value = Math.max(MIN_WIDTH, room.value, width.value ?? drawn.value)
}

// Measured when the control appears, not kept up to date: nothing moves the picture while it
// is selected but the slider itself, and an observer per picture would be paid by every one.
watch(
  () => props.selected,
  (selected) => {
    if (selected) measure()
  },
)

const value = computed(() => width.value ?? drawn.value)

/**
 * Write the width onto the node and keep the picture selected, in one transaction.
 *
 * Not `updateAttributes`: on an atom that is `setNodeMarkup`, which *replaces* the node, and a
 * node selection does not survive its node being replaced — the first version lost it after one
 * step of a drag, and the slider went with it, out from under the pointer. So the selection is
 * put back on the same position. Still a replace step rather than `setNodeAttribute`, because
 * the history folds a drag into one undo only when each step touches the last one's range, and
 * an attribute step touches none.
 */
function setWidth(next: number | null): void {
  const pos = props.getPos()
  if (typeof pos !== 'number') return
  props.editor.commands.command(({ tr }) => {
    tr.setNodeMarkup(pos, undefined, { ...props.node.attrs, width: next })
    tr.setSelection(NodeSelection.create(tr.doc, pos))
    return true
  })
}

function onInput(event: Event): void {
  const next = Math.round(Number((event.target as HTMLInputElement).value))
  if (isImageWidth(next) && next !== width.value) setWidth(next)
}

function reset(): void {
  setWidth(null)
  // Back at its own size, which is where the slider has to start from again.
  void nextTick(() => requestAnimationFrame(measure))
}
</script>

<template>
  <NodeViewWrapper as="span" class="doc-image" :class="selected && 'doc-image-selected'">
    <img
      v-if="url && !failed"
      ref="img"
      class="doc-image-img"
      :src="url"
      :alt="label"
      :title="title ?? label"
      :style="width === null ? undefined : { width: `${width}px` }"
      draggable="false"
      @load="selected && measure()"
      @error="failed = true"
    />
    <!-- Not fetched, so it reads as what it is: somewhere else. -->
    <a
      v-else-if="remoteHref"
      class="doc-image-remote"
      :href="remoteHref"
      target="_blank"
      rel="noopener noreferrer"
      :title="src"
      >{{ label }}</a
    >
    <span v-else-if="remote" class="doc-image-remote" :title="src">{{ label }}</span>
    <span v-else class="doc-image-missing" :title="src">
      {{ t('editor.image.missing', { label }) }}
    </span>

    <!-- Only while selected, and never the document's: `contenteditable="false"` so the caret
         cannot land in it, and the press and the drag stop here so moving the slider is not a
         drag of the picture it sits on. -->
    <span
      v-if="selected && editor.isEditable && url && !failed"
      class="doc-image-size"
      contenteditable="false"
      draggable="false"
      @pointerdown.stop
      @mousedown.stop
      @dragstart.prevent.stop
    >
      <input
        type="range"
        class="doc-image-size-range"
        :min="MIN_WIDTH"
        :max="max"
        step="1"
        :value="value"
        :title="t('editor.image.width')"
        :aria-label="t('editor.image.width')"
        @input="onInput"
      />
      <span class="doc-image-size-value">{{ value }} px</span>
      <button
        type="button"
        class="doc-image-size-reset"
        :disabled="width === null"
        :title="t('editor.image.widthReset')"
        :aria-label="t('editor.image.widthReset')"
        @click="reset"
      >
        <RotateCcw class="size-3.5" />
      </button>
    </span>
  </NodeViewWrapper>
</template>
