/**
 * How wide the Explorer's and Documents' side pane is — per pane, per browser (§6.2, §10.4).
 *
 * Not in the managed project's `config.yaml`. §7 appearance is what the *project* looks like
 * and travels to whoever checks it out, and `docs.font_size` is there because it is a
 * decision about the document — everyone reading that spec should read it the same way. How
 * many pixels this browser on this monitor gives a file tree is neither: it belongs to the
 * reader and the screen in front of them, like the transcript verbosity dial, and in the
 * project it would be a pixel count landing in git on every drag and arriving in a
 * colleague's checkout with someone else's layout. `.claude/rules/styling.md`'s "do not
 * persist appearance to localStorage" is a rule about the *theme*, which travels with the
 * project; this is the mirror case and the same reasoning points the other way.
 *
 * The parsing is here rather than in the component because it is the half with a contract.
 * A stored value can be absent (a first visit), unreadable (hand-edited, or written by a
 * build that spelled the key differently) or simply wider than this pane may be, and all
 * three have to come back as a width the pane can actually take.
 */

/** What the *other* pane keeps whatever the window is. A rail wide enough to swallow the
 *  canvas is a rail nobody can drag back, because the thing it was sizing is gone. */
export const MIN_REMAINDER = 320

/**
 * The viewport ceiling — the half of the clamp that depends on the window rather than on the
 * pane, and therefore the half that has to be re-applied rather than stored.
 *
 * `ResizeHandle` applies it to a drag, an arrow key and a double-click; `SidePane` applies it
 * to the width it renders at, on restore and on every window resize. It is deliberately *not*
 * applied by `paneWidth` below: what came out of storage is what the user chose, and filing
 * that down permanently because they once opened the pane in a narrow window would lose it.
 *
 * `viewport` is passed in rather than read off `window` here so this module stays free of the
 * DOM — `npm test` runs in a node environment, and the arithmetic is the half worth pinning.
 */
export function clampPaneWidth(width: number, min: number, max: number, viewport: number): number {
  const ceiling = Math.max(min, Math.min(max, viewport - MIN_REMAINDER))
  return Math.round(Math.max(min, Math.min(ceiling, width)))
}

export function paneWidth(
  raw: string | null,
  fallback: number,
  min: number,
  max: number,
): number {
  const stored = Number.parseInt(raw ?? '', 10)
  // Absent and corrupt take the same path: `parseInt(null ?? '')` and `parseInt('wide')`
  // are both NaN, and a pane 0px wide is worse than one nobody sized.
  if (!Number.isFinite(stored)) return fallback
  return Math.min(max, Math.max(min, stored))
}
