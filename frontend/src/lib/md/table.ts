/**
 * `| --- |` instead of `| - |` in the delimiter row of a GFM table.
 *
 * §14.2 was answered for compact diffs: cells are not padded out to the widest one, because
 * then editing a single word rewrites every row. `mdast-util-gfm-table` ties two things to
 * that one option though — with `tablePipeAlign: false` it also shrinks the delimiter row to
 * the shortest legal form, `| - | :- | -: |`, which nothing else in the ecosystem writes and
 * which no document in this project already uses.
 *
 * So the delimiter row alone is widened back. It is the second line of the handler's own
 * output and cannot be anything else, which is what makes rewriting it by text safe here
 * and unsafe as a pass over the finished document.
 */

import { gfmTableToMarkdown } from 'mdast-util-gfm-table'
import type { Options as ToMarkdownOptions } from 'mdast-util-to-markdown'
import type { Processor } from 'unified'

import { GFM_OPTIONS } from './conventions'

const DELIMITER_CELL = /^(\s*)(:?)-+(:?)(\s*)$/

function widenDelimiterRow(row: string): string {
  const cells = row.split('|')
  let widened = false
  const next = cells.map((cell, index) => {
    // The outer pipes leave an empty string at each end; those are not cells.
    if (index === 0 || index === cells.length - 1) return cell
    const match = DELIMITER_CELL.exec(cell)
    if (!match) return cell
    widened = true
    return `${match[1]}${match[2]}---${match[3]}${match[4]}`
  })
  return widened ? next.join('|') : row
}

const base = gfmTableToMarkdown(GFM_OPTIONS)
const baseTable = base.handlers?.table

/** remark plugin, applied after `remark-gfm` so this handler is the one that wins. */
export function remarkWideTableDelimiters(this: Processor): undefined {
  if (!baseTable) return

  const extension: ToMarkdownOptions = {
    handlers: {
      table: (node, parent, state, info) => {
        const lines = baseTable.call(state, node, parent, state, info).split('\n')
        if (lines.length > 1) lines[1] = widenDelimiterRow(lines[1])
        return lines.join('\n')
      },
    },
  }

  const data = this.data()
  const extensions = data.toMarkdownExtensions ?? (data.toMarkdownExtensions = [])
  extensions.push(extension)
}
