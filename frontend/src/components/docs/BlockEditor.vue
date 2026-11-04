<script setup lang="ts">
/**
 * The block editor (block-editor spec §4).
 *
 * One view, not three: what is on screen is the document, and Markdown is what it is stored
 * as. The mapping in `lib/md/` is what makes that safe — this component owns interaction and
 * nothing about the file format.
 *
 * Two things here are load-bearing and easy to undo by accident:
 *
 * - **Serialising is not free**, so it happens once per save rather than once per keystroke.
 *   The editor reports `dirty` immediately (§7 wants the indicator honest straight away) and
 *   emits the Markdown only when the debounce fires, on blur, or on `Ctrl+S`.
 * - **The envelope is captured at open and reused for every save.** It carries the
 *   frontmatter, the line ending and the final newline (§7), none of which the tree holds.
 *
 * The parent mounts this with `:key` on the document path. A different document is a
 * different editor: that is what resets the undo history at close, as §7 requires, without
 * anyone having to remember to clear it.
 */
import { EditorContent, useEditor } from '@tiptap/vue-3'
import { GripVertical, Plus } from 'lucide-vue-next'
import { onBeforeUnmount, ref, watch } from 'vue'

import BlockMenu from '@/components/docs/BlockMenu.vue'
import SelectionToolbar from '@/components/docs/SelectionToolbar.vue'
import SlashMenu from '@/components/docs/SlashMenu.vue'
import WikiMenu from '@/components/docs/WikiMenu.vue'
import { t } from '@/i18n'
import { applyTransform, menuFor, type BlockMenuModel } from '@/lib/editor/block-menu'
import { rowAt } from '@/lib/editor/blocks'
import { createExtensions } from '@/lib/editor/extensions'
import { outlineOf, type Heading } from '@/lib/editor/outline'
import { createSlashState } from '@/lib/editor/slash-menu'
import { useBlockDrag } from '@/lib/editor/use-block-drag'
import { createWikiState } from '@/lib/editor/wiki-menu'
import { useSelectionToolbar } from '@/lib/editor/use-selection-toolbar'
import {
  dragCarriesFiles,
  receiveImageDrop,
  type ImageDropOptions,
} from '@/lib/editor/image-drop'
import { uploadDocAsset } from '@/api/client'
import { parseDocument, serializeDocument, type DocumentEnvelope } from '@/lib/md/document'
import { mdastToProseMirror, proseMirrorToMdast, type PmNode } from '@/lib/md/prosemirror'

const props = defineProps<{
  source: string
  /** The open document's path, which a dropped picture is placed beside (§3.4). */
  path: string
  /** Candidates for the `[[` autocomplete (§5). */
  documents: { path: string; title: string }[]
}>()

const emit = defineEmits<{
  /** The user changed something. Cheap, immediate, and what drives the §7 indicator. */
  dirty: []
  /** Markdown ready to write. Debounced, on blur, and on Ctrl+S. */
  change: [markdown: string]
  save: []
  headings: [headings: Heading[]]
  /** Position of the heading the reader is under, for the outline's highlight (§6). */
  active: [pos: number | null]
  navigate: [target: string]
  /** §5: `[[` matched nothing and the user chose to create the document. */
  createDocument: [name: string]
  /** Something the user should see went wrong — a dropped picture that could not be added. */
  failed: [message: string]
}>()

/** §7: "600–800 мс после последнего ввода". */
const AUTOSAVE_DELAY_MS = 700
/** The outline must not be rebuilt on every keystroke (§9). */
const OUTLINE_DELAY_MS = 250

const container = ref<HTMLElement | null>(null)

// Not reactive: the envelope belongs to the file this editor was opened on, and a reactive
// copy would invite something to swap it under a pending save.
let envelope: DocumentEnvelope = { frontmatter: '', eol: '\n', finalNewline: true }
/**
 * Whether anything has been typed since the Markdown was last handed out to be written.
 *
 * A save answers with the bytes it stored and the page adopts that answer, so the document
 * comes back through `source` a moment later — older than the screen if the user kept
 * typing. While this is true the incoming source is **behind** the editor by definition, and
 * replacing the document with it erases what was typed during the round trip. That is
 * content loss (§0.5), not a stale view; `reload()` is how the page asks for a replacement
 * it means.
 */
