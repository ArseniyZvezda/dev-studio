/**
 * Where the user is in the Studio, as a chat message carries it to a team member (§10.9).
 *
 * A chat hands the member the user's *whereabouts* in place of a task's brief: the section
 * they are on, the document or file they have open, and any text they selected. This module
 * is the data half of that — a route and what the page knows about itself in, a
 * `ChatContext` out — kept free of the DOM so it runs under vitest's node environment; the
 * widget reads the route, the docs store and the selection and hands them here.
 */

import type { ChatContext } from '@/api/types'
import { t, type MessageKey } from '@/i18n'

/**
 * The sections as the rail names them in English, by route name (`router/index.ts`).
 *
 * English whatever language the interface speaks, because this is what the member is *told*:
 * it travels as `ChatContext.page`, inside prompts that are English too. What the user reads on
 * a chip is `pageLabelOnScreen`.
 */
export const PAGE_LABELS: Record<string, string> = {
  explorer: 'Explorer',
  docs: 'Documents',
  chats: 'Chats',
  pipelines: 'Pipelines',
  'pipeline-editor': 'Pipeline editor',
  tasks: 'Tasks',
  producer: 'Producer Mode',
  teams: 'Teams',
  statistics: 'Statistics',
  automations: 'Automations',
  vcs: 'Version Control',
  skills: 'Skills',
  mcp: 'MCP Servers',
  plugins: 'Plugins',
  'project-settings': 'Project Settings',
  settings: 'Settings',
}

/** What `pageLabel` calls the root of the address bar. */
const HOME_LABEL = 'Home'

/** The same sections in the language on screen, by the same route names. */
const PAGE_LABEL_KEYS: Record<string, MessageKey> = {
  explorer: 'chat.page.explorer',
  docs: 'chat.page.docs',
  chats: 'nav.section.chats',
  pipelines: 'chat.page.pipelines',
  'pipeline-editor': 'chat.page.pipelineEditor',
  tasks: 'chat.page.tasks',
  producer: 'producerPage.title',
  teams: 'chat.page.teams',
  statistics: 'chat.page.statistics',
  automations: 'chat.page.automations',
  vcs: 'chat.page.vcs',
  skills: 'chat.page.skills',
  mcp: 'chat.page.mcp',
  plugins: 'chat.page.plugins',
  'project-settings': 'nav.section.projectSettings',
  settings: 'chat.page.settings',
}

/** The part of a Vue Router location this reads — a `RouteLocationNormalized` satisfies it. */
export interface PageLocation {
  name?: string | symbol | null
  fullPath: string
  query: Record<string, unknown>
}

/** What the open page knows about itself beyond its address. */
export interface PageState {
  /** The open document's path under the docs folder, when the Documents section has one. */
  docPath?: string | null
  /** Its heading as the editor has it right now. */
  docTitle?: string | null
}

/**
 * How much selected text a message carries. A selection is context, not an upload: the
 * backend describes at most 8 000 characters of it to the member, and this keeps the
 * request itself bounded when somebody selects a whole document.
 */
export const MAX_SELECTION = 16_000

function queryString(query: Record<string, unknown>, key: string): string | null {
  const value = query[key]
  return typeof value === 'string' && value ? value : null
}

/**
 * The section's name for a location — the rail's label for a known route, and a title
 * made of the first path segment for a plugin page the rail learned about at runtime.
 */
export function pageLabel(location: PageLocation): string {
  const name = typeof location.name === 'string' ? location.name : ''
  if (name && PAGE_LABELS[name]) return PAGE_LABELS[name]
  const first = (location.fullPath.split('?')[0] ?? '').split('/').filter(Boolean)[0] ?? ''
  if (!first) return HOME_LABEL
  const words = decodeURIComponent(first).replace(/[-_]+/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/**
 * A label `pageLabel` produced, as the user reads it: a section's name in the language on
 * screen.
 *
 * It takes the English label rather than a route because a sent message keeps only the label,
 * and the chip under it in the transcript has to read like the one over the composer. A plugin
 * page's title is made of its address, which no language owns, and is shown as it is.
 */
export function pageLabelOnScreen(label: string): string {
  if (label === HOME_LABEL) return t('chat.page.home')
  const name = Object.keys(PAGE_LABELS).find((route) => PAGE_LABELS[route] === label)
  const key = name ? PAGE_LABEL_KEYS[name] : undefined
  return key ? t(key) : label
}

/** Everything about the user's position except the selection, which the DOM owns. */
export function describeLocation(
  location: PageLocation,
  state: PageState = {},
): Omit<ChatContext, 'selection'> {
  const name = typeof location.name === 'string' ? location.name : ''
  let document: string | null = null
  let file: string | null = null
  let title = ''
  if (name === 'docs') {
    // The store's answer first: it knows the document even before the address bar follows.
    document = state.docPath ?? queryString(location.query, 'path')
    title = (state.docTitle ?? '').trim()
  } else if (name === 'explorer') {
    file = queryString(location.query, 'path')
  }
  return { page: pageLabel(location), route: location.fullPath, title, document, file }
}

/** The part of a DOM `Selection` this reads, so it can be checked without a document. */
export interface SelectionLike {
  isCollapsed: boolean
  rangeCount: number
  anchorNode: unknown
  toString(): string
}

/**
 * The text of a selection made on the page, or `''`.
 *
 * `inside` says whether a node belongs to the chat widget itself: text selected in the
 * composer or in an earlier reply is not context, it is the conversation. Clipped to
 * `MAX_SELECTION` characters — the member is told when a selection was cut.
 */
export function selectedText(
  selection: SelectionLike | null,
  inside: ((node: unknown) => boolean) | null = null,
): string {
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return ''
  if (inside && selection.anchorNode && inside(selection.anchorNode)) return ''
  const text = selection.toString().trim()
  return text.length > MAX_SELECTION ? text.slice(0, MAX_SELECTION) : text
}

/** The last path segment — what a chip shows for a document or a file. */
export function basename(path: string): string {
  const parts = path.split('/').filter(Boolean)
  return parts[parts.length - 1] ?? path
}
