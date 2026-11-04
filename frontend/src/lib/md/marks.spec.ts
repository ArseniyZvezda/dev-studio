/**
 * Nested emphasis through the editor (block-editor spec §8.2).
 *
 * The editor saves by way of ProseMirror's flat marks, so the nesting is rebuilt on the way out,
 * and a wrong guess is not a reformat — `***a** b*` came back as `***a****&#x20;b*`, and a line
 * already damaged that way gained two literal asterisks on every save after. The plain
 * parse-and-print path never had the problem, which is why it is the yardstick here: whatever
 * the editor writes has to be what `normalise` writes.
 */

import { describe, expect, it } from 'vitest'

import { normalise, parseDocument, serializeDocument } from './document'
import { mdastToProseMirror, proseMirrorToMdast } from './prosemirror'

/** What the editor writes after opening the document and touching nothing. */
function save(source: string): string {
  const { root, envelope } = parseDocument(source)
  return serializeDocument(proseMirrorToMdast(mdastToProseMirror(root)), envelope)
}

const tree = (source: string): string =>
  JSON.stringify(parseDocument(source).root, (key, value) => (key === 'position' ? undefined : value))

describe('emphasis nested in emphasis survives the editor', () => {
  it.each([
    '***Дата:** 14 апреля 2148 года*\n',
    '***a** b*\n',
    '*a **b***\n',
    '**a *b***\n',
    '***a* b**\n',
    '***a***\n',
    '~~**a** b~~\n',
    'Say ***this** loudly* and **that *softly***.\n',
  ])('%s', (source) => {
    expect(save(source)).toBe(normalise(source))
    expect(tree(save(source))).toBe(tree(source))
    expect(save(save(source))).toBe(save(source))
  })

  it('stops a line an earlier save damaged from growing', () => {
    const damaged = '***Дата:****\\*\\* 14 апреля 2148 года*\n'
    const once = save(damaged)
    expect(tree(once)).toBe(tree(damaged))
    expect(save(once)).toBe(once)
  })
})
