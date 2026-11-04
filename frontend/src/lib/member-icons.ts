/**
 * What a team member may wear instead of their initials — the catalogue `avatar_icon` is
 * chosen from (§10.2).
 *
 * **The icons stand for disciplines, not for moods.** A roster is read to find out who does
 * what, so every entry here is labelled with the job it means — Producer, Game design,
 * Concept art, Sound design — and the groups are the departments a game team actually has.
 * That is also why there is no "smiley" row: the mark has to survive being drawn at 12px
 * beside a name on a task card, where only the silhouette of a *kind of work* still reads.
 *
 * **Curated, for the reason `lib/icons.ts` gives**: resolving a Lucide name dynamically
 * would pull roughly a thousand icon components into the bundle. The roster file stores the
 * bare name, and a name this build does not know — written by a newer Studio, or by hand —
 * falls back to the initials, exactly as an unset icon does. Adding one is a line in a group
 * here; the backend only checks that a name has the shape of one (`api/teams.py`).
 */

import {
  Activity,
  AudioWaveform,
  Bone,
  BookOpen,
  Bot,
  Box,
  Brain,
  Briefcase,
  Brush,
  Bug,
  CalendarCheck,
  Camera,
  ChartLine,
  ClipboardList,
  Code,
  Coins,
  Compass,
  Cpu,
  Crown,
  Database,
  FlaskConical,
  Gamepad2,
  GitBranch,
  Headphones,
  Joystick,
  Languages,
  LayoutDashboard,
  Lightbulb,
  LifeBuoy,
  Map as MapIcon,
  Megaphone,
  MessageSquare,
  MessagesSquare,
  Mic,
  Microscope,
  Music,
  Network,
  Palette,
  PenTool,
  PersonStanding,
  Puzzle,
  Rocket,
  Scale,
  Scroll,
  Server,
  Shapes,
  Shield,
  SlidersHorizontal,
  Sparkles,
  Star,
  Store,
  Target,
  Terminal,
  User,
  Users,
  Wand2,
  Wrench,
} from 'lucide-vue-next'
import type { Component } from 'vue'

import { t, type MessageKey } from '@/i18n'

/**
 * The four fields any surface needs to draw a member's mark. `TeamMember` satisfies it, and
 * so does a `MemberSpend` row off the stats endpoint and a half-filled draft in the member
 * editor — which is why `MemberMark` takes this rather than a whole member.
 */
export interface MemberLook {
  name: string
  kind?: string | null
  avatar_color?: string | null
  avatar_icon?: string | null
}

export interface MemberIcon {
  /** The Lucide identifier — what the roster's `avatar_icon` stores. */
  name: string
  /** The kind of work it stands for, in the interface language. This is what the picker reads out. */
  label: string
  component: Component
}

export interface MemberIconGroup {
  /** The department, as the picker heads the row of icons under it, in the interface language. */
  label: string
  icons: readonly MemberIcon[]
}

/**
 * One icon. The label is a getter over its message key — the catalogue is built once, when this
 * module loads, and a label read then would stay in that language (`choice()` in
 * `lib/appearance.ts` is the same idea). Never spread an entry: that copies the string.
 */
function discipline(name: string, labelKey: MessageKey, component: Component): MemberIcon {
  return {
    name,
    get label() {
      return t(labelKey)
    },
    component,
  }
}

/** One department, its heading looked up on reading like the icons' labels. */
function department(labelKey: MessageKey, icons: readonly MemberIcon[]): MemberIconGroup {
  return {
    get label() {
      return t(labelKey)
    },
    icons,
  }
}

/**
 * The catalogue, by department. The order is the order of a project rather than of the
 * alphabet — who decides, who designs, who writes it, who draws it, who makes it sound,
 * who builds it, who checks it, who sells it — so a person scanning for their own role
 * stops in roughly the right place before reading any label.
 */
