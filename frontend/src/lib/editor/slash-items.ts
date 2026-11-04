/**
 * What the `/` menu can insert, and how it is searched (block-editor spec §4.3).
 *
 * The labels follow the interface language, and the **search is bilingual** whichever one it
 * is: a designer typing `заг` is telling the menu what they want just as clearly as one typing
 * `head`, and refusing to understand them because the button says "Heading" — or «Заголовок»
 * — would be pedantry. So a query is matched against the English name, the name on screen and
 * the aliases. Synonyms are here for the same reason — `todo` and `галочка` both mean the
 * checklist, and neither is its name.
 *
 * Headings 4–6 are deliberately absent. §3.1 keeps them working in a file and out of the
 * menus, because six levels of heading in a picker is noise for the two people a year who
 * want the fifth.
 */

import {
  Code2,
  Heading1,
  Heading2,
  Heading3,
  Info,
  List,
  ListOrdered,
  ListTodo,
  Minus,
  Quote,
  Table as TableIcon,
  Text as TextIcon,
  Triangle,
} from 'lucide-vue-next'
import type { Editor, Range } from '@tiptap/core'

import { t, type MessageKey } from '@/i18n'
import { en } from '@/i18n/messages'

export type SlashGroup = 'Basic' | 'Lists' | 'Advanced'

export interface SlashItem {
  id: string
  /** The item's name, shown through `slashItemLabel` in the language on screen. */
  labelKey: MessageKey
  group: SlashGroup
  hint: string
  icon: typeof TextIcon
  /** Extra words this item answers to — Russian, English, and what people call it. */
  aliases: string[]
  /**
   * Apply the item, consuming `range` — the typed `/query`.
   *
   * Items that *change* the current block delete the range and set the node type. Items that
   * *insert* a new block use `insertContentAt(range, …)` instead, and that difference is a
   * fix rather than a style: `deleteRange(…).insertTable(…)` in one chain left the query text
   * behind and put the table after it, because the insert used a selection the delete had
   * not moved. Replacing the range in a single command cannot get that wrong.
   */
  run: (editor: Editor, range: Range) => void
}

export const SLASH_GROUPS: SlashGroup[] = ['Basic', 'Lists', 'Advanced']

/** A group's id is what the code and the tests speak; its heading is a message. */
const GROUP_LABELS: Record<SlashGroup, MessageKey> = {
  Basic: 'editor.slash.groups.basic',
  Lists: 'editor.slash.groups.lists',
  Advanced: 'editor.slash.groups.advanced',
}

/** A group's heading in the popup, in the language on screen. */
export function slashGroupLabel(group: SlashGroup): string {
  return t(GROUP_LABELS[group])
}

/** An item's name in the language on screen — looked up when it is shown, never stored. */
export function slashItemLabel(item: SlashItem): string {
  return t(item.labelKey)
}

/**
 * An item's name as English has it, whatever the interface speaks: the name the aliases were
 * written beside, which a query is matched against in every language.
 */
function englishLabel(item: SlashItem): string {
  let node: unknown = en
  for (const part of item.labelKey.split('.')) {
    node = (node as Record<string, unknown> | undefined)?.[part]
  }
  return typeof node === 'string' ? node : slashItemLabel(item)
}

function tableRow(cell: 'tableHeader' | 'tableCell', columns: number) {
  return {
    type: 'tableRow',
    content: Array.from({ length: columns }, () => ({
      type: cell,
      attrs: { colspan: 1, rowspan: 1, colwidth: null },
      content: [{ type: 'paragraph' }],
    })),
  }
}

