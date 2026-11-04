/**
 * Lets an element that a `v-if` removes leave the way it arrived (spec §7, the motion section
 * of `base.css`).
 *
 * Every dialog here is mounted by its caller with `v-if` and teleported to `<body>`, so when
 * it closes Vue takes its overlay out of the document in the same frame — the veil vanishes
 * and the page behind snaps back to full brightness. Vue's own `<Transition>` cannot help from
 * the inside: a transition whose component is itself being unmounted skips its leave, so the
 * exit would have to be written at every call site, around components whose root is a
 * `Teleport`.
 *
 * So the exit is played on what Vue already let go of. After the unmount the very same
 * element goes back where it was for one beat, inert and wearing `vxs-leaving`; the CSS runs
 * the exit, and `animationend` removes it for good. By then the component is gone — its
 * effects stopped, its listeners' owners unmounted — so nothing about closing a dialog waits
 * on the animation: focus, `Escape`, the flush a form does on its way out all happen exactly
 * when they did before. What is on screen for those 130ms is a picture of the dialog, which is
 * all an exit ever shows.
 *
 * Three things there are not optional:
 *
 * - **`inert`**, or the picture is a dialog that can still be clicked and tabbed into, with
 *   handlers that call into components that no longer exist.
 * - **Scroll positions are put back.** An element loses them when it leaves the document, so a
 *   transcript read to its end would jump to its top for its last frames.
 * - **No animation, no wait.** Under `data-instant`, or wherever the CSS gives the class no
 *   animation, the element is not put back at all — and a timer removes one whose
 *   `animationend` never comes, because a veil that stayed would cover the app for good.
 */

import { onBeforeUnmount, onUnmounted } from 'vue'

/** The class the exit keys on; the keyframes are in `base.css`. */
const LEAVING = 'vxs-leaving'

/** Past the slowest beat by a wide margin — a net under a lost `animationend`, not a timing. */
const GIVE_UP_MS = 1000

interface Scrolled {
  element: Element
  top: number
  left: number
}

/**
 * Play the leave of `target()` — the element this component renders, usually a teleported
 * overlay — when the component unmounts. `enabled` is asked at that moment, for the dialog
 * that should close as plainly as it opened.
 */
export function useLeave(target: () => HTMLElement | null, enabled: () => boolean = () => true): void {
  let element: HTMLElement | null = null
  let parent: Node | null = null
  let next: Node | null = null
  let scrolled: Scrolled[] = []

  onBeforeUnmount(() => {
    const candidate = target()
    if (!candidate?.isConnected || !enabled()) return
    element = candidate
    parent = candidate.parentNode
    next = candidate.nextElementSibling
    scrolled = [candidate, ...candidate.querySelectorAll('*')]
      .filter((node) => node.scrollTop !== 0 || node.scrollLeft !== 0)
      .map((node) => ({ element: node, top: node.scrollTop, left: node.scrollLeft }))
  })

  // After the DOM is gone and before anything is painted: the hook runs in the same flush.
  onUnmounted(() => {
    const ghost = element
    const home = parent
    element = parent = null
    if (!ghost || !home?.isConnected) return

    ghost.classList.add(LEAVING)
    ghost.inert = true
    ghost.setAttribute('aria-hidden', 'true')
    // Back where it was, so a dialog opened in the same breath is drawn over it, not under.
    home.insertBefore(ghost, next?.parentNode === home ? next : null)
    for (const { element: node, top, left } of scrolled) {
      node.scrollTop = top
      node.scrollLeft = left
    }
    scrolled = []

    if (getComputedStyle(ghost).animationName === 'none') {
      ghost.remove()
      return
    }
    const giveUp = window.setTimeout(() => ghost.remove(), GIVE_UP_MS)
    ghost.addEventListener('animationend', (event) => {
      if (event.target !== ghost) return
      window.clearTimeout(giveUp)
      ghost.remove()
    })
  })
}