let unsaved = false
let autosave: ReturnType<typeof setTimeout> | null = null
let outlineTimer: ReturnType<typeof setTimeout> | null = null

function initialContent(source: string): PmNode {
  const parsed = parseDocument(source)
  envelope = parsed.envelope
  return mdastToProseMirror(parsed.root)
}

const slash = createSlashState()
const wiki = createWikiState()

/**
 * §5: the offer to create a document `[[` did not find.
 *
 * The link is written straight away, so the sentence being typed is not interrupted; the
 * page creates the file. Both happen even though the document is not open yet, which is what
 * makes `[[` a way of *planning* documents rather than only referencing them.
 */
wiki.selectNew = (name: string) => {
  wiki.select({ path: '', title: name, target: name, hint: null })
  emit('createDocument', name)
}

/**
 * How a dropped picture is added (§3.4). One object, used by the ProseMirror plugin *and* by
 * the margin handler below, so both take the same path.
 *
 * `upload` is a function rather than a value because the extension is built once while the
 * path is a prop: a captured value would keep uploading beside the first document opened.
 */
const imageDrop: ImageDropOptions = {
  upload: () => (props.path ? (file: File) => uploadDocAsset(props.path, file) : null),
  onError: (message) => emit('failed', message),
}

/** Whether a drag carrying files is over the document, for the drop affordance. */
const dropping = ref(false)
// `dragenter`/`dragleave` fire for every child the pointer crosses, so the state is a depth
// count rather than a boolean set by the last event to arrive.
let dragDepth = 0

function onDragEnter(event: DragEvent) {
  if (!dragCarriesFiles(event)) return
  dragDepth += 1
  dropping.value = true
}

function onDragLeave(event: DragEvent) {
  if (!dragCarriesFiles(event)) return
  dragDepth = Math.max(0, dragDepth - 1)
  if (!dragDepth) dropping.value = false
}

/**
 * A drop that never reached ProseMirror.
 *
 * The content column is narrower than the scroller, so the whole left and right margin —
 * where the gutter lives, and a natural place to aim — is outside `view.dom` and the plugin's
 * `handleDrop` never sees it. The position is probed at the same y with x pulled inside the
 * column, which is the trick the gutter already uses to find the row beside the pointer.
 */
function onDrop(event: DragEvent) {
  dragDepth = 0
  dropping.value = false
  const files = [...(event.dataTransfer?.files ?? [])]
  if (!files.length || !editor.value) return
  const view = editor.value.view
  if (view.dom.contains(event.target as Node)) return

  event.preventDefault()
  const box = view.dom.getBoundingClientRect()
  const inside = (value: number, low: number, high: number) =>
    Math.min(Math.max(value, low + 1), high - 1)
  const at = view.posAtCoords({
    left: inside(event.clientX, box.left, box.right),
    top: inside(event.clientY, box.top, box.bottom),
  })
  void receiveImageDrop(view, files, at?.pos ?? view.state.doc.content.size, imageDrop)
}

const editor = useEditor({
  content: initialContent(props.source),
  extensions: createExtensions({
    slash,
    wiki,
    documents: () => props.documents,
    images: imageDrop,
  }),
  editorProps: {
    attributes: {
      // `markdown-body` is the app's existing document styling (§4.10 — inherit the theme,
      // do not invent a second one). `block-editor` adds only what an editable one needs.
      class: 'markdown-body block-editor',
      spellcheck: 'false',
    },
  },
  onUpdate: ({ transaction }) => {
    if (!transaction.docChanged) return
    unsaved = true
    emit('dirty')
    schedule()
    scheduleOutline()
  },
  onBlur: () => flush(),
  onCreate: () => refreshOutline(),
})

const {
  gutter,
  indicator,
  preview,
  marquee,
  dragging,
  onPointerMove,
  onPointerLeave,
  startMarquee,
  startDrag,
  insertBelow,
} = useBlockDrag(editor, container)

