import { describe, expect, it } from 'vitest'
import { writePathsFromText } from './task-write-paths'

describe('explicit write-file entries', () => {
  it('keeps paths with spaces and Windows separators as whole entries', () => {
    expect(writePathsFromText('  docs/design notes.md\r\nD:\\Game Project\\Source\\Player.as\n')).toEqual([
      'docs/design notes.md', 'D:\\Game Project\\Source\\Player.as',
    ])
  })

  it('clears the list with blank input and drops blank lines and exact duplicates', () => {
    expect(writePathsFromText(' \r\n\n')).toEqual([])
    expect(writePathsFromText('src/a.py\n\nsrc/b.py\n src/a.py ')).toEqual(['src/a.py', 'src/b.py'])
  })

  it('does not turn commas or filename-like words in an entry into extra paths', () => {
    expect(writePathsFromText('docs/one, two.md\nRead docs/design.md')).toEqual([
      'docs/one, two.md', 'Read docs/design.md',
    ])
  })
})
