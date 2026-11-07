/**
 * §8 of the block-editor spec, checked on a corpus rather than argued about.
 *
 * The fixtures next door are written in the dialect `conventions.ts` fixes, so the strong
 * assertion is available: reading and writing a canonical document changes nothing at all.
 * Everything the editor is allowed to reformat is exercised separately, from messy input,
 * where only §8.2 — stable from the second save on — has to hold.
 *
 * Point `VOXARIUM_STUDIO_DOC_CORPUS` at a directory of real `.md` files to run the stability and
 * no-loss checks over them too; `.scratch/demo-project/.voxarium_studio/docs` is the one to hand.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { normalise, parseDocument, serializeDocument } from './document'
import type { WikiLink } from './wikilink'

const FIXTURES = fileURLToPath(new URL('./__fixtures__', import.meta.url))

function fixture(name: string): string {
  return readFileSync(join(FIXTURES, name), 'utf8')
}

const FIXTURE_NAMES = readdirSync(FIXTURES).filter((name) => name.endsWith('.md'))

function markdownFilesIn(directory: string): string[] {
  const found: string[] = []
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry)
    if (statSync(path).isDirectory()) found.push(...markdownFilesIn(path))
    else if (/\.(md|markdown|txt)$/i.test(entry)) found.push(path)
  }
  return found
}

/** Every visible character, so "nothing was lost" can be asserted without caring where. */
function letters(source: string): string {
  return source.replace(/\s+/g, '')
}

describe('the corpus', () => {
  it('has fixtures to check', () => {
    expect(FIXTURE_NAMES.length).toBeGreaterThan(0)
  })

  it.each(FIXTURE_NAMES)('%s is already in the dialect, so a save changes nothing', (name) => {
    const source = fixture(name)
    expect(normalise(source)).toBe(source)
  })

  it.each(FIXTURE_NAMES)('%s is stable from the second save on (§8.2)', (name) => {
    const once = normalise(fixture(name))
    expect(normalise(once)).toBe(once)
  })

  it.each(FIXTURE_NAMES)('%s never leaks a masking sentinel to disk', (name) => {
    expect(normalise(fixture(name))).not.toMatch(/[\uE000-\uE003]/)
  })
})

