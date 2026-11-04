/**
 * `![shot](shot.png){width=300}` — how wide a picture is drawn (block-editor spec §3.4).
 *
 * Pandoc's link attributes, the family the colour spans' `[…]{.green}` already belong to
 * (§3.2.1), and read the same way by the tools these documents are also opened in: Pandoc and
 * `markdown-it-attrs` give the `<img>` its `width`, and a renderer that knows neither draws the
 * picture at its own size with the braces after it — the picture is never lost.
 *
 * One key, one unit, nothing else. `width` in whole pixels is what the editor's slider writes,
 * and it is the only attribute this module turns into anything: `{height=200}`, `{.centre}`,
 * `{width=50%}` and `{width=300 height=200}` stay the characters the author typed (§8.3), the
 * way a colour word outside the closed list does.
 *
 * ## After the parse, where the colour spans mask before it
 *
 * The attribute can only follow an image, and an image is a construct the parser already
 * resolves — brackets, titles, angle-bracket destinations and all — so "is this `{` right
 * after a picture" is a question about the *tree*, answered by the text node that follows an
 * `image`. Recognising pictures in raw text would mean re-implementing that parse, and getting
 * it wrong moves a picture's end into the next word.
 *
 * What the tree cannot say is whether the brace was escaped: `\{width=300}` parses to the same
 * text node as `{width=300}`, and the backslash is the author saying "not an attribute". So
 * the pass reads the source at the offset where that text node starts, which `position` gives
 * it — which is also why every parse, a colour span's content included, has to run it with
 * the text it parsed (`document.ts`).
 */

import type { Image, Literal, Nodes, Parents, Root, Text } from 'mdast'
import {
  defaultHandlers,
  type Handle,
  type Info,
  type Options as ToMarkdownOptions,
  type State,
} from 'mdast-util-to-markdown'
import type { Processor } from 'unified'
import { EXIT, visit } from 'unist-util-visit'

/** `\{`, printed as it is: the one escape `escapeWidthLookalikes` puts into a tree. */
export interface ImageWidthEscape extends Literal {
  type: 'imageWidthEscape'
}

declare module 'mdast' {
  interface ImageData {
    /** Whole pixels, from a `{width=…}` written straight after the image. */
    width?: number
  }
  interface PhrasingContentMap {
    imageWidthEscape: ImageWidthEscape
  }
  interface RootContentMap {
    imageWidthEscape: ImageWidthEscape
  }
}

/** The widest a file may ask for: wider than any screen, narrower than a slipped key. */
export const MAX_IMAGE_WIDTH = 10000

/**
 * The widths the file can hold, as the one pattern both directions use: 1 to 10000, no
 * leading zero. The parser reads exactly these and the serializer escapes exactly these, so a
 * brace that is *not* one of them — `{width=0}`, `{width=007}` — is prose both ways and comes
 * back byte for byte.
 */
const READABLE = '(?:[1-9]\\d{0,3}|10000)'

const ATTRIBUTE = new RegExp(`^\\{width=(${READABLE})\\}`)

export function isImageWidth(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= MAX_IMAGE_WIDTH
}

/** The width an image node carries, or `null` for the picture's own size. */
export function imageWidth(node: Image): number | null {
  const width = node.data?.width
  return isImageWidth(width) ? width : null
}

// ---------------------------------------------------------------------------- reading

/** The attribute a text node opens with, if it touches the image before it and is unescaped. */
function attributeAfter(
  image: Image,
  text: Text,
  source: string,
): { width: number; length: number } | null {
  const end = image.position?.end.offset
  const start = text.position?.start.offset
  // Touching or nothing: `![a](a.png) {width=3}` is a sentence that mentions a width.
  if (end === undefined || start === undefined || start !== end) return null
  const match = ATTRIBUTE.exec(text.value)
  // The same characters in the source, or the text got there through an escape.
  if (!match || !source.startsWith(match[0], start)) return null
  return { width: Number(match[1]), length: match[0].length }
}

/**
 * Move every `{width=…}` that directly follows an image into the image, in place.
 *
 * `source` is the text the tree was parsed from — the masked document, or a colour span's
 * content — and is read only to tell `{` from `\{`. Runs straight after the parse, before the
 * colour and wiki-link passes rewrite text nodes and drop their positions.
 */