const toolbar = useSelectionToolbar(editor)

/** §4.5: the block's own menu, opened by right-click or by the gutter handle. */
const menu = ref<{ model: BlockMenuModel; at: { x: number; y: number } } | null>(null)

function copyText(text: string): void {
  void navigator.clipboard.writeText(text).catch(() => {})
}

function openMenu(at: { x: number; y: number }, pos: number): void {
  const instance = editor.value
  if (!instance) return
  const row = rowAt(instance.state.doc, pos)
  if (!row) return
  menu.value = { model: menuFor(instance, row, copyText), at }
}

/**
 * §4.5: our menu replaces the native one everywhere inside the editor.
 *
 * With a live text selection it additionally carries Copy / Cut / Paste, which is what the
 * native menu would have been wanted for — `menuFor` decides that, from the selection.
 */
function onContextMenu(event: MouseEvent): void {
  const instance = editor.value
  if (!instance) return
  const found = instance.view.posAtCoords({ left: event.clientX, top: event.clientY })
  if (!found) return
  event.preventDefault()
  openMenu({ x: event.clientX, y: event.clientY }, found.inside >= 0 ? found.inside : found.pos)
}

function openMenuFromHandle(event: MouseEvent, pos: number): void {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  openMenu({ x: rect.right + 4, y: rect.top }, pos)
}

function closeMenu(): void {
  menu.value = null
  editor.value?.commands.focus()
}

function onTransform(target: Parameters<typeof applyTransform>[2]): void {
  const instance = editor.value
  const row = menu.value?.model.row
  if (instance && row) applyTransform(instance, row, target)
}

function serialize(): string {
  const document = editor.value?.getJSON() as PmNode | undefined
  if (!document) return props.source
  return serializeDocument(proseMirrorToMdast(document), envelope)
}

/** Serialize *and* mark it as handed over — for every caller about to write the file. */
function takeMarkdown(): string {
  unsaved = false
  return serialize()
}

function schedule(): void {
  if (autosave) clearTimeout(autosave)
  autosave = setTimeout(flush, AUTOSAVE_DELAY_MS)
}

/**
 * Write out a pending edit — on the debounce, on blur, on `Ctrl+S`, and before the page
 * switches documents.
 *
 * **The pending timer is the record that there is anything to write**, and the guard on it
 * is not an optimisation. `flush` is called on every document switch, so without it,
 * clicking through ten documents saves ten files that nobody edited — which is exactly the
 * check §8.2 spells out ("open ten documents, close them, Plastic is clean").
 */
function flush(): void {
  if (!autosave) return
  clearTimeout(autosave)
  autosave = null
  emit('change', takeMarkdown())
}

/** Drop the pending debounce without writing — what an explicit save supersedes. */
function cancelPending(): void {
  if (autosave) clearTimeout(autosave)
  autosave = null
}

function scheduleOutline(): void {
  if (outlineTimer) clearTimeout(outlineTimer)
  outlineTimer = setTimeout(refreshOutline, OUTLINE_DELAY_MS)
}

function refreshOutline(): void {
  outlineTimer = null
  const instance = editor.value
  if (instance) emit('headings', outlineOf(instance.state.doc))
}

/**
 * §5: a wiki link is not an anchor, so its node view raises a bubbling DOM event and the
 * page decides what "go there" means. Listening on the container rather than on each node
 * keeps that one listener however long the document is.
 */
function onWikiLink(event: Event): void {
  emit('navigate', String((event as CustomEvent<string>).detail ?? ''))
}

