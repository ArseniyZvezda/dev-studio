/**
 * How another person's cursor and selection are drawn in the editor (dev spec §16.4).
 *
 * Drawn by decorations, never by nodes: a cursor is not part of the document, and nothing
 * about who is looking where may reach the Markdown. The colour is the one the server gave
 * the person, so they are the same colour in everybody's editor.
 */

import type { DecorationAttrs } from '@tiptap/pm/view'

interface CaretUser {
  name?: unknown
  color?: unknown
}

/** A colour string that is safe to put in a style, or a neutral one. */
function colorOf(user: CaretUser): string {
  const color = typeof user.color === 'string' ? user.color.trim() : ''
  return /^#[0-9a-f]{3,8}$/i.test(color) ? color : '#888888'
}

export function renderCaret(user: CaretUser): HTMLElement {
  const color = colorOf(user)
  const caret = document.createElement('span')
  caret.classList.add('collab-caret')
  caret.style.borderColor = color

  const label = document.createElement('span')
  label.classList.add('collab-caret-label')
  label.style.backgroundColor = color
  // As text, never as HTML: the name was typed by whoever runs the server.
  label.textContent = typeof user.name === 'string' ? user.name : ''
  caret.append(label)
  return caret
}

export function renderSelection(user: CaretUser): DecorationAttrs {
  // The colour at a fixed low alpha, so text under somebody's selection stays readable in
  // both themes whatever colour they were given.
  return { class: 'collab-selection', style: `background-color: ${colorOf(user)}33` }
}
