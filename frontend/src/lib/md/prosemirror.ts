/**
 * mdast ↔ ProseMirror, the bridge the block editor stands on (block-editor spec §3).
 *
 * Plain JSON in both directions, with no editor instance involved, so the mapping can be
 * tested the same way §8 is: as functions over documents. The rule that decides every
 * ambiguous case is §8.3 — anything this file does not model is carried through as its
 * original mdast node inside an `unsupported` block, never dropped and never guessed at.
 *
 * Two shape differences do most of the work here. mdast nests inline markup (`strong`
 * containing `emphasis` containing `text`) while ProseMirror puts flat text under a set of
 * marks, so one side has to be rebuilt from the other. And mdast's list carries the
 * checkbox on the item while ProseMirror needs to know the list's kind up front.
 */

import type {
  BlockContent,
  DefinitionContent,
  ListItem,
  Nodes,
  Paragraph,
  PhrasingContent,
  Root,
  RootContent,
  TableCell,
} from 'mdast'

import { imageWidth, isImageWidth } from './image-width'
import type { WikiLink } from './wikilink'

export interface PmMark {
  type: string
  attrs?: Record<string, unknown>
}

export interface PmNode {
  type: string
  attrs?: Record<string, unknown>
  content?: PmNode[]
  text?: string
  marks?: PmMark[]
}

/** The four callout kinds of §3.1. Any other directive name is not a callout. */
export const CALLOUT_KINDS = ['note', 'tip', 'warning', 'danger'] as const
export type CalloutKind = (typeof CALLOUT_KINDS)[number]

const CALLOUTS = new Set<string>(CALLOUT_KINDS)

/**
 * Outermost first. `code` is absent on purpose: inline code cannot contain markup in
 * Markdown, so it is always the leaf and is handled as one.
 *
 * `textColor` sits under `link` and over the rest, so a coloured phrase is written
 * `[**Blocked**]{.red}` rather than `**[Blocked]{.red}**` — the colour wraps the phrase,
 * which is how a person writes one. Under `link` because the other way round produces
 * `[[label]{.red}](url)`, and a bracket pair opening a link label is the one place where
 * two syntaxes that both start with `[` would have to be told apart by lookahead.
 */
const MARK_ORDER = ['link', 'textColor', 'bold', 'italic', 'strike'] as const

const MARK_TO_MDAST: Record<string, 'link' | 'strong' | 'emphasis' | 'delete'> = {
  link: 'link',
  bold: 'strong',
  italic: 'emphasis',
  strike: 'delete',
}

// --------------------------------------------------------------------- mdast → pm

function unsupported(node: Nodes, inline: boolean, marks: PmMark[] = []): PmNode {
  return {
    type: inline ? 'unsupportedInline' : 'unsupportedBlock',
    attrs: { mdast: structuredClone(node) },
    ...(marks.length && { marks }),
  }
}

function inlineToPm(nodes: readonly PhrasingContent[], marks: PmMark[]): PmNode[] {
  const out: PmNode[] = []

  for (const node of nodes) {
    switch (node.type) {
      case 'text':
        if (node.value) out.push({ type: 'text', text: node.value, ...(marks.length && { marks }) })
        break
      case 'inlineCode':
        out.push({ type: 'text', text: node.value, marks: [...marks, { type: 'code' }] })
        break
      case 'strong':
        out.push(...inlineToPm(node.children, [...marks, { type: 'bold' }]))
        break
      case 'emphasis':
        out.push(...inlineToPm(node.children, [...marks, { type: 'italic' }]))
        break
      case 'delete':
        out.push(...inlineToPm(node.children, [...marks, { type: 'strike' }]))
        break
      case 'colorSpan':
        out.push(
          ...inlineToPm(node.children, [...marks, { type: 'textColor', attrs: { color: node.color } }]),
        )
        break
      case 'link':
        out.push(
          ...inlineToPm(node.children, [
            ...marks,
            { type: 'link', attrs: { href: node.url, title: node.title ?? null } },
          ]),
        )
        break
      case 'break':
        out.push({ type: 'hardBreak', ...(marks.length && { marks }) })
        break
      case 'image':
        // Marks reach the atoms too: a wiki link or an image inside `**…**` carries the
        // emphasis, and dropping it here is what splits one link into three on the way back.
        out.push({
          type: 'image',
          attrs: {
            src: node.url,
            alt: node.alt ?? null,
            title: node.title ?? null,
            // Only when the file names one, so a picture without is the node it always was.
            ...(imageWidth(node) !== null && { width: imageWidth(node) }),
          },
          ...(marks.length && { marks }),
        })
        break
      case 'wikiLink': {
        const link = node as WikiLink
        out.push({
          type: 'wikiLink',
          attrs: { value: link.value, target: link.target, label: link.label },
          ...(marks.length && { marks }),
        })
        break
      }
      default:
        out.push(unsupported(node, true, marks))
    }
  }

  return out
}

