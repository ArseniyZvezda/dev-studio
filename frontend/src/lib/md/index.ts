/**
 * The Markdown document layer for the block editor (block-editor spec §8).
 *
 * Deliberately free of Vue and of the DOM: the guarantees in §8 — nothing lost, nothing
 * moved on a second save — are properties of these functions alone, so they are checked on
 * a corpus of real documents rather than through a rendered editor.
 */

export {
  applyColorSpans,
  isTextColor,
  maskColors,
  sameColor,
  TEXT_COLOR_CHOICES,
  TEXT_COLORS,
  type ColorSpan,
  type TextColor,
} from './color'
export { GFM_OPTIONS, STRINGIFY_OPTIONS } from './conventions'
export {
  normalise,
  parseDocument,
  serializeDocument,
  type DocumentEnvelope,
  type Eol,
  type ParsedDocument,
} from './document'
export { imageWidth, isImageWidth, MAX_IMAGE_WIDTH } from './image-width'
export { WIKI_LINK, type WikiLink } from './wikilink'