function onKeydown(event: KeyboardEvent): void {
  const modifier = event.ctrlKey || event.metaKey
  if (modifier && event.key.toLowerCase() === 's') {
    // The browser's "save page" is never what someone means inside an editor.
    event.preventDefault()
    // Not `flush()`: §4.8 calls this a *forced* save, and the pending debounce is the only
    // record `flush` has that anything is worth writing. After a save that failed — a
    // read-only file, the backend restarting — nothing is pending and the document is still
    // dirty, and that is exactly when someone reaches for `Ctrl+S`. The page decides what
    // saving means; the store is what refuses to write a document nobody edited (§8.2).
    cancelPending()
    emit('save')
    return
  }
  // §4.8: `Ctrl+K` is the link. The browser would otherwise take it for its address bar.
  if (modifier && !event.shiftKey && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    if (!editor.value?.state.selection.empty) toolbar.startLink()
    return
  }

  /**
   * §10: every action in §4.5 has to be reachable without a mouse.
   *
   * The gutter appears on hover, so its handle is not a keyboard target — the menu needs its
   * own way in. `Shift+F10` and the Menu key are the platform's own "open the context menu"
   * keys, which is what someone using the keyboard will already be pressing.
   */
  if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
    const instance = editor.value
    if (!instance) return
    event.preventDefault()
    const caret = instance.view.coordsAtPos(instance.state.selection.from)
    openMenu({ x: caret.left, y: caret.bottom + 4 }, instance.state.selection.from)
  }
}

// A pending edit must not be lost to navigation; the parent also calls `flush` before it
// switches documents.
onBeforeUnmount(() => {
  if (outlineTimer) clearTimeout(outlineTimer)
  flush()
})

/**
 * The source changed underneath us: the file was re-read, or a save's answer was adopted.
 *
 * Adopting it is only safe while nothing is waiting to be written. A save answers with what
 * it stored, which is **older than the screen** whenever the user kept typing during the
 * round trip — so with `unsaved` set, the incoming text is behind the document by definition
 * and taking it would delete the difference. Discarding an edit on purpose goes through
 * `reload()`, where the user asked for it.
 */
watch(
  () => props.source,
  (next) => {
    const instance = editor.value
    if (!instance || unsaved || next === serialize()) return
    instance.commands.setContent(initialContent(next), { emitUpdate: false })
    refreshOutline()
  },
)

/**
 * Which heading the reader is under (§6).
 *
 * Measured from the DOM rather than from positions: what counts is what is on screen, and a
 * heading's position says nothing about how tall the blocks before it are. Coalesced into a
 * frame, because scroll fires far more often than the answer changes.
 */
let scrolling = false

function onScroll(): void {
  if (scrolling) return
  scrolling = true
  requestAnimationFrame(() => {
    scrolling = false
    const instance = editor.value
    const scroller = container.value
    if (!instance || !scroller) return

    const limit = scroller.getBoundingClientRect().top + 24
    let active: number | null = null
    for (const heading of outlineOf(instance.state.doc)) {
      const element = elementAt(heading.pos)
      if (!element) continue
      if (element.getBoundingClientRect().top > limit) break
      active = heading.pos
    }
    emit('active', active)
  })
}

function elementAt(pos: number): HTMLElement | null {
  const instance = editor.value
  if (!instance) return null
  const dom = instance.view.domAtPos(pos + 1).node
  return dom instanceof HTMLElement ? dom : dom.parentElement
}

defineExpose({
  flush,
  takeMarkdown,
  /**
   * Throw away what is in the editor and take `source` as it stands — the answer to the
   * §14.5 prompt, and the one path allowed to discard unsaved edits, because the user chose
   * it. Everything else goes through the watcher above, which refuses to.
   */
  reload: () => {
    const instance = editor.value
    if (!instance) return
    cancelPending()
    unsaved = false
    instance.commands.setContent(initialContent(props.source), { emitUpdate: false })
    refreshOutline()
  },
  focus: () => editor.value?.commands.focus(),
  /** Put a heading at the top of the scroller — what clicking the outline does (§6). */
  scrollTo: (heading: Heading) => {
    elementAt(heading.pos)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  },
})
</script>

