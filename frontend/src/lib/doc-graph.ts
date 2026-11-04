/**
 * Where the documents sit when the corpus is drawn as a graph (block-editor spec §5.1).
 *
 * Pure arithmetic, no Vue and no DOM, for the same reason `lib/org-layout.ts` and
 * `lib/cost-chart.ts` are: the picture is read by *shape*, so the rules that decide the
 * shape are the part worth pinning by data rather than by opening a browser. It is not
 * called `graph.ts` — that name is taken by the Vue Flow translation layer, which is a
 * different graph in a different sense.
 *
 * The layout is a seeded Fruchterman–Reingold settle that runs once, synchronously, when a
 * payload lands. Nothing here animates: the panel shares a main thread with ProseMirror, and
 * §9 of the block-editor spec asks for ≤16 ms input frames on a 3000-line document — a
 * two-second physics animation competing with typing is the one thing that section is about.
 */

import type { DocGraph } from '@/api/types'

/** The unit box the layout runs in. The SVG's viewBox is exactly this, so nothing measures pixels. */
export const BOX = 100

/** Kept clear of the rim so a node on the edge is not half-clipped and its label has room. */
export const PAD = 8

export type Relation = 'focus' | 'inbound' | 'outbound' | 'both' | 'other'

/**
 * How far the picture reaches: 1 hop, 2 hops, or the whole project.
 *
 * `Infinity` has no literal type, so this widens to `number` — the three values are the
 * contract, and `neighbourhood` treats anything else as a plain hop count.
 */
export type Hops = 1 | 2 | typeof Infinity

export interface PlacedNode {
  path: string
  title: string
  relation: Relation
  /** Hops from the focus along *undirected* edges; 0 is the focus itself. */
  hops: number
  in_degree: number
  out_degree: number
  x: number
  y: number
}

export interface PlacedEdge {
  /** Unordered key `a|b`, so a reciprocal pair is one line rather than two on top of each other. */
  key: string
  /** True when either end is the focus — drawn heavier. */
  incident: boolean
  source: string
  target: string
  x1: number
  y1: number
  x2: number
  y2: number
}

export interface PlacedGraph {
  nodes: PlacedNode[]
  edges: PlacedEdge[]
  /**
   * What the summary line and the aria label count. A reciprocal document is counted in
   * both directions, because it genuinely links here *and* is linked from here.
   */
  counts: { inbound: number; outbound: number; other: number }
  /** Documents in the whole corpus, for the "showing N of M" line. */
  total: number
}

/** FNV-1a, 32-bit. Cheap, no dependency, and well spread over short ASCII paths. */
function hashPath(path: string): number {
  let hash = 0x811c9dc5
  for (let i = 0; i < path.length; i += 1) {
    hash ^= path.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193)
  }
  return hash >>> 0
}

/** mulberry32 — one line, deterministic, and good enough for two uniforms per node. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/**
 * Where a document's settle starts, seeded from its **path**.
 *
 * `Math.random()` is fine in app code and wrong here, twice over. A graph is read by shape,
 * so a reader who has learned "the cluster on the left is combat" loses that the moment the
 * panel is reopened and the picture rearranges; and a random layout cannot be tested at all.
 *
 * Seeding per path rather than per array index is the load-bearing half. The contract sorts
 * nodes by path, so a constant seed plus an index is stable only while the corpus is — adding
 * `architecture/combat.md` would shift every later index and redraw the whole picture. Off the
 * path, a new document appears without moving anyone else's starting point, and the settled
 * picture drifts only as far as the new node's forces actually push it.
 */
export function seedPosition(path: string): { x: number; y: number } {
  const rng = mulberry32(hashPath(path))
  // sqrt() so the seeds are uniform over the disc's *area* rather than piled at the centre,
  // which is what a bare uniform radius gives and what makes the first iterations explode.
  const r = 0.5 * BOX * Math.sqrt(rng())
  const a = 2 * Math.PI * rng()
  return { x: BOX / 2 + r * Math.cos(a), y: BOX / 2 + r * Math.sin(a) }
}

