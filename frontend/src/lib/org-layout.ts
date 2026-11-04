/**
 * Tidy-tree placement for the org chart (spec §10.2).
 *
 * A pure function over the store's `OrgNode` tree, kept out of the component for the same
 * reason the editor's transforms are: it is the one part of the canvas that can be reasoned
 * about — and later corrected — without a DOM.
 *
 * The rule is the classic one: a leaf takes the next free column, a manager sits centred
 * over the reports they own, and separate roots get a blank column between them so two
 * unrelated trees never read as one.
 *
 * This is the *fallback*, not the layout: a member the user has dragged carries their own
 * `pos_x`/`pos_y` and is placed there instead. What is derived here is where to put someone
 * nobody has arranged — a member just added, or the whole team before it was ever touched —
 * so an untouched roster still stays free of layout data on disk, and "Tidy up" is exactly
 * "forget the hand placements and come back here".
 */

import type { OrgNode } from '@/stores/teams'

export const MEMBER_NODE_WIDTH = 220

const NODE_HEIGHT = 76
const COLUMN_GAP = 44
const ROW_GAP = 64

export interface Point {
  x: number
  y: number
}

export function layoutOrgTree(roots: OrgNode[]): Map<string, Point> {
  const positions = new Map<string, Point>()
  let nextColumn = 0

  function place(node: OrgNode, depth: number): number {
    const y = depth * (NODE_HEIGHT + ROW_GAP)

    if (!node.reports.length) {
      const x = nextColumn * (MEMBER_NODE_WIDTH + COLUMN_GAP)
      nextColumn += 1
      positions.set(node.member.id, { x, y })
      return x
    }

    const childCentres = node.reports.map((report) => place(report, depth + 1))
    const x = (childCentres[0]! + childCentres[childCentres.length - 1]!) / 2
    positions.set(node.member.id, { x, y })
    return x
  }

  for (const root of roots) {
    place(root, 0)
    nextColumn += 1
  }
  return positions
}