export function applyImageWidths(tree: Root, source: string): void {
  visit(tree, (node: Nodes) => {
    if (!('children' in node)) return
    const children = (node as Parents).children as Nodes[]
    for (let index = 0; index + 1 < children.length; index += 1) {
      const image = children[index]
      const next = children[index + 1]
      if (image.type !== 'image' || next.type !== 'text') continue

      const attribute = attributeAfter(image, next, source)
      if (!attribute) continue
      image.data = { ...image.data, width: attribute.width }

      const rest = next.value.slice(attribute.length)
      if (!rest) {
        children.splice(index + 1, 1)
        continue
      }
      next.value = rest
      if (next.position) {
        const { start } = next.position
        next.position = {
          ...next.position,
          start: {
            ...start,
            column: start.column + attribute.length,
            offset: (start.offset ?? 0) + attribute.length,
          },
        }
      }
    }
  })
}

// ---------------------------------------------------------------------------- writing

/** `peek` is read at runtime by `mdast-util-to-markdown` but is absent from `Handle`. */
type PeekableHandle = Handle & { peek?: Handle }

const defaultImage = defaultHandlers.image as PeekableHandle

/**
 * An image exactly as the default handler prints it, then its width.
 *
 * Only a width this module would read back is written. Anything else would print braces the
 * parser then refuses: the picture would come back without its width and the file would hold
 * an attribute nobody wrote.
 */
const image: PeekableHandle = (
  node: Image,
  parent: Parents | undefined,
  state: State,
  info: Info,
): string => {
  const value = defaultImage(node, parent, state, info)
  const width = imageWidth(node)
  return width === null ? value : `${value}{width=${width}}`
}

image.peek = defaultImage.peek

const imageWidthEscape: Handle = (node: ImageWidthEscape): string => node.value

const toMarkdownExtension: ToMarkdownOptions = { handlers: { image, imageWidthEscape } }

/** A picture the file gives no width, followed by text that would read back as one. */
function lookalike(node: Nodes, next: Nodes | undefined): next is Text {
  return (
    node.type === 'image' &&
    imageWidth(node) === null &&
    next?.type === 'text' &&
    ATTRIBUTE.test(next.value)
  )
}

function hasLookalike(tree: Root): boolean {
  let found = false
  visit(tree, (node: Nodes) => {
    if (!('children' in node)) return
    const children = (node as Parents).children as Nodes[]
    found = children.some((child, index) => lookalike(child, children[index + 1]))
    return found ? EXIT : undefined
  })
  return found
}

/**
 * The tree to print, with text that would read back as the width of the picture before it
 * escaped: `![a](a.png)` followed by the words `{width=5}` would otherwise come back on the
 * next open as a picture five pixels wide.
 *
 * A pass over the tree rather than an `unsafe` pattern, because the serializer's escaping sees
 * one character of context, and a link ends in `)` exactly as a picture does — the pattern
 * escaped `[a](a.png){width=3}` as well, an edit to prose that nobody made. Only a picture
 * *without* a width needs it: after `{width=64}` a second brace is plain text to the parser.
 *
 * Copy-on-write, since the tree may be one the caller keeps (`serializeNode` prints a stored
 * unsupported block): nothing is cloned unless something has to change.
 */
export function escapeWidthLookalikes(tree: Root): Root {
  if (!hasLookalike(tree)) return tree
  const copy = structuredClone(tree)
  visit(copy, (node: Nodes) => {
    if (!('children' in node)) return
    const children = (node as Parents).children as Nodes[]
    for (let index = 0; index + 1 < children.length; index += 1) {
      const next = children[index + 1]
      if (!lookalike(children[index], next)) continue
      const escape: ImageWidthEscape = { type: 'imageWidthEscape', value: '\\{' }
      children.splice(index + 1, 1, escape, { ...next, value: next.value.slice(1) })
      index += 1
    }
  })
  return copy
}

/** remark plugin: teach the serializer to write a picture's width. Reading is `applyImageWidths`. */
export function remarkImageWidth(this: Processor): undefined {
  const data = this.data()
  const extensions = data.toMarkdownExtensions ?? (data.toMarkdownExtensions = [])
  extensions.push(toMarkdownExtension)
}