/** Hops from the focus along undirected edges, for everything within reach. */
function distances(graph: DocGraph, focus: string, hops: Hops): Map<string, number> {
  const reached = new Map<string, number>()
  const known = new Set(graph.nodes.map((node) => node.path))
  if (!known.has(focus)) return reached

  // Undirected on purpose: "what depends on what around here" does not care which end of
  // the link the reader happens to be standing on.
  const neighbours = new Map<string, Set<string>>()
  const link = (a: string, b: string) => {
    if (!known.has(a) || !known.has(b) || a === b) return
    if (!neighbours.has(a)) neighbours.set(a, new Set())
    neighbours.get(a)!.add(b)
  }
  for (const edge of graph.edges) {
    link(edge.source, edge.target)
    link(edge.target, edge.source)
  }

  reached.set(focus, 0)
  let frontier = [focus]
  for (let depth = 1; depth <= hops && frontier.length; depth += 1) {
    const next: string[] = []
    for (const path of frontier) {
      for (const neighbour of neighbours.get(path) ?? []) {
        if (reached.has(neighbour)) continue
        reached.set(neighbour, depth)
        next.push(neighbour)
      }
    }
    frontier = next
  }

  // "Whole project" means every document, including the ones nothing links to — those are
  // the reference picture's outer ring of loose dots, and dropping them would misreport how
  // much of the corpus is actually connected.
  if (hops === Infinity) {
    for (const node of graph.nodes) if (!reached.has(node.path)) reached.set(node.path, Infinity)
  }
  return reached
}

/** The documents the picture shows: the focus, and everything within `hops` of it. */
export function neighbourhood(graph: DocGraph, focus: string, hops: Hops): Set<string> {
  return new Set(distances(graph, focus, hops).keys())
}

const GRAVITY = 0.05

export function layoutDocGraph(graph: DocGraph, focus: string, hops: Hops): PlacedGraph {
  const total = graph.nodes.length
  const empty: PlacedGraph = {
    nodes: [],
    edges: [],
    counts: { inbound: 0, outbound: 0, other: 0 },
    total,
  }
  // A document created a beat ago is not in the payload yet. The picture says so in a
  // sentence rather than throwing; `invalidateGraph()` corrects it on the next refresh.
  if (!graph.nodes.some((node) => node.path === focus)) return empty

  const reached = distances(graph, focus, hops)
  const inbound = new Set<string>()
  const outbound = new Set<string>()
  for (const edge of graph.edges) {
    if (edge.target === focus) inbound.add(edge.source)
    if (edge.source === focus) outbound.add(edge.target)
  }

  const nodes: PlacedNode[] = graph.nodes
    .filter((node) => reached.has(node.path))
    .map((node) => {
      const isIn = inbound.has(node.path)
      const isOut = outbound.has(node.path)
      const relation: Relation =
        node.path === focus
          ? 'focus'
          : isIn && isOut
            ? 'both'
            : isIn
              ? 'inbound'
              : isOut
                ? 'outbound'
                : 'other'
      const seed = node.path === focus ? { x: BOX / 2, y: BOX / 2 } : seedPosition(node.path)
      return {
        path: node.path,
        title: node.title,
        relation,
        hops: reached.get(node.path)!,
        in_degree: node.in_degree,
        out_degree: node.out_degree,
        x: seed.x,
        y: seed.y,
      }
    })

  const index = new Map(nodes.map((node, at) => [node.path, at]))

  // `A→B` and `B→A` are two contract edges over the same two points. Drawn as two strokes at
  // opacity 0.45 they composite to 0.70, and one pair of documents reads as emphasised for no
  // reason — so the drawn edge is keyed unordered and de-duplicated on that key.
  const seen = new Set<string>()
  const pairs: { key: string; a: number; b: number; source: string; target: string }[] = []
  for (const edge of graph.edges) {
    const a = index.get(edge.source)
    const b = index.get(edge.target)
    if (a === undefined || b === undefined || a === b) continue
    const key =
      edge.source < edge.target ? `${edge.source}|${edge.target}` : `${edge.target}|${edge.source}`
    if (seen.has(key)) continue
    seen.add(key)
    pairs.push({ key, a, b, source: edge.source, target: edge.target })
  }

  settle(nodes, pairs, index.get(focus)!)

  return {
    nodes,
    edges: pairs.map((pair) => ({
      key: pair.key,
      incident: pair.source === focus || pair.target === focus,
      source: pair.source,
      target: pair.target,
      x1: nodes[pair.a].x,
      y1: nodes[pair.a].y,
      x2: nodes[pair.b].x,
      y2: nodes[pair.b].y,
    })),
    counts: {
      inbound: nodes.filter((node) => node.relation === 'inbound' || node.relation === 'both')
        .length,
      outbound: nodes.filter((node) => node.relation === 'outbound' || node.relation === 'both')
        .length,
      other: nodes.filter((node) => node.relation === 'other').length,
    },
    total,
  }
}