function paragraphToPm(node: Paragraph): PmNode {
  const content = inlineToPm(node.children, [])
  return content.length ? { type: 'paragraph', content } : { type: 'paragraph' }
}

/** A directive's label is a paragraph flagged by remark, and it is always the first child. */
function splitDirectiveLabel(
  children: readonly RootContent[],
): [label: Paragraph | null, body: RootContent[]] {
  const first = children[0]
  if (first?.type === 'paragraph' && first.data?.directiveLabel) {
    return [first, [...children.slice(1)]]
  }
  return [null, [...children]]
}

function directiveToPm(
  type: 'callout' | 'toggle',
  attrs: Record<string, unknown>,
  children: readonly RootContent[],
): PmNode {
  const [label, body] = splitDirectiveLabel(children)
  // The title node is always present, even for `:::note` with no label. That keeps it the
  // first child in every case, which is what lets a collapsed toggle hide "everything after
  // the title" — and an empty one is simply not written back (§3.3).
  const title: PmNode = { type: 'directiveTitle' }
  if (label) title.content = inlineToPm(label.children, [])

  const content = blocksToPm(body)
  // `block+` in the schema: an empty container still needs somewhere to put the caret.
  if (!content.length) content.push({ type: 'paragraph' })
  return { type, attrs, content: [title, ...content] }
}

function listToPm(node: Extract<RootContent, { type: 'list' }>): PmNode {
  const items = node.children
  const isTaskList = items.some((item) => item.checked !== null && item.checked !== undefined)

  const content = items.map((item: ListItem): PmNode => ({
    type: isTaskList ? 'taskItem' : 'listItem',
    ...(isTaskList && { attrs: { checked: item.checked ?? null } }),
    content: blocksToPm(item.children),
  }))

  if (isTaskList) return { type: 'taskList', content }
  return node.ordered
    ? { type: 'orderedList', attrs: { start: node.start ?? 1 }, content }
    : { type: 'bulletList', content }
}

function cellToPm(cell: TableCell, header: boolean): PmNode {
  const content = inlineToPm(cell.children, [])
  return {
    type: header ? 'tableHeader' : 'tableCell',
    attrs: { colspan: 1, rowspan: 1, colwidth: null },
    content: [content.length ? { type: 'paragraph', content } : { type: 'paragraph' }],
  }
}

function tableToPm(node: Extract<RootContent, { type: 'table' }>): PmNode {
  return {
    type: 'table',
    // Alignment is a property of the column in GFM, not of a cell, so it lives on the
    // table. §4.5 edits it; here it only has to survive.
    attrs: { align: node.align?.map((value) => value ?? null) ?? [] },
    content: node.children.map((row, index) => ({
      type: 'tableRow',
      content: row.children.map((cell) => cellToPm(cell, index === 0)),
    })),
  }
}