export const SLASH_ITEMS: SlashItem[] = [
  {
    id: 'paragraph',
    labelKey: 'editor.blocks.paragraph',
    group: 'Basic',
    hint: '',
    icon: TextIcon,
    aliases: ['текст', 'абзац', 'параграф', 'plain', 'paragraph', 'body'],
    run: (editor, range) => editor.chain().focus().deleteRange(range).setParagraph().run(),
  },
  {
    id: 'heading1',
    labelKey: 'editor.blocks.heading1',
    group: 'Basic',
    hint: '#',
    icon: Heading1,
    aliases: ['заголовок', 'заг', 'h1', 'head', 'title', 'название'],
    run: (editor, range) =>
      editor.chain().focus().deleteRange(range).setNode('heading', { level: 1 }).run(),
  },
  {
    id: 'heading2',
    labelKey: 'editor.blocks.heading2',
    group: 'Basic',
    hint: '##',
    icon: Heading2,
    aliases: ['заголовок', 'заг', 'h2', 'head', 'подзаголовок', 'раздел', 'section'],
    run: (editor, range) =>
      editor.chain().focus().deleteRange(range).setNode('heading', { level: 2 }).run(),
  },
  {
    id: 'heading3',
    labelKey: 'editor.blocks.heading3',
    group: 'Basic',
    hint: '###',
    icon: Heading3,
    aliases: ['заголовок', 'заг', 'h3', 'head', 'подраздел'],
    run: (editor, range) =>
      editor.chain().focus().deleteRange(range).setNode('heading', { level: 3 }).run(),
  },
  {
    id: 'blockquote',
    labelKey: 'editor.blocks.blockquote',
    group: 'Basic',
    hint: '>',
    icon: Quote,
    aliases: ['цитата', 'quote', 'блокквот', 'blockquote', 'выдержка'],
    run: (editor, range) => editor.chain().focus().deleteRange(range).setBlockquote().run(),
  },
  {
    id: 'horizontalRule',
    labelKey: 'editor.blocks.horizontalRule',
    group: 'Basic',
    hint: '---',
    icon: Minus,
    aliases: ['разделитель', 'линия', 'черта', 'divider', 'rule', 'hr', 'separator'],
    run: (editor, range) => editor.chain().focus().deleteRange(range).setHorizontalRule().run(),
  },

  {
    id: 'bulletList',
    labelKey: 'editor.blocks.bulletList',
    group: 'Lists',
    hint: '-',
    icon: List,
    aliases: ['список', 'маркированный', 'буллет', 'bullet', 'list', 'ul', 'точки'],
    run: (editor, range) => editor.chain().focus().deleteRange(range).toggleBulletList().run(),
  },
  {
    id: 'orderedList',
    labelKey: 'editor.blocks.orderedList',
    group: 'Lists',
    hint: '1.',
    icon: ListOrdered,
    aliases: ['нумерованный', 'номер', 'список', 'numbered', 'ordered', 'ol', 'цифры'],
    run: (editor, range) => editor.chain().focus().deleteRange(range).toggleOrderedList().run(),
  },
  {
    id: 'taskList',
    labelKey: 'editor.blocks.taskList',
    group: 'Lists',
    hint: '- [ ]',
    icon: ListTodo,
    aliases: ['чек', 'чеклист', 'галочка', 'задача', 'todo', 'task', 'check', 'checkbox'],
    run: (editor, range) => editor.chain().focus().deleteRange(range).toggleTaskList().run(),
  },

  {
    id: 'codeBlock',
    labelKey: 'editor.blocks.codeBlock',
    group: 'Advanced',
    hint: '```',
    icon: Code2,
    aliases: ['код', 'листинг', 'code', 'snippet', 'pre', 'программа'],
    run: (editor, range) => editor.chain().focus().deleteRange(range).setCodeBlock().run(),
  },
  {
    id: 'table',
    labelKey: 'editor.blocks.table',
    group: 'Advanced',
    hint: '|',
    icon: TableIcon,
    aliases: ['таблица', 'таб', 'table', 'grid', 'колонки'],
    // Built by hand rather than with `insertTable`, for two reasons: the range replacement
    // has to be atomic (see `insertContentAt` below), and `align` is this schema's own
    // attribute, which the built-in command knows nothing about.
    run: (editor, range) =>
      editor
        .chain()
        .focus()
        .insertContentAt(range, {
          type: 'table',
          attrs: { align: [null, null] },
          content: [tableRow('tableHeader', 2), tableRow('tableCell', 2), tableRow('tableCell', 2)],
        })
        .run(),
  },
  {
    id: 'callout',
    labelKey: 'editor.blocks.callout',
    group: 'Advanced',
    hint: ':::note',
    icon: Info,
    // One item, not four: the kind is a property of the block (§3.1), edited in its menu.
    // The names of all four still find it, because that is what people will type.
    aliases: [
      'выноска',
      'заметка',
      'подсказка',
      'предупреждение',
      'опасность',
      'callout',
      'note',
      'tip',
      'warning',
      'danger',
      'admonition',
      'info',
    ],
    run: (editor, range) =>
      editor
        .chain()
        .focus()
        .insertContentAt(range, {
          type: 'callout',
          attrs: { kind: 'note' },
          content: [{ type: 'directiveTitle' }, { type: 'paragraph' }],
        })
        .run(),
  },
  {
    id: 'toggle',
    labelKey: 'editor.blocks.toggle',
    group: 'Advanced',
    hint: ':::toggle',
    icon: Triangle,
    aliases: ['тоггл', 'сворачивание', 'спойлер', 'детали', 'toggle', 'collapse', 'details', 'spoiler'],
    run: (editor, range) =>
      editor
        .chain()
        .focus()
        .insertContentAt(range, {
          type: 'toggle',
          attrs: { open: false },
          content: [{ type: 'directiveTitle' }, { type: 'paragraph' }],
        })
        .run(),
  },
]