export const MEMBER_ICON_GROUPS: readonly MemberIconGroup[] = [
  department('teams.departments.production', [
    discipline('crown', 'teams.icons.director', Crown),
    discipline('briefcase', 'teams.icons.producer', Briefcase),
    discipline('clipboard-list', 'teams.icons.projectManager', ClipboardList),
    discipline('calendar-check', 'teams.icons.planning', CalendarCheck),
    discipline('compass', 'teams.icons.strategy', Compass),
    discipline('users', 'teams.icons.teamLead', Users),
  ]),
  department('teams.departments.design', [
    discipline('gamepad-2', 'teams.icons.gameDesign', Gamepad2),
    discipline('map', 'teams.icons.levelDesign', MapIcon),
    discipline('network', 'teams.icons.systemsDesign', Network),
    discipline('puzzle', 'teams.icons.prototyping', Puzzle),
    discipline('coins', 'teams.icons.economyDesign', Coins),
    discipline('scale', 'teams.icons.balancing', Scale),
  ]),
  department('teams.departments.writing', [
    discipline('pen-tool', 'teams.icons.writer', PenTool),
    discipline('book-open', 'teams.icons.narrativeDesign', BookOpen),
    discipline('message-square', 'teams.icons.dialogue', MessageSquare),
    discipline('scroll', 'teams.icons.lore', Scroll),
    discipline('languages', 'teams.icons.localisation', Languages),
  ]),
  department('teams.departments.art', [
    discipline('palette', 'teams.icons.artist', Palette),
    discipline('brush', 'teams.icons.conceptArt', Brush),
    discipline('box', 'teams.icons.modelling3d', Box),
    discipline('shapes', 'teams.icons.environmentArt', Shapes),
    discipline('person-standing', 'teams.icons.animation', PersonStanding),
    discipline('bone', 'teams.icons.rigging', Bone),
    discipline('sparkles', 'teams.icons.vfx', Sparkles),
    discipline('wand-2', 'teams.icons.technicalArt', Wand2),
    discipline('layout-dashboard', 'teams.icons.uiUx', LayoutDashboard),
    discipline('camera', 'teams.icons.cinematics', Camera),
  ]),
  department('teams.departments.audio', [
    discipline('audio-waveform', 'teams.icons.soundDesign', AudioWaveform),
    discipline('music', 'teams.icons.composer', Music),
    discipline('mic', 'teams.icons.voice', Mic),
    discipline('sliders-horizontal', 'teams.icons.mixing', SlidersHorizontal),
    discipline('headphones', 'teams.icons.audioLead', Headphones),
  ]),
  department('teams.departments.engineering', [
    discipline('code', 'teams.icons.developer', Code),
    discipline('cpu', 'teams.icons.engineProgrammer', Cpu),
    discipline('joystick', 'teams.icons.gameplayProgrammer', Joystick),
    discipline('wrench', 'teams.icons.toolsProgrammer', Wrench),
    discipline('database', 'teams.icons.data', Database),
    discipline('terminal', 'teams.icons.scripting', Terminal),
    discipline('brain', 'teams.icons.aiProgrammer', Brain),
    discipline('git-branch', 'teams.icons.buildAndRelease', GitBranch),
    discipline('server', 'teams.icons.infrastructure', Server),
  ]),
  department('teams.departments.quality', [
    discipline('bug', 'teams.icons.qaTester', Bug),
    discipline('flask-conical', 'teams.icons.testAutomation', FlaskConical),
    discipline('shield', 'teams.icons.security', Shield),
    discipline('activity', 'teams.icons.performance', Activity),
    discipline('chart-line', 'teams.icons.analytics', ChartLine),
    discipline('microscope', 'teams.icons.research', Microscope),
  ]),
  department('teams.departments.outreach', [
    discipline('megaphone', 'teams.icons.marketing', Megaphone),
    discipline('messages-square', 'teams.icons.community', MessagesSquare),
    discipline('store', 'teams.icons.publishing', Store),
    discipline('life-buoy', 'teams.icons.support', LifeBuoy),
    discipline('rocket', 'teams.icons.launch', Rocket),
  ]),
  department('teams.departments.general', [
    discipline('bot', 'teams.icons.aiAgent', Bot),
    discipline('user', 'teams.icons.person', User),
    discipline('lightbulb', 'teams.icons.ideas', Lightbulb),
    discipline('target', 'teams.icons.goals', Target),
    discipline('star', 'teams.icons.anythingElse', Star),
  ]),
]

/** Every icon in the catalogue, flat — what the lookup and the tests walk. */
export const MEMBER_ICONS: readonly MemberIcon[] = MEMBER_ICON_GROUPS.flatMap(
  (group) => group.icons,
)

const ICONS_BY_NAME = new Map(MEMBER_ICONS.map((icon) => [icon.name, icon]))

/**
 * The icon a stored name draws, or `null` when the member has none — including a name this
 * build does not know, which is the same to a reader as having chosen nothing.
 */
export function memberIcon(name: string | null | undefined): MemberIcon | null {
  return name ? (ICONS_BY_NAME.get(name) ?? null) : null
}