function blockToPm(node: RootContent): PmNode {
  switch (node.type) {
    case 'paragraph':
      return paragraphToPm(node)
    case 'heading':
      return { type: 'heading', attrs: { level: node.depth }, content: inlineToPm(node.children, []) }
    case 'thematicBreak':
      return { type: 'horizontalRule' }
    case 'blockquote':
      return { type: 'blockquote', content: blocksToPm(node.children) }
    case 'code':
      return {
        type: 'codeBlock',
        attrs: { language: node.lang ?? null, meta: node.meta ?? null },
        ...(node.value && { content: [{ type: 'text', text: node.value }] }),
      }
    case 'list':
      return listToPm(node)
    case 'table':
      return tableToPm(node)
    case 'containerDirective':
      if (CALLOUTS.has(node.name)) {
        return directiveToPm('callout', { kind: node.name }, node.children)
      }
      if (node.name === 'toggle') {
        return directiveToPm('toggle', { open: node.attributes?.open !== undefined && node.attributes.open !== null }, node.children)
      }
      return unsupported(node, false)
    default:
      return unsupported(node, false)
  }
}

function blocksToPm(nodes: readonly RootContent[]): PmNode[] {
  return nodes.map(blockToPm)
}

export function mdastToProseMirror(root: Root): PmNode {
  const content = blocksToPm(root.children)
  // ProseMirror's `doc` is `block+`; an empty file is one empty paragraph, which is also
  // what §4.10 asks the user to be shown.
  return { type: 'doc', content: content.length ? content : [{ type: 'paragraph' }] }
}

// --------------------------------------------------------------------- pm → mdast

function markKey(mark: PmMark): string {
  return mark.attrs ? `${mark.type}:${JSON.stringify(mark.attrs)}` : mark.type
}

function hasMark(node: PmNode, key: string): boolean {
  return (node.marks ?? []).some((mark) => markKey(mark) === key)
}

function withoutMark(node: PmNode, key: string): PmNode {
  return { ...node, marks: (node.marks ?? []).filter((mark) => markKey(mark) !== key) }
}

function leafToMdast(node: PmNode): PhrasingContent[] {
  switch (node.type) {
    case 'text': {
      if (!node.text) return []
      const code = (node.marks ?? []).find((mark) => mark.type === 'code')
      return [code ? { type: 'inlineCode', value: node.text } : { type: 'text', value: node.text }]
    }
    case 'hardBreak':
      return [{ type: 'break' }]
    case 'image': {
      const width = node.attrs?.width
      return [
        {
          type: 'image',
          url: String(node.attrs?.src ?? ''),
          alt: (node.attrs?.alt as string | null) ?? null,
          title: (node.attrs?.title as string | null) ?? null,
          // A width the parser would refuse is not written at all (`image-width.ts`).
          ...(isImageWidth(width) && { data: { width } }),
        },
      ]
    }
    case 'wikiLink':
      return [
        {
          type: 'wikiLink',
          value: String(node.attrs?.value ?? ''),
          target: String(node.attrs?.target ?? ''),
          label: (node.attrs?.label as string | null) ?? null,
        } as WikiLink,
      ]
    case 'unsupportedInline':
      return [node.attrs?.mdast as PhrasingContent]
    default:
      return []
  }
}

/** Marks that wrap everything else a node carries, in this order — see `MARK_ORDER`. */
const OUTERMOST = ['link', 'textColor'] as const

/**
 * The mark to open at `index`: a link, then a colour span, if the node has one; otherwise, of
 * its bold, italic and strike, the one whose run from here is longest, ties going to
 * `MARK_ORDER`.
 *
 * Longest-run because the mark that spans a phrase has to wrap the ones that do not. `***a** b*`
 * is emphasis around strong; opening bold first — the fixed order alone used to decide it — gave
 * strong around emphasis followed by more emphasis, which prints as `***a****&#x20;b*` and then
 * `***a****\*\*…`: a delimiter run CommonMark reads back differently, so every save of such a
 * line lost its structure or gained two literal asterisks (found 2026-09-11 in an exported
 * document whose `***Дата:**` lines had grown to six). Links and colour spans keep the fixed
 * order, for the reason `MARK_ORDER` gives.
 */