<template>
  <div
    ref="container"
    class="block-editor-scroller relative min-h-0 flex-1 overflow-auto"
    :class="dropping && 'block-editor-dropping'"
    @wiki-link="onWikiLink"
    @keydown="onKeydown"
    @scroll.passive="onScroll"
    @pointermove="onPointerMove"
    @pointerleave="onPointerLeave"
    @pointerdown="startMarquee"
    @contextmenu="onContextMenu"
    @dragenter="onDragEnter"
    @dragleave="onDragLeave"
    @dragover.prevent
    @drop="onDrop"
  >
    <EditorContent v-if="editor" :editor="editor" class="block-editor-column" />

    <!-- The row under the pointer, outlined. Drawn as a sibling rather than a class on the
         node, for the reason the whole module already follows: ProseMirror rewrites a node's
         attributes when it next syncs it, so anything visual that is not in the document has
         to live outside it. Behind nothing and clickable through — it is a hint, not a
         target. -->
    <div
      v-if="gutter && !dragging"
      class="block-hover"
      :style="{
        top: `${gutter.top}px`,
        left: `${gutter.left}px`,
        width: `${gutter.width}px`,
        height: `${gutter.height}px`,
      }"
    />

    <!-- §4.1: outside the content column, so appearing never moves the text. The hover
         band is the whole left margin, which is what stops a diagonal reach from losing it.
         Its x comes from the measured column (`--vxs-doc-column-left`); the offset that
         hangs it off that edge stays in CSS, beside the button sizes it depends on. -->
    <div
      v-if="gutter && !dragging"
      class="block-gutter"
      :style="{
        top: `${gutter.top}px`,
        height: `${Math.min(gutter.height, 28)}px`,
        '--vxs-doc-column-left': `${gutter.left}px`,
      }"
    >
      <button
        type="button"
        class="block-gutter-button"
        :title="t('editor.gutter.insert')"
        @click="insertBelow(gutter.pos)"
      >
        <Plus class="size-3.5" />
      </button>
      <!-- §4.5: clicking the handle opens the same menu as a right-click. Holding and
           moving is a drag — the threshold in `use-block-drag` is what tells them apart. -->
      <button
        type="button"
        class="block-gutter-button block-gutter-handle"
        :title="t('editor.gutter.handle')"
        @pointerdown.prevent="startDrag($event, gutter.pos)"
        @click="openMenuFromHandle($event, gutter.pos)"
      >
        <GripVertical class="size-3.5" />
      </button>
    </div>

    <!-- §4.2: full width of the column, shifted and shortened to show the nesting level. -->
    <div
      v-if="indicator"
      class="block-drop-indicator"
      :style="{
        top: `${indicator.top}px`,
        left: `${indicator.left}px`,
        width: `${indicator.width}px`,
      }"
    />

    <!-- §4.7: sweeping the empty space beside the text selects what it crosses. -->
    <div
      v-if="marquee"
      class="block-marquee"
      :style="{
        top: `${marquee.top}px`,
        left: `${marquee.left}px`,
        width: `${marquee.width}px`,
        height: `${marquee.height}px`,
      }"
    />

    <SlashMenu :state="slash" />
    <WikiMenu :state="wiki" />

    <BlockMenu
      v-if="menu"
      :model="menu.model"
      :at="menu.at"
      @close="closeMenu"
      @transform="onTransform"
    />

    <SelectionToolbar
      v-if="editor && toolbar.position.value && !slash.open"
      :editor="editor"
      :position="toolbar.position.value"
      :editing-link="toolbar.editingLink.value"
      :link-draft="toolbar.linkDraft.value"
      :picking-color="toolbar.pickingColor.value"
      @update:link-draft="toolbar.linkDraft.value = $event"
      @start-link="toolbar.startLink()"
      @apply-link="toolbar.applyLink()"
      @cancel-link="toolbar.editingLink.value = false"
      @start-color="toolbar.startColor()"
      @apply-color="toolbar.applyColor($event)"
      @cancel-color="toolbar.pickingColor.value = false"
      @turn-into="editor && openMenu($event, editor.state.selection.from)"
    />

    <!-- Inside the root element, not beside it: a second root turns the component into a
         fragment, and the sizing classes the page puts on it are then silently dropped. -->
    <Teleport to="body">
      <div
        v-if="preview"
        class="block-drag-preview"
        :style="{ top: `${preview.y + 12}px`, left: `${preview.x + 12}px` }"
      >
        {{ preview.text }}
        <span v-if="preview.count > 1" class="block-drag-count">{{ preview.count }}</span>
      </div>
    </Teleport>
  </div>
</template>
