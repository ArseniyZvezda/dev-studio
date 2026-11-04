/**
 * Markdown in, Markdown out — the layer the block editor sits on (block-editor spec §8).
 *
 * The editor's document model is mdast. This module is the only place that knows how a
 * document becomes bytes, which is what lets §8 be tested on a corpus of real files before
 * any UI exists, and what keeps "the file is the format" true once the UI does.
 *
 * What the tree does *not* carry is kept in an envelope beside it: the frontmatter block,
 * the line ending, and whether the file ended with a newline. §7 requires all three back
 * unchanged, and none of them survive a parse — remark normalises line endings, and a
 * re-serialised YAML block is not the bytes the author wrote. So the frontmatter is cut off
 * by bytes and pasted back by bytes; it is never parsed at all.
 *
 * One consequence worth stating because it decides how the store is written: a document is
 * "dirty" when the user edited it, **never** when the serialized form differs from the file
 * on disk. Those differ for every file that is not already in this dialect, and treating
 * that as an edit would rewrite every document the moment it is opened — exactly what §8.2
 * forbids.
 */

import type { Root, RootContent } from 'mdast'
import remarkDirective from 'remark-directive'
import remarkGfm from 'remark-gfm'
import remarkParse from 'remark-parse'
import remarkStringify from 'remark-stringify'
import { unified } from 'unified'

import { applyColorSpans, maskColors, remarkColorSpan } from './color'
import { GFM_OPTIONS, STRINGIFY_OPTIONS } from './conventions'
import { applyImageWidths, escapeWidthLookalikes, remarkImageWidth } from './image-width'
import { remarkWideTableDelimiters } from './table'
import { applyWikiLinks, maskWikiLinks, remarkWikiLink } from './wikilink'

export type Eol = '\n' | '\r\n'

/** Everything about the file that the syntax tree cannot hold (§7). */
export interface DocumentEnvelope {
  /** The frontmatter block verbatim, fences and trailing newline included, or `''`. */
  frontmatter: string
  eol: Eol
  finalNewline: boolean
}

export interface ParsedDocument {
  root: Root
  envelope: DocumentEnvelope
  /** The bytes this was parsed from, so a caller can tell a real edit from a reformat. */
  source: string
}

/**
 * `parse()` rather than `process()`: transformers would run the wiki-link pass without the
 * mask it needs, and the mask is per document.
 */
const parser = unified().use(remarkParse).use(remarkGfm, GFM_OPTIONS).use(remarkDirective)

const serializer = unified()
  .use(remarkStringify, STRINGIFY_OPTIONS)
  .use(remarkGfm, GFM_OPTIONS)
  .use(remarkDirective)
  .use(remarkWikiLink)
  .use(remarkColorSpan)
  .use(remarkImageWidth)
  // After remark-gfm: the later extension's handler is the one that wins.
  .use(remarkWideTableDelimiters)

/**
 * The closing fence takes the blank lines after it with it. They are not part of the tree —
 * remark drops leading blank lines — so leaving them out of the envelope would delete the
 * gap between the frontmatter and the first heading on the first save.
 */
const FRONTMATTER = /^---\n(?:[\s\S]*?\n)??(?:---|\.\.\.)[ \t]*(?:\n+|$)/

/**
 * Split a leading YAML frontmatter block off the source.
 *
 * Deliberately textual. Parsing the YAML and writing it back would reformat quoting,
 * key order and comments — §7 asks for it byte for byte, and the Studio has no reason to
 * understand it yet (§14.7).
 */
function splitFrontmatter(source: string): [frontmatter: string, body: string] {
  if (!source.startsWith('---')) return ['', source]
  const match = FRONTMATTER.exec(source)
  return match ? [match[0], source.slice(match[0].length)] : ['', source]
}

function detectEol(source: string): Eol {
  return source.includes('\r\n') ? '\r\n' : '\n'
}

/**
 * Parse, then give each picture the width written straight after it (`image-width.ts`). Every
 * parse goes through here — the document's and each colour span's content — because that pass
 * reads the text the tree came from to tell `{` from `\{`.
 */
function parseMarkdown(markdown: string): Root {
  const root = parser.parse(markdown)
  applyImageWidths(root, markdown)
  return root
}

export function parseDocument(source: string): ParsedDocument {
  const eol = detectEol(source)
  const normalised = eol === '\n' ? source : source.replace(/\r\n/g, '\n')
  const [frontmatter, body] = splitFrontmatter(normalised)

  // Wiki links first, colours second, and both before the parser sees a character. The
  // order is what lets `[[[dash]] and more]{.green}` work: the wiki mask leaves a sentinel
  // where the brackets were, so the colour scanner's own bracket matching cannot be thrown
  // by them. Restoring goes the other way — the colour spans have to be nodes in the tree
  // before `applyWikiLinks` can reach the text inside one.
  const masked = maskWikiLinks(body)
  const colors = maskColors(masked.text)
  const root = parseMarkdown(colors.text)
  applyColorSpans(root, colors, parseMarkdown)
  applyWikiLinks(root, masked)

  return {
    root,
    envelope: {
      frontmatter,
      eol,
      finalNewline: normalised.endsWith('\n'),
    },
    source,
  }
}

export function serializeDocument(root: Root, envelope: DocumentEnvelope): string {
  // An empty tree stringifies to `''`; anything else ends in exactly one newline.
  const body = root.children.length ? serializer.stringify(escapeWidthLookalikes(root)) : ''
  let text = envelope.frontmatter + body
  if (!envelope.finalNewline && text.endsWith('\n')) text = text.slice(0, -1)
  return envelope.eol === '\n' ? text : text.replace(/\n/g, envelope.eol)
}

/**
 * The file as this editor would write it, without going through the editor.
 *
 * This is what §8.2 is stated in terms of: the first save may move bytes, `normalise` twice
 * may not.
 */
export function normalise(source: string): string {
  const document = parseDocument(source)
  return serializeDocument(document.root, document.envelope)
}

/**
 * One node as Markdown — what an unsupported block shows the user (§3.4).
 *
 * They are not editable, so this is a rendering, not a round trip: what goes back on save
 * is the node itself, never this string.
 */
export function serializeNode(node: RootContent): string {
  return serializer
    .stringify(escapeWidthLookalikes({ type: 'root', children: [node] }))
    .replace(/\n$/, '')
}
