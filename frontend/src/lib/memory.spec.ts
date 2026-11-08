import { describe, expect, it } from 'vitest'

import { filterMemory, inMemoryFolder, indexFill, isMemoryName, toMemoryName } from './memory'

describe('memory names', () => {
  it('accepts what the backend accepts', () => {
    expect(isMemoryName('build-uses-ue-5-4')).toBe(true)
    expect(isMemoryName('a')).toBe(true)
    expect(isMemoryName('x'.repeat(64))).toBe(true)
    for (const bad of ['', '-lead', 'Build', 'with space', 'a/b', 'dot.md', 'x'.repeat(65)]) {
      expect(isMemoryName(bad)).toBe(false)
    }
  })

  it('turns a typed name into an id', () => {
    expect(toMemoryName('Build uses UE 5.4')).toBe('build-uses-ue-5-4')
    expect(toMemoryName('  --Art   style!! ')).toBe('art-style')
    expect(toMemoryName('Сборка UE')).toBe('ue')
    expect(toMemoryName('a'.repeat(63) + ' b')).toBe('a'.repeat(63))
    expect(isMemoryName(toMemoryName('Levels live in Content/Maps'))).toBe(true)
  })
})

describe('filterMemory', () => {
  const entries = [
    { name: 'save-format', description: 'Saves are JSON' },
    { name: 'audio', description: 'Mix at -14 LUFS' },
  ]

  it('keeps entries holding every word', () => {
    expect(filterMemory(entries, 'json SAVE')).toEqual([entries[0]])
    expect(filterMemory(entries, 'lufs')).toEqual([entries[1]])
    expect(filterMemory(entries, 'json lufs')).toEqual([])
    expect(filterMemory(entries, '  ')).toBe(entries)
  })
})

describe('inMemoryFolder', () => {
  it('matches the folder and what is under it, not its neighbours', () => {
    expect(inMemoryFolder('voxarium_studio/memory', 'voxarium_studio/memory/')).toBe(true)
    expect(inMemoryFolder('voxarium_studio/memory/a.md', 'voxarium_studio/memory')).toBe(true)
    expect(inMemoryFolder('voxarium_studio/memory-old/a.md', 'voxarium_studio/memory')).toBe(false)
  })
})

describe('indexFill', () => {
  it('is a clamped share of the cap', () => {
    expect(indexFill(2000, 8000)).toBe(0.25)
    expect(indexFill(9000, 8000)).toBe(1)
    expect(indexFill(10, 0)).toBe(1)
  })
})