/**
 * Classic Fruchterman–Reingold with a centring term, then a fit pass about the focus.
 *
 * The constants are not precious, because the fit pass normalises whatever they produce: a
 * uniform scale about the focus makes the picture fill the square however the forces happen
 * to land, and keeps the focus dead centre. Gravity is mild on purpose — the box clamp does
 * containment, and gravity exists only so that disconnected components, which a real corpus
 * has plenty of, gather into a readable outer ring instead of being flung into the corners.
 *
 * Cost: `n(n-1)/2` pair steps plus one per edge, per iteration. A 12-node neighbourhood is
 * ~26k steps (<1 ms); a sample 49-document "Whole project" is ~260k (~5 ms). Once per corpus
 * change, never on a keystroke.
 */
function settle(
  nodes: PlacedNode[],
  pairs: { a: number; b: number }[],
  focusAt: number,
): void {
  const n = nodes.length
  const centre = BOX / 2
  if (n > 1) {
    const k = 0.9 * Math.sqrt((BOX * BOX) / n)
    const iterations = Math.min(300, Math.max(60, Math.round(9000 / n)))
    const t0 = BOX / 6
    const dx = new Float64Array(n)
    const dy = new Float64Array(n)

    for (let step = 0; step < iterations; step += 1) {
      dx.fill(0)
      dy.fill(0)

      for (let u = 0; u < n; u += 1) {
        for (let v = u + 1; v < n; v += 1) {
          const ax = nodes[v].x - nodes[u].x
          const ay = nodes[v].y - nodes[u].y
          const length = Math.max(Math.hypot(ax, ay), 0.01)
          const force = (k * k) / length
          dx[u] -= (ax / length) * force
          dy[u] -= (ay / length) * force
          dx[v] += (ax / length) * force
          dy[v] += (ay / length) * force
        }
      }

      for (const pair of pairs) {
        const ax = nodes[pair.b].x - nodes[pair.a].x
        const ay = nodes[pair.b].y - nodes[pair.a].y
        const length = Math.max(Math.hypot(ax, ay), 0.01)
        const force = (length * length) / k
        dx[pair.a] += (ax / length) * force
        dy[pair.a] += (ay / length) * force
        dx[pair.b] -= (ax / length) * force
        dy[pair.b] -= (ay / length) * force
      }

      const temperature = t0 * (1 - step / iterations)
      for (let u = 0; u < n; u += 1) {
        // The focus never moves: it is the "you are here" marker, and a centre that drifts
        // makes the picture unreadable between one document and the next.
        if (u === focusAt) continue
        const node = nodes[u]
        const gx = dx[u] + (centre - node.x) * GRAVITY
        const gy = dy[u] + (centre - node.y) * GRAVITY
        const length = Math.max(Math.hypot(gx, gy), 0.01)
        const move = Math.min(length, temperature)
        node.x = Math.min(BOX, Math.max(0, node.x + (gx / length) * move))
        node.y = Math.min(BOX, Math.max(0, node.y + (gy / length) * move))
      }
    }
  }

  let spread = 0
  for (const node of nodes) {
    spread = Math.max(spread, Math.abs(node.x - centre), Math.abs(node.y - centre))
  }
  // Degenerate (a single node, or every node on top of the focus): leave it at the centre.
  const scale = spread > 0.001 ? (BOX / 2 - PAD) / spread : 1
  for (const node of nodes) {
    node.x = centre + (node.x - centre) * scale
    node.y = centre + (node.y - centre) * scale
  }
}
