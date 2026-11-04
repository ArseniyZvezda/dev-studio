/**
 * How big the chat popup is (§10.9), and how big it may be.
 *
 * The popup is anchored to the bottom-right corner, so it is resized from its top-left — a
 * drag that grows it leftwards and upwards. Two values are kept apart the way `SidePane`
 * keeps a pane's width: the size the user *chose*, which is what is stored, and the size
 * that *fits*, which is the chosen one held inside the viewport. Storing the fitted size
 * would file the choice down permanently to the smallest window the popup was ever opened
 * in. No DOM here, so the arithmetic runs under vitest's node environment.
 */

export interface ChatSize {
  width: number
  height: number
}

export const DEFAULT_CHAT_SIZE: ChatSize = { width: 384, height: 544 }
export const MIN_CHAT_SIZE: ChatSize = { width: 320, height: 360 }

/** Room the popup never takes: the gutter beside it, and the launcher button below it. */
export const CHAT_GUTTER = { right: 40, bottom: 112 }

/** The largest popup this viewport holds, never smaller than the minimum. */
export function maxChatSize(viewportWidth: number, viewportHeight: number): ChatSize {
  return {
    width: Math.max(MIN_CHAT_SIZE.width, viewportWidth - CHAT_GUTTER.right),
    height: Math.max(MIN_CHAT_SIZE.height, viewportHeight - CHAT_GUTTER.bottom),
  }
}

/** The chosen size held inside the viewport, whole pixels. */
export function fitChatSize(
  chosen: ChatSize,
  viewportWidth: number,
  viewportHeight: number,
): ChatSize {
  const max = maxChatSize(viewportWidth, viewportHeight)
  return {
    width: Math.round(Math.min(max.width, Math.max(MIN_CHAT_SIZE.width, chosen.width))),
    height: Math.round(Math.min(max.height, Math.max(MIN_CHAT_SIZE.height, chosen.height))),
  }
}

/** A stored size, or the default for nothing and for anything that is not two numbers. */
export function readChatSize(raw: string | null): ChatSize {
  if (!raw) return { ...DEFAULT_CHAT_SIZE }
  try {
    const parsed = JSON.parse(raw) as Partial<ChatSize> | null
    const width = Number(parsed?.width)
    const height = Number(parsed?.height)
    if (!Number.isFinite(width) || !Number.isFinite(height)) return { ...DEFAULT_CHAT_SIZE }
    return {
      width: Math.max(MIN_CHAT_SIZE.width, Math.round(width)),
      height: Math.max(MIN_CHAT_SIZE.height, Math.round(height)),
    }
  } catch {
    return { ...DEFAULT_CHAT_SIZE }
  }
}

/** Which of the popup's edges a drag started on. */
export type ResizeEdge = 'left' | 'top' | 'corner'

/**
 * The size a drag has reached: the pointer's travel since it began, with the sign of a
 * popup anchored at the bottom-right — moving left and up grows it.
 */
export function dragChatSize(
  start: ChatSize,
  edge: ResizeEdge,
  dx: number,
  dy: number,
  viewportWidth: number,
  viewportHeight: number,
): ChatSize {
  const width = edge === 'top' ? start.width : start.width - dx
  const height = edge === 'left' ? start.height : start.height - dy
  return fitChatSize({ width, height }, viewportWidth, viewportHeight)
}
