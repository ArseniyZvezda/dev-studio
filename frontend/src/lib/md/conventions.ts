/**
 * The Markdown dialect the editor writes, fixed once (block-editor spec §8.1).
 *
 * Every one of these is a choice the serializer would otherwise make per document, and a
 * choice that drifts is what turns "I moved one block" into a diff of the whole file. They
 * are picked to match the documents already on disk, so the first save — the one §8.2
 * allows to reformat — moves as few bytes as possible.
 *
 * `remark-stringify` does not wrap by column and has no option to, which is the other half
 * of §8.1: a paragraph is one line however long it is, so editing a word rewrites a word.
 */

import type { Options as GfmOptions } from 'remark-gfm'
import type { Options as StringifyOptions } from 'remark-stringify'

export const STRINGIFY_OPTIONS: Readonly<StringifyOptions> = Object.freeze({
  bullet: '-',
  // `*` for emphasis and `**` for strong, which is what `strong: '*'` produces. Underscore
  // emphasis is invisible inside snake_case identifiers, and these documents are full of them.
  emphasis: '*',
  strong: '*',
  fence: '`',
  // Always fence, never indent: an indented code block is indistinguishable from a nested
  // list item until you count spaces, and it cannot carry a language (§3.1).
  fences: true,
  rule: '-',
  ruleRepetition: 3,
  ruleSpaces: false,
  // `- item`, continuation indented by two. `'mixed'` would vary the indent with the
  // marker width and make nested lists reflow whenever a list becomes numbered.
  listItemIndent: 'one',
  incrementListMarker: true,
  // ATX (`## Rules`), never setext underlines — levels 3 and up have no setext form, so a
  // document with both would use two heading styles at once.
  setext: false,
  resourceLink: false,
  tightDefinitions: true,
  quote: '"',
} satisfies StringifyOptions)

export const GFM_OPTIONS: Readonly<GfmOptions> = Object.freeze({
  // One space inside every cell, no padding to the widest cell. Aligned columns look
  // better raw but make any edit rewrite every row of the table — §14.2, answered for
  // compact diffs. This is also how the tables already in the corpus are written.
  tableCellPadding: true,
  tablePipeAlign: false,
  // `~one tilde~` stays literal text. GFM accepts it as strikethrough, but §3.2 spells
  // strikethrough `~~`, and treating a lone tilde as markup rewrites prose that meant it.
  singleTilde: false,
} satisfies GfmOptions)
