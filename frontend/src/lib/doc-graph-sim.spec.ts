/**
 * What each of the graph page's five knobs does to the settled shape, pinned by data rather than
 * by looking at the page — and that the same corpus settles into the same shape every time.
 */
import { describe, expect, it } from 'vitest'

import type { DocGraph } from '@/api/types'
import {
  DEFAULT_FORCES,
  FORCE_LIMITS,
  MAX_GROWTH,
  bounds,
  growth,
  radiusOf,
  clampForces,
  createSimulation,
  folderOf,
  isRunning,
  parsePlacement,
  placementOf,
  reheat,
  step,
  type GraphForces,
  type Simulation,
} from '@/lib/doc-graph-sim'

function graph(paths: string[], edges: [string, string][] = []): DocGraph {
  return {
    nodes: paths.map((path) => ({ path, title: path, in_degree: 0, out_degree: 0 })),
    edges: edges.map(([source, target]) => ({ source, target })),
  }
}

function settle(sim: Simulation, limit = 2000): Simulation {
  for (let i = 0; i < limit && step(sim); i += 1);
  return sim
}

function run(corpus: DocGraph, forces: Partial<GraphForces> = {}): Simulation {
  return settle(createSimulation(corpus, { ...DEFAULT_FORCES, ...forces }))
}

function distance(sim: Simulation, a: string, b: string): number {
  const p = sim.nodes.find((node) => node.path === a)!
  const q = sim.nodes.find((node) => node.path === b)!
  return Math.hypot(p.x - q.x, p.y - q.y)
}

const PAIR = graph(['a.md', 'b.md', 'c.md', 'd.md'], [['a.md', 'b.md']])

describe('clampForces', () => {
  it('falls back key by key and holds every value inside its range', () => {
    expect(clampForces(null)).toEqual(DEFAULT_FORCES)
    expect(clampForces({ nodeSize: 1000, linkDistance: 'far', linkStrength: -3 })).toEqual({
      ...DEFAULT_FORCES,
      nodeSize: FORCE_LIMITS.nodeSize.max,
      linkStrength: FORCE_LIMITS.linkStrength.min,
    })
  })
})

describe('folderOf', () => {
  it('is the path up to the last slash, and empty at the top', () => {
    expect(folderOf('features/combat/dash.md')).toBe('features/combat')
    expect(folderOf('readme.md')).toBe('')
  })
})