function outerMark(nodes: readonly PmNode[], index: number): PmMark | undefined {
  const marks = nodes[index].marks ?? []
  for (const type of OUTERMOST) {
    const mark = marks.find((entry) => entry.type === type)
    if (mark) return mark
  }
  let best: PmMark | undefined
  let bestRun = 0
  for (const type of MARK_ORDER) {
    const mark = marks.find((entry) => entry.type === type)
    if (!mark) continue
    const key = markKey(mark)
    let end = index + 1
    while (end < nodes.length && hasMark(nodes[end], key)) end += 1
    if (end - index > bestRun) {
      best = mark
      bestRun = end - index
    }
  }
  return best
}

/**
 * Rebuild mdast's nesting from ProseMirror's flat marks.
 *
 * Greedy by design: take the outermost mark the first node carries, extend it over as many
 * following nodes as share it, and recurse. That produces `**a *b*** ` rather than
 * `**a** **_b_**` for the same marks, which is what a person would have written.
 */
function inlineToMdast(nodes: readonly PmNode[]): PhrasingContent[] {
  const out: PhrasingContent[] = []
  let index = 0

  while (index < nodes.length) {
    const node = nodes[index]
    const mark = outerMark(nodes, index)

    if (!mark) {
      out.push(...leafToMdast(node))
      index += 1
      continue
    }

    const key = markKey(mark)
    let end = index + 1
    while (end < nodes.length && hasMark(nodes[end], key)) end += 1

    const children = inlineToMdast(nodes.slice(index, end).map((child) => withoutMark(child, key)))
    if (mark.type === 'textColor') {
      // The word as the file spelled it, so `grey` never comes back as `gray` (§8.2). A
      // mark that somehow lost its attribute is not written as a span at all — an empty
      // class would be a `[…]{.}` nothing can read.
      const color = mark.attrs?.color
      if (typeof color === 'string' && color) out.push({ type: 'colorSpan', color, children })
      else out.push(...children)
      index = end
      continue
    }
    const type = MARK_TO_MDAST[mark.type]
    if (type === 'link') {
      out.push({
        type: 'link',
        url: String(mark.attrs?.href ?? ''),
        title: (mark.attrs?.title as string | null) ?? null,
        children,
      })
    } else {
      out.push({ type, children })
    }
    index = end
  }

  return out
}

type BlockOut = BlockContent | DefinitionContent

/**
 * Drop whitespace hanging off the end of a text block.
 *
 * It carries nothing in Markdown, and writing it out is worse than dropping it: at the end
 * of a block `mdast-util-to-markdown` has to escape a trailing space as `&#x20;` so it
 * survives the round trip, and that entity is what would land in the file — for a space
 * somebody left while thinking. Two trailing spaces are a hard break, which the parser has
 * already turned into a `break` node before this ever runs.
 */
function trimTrailing(children: PhrasingContent[]): PhrasingContent[] {
  const last = children[children.length - 1]
  if (last?.type !== 'text') return children
  const value = last.value.replace(/[ \t]+$/, '')
  if (value === last.value) return children
  return value
    ? [...children.slice(0, -1), { ...last, value }]
    : trimTrailing(children.slice(0, -1))
}

function titleParagraph(node: PmNode | undefined): Paragraph | null {
  if (!node || node.type !== 'directiveTitle') return null
  const children = inlineToMdast(node.content ?? [])
  if (!children.length) return null
  return { type: 'paragraph', data: { directiveLabel: true }, children }
}

function listItemsToMdast(nodes: readonly PmNode[], task: boolean): ListItem[] {
  return nodes.map((item) => {
    const children = blocksToMdast(item.content ?? []) as ListItem['children']
    return {
      type: 'listItem',
      checked: task ? ((item.attrs?.checked as boolean | null) ?? null) : null,
      // ProseMirror has no notion of a loose list, so it is derived. An item holding a
      // second *block* needs the blank lines, or that block reads as a sibling of the list
      // rather than as part of the item — but a nested list is not one of those cases, and
      // spreading for it would put a blank line inside every indented list in the project.
      spread: children.some((child, index) => index > 0 && child.type !== 'list'),
      children,
    }
  })
}