describe('reformatting messy input', () => {
  // Each of these is written the way a person or an agent might, not the way the editor
  // writes. The first save is allowed to move them; the second is not (§8.2).
  const MESSY = {
    'setext headings': 'Title\n=====\n\nSection\n-------\n\nBody.\n',
    'star bullets': '* one\n* two\n* three\n',
    'plus bullets': '+ one\n+ two\n',
    'underscore emphasis': '_italic_ and __bold__ text.\n',
    'indented code': '    const x = 1\n    const y = 2\n',
    'padded table': '| Stat     | Value |\n| -------- | ----- |\n| Distance | 6 m   |\n',
    'tilde fence': '~~~js\nconst x = 1\n~~~\n',
    'star rule': '***\n',
    'lazy continuation': '> a quote\ncontinued lazily\n',
    'trailing spaces': 'A line with trailing spaces.   \n\nAnd another.\n',
    'many blank lines': '# Title\n\n\n\n\nBody after four blank lines.\n',
  }

  it.each(Object.entries(MESSY))('%s is stable after the first save', (_name, source) => {
    const once = normalise(source)
    expect(normalise(once)).toBe(once)
  })

  it.each(Object.entries(MESSY))('%s keeps every character', (_name, source) => {
    // Markers may change; the words may not disappear.
    const markers = /[*_~=#>|+\-`]/g
    expect(letters(normalise(source)).replace(markers, '')).toBe(
      letters(source).replace(markers, ''),
    )
  })
})

describe('the envelope (§7)', () => {
  it('keeps CRLF line endings', () => {
    const source = '# Title\r\n\r\nBody.\r\n'
    expect(normalise(source)).toBe(source)
  })

  it('keeps LF line endings', () => {
    const source = '# Title\n\nBody.\n'
    expect(normalise(source)).toBe(source)
  })

  it('keeps a missing final newline', () => {
    expect(normalise('# Title')).toBe('# Title')
  })

  it('keeps a present final newline', () => {
    expect(normalise('# Title\n')).toBe('# Title\n')
  })

  it('leaves an empty document empty', () => {
    expect(normalise('')).toBe('')
  })

  it('holds frontmatter byte for byte, comments and odd spacing included', () => {
    const frontmatter = '---\ntitle:   Held\n# a comment\nlist: [a, "b"]\n---\n'
    const source = `${frontmatter}\n# Body\n`
    const { envelope } = parseDocument(source)
    // The blank line after the closing fence belongs to the envelope too: it is not in the
    // tree, so anything that does not carry it here deletes it on the first save.
    expect(envelope.frontmatter).toBe(`${frontmatter}\n`)
    expect(normalise(source)).toBe(source)
  })

  it('carries a frontmatter-only document', () => {
    const source = '---\na: 1\n---\n'
    expect(parseDocument(source).envelope.frontmatter).toBe(source)
    expect(normalise(source)).toBe(source)
  })

  it('does not mistake a leading thematic break for frontmatter', () => {
    // No closing fence anywhere, so there is nothing that could be frontmatter.
    const { envelope } = parseDocument('---\n\n# Title\n')
    expect(envelope.frontmatter).toBe('')
  })

  it('does not reformat the YAML it carries', () => {
    const source = '---\na:    1\n---\n\ntext\n'
    expect(normalise(source)).toContain('a:    1')
  })
})

describe('wiki links (§5)', () => {
  const FORMS = [
    '[[test-plan]]',
    '[[qa/test-plan|the QA plan]]',
    '[[a document with spaces]]',
    '[[Mixed Case Name]]',
    '[[dash|]]',
  ]

  it.each(FORMS)('%s survives byte for byte', (link) => {
    expect(normalise(`Text ${link} more.\n`)).toBe(`Text ${link} more.\n`)
  })

  it.each(FORMS)('%s survives inside emphasis', (link) => {
    expect(normalise(`Text **${link}** more.\n`)).toBe(`Text **${link}** more.\n`)
  })

  it('parses target and label', () => {
    const { root } = parseDocument('See [[qa/test-plan|the QA plan]].\n')
    const paragraph = root.children[0]
    if (paragraph.type !== 'paragraph') throw new Error('expected a paragraph')
    const link = paragraph.children.find((child) => child.type === 'wikiLink') as WikiLink
    expect(link.target).toBe('qa/test-plan')
    expect(link.label).toBe('the QA plan')
    expect(link.value).toBe('qa/test-plan|the QA plan')
  })

  it('leaves the label null when there is none', () => {
    const { root } = parseDocument('See [[dash]].\n')
    const paragraph = root.children[0]
    if (paragraph.type !== 'paragraph') throw new Error('expected a paragraph')
    const link = paragraph.children.find((child) => child.type === 'wikiLink') as WikiLink
    expect(link.target).toBe('dash')
    expect(link.label).toBeNull()
  })

  it('is not a link inside a code span', () => {
    const source = 'Write `[[dash]]` to link.\n'
    const { root } = parseDocument(source)
    const paragraph = root.children[0]
    if (paragraph.type !== 'paragraph') throw new Error('expected a paragraph')
    expect(paragraph.children.some((child) => child.type === 'wikiLink')).toBe(false)
    expect(normalise(source)).toBe(source)
  })

  it('is not a link inside a fenced code block', () => {
    const source = '```\n[[dash]]\n```\n'
    const { root } = parseDocument(source)
    const block = root.children[0]
    if (block.type !== 'code') throw new Error('expected a code block')
    expect(block.value).toBe('[[dash]]')
    expect(normalise(source)).toBe(source)
  })

  it('survives a link definition that collides with the target', () => {
    // `[[dash]]` next to `[dash]: …` is what would make a text-level pass misread the
    // brackets as a shortcut reference wrapped in literal ones.
    const source = 'See [[dash]] and [dash].\n\n[dash]: https://example.invalid/d\n'
    expect(normalise(source)).toBe(source)
    const { root } = parseDocument(source)
    const paragraph = root.children[0]
    if (paragraph.type !== 'paragraph') throw new Error('expected a paragraph')
    expect(paragraph.children.some((child) => child.type === 'wikiLink')).toBe(true)
  })

  it('does not rewrite the target to its resolved form', () => {
    // The whole point of §5: resolution is a view concern, the file keeps what was typed.
    expect(normalise('[[test-plan]]\n')).toBe('[[test-plan]]\n')
    expect(normalise('[[Test Plan]]\n')).toBe('[[Test Plan]]\n')
  })
})

describe('the fixed dialect (§8.1)', () => {
  it('does not wrap long paragraphs', () => {
    const sentence = `${'word '.repeat(90).trim()}.`
    const output = normalise(`${sentence}\n`)
    expect(output.split('\n').filter((line) => line.length > 0)).toHaveLength(1)
  })

  it('writes bullets as -', () => {
    expect(normalise('* one\n* two\n')).toBe('- one\n- two\n')
  })

  it('writes strong as ** and emphasis as *', () => {
    expect(normalise('__bold__ and _italic_.\n')).toBe('**bold** and *italic*.\n')
  })

  it('fences code rather than indenting it, and keeps the language', () => {
    expect(normalise('    const x = 1\n')).toBe('```\nconst x = 1\n```\n')
    expect(normalise('~~~ts\nconst x = 1\n~~~\n')).toBe('```ts\nconst x = 1\n```\n')
  })

  it('writes thematic breaks as ---', () => {
    expect(normalise('***\n')).toBe('---\n')
  })

  it('writes headings as ATX', () => {
    expect(normalise('Title\n=====\n')).toBe('# Title\n')
  })

  it('writes tables one space per cell, without padding to the widest', () => {
    const source = '| Stat     | Value |\n| -------- | ----- |\n| Distance | 6 m   |\n'
    expect(normalise(source)).toBe('| Stat | Value |\n| --- | --- |\n| Distance | 6 m |\n')
  })

  it('keeps table column alignment', () => {
    const source = '| a | b | c |\n| :--- | :---: | ---: |\n| 1 | 2 | 3 |\n'
    expect(normalise(source)).toBe(source)
  })

  it('writes a soft break as a backslash (§14.1)', () => {
    const source = 'first\\\nsecond\n'
    expect(normalise(source)).toBe(source)
    // Two trailing spaces mean the same thing and are normalised to the visible form.
    expect(normalise('first  \nsecond\n')).toBe(source)
  })
})

describe('constructs outside §3.1 (§8.3)', () => {
  const KEEP = {
    image: '![Dash frames](../assets/dash.png "Frame sheet")\n',
    'inline html': 'text with <kbd>Ctrl</kbd> in it\n',
    'html block': '<details>\n  <summary>By hand</summary>\n</details>\n',
    'link definition': 'Use [the spec][spec].\n\n[spec]: https://example.invalid/s "The spec"\n',
    footnote: 'A claim[^n]\n\n[^n]: The evidence.\n',
    autolink: '<https://example.invalid/raw>\n',
    'escaped markers': '\\*not emphasis\\* and \\[not a link]\n',
  }

  it.each(Object.entries(KEEP))('%s comes back unchanged', (_name, source) => {
    expect(normalise(source)).toBe(source)
  })

  it('drops only the redundant half of an escaped bracket pair', () => {
    // `\]` carries nothing once `\[` is escaped — no link can start — so the serializer
    // writes the shorter form. The escape that does the work is kept, which is what §8.3
    // is actually about; the second save then changes nothing.
    const once = normalise('\\[not a link\\]\n')
    expect(once).toBe('\\[not a link]\n')
    expect(normalise(once)).toBe(once)
  })
})

describe('directives (§3.3)', () => {
  it('keeps a callout with a heading', () => {
    const source = ':::warning[Blocked]\nBody.\n:::\n'
    expect(normalise(source)).toBe(source)
  })

  it('keeps a callout without a heading', () => {
    const source = ':::note\nBody.\n:::\n'
    expect(normalise(source)).toBe(source)
  })

  it('keeps a toggle with its open attribute', () => {
    const source = ':::toggle[Details]{open}\nBody.\n:::\n'
    expect(normalise(source)).toBe(source)
  })

  it('exposes the name, label and attributes to the editor', () => {
    const { root } = parseDocument(':::toggle[Details]{open}\nBody.\n:::\n')
    const directive = root.children[0]
    if (directive.type !== 'containerDirective') throw new Error('expected a directive')
    expect(directive.name).toBe('toggle')
    expect(directive.attributes?.open).toBe('')
    const label = directive.children[0]
    if (label.type !== 'paragraph') throw new Error('expected a label paragraph')
    expect(label.data?.directiveLabel).toBe(true)
  })
})

describe('an external corpus', () => {
  const directory = process.env.VOXARIUM_STUDIO_DOC_CORPUS
  const files = directory ? markdownFilesIn(directory) : []

  it('was pointed at some documents, or was not asked for', () => {
    expect(directory === undefined || files.length > 0).toBe(true)
  })

  if (files.length) {
    it.each(files)('%s is stable from the second save on', (path) => {
      const once = normalise(readFileSync(path, 'utf8'))
      expect(normalise(once)).toBe(once)
    })

    it.each(files)('%s keeps every wiki link', (path) => {
      const source = readFileSync(path, 'utf8')
      const before = source.match(/\[\[[^[\]]*\]\]/g) ?? []
      const after = normalise(source).match(/\[\[[^[\]]*\]\]/g) ?? []
      expect(after).toEqual(before)
    })
  }
})

describe('serializeDocument', () => {
  it('round-trips a parsed document through its own envelope', () => {
    const source = '---\r\na: 1\r\n---\r\n\r\n# Title\r\n'
    const { root, envelope } = parseDocument(source)
    expect(envelope.eol).toBe('\r\n')
    expect(serializeDocument(root, envelope)).toBe(source)
  })

  it('re-serialises an edited tree into the original envelope', () => {
    const { root, envelope } = parseDocument('---\na: 1\n---\n\n# Title\r\n\r\nBody.')
    root.children.push({ type: 'thematicBreak' })
    // Frontmatter, CRLF and the missing final newline all survive the edit.
    expect(serializeDocument(root, envelope)).toBe('---\r\na: 1\r\n---\r\n\r\n# Title\r\n\r\nBody.\r\n\r\n---')
  })
})
