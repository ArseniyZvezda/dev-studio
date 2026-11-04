<script setup lang="ts">
/**
 * One message of prose, drawn as the Markdown it was written in.
 *
 * Agents answer in Markdown whether or not anyone asked them to — headings, bullet lists,
 * fenced code — and a transcript that showed the asterisks and backticks was harder to read
 * than the plain sentences they decorate. This is that text rendered.
 *
 * The Studio's own changelog is drawn with it too (`variant: 'document'`). Not because that
 * file is untrusted — it ships with the build — but because a second Markdown renderer
 * would be a second set of rules about what may render, and one is enough.
 *
 * **No HTML string is produced here, and none may be.** A transcript is model output, the
 * same threat model as a document (`.claude/rules/frontend-vue.md`), so the mdast tree is
 * mapped node by node onto vnodes: a node type this file does not name cannot render at
 * all, and an `html` node is shown as the text it is — exactly what the block editor does
 * with one (block-editor spec §3.4). The parser is the same `remark` + GFM pair as the
 * document layer, on the same options, so a table that round-trips there reads the same here.
 */
import type { Image, Link, List, ListItem, Root, RootContent, Table, TableRow } from 'mdast'
import remarkGfm from 'remark-gfm'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import { computed, h, type VNodeChild } from 'vue'

import { applyColorSpans, isTextColor, maskColors, GFM_OPTIONS } from '@/lib/md'
import { safeLinkHref } from '@/lib/links'

const props = withDefaults(
  defineProps<{
    text: string
    /**
     * How it is set, not what may render — the safety rules above are the same either way.
     *
     * `chat` is a message in a transcript: tight, and all six headings at one size, because
     * a heading there is a line of emphasis rather than a section. `document` is the
     * app's document typography, where the heading levels mean what they say — which is
     * what a changelog is made of.
     */
    variant?: 'chat' | 'document'
  }>(),
  { variant: 'chat' },
)

/** `parse()`, never `process()`: there is no transform to run and nothing to serialize. */
const parser = unified().use(remarkParse).use(remarkGfm, GFM_OPTIONS)

/**
 * `[shipped]{.green}` reads the same here as in a document (block-editor spec §3.2).
 *
 * Same masking pass, for the same reason: `[ok]` is a shortcut link reference and would be
 * one the moment a message happened to define `[ok]:`. There is no serializer on this side,
 * so nothing has to be written back — only recognised, and rendered as the closed list of
 * colour words the stylesheet knows.
 */
const tree = computed<Root>(() => {
  const masked = maskColors(props.text)
  const root = parser.parse(masked.text)
  applyColorSpans(root, masked, (markdown) => parser.parse(markdown))
  return root
})

const HEADINGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'] as const

function kids(node: { children?: RootContent[] }): VNodeChild[] {
  return (node.children ?? []).map(render)
}

function renderList(node: List): VNodeChild {
  const start = node.ordered && node.start != null && node.start !== 1 ? node.start : undefined
  return h(node.ordered ? 'ol' : 'ul', { start }, kids(node))
}

/**
 * `- [x] done` keeps its box, disabled: this is a record of what an agent wrote, not a form.
 * An item with no box is an ordinary `li` — GFM allows the two to share a list.
 */
function renderListItem(node: ListItem): VNodeChild {
  if (node.checked == null) return h('li', kids(node))
  return h('li', { class: 'task-item' }, [
    h('input', { type: 'checkbox', checked: node.checked, disabled: true }),
    h('div', { class: node.checked ? 'task-done' : undefined }, kids(node)),
  ])
}

function renderLink(node: Link): VNodeChild {
  const href = safeLinkHref(node.url)
  if (!href) return h('span', kids(node))
  return h(
    'a',
    { href, target: '_blank', rel: 'noopener noreferrer', title: node.title ?? node.url },
    kids(node),
  )
}

/**
 * An image is a link, never an `<img>`. Rendering one would make the browser fetch a host
 * the *model* chose, on open, before anyone had read the message it arrived in.
 */
function renderImage(node: Image): VNodeChild {
  const label = node.alt?.trim() || node.title?.trim() || node.url
  const href = safeLinkHref(node.url)
  if (!href) return h('span', label)
  return h('a', { href, target: '_blank', rel: 'noopener noreferrer', title: node.url }, label)
}

function renderTable(node: Table): VNodeChild {
  const align = node.align ?? []
  const cells = (row: TableRow, tag: 'th' | 'td') =>
    h(
      'tr',
      row.children.map((cell, column) =>
        h(tag, { style: align[column] ? { textAlign: align[column] } : undefined }, kids(cell)),
      ),
    )
  const [head, ...body] = node.children
  return h('table', [
    head ? h('thead', cells(head, 'th')) : null,
    body.length ? h('tbody', body.map((row) => cells(row, 'td'))) : null,
  ])
}

function render(node: RootContent): VNodeChild {
  switch (node.type) {
    case 'text':
      return node.value
    case 'paragraph':
      return h('p', kids(node))
    case 'heading':
      return h(HEADINGS[Math.min(Math.max(node.depth, 1), 6) - 1], kids(node))
    case 'strong':
      return h('strong', kids(node))
    case 'emphasis':
      return h('em', kids(node))
    case 'delete':
      return h('del', kids(node))
    case 'colorSpan':
      // Checked again here rather than trusted from the parser: this file's rule is that a
      // node it does not name cannot render, and a class attribute is exactly the kind of
      // thing that must not arrive from model output unfiltered.
      return isTextColor(node.color) ? h('span', { class: node.color }, kids(node)) : kids(node)
    case 'inlineCode':
      return h('code', node.value)
    case 'code':
      return h('pre', h('code', node.value))
    case 'blockquote':
      return h('blockquote', kids(node))
    case 'list':
      return renderList(node)
    case 'listItem':
      return renderListItem(node)
    case 'link':
      return renderLink(node)
    case 'image':
      return renderImage(node)
    case 'table':
      return renderTable(node)
    case 'break':
      return h('br')
    case 'thematicBreak':
      return h('hr')
    default:
      // Everything this file does not model — `html` above all, but also footnotes and
      // reference links — falls back to its own text. Nothing is dropped, nothing is markup.
      if ('children' in node) return h('span', kids(node))
      return 'value' in node ? node.value : null
  }
}

/**
 * A functional component rather than a template: mdast is a tree of a dozen-odd node types
 * and a template able to walk it would be a `v-if` chain per level. Declared once per
 * instance so `:is` is stable, with the parse cached in a `computed` behind it — only the
 * vnodes are rebuilt on a re-render, which is what Vue requires of them.
 */
function Body(): VNodeChild {
  return kids(tree.value)
}
</script>

<template>
  <div class="markdown-body" :class="{ 'markdown-chat': variant === 'chat' }">
    <component :is="Body" />
  </div>
</template>
