/**
 * The link graph's layout (block-editor spec §5.1).
 *
 * A picture is read by shape, so what is pinned here is the shape's contract rather than
 * its pixels: the same corpus settles the same way twice, the focus is dead centre, nothing
 * escapes the padded box, and the four relations say what they say. A layout that moved on
 * every reopen would be untestable and unreadable for the same reason.
 */
import { describe, expect, it } from 'vitest'

import type { DocGraph } from '@/api/types'
import { BOX, PAD, layoutDocGraph, neighbourhood, seedPosition } from '@/lib/doc-graph'

function node(path: string, inDegree = 0, outDegree = 0) {
  return { path, title: path.replace(/\.md$/, ''), in_degree: inDegree, out_degree: outDegree }
}

/**
 * `a.md` is the focus: `b` links to it, it links to `c`, it and `d` link to each other,
 * `c` links on to `e` (two hops away), and `lone` is in the corpus with no links at all.
 */
const corpus: DocGraph = {
  nodes: [node('a.md', 2, 2), node('b.md', 0, 1), node('c.md', 1, 1), node('d.md', 1, 1), node('e.md', 1, 0), node('lone.md')],
  edges: [
    { source: 'b.md', target: 'a.md' },
    { source: 'a.md', target: 'c.md' },
    { source: 'a.md', target: 'd.md' },
    { source: 'd.md', target: 'a.md' },
    { source: 'c.md', target: 'e.md' },
  ],
}

describe('neighbourhood', () => {
  it('reaches one hop in both directions at once', () => {
    // Undirected: a document the reader linked to and one that linked to them are equally
    // "around here", and asking which end of the arrow they stand on helps nobody.
    expect([...neighbourhood(corpus, 'a.md', 1)].sort()).toEqual(['a.md', 'b.md', 'c.md', 'd.md'])
  })

  it('adds their neighbours at two hops', () => {
    expect([...neighbourhood(corpus, 'a.md', 2)].sort()).toEqual([
      'a.md',
      'b.md',
      'c.md',
      'd.md',
      'e.md',
    ])
  })

  it('is every document at Infinity, unlinked ones included', () => {
    // The loose dots are half of what the whole-project picture says — dropping them would
    // misreport how much of the corpus is actually connected.
    expect(neighbourhood(corpus, 'a.md', Infinity).size).toBe(6)
    expect(neighbourhood(corpus, 'a.md', Infinity).has('lone.md')).toBe(true)
  })
})

describe('layoutDocGraph', () => {
  it('settles the same corpus to the same coordinates every time', () => {
    // Reopening the panel must not rearrange the picture: the reader's memory of where the
    // clusters are is most of what makes a small graph legible.
    expect(layoutDocGraph(corpus, 'a.md', 2)).toEqual(layoutDocGraph(corpus, 'a.md', 2))
  })

  it('pins the focus dead centre', () => {
    const placed = layoutDocGraph(corpus, 'a.md', Infinity)
    const focus = placed.nodes.find((entry) => entry.path === 'a.md')
    expect(focus).toMatchObject({ x: BOX / 2, y: BOX / 2, relation: 'focus', hops: 0 })
  })

  it('keeps every node inside the padded box', () => {
    for (const hops of [1, 2, Infinity] as const) {
      for (const entry of layoutDocGraph(corpus, 'a.md', hops).nodes) {
        expect(entry.x).toBeGreaterThanOrEqual(PAD - 1e-9)
        expect(entry.x).toBeLessThanOrEqual(BOX - PAD + 1e-9)
        expect(entry.y).toBeGreaterThanOrEqual(PAD - 1e-9)
        expect(entry.y).toBeLessThanOrEqual(BOX - PAD + 1e-9)
      }
    }
  })

  it('says which direction each document sits in, and counts a reciprocal one twice', () => {
    const placed = layoutDocGraph(corpus, 'a.md', 2)
    const relations = Object.fromEntries(placed.nodes.map((entry) => [entry.path, entry.relation]))
    expect(relations).toEqual({
      'a.md': 'focus',
      'b.md': 'inbound',
      'c.md': 'outbound',
      'd.md': 'both',
      'e.md': 'other',
    })
    // `d` links here *and* is linked from here, so it is in both totals — that is what the
    // aria summary claims, and the claim has to be true.
    expect(placed.counts).toEqual({ inbound: 2, outbound: 2, other: 1 })
    expect(placed.total).toBe(6)
  })

  it('draws a reciprocal pair as one line, keyed unordered', () => {
    // Two strokes at opacity 0.45 composite to 0.70, and that pair of documents would read
    // as emphasised for no reason at all.
    const keys = layoutDocGraph(corpus, 'a.md', 1).edges.map((edge) => edge.key)
    expect(keys).toEqual(['a.md|b.md', 'a.md|c.md', 'a.md|d.md'])
  })

  it('marks an edge incident when either end is the focus', () => {
    const placed = layoutDocGraph(corpus, 'a.md', 2)
    const incident = Object.fromEntries(placed.edges.map((edge) => [edge.key, edge.incident]))
    expect(incident).toEqual({
      'a.md|b.md': true,
      'a.md|c.md': true,
      'a.md|d.md': true,
      'c.md|e.md': false,
    })
  })

  it('gives a document nobody links to one node and nothing else', () => {
    // The component's "nothing links to or from this document yet" case — one dot in a
    // square says less than the sentence does.
    const placed = layoutDocGraph(corpus, 'lone.md', 1)
    expect(placed.nodes.map((entry) => entry.path)).toEqual(['lone.md'])
    expect(placed.edges).toEqual([])
    expect(placed.counts).toEqual({ inbound: 0, outbound: 0, other: 0 })
  })

  it('survives a focus the payload has not indexed yet', () => {
    // A document created a beat ago. The refresh corrects it; throwing would take the whole
    // editor down with it.
    const placed = layoutDocGraph(corpus, 'brand-new.md', 1)
    expect(placed.nodes).toEqual([])
    expect(placed.edges).toEqual([])
    expect(placed.total).toBe(6)
  })
})

describe('seedPosition', () => {
  it('is a function of the path, so a growing corpus leaves everyone else where they were', () => {
    // Seeding off the array index would have made adding `architecture/combat.md` shift every
    // later index and redraw the whole picture; the contract sorts nodes by path.
    const before = corpus.nodes.map((entry) => [entry.path, seedPosition(entry.path)] as const)
    const grown = [...corpus.nodes, node('architecture/combat.md')]
    for (const [path, seed] of before) {
      expect(grown.some((entry) => entry.path === path)).toBe(true)
      expect(seedPosition(path)).toEqual(seed)
    }
  })

  it('spreads paths over the box rather than piling them at the centre', () => {
    const seeds = ['a.md', 'b.md', 'c.md', 'd.md', 'e.md'].map(seedPosition)
    for (const seed of seeds) {
      expect(seed.x).toBeGreaterThanOrEqual(0)
      expect(seed.x).toBeLessThanOrEqual(BOX)
      expect(seed.y).toBeGreaterThanOrEqual(0)
      expect(seed.y).toBeLessThanOrEqual(BOX)
    }
    expect(new Set(seeds.map((seed) => `${seed.x},${seed.y}`)).size).toBe(5)
  })
})
