/**
 * `[text]{.green}` — colour the author chose, as an inline span (block-editor spec §3.2).
 *
 * The syntax is Pandoc's bracketed span, which is also what `markdown-it-attrs` and most
 * static site generators read. That matters more than elegance here: these documents are
 * opened in Unreal, in a text editor and on the odd Markdown viewer, and the requirement is
 * that a renderer which does *not* know the syntax leaves the **words** on the page. A
 * `[…]{.green}` degrades to visible brackets; a `<span style>` degrades to nothing at all,
 * and §3.2 forbids the HTML anyway.
 *
 * The class name in the file *is* the colour word, so the stylesheet a renderer needs is
 * `.green { color: … }` and nothing else. That is why the closed list below is spelled in
 * plain colour words rather than in the Studio's own semantic vocabulary: the author named
 * a colour, not a meaning, and `--vxs-success` moves under the accessible status palette
 * while a word an author typed must not.
 *
 * ## Why a masking pass rather than a parser extension
 *
 * Same reason `wikilink.ts` gives, and one more. `[ok]` is a **shortcut link reference** to
 * CommonMark: with `[ok]: https://…` defined anywhere in the document it parses as a link
 * and the `{.green}` is left dangling, so a span's meaning would depend on a definition
 * three hundred lines away. Masking the whole span before Markdown is parsed removes that
 * coupling, and gives the same guarantee the wiki links have — what goes back to disk is
 * the characters that came off it.
 *
 * A scanner rather than a regular expression, because three cases decide correctness and a
 * regular expression can express none of them together: brackets nest (`[[a](url)]{.red}`),
 * a backtick run wins over a bracket in CommonMark (`` [a `]` b]{.green} `` closes at the
 * *second* `]`), and `\[` is an author saying "not a span". Getting any of those wrong does
 * not mangle a colour — it moves the end of the span into the middle of a sentence.
 *
 * A span lives on **one line**. Nothing this editor writes wraps a paragraph (§8.1), so the
 * restriction costs nothing here, and it is what keeps an unmatched `[` from scanning to
 * the end of a 3000-line document (§9) or across a blank line into the next block.
 */

import type { Nodes, Parent, Parents, PhrasingContent, Root, Text } from 'mdast'
import type {
  Handle,
  Info,
  Options as ToMarkdownOptions,
  State,
} from 'mdast-util-to-markdown'
import type { Processor } from 'unified'
import { SKIP, visit } from 'unist-util-visit'

/**
 * Every colour word the file may carry, and the only ones the stylesheet defines.
 *
 * Ten colours and one spelling: `grey` is `gray`, kept because refusing it would mean a
 * document silently loses its colour over a vowel. Both are written back as they were
 * found — the serializer prints `node.color`, never a normalised form.
 *
 * A word outside this list is not a span at all: `[Ship it]{.brand}` stays the characters
 * the author typed, which is §8.3 applied to a syntax rather than to a block. It also
 * closes the one hole this feature could otherwise open — these documents are written by
 * agents, and an open class attribute would be a styling hook the model chose.
 */
export const TEXT_COLORS = [
  'red',
  'orange',
  'yellow',
  'green',
  'cyan',
  'blue',
  'purple',
  'pink',
  'brown',
  'gray',
  'grey',
] as const

export type TextColor = (typeof TEXT_COLORS)[number]

/** The colours offered in the UI. `grey` is read but never written by a click. */
export const TEXT_COLOR_CHOICES = TEXT_COLORS.filter((name) => name !== 'grey')

const NAMES = new Set<string>(TEXT_COLORS)

export function isTextColor(value: unknown): value is TextColor {
  return typeof value === 'string' && NAMES.has(value)
}

/** `grey` and `gray` are one colour; only the *stored* spelling is preserved. */
export function sameColor(a: unknown, b: unknown): boolean {
  const canonical = (value: unknown) => (value === 'grey' ? 'gray' : value)
  return canonical(a) === canonical(b)
}

export interface ColorSpan extends Parent {
  type: 'colorSpan'
  /** The word as written in the file. Printed back verbatim. */
  color: string
  children: PhrasingContent[]
}

declare module 'mdast' {
  interface PhrasingContentMap {
    colorSpan: ColorSpan
  }
  interface RootContentMap {
    colorSpan: ColorSpan
  }
}

declare module 'mdast-util-to-markdown' {
  interface ConstructNameMap {
    colorSpan: 'colorSpan'
  }
}

// --------------------------------------------------------------------------- scanning

interface Span {
  /** Everything between the brackets, unparsed. */
  inner: string
  color: string
  /** The whole `[…]{.color}`, so it can be put back without the source in hand. */
  raw: string
  /** One past the closing brace, in the source it was found in. */
  end: number
}

