/**
 * The block types Markdown has and Tiptap does not (block-editor spec §3.1, §3.3, §3.4).
 *
 * Four of them, and each exists for a reason the schema cannot express any other way:
 *
 * - `wikiLink` is an **atom**, so the caret cannot get inside `[[test-plan]]` and edit it
 *   into something that no longer resolves (§5). Deleting it removes the whole link.
 * - `callout` and `toggle` are the two directives §2 allows, with an optional
 *   `directiveTitle` child rather than a string attribute — a label may carry inline markup,
 *   and a string would flatten it on the first save.
 * - `unsupported*` carries an mdast node the editor does not model and gives it back
 *   untouched. This is §8.3 made structural: anything unknown is *held*, not re-typed.
 *
 * Rendering is plain `renderHTML` wherever the node has no behaviour, and a node view only
 * where one is needed. A node view costs a component instance per block on a 3000-line
 * document (§9).
 */

import { mergeAttributes, Node, nodeInputRule } from '@tiptap/core'
import { VueNodeViewRenderer } from '@tiptap/vue-3'
import type { PhrasingContent, RootContent } from 'mdast'

import CalloutView from '@/components/docs/nodes/CalloutView.vue'
import ToggleView from '@/components/docs/nodes/ToggleView.vue'
import UnsupportedView from '@/components/docs/nodes/UnsupportedView.vue'
import WikiLinkView from '@/components/docs/nodes/WikiLinkView.vue'
import { CALLOUT_KINDS } from '@/lib/md/prosemirror'

/**
 * `[[target]]` / `[[target|label]]`.
 *
 * `value` is the raw text between the brackets and the only thing written back — §5 asks
 * for byte-for-byte, so `target` and `label` are conveniences for the view, never sources.
 */
export const WikiLink = Node.create({
  name: 'wikiLink',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,

  addAttributes() {
    return {
      value: { default: '' },
      target: { default: '' },
      label: { default: null },
    }
  },

  parseHTML() {
    return [{ tag: 'span[data-wiki-link]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['span', mergeAttributes({ 'data-wiki-link': '' }, HTMLAttributes)]
  },

  renderText({ node }) {
    // What lands in the clipboard, which §5 says must be the source form.
    return `[[${node.attrs.value}]]`
  },

  addNodeView() {
    return VueNodeViewRenderer(WikiLinkView)
  },

  /**
   * Typing `[[a-document]]` makes a link there and then.
   *
   * The autocomplete §5 asks for arrives with the rest of the wiki-link work, but this
   * cannot wait for it: without a rule, typed brackets stay text, and the serializer
   * correctly escapes text — so `[[dash]]` would reach the file as `\[\[dash]]` and stop
   * being a link at all. Silently rewriting what someone typed is the one thing §0.5 rules
   * out, so the rule lands with the node.
   */
  addInputRules() {
    return [
      nodeInputRule({
        // No capture group, deliberately: `nodeInputRule` narrows the replaced range to
        // `match[1]` when there is one, which would leave the brackets in the paragraph
        // with the node between them — and serialize as `\[\[[[dash]]\]\]`.
        find: /\[\[[^[\]]+\]\]$/,
        type: this.type,
        getAttributes: (match) => {
          const value = match[0].slice(2, -2)
          const bar = value.indexOf('|')
          return {
            value,
            target: (bar === -1 ? value : value.slice(0, bar)).trim(),
            label: bar === -1 ? null : value.slice(bar + 1).trim() || null,
          }
        },
      }),
    ]
  },
})

/** The label of a callout or a toggle. Inline content, so `**bold**` in it survives. */
export const DirectiveTitle = Node.create({
  name: 'directiveTitle',
  content: 'inline*',
  defining: true,
  selectable: false,

  parseHTML() {
    return [{ tag: 'div[data-directive-title]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['div', mergeAttributes({ 'data-directive-title': '' }, HTMLAttributes), 0]
  },
})

/** `:::note[Title]` … `:::` — one of the four kinds in §3.1, and nothing else. */
export const Callout = Node.create({
  name: 'callout',
  group: 'block',
  // The title, when present, is always first; `block+` is what lets a callout hold lists,
  // code and tables, which §3.3 requires.
  content: 'directiveTitle block+',
  defining: true,

  addAttributes() {
    return {
      kind: {
        default: 'note',
        parseHTML: (element) => element.getAttribute('data-kind') ?? 'note',
        renderHTML: (attributes) => ({ 'data-kind': attributes.kind }),
      },
    }
  },

  parseHTML() {
    return [{ tag: 'div[data-callout]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['div', mergeAttributes({ 'data-callout': '' }, HTMLAttributes), 0]
  },

  addNodeView() {
    return VueNodeViewRenderer(CalloutView)
  },
})

/**
 * `:::toggle[Title]{open}` … `:::`
 *
 * `open` is the *file's* default, not the current state. §3.3 is explicit that clicking a
 * toggle open must not dirty the document, so the collapsed state lives in the node view
 * and never reaches an attribute.
 */
export const Toggle = Node.create({
  name: 'toggle',
  group: 'block',
  content: 'directiveTitle block+',
  defining: true,

  addAttributes() {
    return {
      open: {
        default: false,
        parseHTML: (element) => element.hasAttribute('data-open'),
        renderHTML: (attributes) => (attributes.open ? { 'data-open': '' } : {}),
      },
    }
  },

  parseHTML() {
    return [{ tag: 'div[data-toggle]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['div', mergeAttributes({ 'data-toggle': '' }, HTMLAttributes), 0]
  },

  addNodeView() {
    return VueNodeViewRenderer(ToggleView)
  },
})

export const UnsupportedBlock = Node.create({
  name: 'unsupportedBlock',
  group: 'block',
  atom: true,
  selectable: true,
  draggable: true,

  addAttributes() {
    return { mdast: { default: null as RootContent | null } }
  },

  parseHTML() {
    return [{ tag: 'div[data-unsupported]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['div', mergeAttributes({ 'data-unsupported': '' }, HTMLAttributes)]
  },

  addNodeView() {
    return VueNodeViewRenderer(UnsupportedView)
  },
})

export const UnsupportedInline = Node.create({
  name: 'unsupportedInline',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,

  addAttributes() {
    return { mdast: { default: null as PhrasingContent | null } }
  },

  parseHTML() {
    return [{ tag: 'span[data-unsupported]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['span', mergeAttributes({ 'data-unsupported': '' }, HTMLAttributes)]
  },

  addNodeView() {
    return VueNodeViewRenderer(UnsupportedView)
  },
})

export const CALLOUT_KIND_LIST = CALLOUT_KINDS
