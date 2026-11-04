/**
 * What a folder of the Documents tree may look like — the catalogue its `vxs-folder.json` is
 * written from (block-editor spec §5.2).
 *
 * **The colours are the text colours** (§3.2.1), not a palette of their own: ten words, each
 * with a dark and a light value tuned for contrast in `tokens.css` (`--vxs-md-*`). A folder
 * painted on a dark screen therefore still reads on a light one, and a folder and a phrase
 * both called green are the same green.
 *
 * **The icons are a curated list**, for the reason `lib/icons.ts` gives: looking a Lucide name
 * up dynamically would pull a thousand components into the bundle. The file stores the name,
 * and a name missing here — written by a newer Studio, or by hand — draws the plain folder
 * rather than nothing. Adding an icon is a line in `FOLDER_ICONS` and its label in the locale
 * files (`docs.folderIcons`, keyed by the stored name); the backend checks only that a name has
 * the shape of one.
 */

import {
  Archive,
  BookOpen,
  Bookmark,
  Bug,
  Calendar,
  Castle,
  ClipboardList,
  Code,
  Cpu,
  Crown,
  Database,
  FileText,
  Film,
  Flag,
  FlaskConical,
  Gamepad2,
  Gem,
  Heart,
  Image as ImageIcon,
  Layers,
  Lightbulb,
  Map as MapIcon,
  MessageSquare,
  Music,
  Notebook,
  Palette,
  PenTool,
  Rocket,
  Scroll,
  Shield,
  Sparkles,
  Star,
  Swords,
  Target,
  Terminal,
  Trophy,
  Users,
  Wrench,
  Zap,
} from 'lucide-vue-next'
import type { Component } from 'vue'

import { t, type MessageKey } from '@/i18n'
import { TEXT_COLOR_CHOICES } from '@/lib/md/color'

export type FolderColor = (typeof TEXT_COLOR_CHOICES)[number]

export interface FolderIcon {
  /** The Lucide identifier — what `vxs-folder.json` stores. */
  name: string
  /** What the menu calls it, in the language on screen — a getter, so never spread one. */
  label: string
  component: Component
}

/**
 * One entry of the catalogue. The label is looked up whenever it is read, as `choice()` in
 * `lib/appearance.ts` does: resolved once at import, it would stay in the language the page
 * loaded in. The name is what the file stores, and is never translated.
 */
function icon(name: string, labelKey: MessageKey, component: Component): FolderIcon {
  return {
    name,
    component,
    get label() {
      return t(labelKey)
    },
  }
}

/** The ten colours, in the order the selection toolbar offers them. `grey` is never written. */
export const FOLDER_COLORS: readonly FolderColor[] = TEXT_COLOR_CHOICES

/**
 * Every icon a folder may wear, in the order the menu lays them out: writing, planning,
 * building, the game, and media. The plain folder is not in the list — it is what `null`
 * means, and it opens and closes with the folder where a chosen icon stays put.
 */
export const FOLDER_ICONS: readonly FolderIcon[] = [
  icon('book-open', 'docs.folderIcons.book-open', BookOpen),
  icon('notebook', 'docs.folderIcons.notebook', Notebook),
  icon('file-text', 'docs.folderIcons.file-text', FileText),
  icon('scroll', 'docs.folderIcons.scroll', Scroll),
  icon('clipboard-list', 'docs.folderIcons.clipboard-list', ClipboardList),
  icon('bookmark', 'docs.folderIcons.bookmark', Bookmark),
  icon('archive', 'docs.folderIcons.archive', Archive),
  icon('lightbulb', 'docs.folderIcons.lightbulb', Lightbulb),
  icon('target', 'docs.folderIcons.target', Target),
  icon('flag', 'docs.folderIcons.flag', Flag),
  icon('calendar', 'docs.folderIcons.calendar', Calendar),
  icon('users', 'docs.folderIcons.users', Users),
  icon('message-square', 'docs.folderIcons.message-square', MessageSquare),
  icon('pen-tool', 'docs.folderIcons.pen-tool', PenTool),
  icon('palette', 'docs.folderIcons.palette', Palette),
  icon('code', 'docs.folderIcons.code', Code),
  icon('terminal', 'docs.folderIcons.terminal', Terminal),
  icon('bug', 'docs.folderIcons.bug', Bug),
  icon('flask-conical', 'docs.folderIcons.flask-conical', FlaskConical),
  icon('wrench', 'docs.folderIcons.wrench', Wrench),
  icon('cpu', 'docs.folderIcons.cpu', Cpu),
  icon('database', 'docs.folderIcons.database', Database),
  icon('layers', 'docs.folderIcons.layers', Layers),
  icon('gamepad-2', 'docs.folderIcons.gamepad-2', Gamepad2),
  icon('swords', 'docs.folderIcons.swords', Swords),
  icon('shield', 'docs.folderIcons.shield', Shield),
  icon('map', 'docs.folderIcons.map', MapIcon),
  icon('castle', 'docs.folderIcons.castle', Castle),
  icon('crown', 'docs.folderIcons.crown', Crown),
  icon('gem', 'docs.folderIcons.gem', Gem),
  icon('trophy', 'docs.folderIcons.trophy', Trophy),
  icon('image', 'docs.folderIcons.image', ImageIcon),
  icon('film', 'docs.folderIcons.film', Film),
  icon('music', 'docs.folderIcons.music', Music),
  icon('sparkles', 'docs.folderIcons.sparkles', Sparkles),
  icon('zap', 'docs.folderIcons.zap', Zap),
  icon('star', 'docs.folderIcons.star', Star),
  icon('heart', 'docs.folderIcons.heart', Heart),
  icon('rocket', 'docs.folderIcons.rocket', Rocket),
]

const ICONS_BY_NAME = new Map(FOLDER_ICONS.map((icon) => [icon.name, icon]))

/** The icon a stored name draws, or `null` for the plain folder — including a name not listed. */
export function folderIcon(name: string | null | undefined): FolderIcon | null {
  return name ? (ICONS_BY_NAME.get(name) ?? null) : null
}

export function isFolderColor(value: unknown): value is FolderColor {
  return typeof value === 'string' && (FOLDER_COLORS as readonly string[]).includes(value)
}

/**
 * The CSS colour a folder's icon is drawn in — a token, never a literal (`styling.md`, level 3).
 * `null` for no colour, which leaves the icon the accent every other folder wears.
 */
export function folderColorValue(color: string | null | undefined): string | null {
  const canonical = color === 'grey' ? 'gray' : color
  return isFolderColor(canonical) ? `var(--vxs-md-${canonical})` : null
}

/** What the menu calls a colour, in the language on screen; the word in the file stays as it is. */
export function folderColorLabel(color: FolderColor): string {
  return t(`docs.folderColors.${color}`)
}