describe('the simulation', () => {
  it('settles the same corpus into the same shape every time', () => {
    const first = run(PAIR)
    const second = run(PAIR)
    expect(second.nodes.map((node) => [node.x, node.y])).toEqual(
      first.nodes.map((node) => [node.x, node.y]),
    )
  })

  it('cools to a stop, and a reheat wakes it', () => {
    const sim = run(PAIR)
    expect(isRunning(sim)).toBe(false)
    reheat(sim)
    expect(step(sim)).toBe(true)
  })

  it('draws one spring for a reciprocal pair, and none for a self-link', () => {
    const sim = createSimulation(
      graph(['a.md', 'b.md'], [['a.md', 'b.md'], ['b.md', 'a.md'], ['a.md', 'a.md']]),
      DEFAULT_FORCES,
    )
    expect(sim.links).toHaveLength(1)
    expect(sim.neighbours[0]).toEqual(new Set([1]))
  })

  it('holds linked documents nearer their link distance the stronger the link', () => {
    // A spring pulls towards its rest length, from either side: the stronger it is, the nearer
    // the pair settles to the link distance.
    const off = (linkStrength: number) =>
      Math.abs(
        distance(run(PAIR, { linkStrength }), 'a.md', 'b.md') - DEFAULT_FORCES.linkDistance,
      )
    expect(off(2)).toBeLessThan(off(0.1))
  })

  it('rests a link longer the longer its distance', () => {
    const short = distance(run(PAIR, { linkDistance: 30 }), 'a.md', 'b.md')
    const long = distance(run(PAIR, { linkDistance: 250 }), 'a.md', 'b.md')
    expect(long).toBeGreaterThan(short)
  })

  it('gathers a folder together the stronger the folder pull', () => {
    const corpus = graph(['x/a.md', 'x/b.md', 'x/c.md', 'y/d.md', 'y/e.md', 'y/f.md'])
    const spread = (sim: Simulation) =>
      distance(sim, 'x/a.md', 'x/b.md') +
      distance(sim, 'x/b.md', 'x/c.md') +
      distance(sim, 'y/d.md', 'y/e.md')
    expect(spread(run(corpus, { folderStrength: 2 }))).toBeLessThan(
      spread(run(corpus, { folderStrength: 0 })),
    )
  })

  it('pushes documents with no link between them further apart', () => {
    const weak = distance(run(PAIR, { unlinkedRepel: 0 }), 'c.md', 'd.md')
    const strong = distance(run(PAIR, { unlinkedRepel: 3 }), 'c.md', 'd.md')
    expect(strong).toBeGreaterThan(weak)
  })

  it('leaves a linked pair to its own forces whatever the unlinked repulsion', () => {
    const alone = graph(['a.md', 'b.md'], [['a.md', 'b.md']])
    const settled = (unlinkedRepel: number) =>
      run(alone, { unlinkedRepel }).nodes.map((node) => [node.x, node.y])
    expect(settled(3)).toEqual(settled(0))
  })

  it('draws the most linked document at 175% and an unlinked one at the set size', () => {
    const hub = graph(
      ['hub.md', 'a.md', 'b.md', 'c.md', 'd.md', 'lone.md'],
      [['hub.md', 'a.md'], ['hub.md', 'b.md'], ['hub.md', 'c.md'], ['hub.md', 'd.md']],
    )
    const sim = createSimulation(hub, DEFAULT_FORCES)
    const size = (path: string) => radiusOf(sim.nodes.find((node) => node.path === path)!, sim.forces)
    expect(size('hub.md')).toBeCloseTo(DEFAULT_FORCES.nodeSize * MAX_GROWTH)
    expect(size('lone.md')).toBe(DEFAULT_FORCES.nodeSize)
    expect(size('a.md')).toBeGreaterThan(size('lone.md'))
    expect(size('a.md')).toBeLessThan(size('hub.md'))
  })

  it('grows by the square root of the share of links', () => {
    expect(growth(0, 10)).toBe(1)
    expect(growth(10, 10)).toBe(MAX_GROWTH)
    expect(growth(25, 100)).toBeCloseTo(1 + (MAX_GROWTH - 1) * 0.5)
    expect(growth(3, 0)).toBe(1)
  })

  it('keeps bigger nodes further apart', () => {
    const small = run(PAIR, { nodeSize: 3 })
    const large = run(PAIR, { nodeSize: 24 })
    const box = (sim: Simulation) => {
      const b = bounds(sim)!
      return b.maxX - b.minX
    }
    expect(box(large)).toBeGreaterThan(box(small))
  })

  it('holds a dragged node exactly where the pointer put it', () => {
    const sim = createSimulation(PAIR, DEFAULT_FORCES)
    sim.nodes[0].fx = 123
    sim.nodes[0].fy = -45
    settle(sim)
    expect([sim.nodes[0].x, sim.nodes[0].y]).toEqual([123, -45])
  })

  it('starts a known document where the last picture left it', () => {
    const before = placementOf(run(PAIR))
    const after = createSimulation(
      graph([...PAIR.nodes.map((node) => node.path), 'e.md'], [['a.md', 'b.md']]),
      DEFAULT_FORCES,
      before,
    )
    expect([after.nodes[0].x, after.nodes[0].y]).toEqual(before.positions['a.md'])
    // A new document has to find its place, so the picture moves again.
    expect(isRunning(after)).toBe(true)
  })

  it('opens a picture left at rest exactly as it was, and still', () => {
    const before = placementOf(run(PAIR))
    const after = createSimulation(PAIR, DEFAULT_FORCES, JSON.parse(JSON.stringify(before)))
    expect(Object.fromEntries(after.nodes.map((node) => [node.path, [node.x, node.y]]))).toEqual(
      before.positions,
    )
    expect(isRunning(after)).toBe(false)
  })

  it('nudges a picture whose documents stayed but whose links changed', () => {
    const before = placementOf(run(PAIR))
    const relinked = graph(
      PAIR.nodes.map((node) => node.path),
      [['a.md', 'b.md'], ['c.md', 'd.md']],
    )
    expect(isRunning(createSimulation(relinked, DEFAULT_FORCES, before))).toBe(true)
  })
})

describe('parsePlacement', () => {
  it('reads back what placementOf wrote', () => {
    const placement = placementOf(run(PAIR))
    expect(parsePlacement(JSON.parse(JSON.stringify(placement)))).toEqual(placement)
  })

  it('refuses what is not a placement, and drops a bad position on its own', () => {
    expect(parsePlacement(null)).toBeNull()
    expect(parsePlacement({ positions: {} })).toBeNull()
    expect(
      parsePlacement({ positions: { 'a.md': [1, 2], 'b.md': ['x', 2], 'c.md': [1] }, links: '0:0' }),
    ).toEqual({ positions: { 'a.md': [1, 2] }, links: '0:0', alpha: 1 })
  })
})
