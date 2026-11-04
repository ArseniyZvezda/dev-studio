/**
 * `[[wiki links]]` as a first-class inline node (block-editor spec §5).
 *
 * Two requirements shape this. The link must come back **byte for byte** — `[[test-plan]]`
 * may not become `[[Test plan]]` or a plain Markdown link — so the node stores the raw text
 * between the brackets and the serializer prints exactly that back. And the editor's idea
 * of what counts as a wiki link has to agree with the backend's, because Backlinks and
 * Unresolved links are computed there (`teams/docs.py`) and shown next to the document; a
 * link the panel counts but the editor renders as prose is a bug the user cannot explain.
 *
 * Hence masking rather than a post-parse walk over text nodes. Every match of the backend's
 * own regex is swapped for a sentinel *before* Markdown is parsed, so `[[a *b* c]]` is one
 * atom to both sides, and `[[foo]]` cannot be re-read as a link reference just because the
 * document happens to define `[foo]:` somewhere. Sentinels that land inside code spans,
 * code blocks or raw HTML are put back verbatim, since those are not links anywhere.
 */

import type { Literal, Nodes, Parents, PhrasingContent, Root, Text } from 'mdast'
import type { Options as ToMarkdownOptions } from 'mdast-util-to-markdown'
import type { Processor } from 'unified'
import { SKIP, visit } from 'unist-util-visit'

/** Kept identical to `WIKI_LINK` in `backend/voxarium_studio/teams/docs.py` — see the docstring. */
export const WIKI_LINK = /\[\[([^[\]|]+?)(?:\|([^[\]]*))?\]\]/g

export interface WikiLink extends Literal {
  type: 'wikiLink'
  /** The raw text between the brackets, `target` or `target|label`. Serialized as-is. */
  value: string
  target: string
  label: string | null
}

declare module 'mdast' {
  interface PhrasingContentMap {
    wikiLink: WikiLink
  }
  interface RootContentMap {
    wikiLink: WikiLink
  }
}

/**
 * Private-use characters, tried in order.
 *
 * A document that already contains the sentinel would have it restored as if it were a
 * link, so the first one absent from the source wins. Falling through every candidate is
 * possible in theory and means "do not mask", which degrades to leaving the brackets as
 * prose — never to losing them.
 */
const SENTINELS = ['\uE000', '\uE001', '\uE002', '\uE003']

export interface MaskedSource {
  /** The source with every wiki link replaced by `<sentinel><index><sentinel>`. */
  text: string
  /** Raw `[[…]]` occurrences, indexed by the number in the sentinel. */
  links: string[]
  sentinel: string | null
}

export function maskWikiLinks(source: string): MaskedSource {
  const sentinel = SENTINELS.find((candidate) => !source.includes(candidate)) ?? null
  if (sentinel === null) return { text: source, links: [], sentinel }

  const links: string[] = []
  const text = source.replace(WIKI_LINK, (raw) => {
    const index = links.push(raw) - 1
    return `${sentinel}${index}${sentinel}`
  })
  return { text, links, sentinel }
}

/** Put every sentinel in a string back as the `[[…]]` it stands for. */
export function unmask(value: string, masked: MaskedSource): string {
  if (masked.sentinel === null || !masked.links.length) return value
  const pattern = new RegExp(`${masked.sentinel}(\\d+)${masked.sentinel}`, 'g')
  return value.replace(pattern, (whole, index: string) => masked.links[Number(index)] ?? whole)
}

function parseWikiLink(raw: string): WikiLink {
  const body = raw.slice(2, -2)
  const bar = body.indexOf('|')
  const target = (bar === -1 ? body : body.slice(0, bar)).trim()
  const label = bar === -1 ? null : body.slice(bar + 1).trim() || null
  return { type: 'wikiLink', value: body, target, label }
}

/** Cut a masked text node into text and `wikiLink` nodes. */
function splitText(node: Text, masked: MaskedSource): PhrasingContent[] {
  if (masked.sentinel === null) return [node]
  const pattern = new RegExp(`${masked.sentinel}(\\d+)${masked.sentinel}`, 'g')

  const pieces: PhrasingContent[] = []
  let cursor = 0
  for (const match of node.value.matchAll(pattern)) {
    const raw = masked.links[Number(match[1])]
    if (raw === undefined) continue
    if (match.index > cursor) {
      pieces.push({ type: 'text', value: node.value.slice(cursor, match.index) })
    }
    pieces.push(parseWikiLink(raw))
    cursor = match.index + match[0].length
  }
  if (!pieces.length) return [node]
  if (cursor < node.value.length) pieces.push({ type: 'text', value: node.value.slice(cursor) })
  return pieces
}

/**
 * Fields that reach the output as text. A sentinel in any of them has to be restored, or
 * a private-use character ends up on disk — the one failure mode this whole approach must
 * not have.
 */
const TEXTUAL_FIELDS = ['value', 'url', 'title', 'alt', 'label', 'identifier'] as const

/** Turn the sentinels in a freshly parsed tree back into wiki links, in place. */
export function applyWikiLinks(tree: Root, masked: MaskedSource): void {
  if (masked.sentinel === null || !masked.links.length) return

  // Non-text nodes first: a sentinel inside `inlineCode` or an HTML block is not a link,
  // it is the characters the author wrote, and it goes back untouched.
  visit(tree, (node: Nodes) => {
    if (node.type === 'text') return
    for (const field of TEXTUAL_FIELDS) {
      const value = (node as unknown as Record<string, unknown>)[field]
      if (typeof value === 'string') {
        ;(node as unknown as Record<string, unknown>)[field] = unmask(value, masked)
      }
    }
  })

  visit(tree, 'text', (node: Text, index: number | undefined, parent: Parents | undefined) => {
    if (parent === undefined || index === undefined) return
    const pieces = splitText(node, masked)
    if (pieces.length === 1 && pieces[0] === node) return
    parent.children.splice(index, 1, ...(pieces as Parents['children']))
    // Past the replacements: they are final, and a `wikiLink` has nothing to visit.
    return [SKIP, index + pieces.length]
  })
}

/**
 * Print a wiki link back exactly as it was written.
 *
 * `mdast-util-to-markdown` inserts a handler's result without escaping it, which is what
 * makes "byte for byte" achievable here — the brackets survive, while the prose around
 * them is still escaped normally.
 */
const toMarkdownExtension: ToMarkdownOptions = {
  handlers: {
    wikiLink: (node) => `[[${(node as WikiLink).value}]]`,
  },
}

/** remark plugin: teach the serializer about `wikiLink`. Parsing is done by masking. */
export function remarkWikiLink(this: Processor): undefined {
  const data = this.data()
  const extensions = data.toMarkdownExtensions ?? (data.toMarkdownExtensions = [])
  extensions.push(toMarkdownExtension)
}