/**
 * Past a code span, given the index of its opening backtick run.
 *
 * CommonMark resolves code spans before links, so a `]` inside backticks does not close a
 * bracket. An unterminated run is literal text and only the backticks themselves are
 * consumed — same rule the parser applies.
 */
function skipCodeSpan(source: string, at: number, limit: number): number {
  let opened = at
  while (opened < limit && source[opened] === '`') opened += 1
  const width = opened - at

  let cursor = opened
  while (cursor < limit) {
    if (source[cursor] !== '`') {
      cursor += 1
      continue
    }
    let closed = cursor
    while (closed < limit && source[closed] === '`') closed += 1
    if (closed - cursor === width) return closed
    cursor = closed
  }
  return opened
}

/** `{.green}` and nothing else — no id, no key=value, no spaces. */
const ATTRIBUTE = /^\{\.([a-z]+)\}/

/**
 * The span opening at `open`, or `null` if those characters are ordinary prose.
 *
 * Bounded to the line the bracket is on, which is both the rule (§8.1 writes a paragraph as
 * one line) and the reason an unmatched `[` costs a line's worth of scanning rather than a
 * document's.
 */
function spanAt(source: string, open: number): Span | null {
  const newline = source.indexOf('\n', open)
  const limit = newline === -1 ? source.length : newline

  let depth = 0
  let cursor = open + 1
  while (cursor < limit) {
    const character = source[cursor]
    if (character === '\\') {
      cursor += 2
      continue
    }
    if (character === '`') {
      cursor = skipCodeSpan(source, cursor, limit)
      continue
    }
    if (character === '[') {
      depth += 1
      cursor += 1
      continue
    }
    if (character === ']') {
      if (depth === 0) break
      depth -= 1
      cursor += 1
      continue
    }
    cursor += 1
  }

  if (cursor >= limit || source[cursor] !== ']') return null
  const inner = source.slice(open + 1, cursor)
  // `[]{.green}` is not a span: there is nothing to colour, and treating it as one would
  // put an empty node in the tree that the serializer would then have to invent text for.
  if (!inner) return null

  const attribute = ATTRIBUTE.exec(source.slice(cursor + 1, limit))
  if (!attribute || !NAMES.has(attribute[1])) return null

  return {
    inner,
    color: attribute[1],
    raw: source.slice(open, cursor + 1 + attribute[0].length),
    end: cursor + 1 + attribute[0].length,
  }
}

// ---------------------------------------------------------------------------- masking

/**
 * Private-use characters, tried in order and kept clear of `wikilink.ts`'s own block.
 *
 * Both masks are live at once — wiki links are masked first, colours second — so a shared
 * candidate would have the colour pass reject the character the wiki pass had just put in.
 * Falling through every candidate means "do not mask", which leaves the brackets as prose
 * and never loses them.
 */
const SENTINELS = ['\uE010', '\uE011', '\uE012', '\uE013']

export interface MaskedColors {
  /** The source with every span replaced by `<sentinel><index><sentinel>`. */
  text: string
  spans: Span[]
  sentinel: string | null
}

export function maskColors(source: string): MaskedColors {
  const sentinel = SENTINELS.find((candidate) => !source.includes(candidate)) ?? null
  if (sentinel === null) return { text: source, spans: [], sentinel }

  const spans: Span[] = []
  const pieces: string[] = []
  let plain = 0
  let cursor = 0

  while (cursor < source.length) {
    const character = source[cursor]
    if (character === '\\') {
      cursor += 2
      continue
    }
    if (character === '`') {
      cursor = skipCodeSpan(source, cursor, source.length)
      continue
    }
    if (character !== '[') {
      cursor += 1
      continue
    }

    const span = spanAt(source, cursor)
    if (!span) {
      cursor += 1
      continue
    }
    pieces.push(source.slice(plain, cursor), `${sentinel}${spans.push(span) - 1}${sentinel}`)
    cursor = span.end
    plain = span.end
  }

  if (!spans.length) return { text: source, spans, sentinel }
  pieces.push(source.slice(plain))
  return { text: pieces.join(''), spans, sentinel }
}

/** Put every sentinel in a string back as the `[…]{.color}` it stands for. */
export function unmaskColors(value: string, masked: MaskedColors): string {
  if (masked.sentinel === null || !masked.spans.length) return value
  const pattern = new RegExp(`${masked.sentinel}(\\d+)${masked.sentinel}`, 'g')
  return value.replace(pattern, (whole, index: string) => masked.spans[Number(index)]?.raw ?? whole)
}

// ------------------------------------------------------------------------ mask → tree

/**
 * Fields that reach the output as text. A sentinel in any of them has to be restored, or a
 * private-use character ends up on disk — the one failure mode this approach must not have.
 */
const TEXTUAL_FIELDS = ['value', 'url', 'title', 'alt', 'label', 'identifier'] as const