/**
 * Score one candidate against a query, or `null` when it does not match.
 *
 * Subsequence matching rather than substring: `hd1` should find "Heading 1", because that is
 * how people type when they already know what they want. A run of adjacent characters scores
 * higher than a scattered one, and a match at the start of a word higher still, which is what
 * keeps "Heading 1" above "Bulleted list" for `he`.
 */
function score(candidate: string, query: string): number | null {
  const haystack = candidate.toLowerCase()
  const needle = query.toLowerCase()
  if (!needle) return 0

  let total = 0
  let cursor = 0
  let previous = -2

  for (const [index, character] of [...needle].entries()) {
    const found = haystack.indexOf(character, cursor)
    if (found === -1) return null
    // Starting on the first letter counts for a lot. Without it a scattered match with a
    // long adjacent tail beats an obvious one: `blist` picked "Numbered list" over
    // "Bulleted list", because `list` is adjacent in both and only the `b` differed.
    if (index === 0 && found === 0) total += 16
    if (found === previous + 1) total += 8
    if (found === 0 || ' -/'.includes(haystack[found - 1] ?? '')) total += 6
    total += Math.max(0, 4 - found / 4)
    previous = found
    cursor = found + 1
  }
  // A short candidate that matched is a better answer than a long one that also did.
  return total + Math.max(0, 12 - haystack.length / 2)
}

export function searchSlashItems(query: string): SlashItem[] {
  const trimmed = query.trim()
  if (!trimmed) return SLASH_ITEMS

  const ranked: { item: SlashItem; score: number }[] = []
  for (const item of SLASH_ITEMS) {
    // The label wins over an alias at equal quality, so the thing the user can see on
    // screen is what a typed prefix of it selects. The English name counts as a label in
    // every language; in English the two are one string.
    const labels = new Set([slashItemLabel(item), englishLabel(item)])
    const best = Math.max(
      ...[...labels].map((label) => score(label, trimmed) ?? -Infinity),
      ...item.aliases.map((alias) => (score(alias, trimmed) ?? -Infinity) - 2),
    )
    if (best > -Infinity) ranked.push({ item, score: best })
  }

  ranked.sort((one, other) => other.score - one.score)
  return ranked.map((entry) => entry.item)
}

/** The matches grouped for display, empty groups dropped (§4.3). */
export function groupSlashItems(items: SlashItem[]): { group: SlashGroup; items: SlashItem[] }[] {
  return SLASH_GROUPS.map((group) => ({
    group,
    items: items.filter((item) => item.group === group),
  })).filter((entry) => entry.items.length > 0)
}