function listOf(children: ListItem[], extra: Record<string, unknown>): Extract<BlockOut, { type: 'list' }> {
  return {
    type: 'list',
    ordered: false,
    start: null,
    ...extra,
    spread: children.some((item) => item.spread),
    children,
  } as Extract<BlockOut, { type: 'list' }>
}

function cellToMdast(node: PmNode): TableCell {
  // A ProseMirror cell holds blocks; a GFM cell holds one line of phrasing. Paragraphs are
  // flattened rather than dropped, which is the only lossy direction in this file — and it
  // is unreachable from the UI, because the editor never puts a second block in a cell.
  const children: PhrasingContent[] = []
  for (const block of node.content ?? []) {
    if (block.type === 'paragraph') children.push(...inlineToMdast(block.content ?? []))
  }
  return { type: 'tableCell', children }
}

function blockToMdast(node: PmNode): BlockOut | null {
  switch (node.type) {
    case 'paragraph': {
      const children = trimTrailing(inlineToMdast(node.content ?? []))
      // Markdown has no empty paragraph, and the editor keeps a trailing one for the caret.
      return children.length ? { type: 'paragraph', children } : null
    }
    case 'heading':
      return {
        type: 'heading',
        depth: (node.attrs?.level as 1 | 2 | 3 | 4 | 5 | 6) ?? 1,
        children: trimTrailing(inlineToMdast(node.content ?? [])),
      }
    case 'horizontalRule':
      return { type: 'thematicBreak' }
    case 'blockquote':
      return { type: 'blockquote', children: blocksToMdast(node.content ?? []) as BlockContent[] }
    case 'codeBlock':
      return {
        type: 'code',
        lang: (node.attrs?.language as string | null) ?? null,
        meta: (node.attrs?.meta as string | null) ?? null,
        value: (node.content ?? []).map((child) => child.text ?? '').join(''),
      }
    case 'bulletList':
      return listOf(listItemsToMdast(node.content ?? [], false), { ordered: false })
    case 'orderedList':
      return listOf(listItemsToMdast(node.content ?? [], false), {
        ordered: true,
        start: (node.attrs?.start as number) ?? 1,
      })
    case 'taskList':
      return listOf(listItemsToMdast(node.content ?? [], true), { ordered: false })
    case 'table': {
      const rows = node.content ?? []
      return {
        type: 'table',
        align: ((node.attrs?.align as (string | null)[]) ?? []).map(
          (value) => (value as 'left' | 'right' | 'center' | null) ?? null,
        ),
        children: rows.map((row) => ({
          type: 'tableRow',
          children: (row.content ?? []).map(cellToMdast),
        })),
      }
    }
    case 'callout':
    case 'toggle': {
      const content = node.content ?? []
      const titled = content[0]?.type === 'directiveTitle'
      // An empty title node means the directive had no label, and gets none back.
      const title = titled ? titleParagraph(content[0]) : null
      const body = blocksToMdast(titled ? content.slice(1) : content)
      const attributes =
        node.type === 'toggle' && node.attrs?.open === true ? { open: '' } : {}
      return {
        type: 'containerDirective',
        name: node.type === 'toggle' ? 'toggle' : String(node.attrs?.kind ?? 'note'),
        attributes,
        children: [...(title ? [title] : []), ...body] as BlockContent[],
      } as unknown as BlockOut
    }
    case 'unsupportedBlock':
      return node.attrs?.mdast as BlockOut
    default:
      return null
  }
}

function blocksToMdast(nodes: readonly PmNode[]): BlockOut[] {
  const out: BlockOut[] = []
  for (const node of nodes) {
    const converted = blockToMdast(node)
    if (converted) out.push(converted)
  }
  return out
}

export function proseMirrorToMdast(doc: PmNode): Root {
  return { type: 'root', children: blocksToMdast(doc.content ?? []) as RootContent[] }
}