/** Parse a span's content the way the surrounding line was parsed. */
function parseInline(source: string, parse: (markdown: string) => Root): PhrasingContent[] {
  const masked = maskColors(source)
  const root = parse(masked.text)
  const first = root.children[0]
  // A span holds phrasing by construction, so anything else means the content read as a
  // block on its own (`[# not a heading]{.red}`). §8.3: show the characters, not a guess.
  if (root.children.length !== 1 || first?.type !== 'paragraph' || !first.children.length) {
    return [{ type: 'text', value: source }]
  }
  applyColorSpans(root, masked, parse)
  return first.children
}

/** Cut a masked text node into text and `colorSpan` nodes. */
function splitText(
  node: Text,
  masked: MaskedColors,
  parse: (markdown: string) => Root,
): PhrasingContent[] {
  if (masked.sentinel === null) return [node]
  const pattern = new RegExp(`${masked.sentinel}(\\d+)${masked.sentinel}`, 'g')

  const pieces: PhrasingContent[] = []
  let cursor = 0
  for (const match of node.value.matchAll(pattern)) {
    const span = masked.spans[Number(match[1])]
    if (span === undefined) continue
    if (match.index > cursor) {
      pieces.push({ type: 'text', value: node.value.slice(cursor, match.index) })
    }
    pieces.push({ type: 'colorSpan', color: span.color, children: parseInline(span.inner, parse) })
    cursor = match.index + match[0].length
  }
  if (!pieces.length) return [node]
  if (cursor < node.value.length) pieces.push({ type: 'text', value: node.value.slice(cursor) })
  return pieces
}

/**
 * Turn the sentinels in a freshly parsed tree into colour spans, in place.
 *
 * `parse` is passed in rather than imported: a span's content is Markdown and has to be
 * parsed by the same processor as the document around it, which lives in `document.ts` —
 * and `document.ts` imports this module.
 *
 * Must run **before** `applyWikiLinks`, which is what puts wiki links back inside a span's
 * content: the colour spans have to be in the tree for that pass to reach them at all.
 */
export function applyColorSpans(
  tree: Root,
  masked: MaskedColors,
  parse: (markdown: string) => Root,
): void {
  if (masked.sentinel === null || !masked.spans.length) return

  // Non-text nodes first: a sentinel inside `inlineCode`, a fenced block or raw HTML is not
  // a span, it is the characters the author wrote, and it goes back untouched.
  visit(tree, (node: Nodes) => {
    if (node.type === 'text') return
    for (const field of TEXTUAL_FIELDS) {
      const value = (node as unknown as Record<string, unknown>)[field]
      if (typeof value === 'string') {
        ;(node as unknown as Record<string, unknown>)[field] = unmaskColors(value, masked)
      }
    }
  })

  visit(tree, 'text', (node: Text, index: number | undefined, parent: Parents | undefined) => {
    if (parent === undefined || index === undefined) return
    const pieces = splitText(node, masked, parse)
    if (pieces.length === 1 && pieces[0] === node) return
    parent.children.splice(index, 1, ...(pieces as Parents['children']))
    // Past the replacements: their content was parsed by `parseInline`, which already ran
    // this pass over it, so visiting into them would do the work twice.
    return [SKIP, index + pieces.length]
  })
}

// ------------------------------------------------------------------------ tree → mask

/** `peek` is read at runtime by `mdast-util-to-markdown` but is absent from `Handle`. */
type PeekableHandle = Handle & { peek?: Handle }

/**
 * Print a span back as `[…]{.color}`.
 *
 * `state.enter('label')` is the load-bearing line. Without it only `[` is escaped inside
 * the children and a `]` the author typed inside a coloured phrase is written raw — which
 * re-reads as the end of the span, moving its boundary into the middle of the sentence.
 * `link.js` in `mdast-util-to-markdown` does exactly this for the same reason.
 *
 * `peek` tells the serializer the first character without making it serialize the whole
 * subtree to find out; a span nested in a span would otherwise be rendered once per level.
 */
const colorSpan: PeekableHandle = (
  node: ColorSpan,
  _parent: Parents | undefined,
  state: State,
  info: Info,
): string => {
  const tracker = state.createTracker(info)
  const exit = state.enter('colorSpan')
  const label = state.enter('label')

  let value = tracker.move('[')
  value += tracker.move(
    state.containerPhrasing(node, { before: value, after: ']', ...tracker.current() }),
  )
  label()
  value += tracker.move(`]{.${node.color}}`)
  exit()
  return value
}

colorSpan.peek = () => '['

const toMarkdownExtension: ToMarkdownOptions = { handlers: { colorSpan } }

/** remark plugin: teach the serializer about `colorSpan`. Parsing is done by masking. */
export function remarkColorSpan(this: Processor): undefined {
  const data = this.data()
  const extensions = data.toMarkdownExtensions ?? (data.toMarkdownExtensions = [])
  extensions.push(toMarkdownExtension)
}
