import type { InjectionKey } from 'vue'

/**
 * How the content of a `ModalDialog` claims `Escape` before the dialog closes on it.
 *
 * A consumer returns `true` when it dealt with the key — an open sub-form it closed, a
 * confirmation it dismissed — and the dialog then leaves the press alone. Returning `false`
 * lets it through to the next consumer and finally to closing the dialog.
 *
 * It is an injection rather than a DOM listener because `keydown` bubbles *up*: the handler
 * lives on the dialog panel, and the panel is an ancestor of the content, so a key pressed
 * while the panel itself holds focus — which is where focus lands whenever a control removes
 * itself — never travels through the content at all. Whoever wants the key has to say so.
 */
export type EscapeConsumer = () => boolean

export const dialogEscape: InjectionKey<(consumer: EscapeConsumer) => void> =
  Symbol('dialogEscape')
