/**
 * The seam a custom canvas node writes one of its own parameters back through.
 *
 * A plugin node component is handed `data.params` and can read them, but until now it
 * could only *show* them: the config panel owns editing, and it does that by emitting an
 * update at `PipelineEditorView`, which a component mounted by Vue Flow has no channel to.
 * Mutating `data.params` in place looks like it works — it is the instance's own object —
 * and then the edit is never marked dirty and the Save button never lights up.
 *
 * So the editor registers a writer here while it is mounted, and the runtime hands plugins
 * a function that goes through it. An eyedropper is the case that forced it: "that colour,
 * the one under my cursor on this node's own preview" is a value only the node can produce
 * and only the document can keep.
 *
 * Registration is module-scoped rather than provide/inject because a plugin's factory
 * receives a runtime object, not a component instance — there is nothing to inject into.
 * Each renderer has its own immutable project session (§5.3.2).
 */

import { ProjectSession, rendererSession } from '@/session/project-session'

type ParamWriter = (nodeId: string, patch: Record<string, unknown>) => void

/** null is a non-editor lifetime, not editor generation zero (spec §5.3.2). */
export type NodeParamBinding = { readonly generation: number | null; readonly write: ParamWriter }

type WriterRegistration = { write: ParamWriter }

const writers = new WeakMap<ProjectSession, WriterRegistration>()
const generations = new WeakMap<ProjectSession, number>()

/** Editor identity is distinct from the renderer's project lifetime (spec §5.3.2). */
export function nodeParamWriterGeneration(session: ProjectSession = rendererSession): number {
  return generations.get(session) ?? 0
}

function advanceGeneration(session: ProjectSession): void {
  generations.set(session, nodeParamWriterGeneration(session) + 1)
}

/** Called by the pipeline editor; the return value withdraws the writer on unmount. */
export function provideNodeParamWriter(fn: ParamWriter, session: ProjectSession = rendererSession): () => void {
  session.capture()()
  // Registration identity, not function identity: an editor may register the same callback again.
  const registration = { write: fn }
  writers.set(session, registration)
  advanceGeneration(session)
  return () => {
    if (writers.get(session) !== registration) return
    writers.delete(session)
    advanceGeneration(session)
  }
}

/**
 * Capture before a lazy import: its eventual component must not acquire a replacement
 * editor's writer, even when that editor contains an identical node id (spec §5.3.2).
 */
export function captureNodeParamWriter(session: ProjectSession = rendererSession): ParamWriter {
  const assertCurrent = session.capture()
  const registration = writers.get(session)
  return (nodeId, patch) => {
    assertCurrent()
    if (!registration || writers.get(session) !== registration) {
      console.warn(`setNodeParams(${nodeId}) ignored: pipeline editor is no longer mounted`)
      return
    }
    registration.write(nodeId, patch)
  }
}

/** Capture once before import, then pass the SAME capability to nested previewers.
 * Registry-created pages have no editor owner even when an editor happens to be open.
 */
export function captureNodeParamBinding(session: ProjectSession = rendererSession, editor = true): NodeParamBinding {
  if (editor && writers.has(session)) {
    return { generation: nodeParamWriterGeneration(session), write: captureNodeParamWriter(session) }
  }
  const assertCurrent = session.capture()
  return { generation: null, write: (nodeId) => {
    assertCurrent()
    console.warn(`setNodeParams(${nodeId}) ignored: runtime has no pipeline editor binding`)
  } }
}

/**
 * Merge `patch` into a node's params, exactly as the config panel would.
 *
 * A no-op with a warning when no editor is mounted: a plugin calling this from a settings
 * page has made a mistake, and a thrown error inside a plugin's event handler is a worse
 * way to find out than a line in the console.
 */
export function setNodeParams(nodeId: string, patch: Record<string, unknown>, session: ProjectSession = rendererSession): void {
  session.capture()()
  const writer = writers.get(session)
  if (!writer) {
    console.warn(`setNodeParams(${nodeId}) ignored: no pipeline editor is mounted`)
    return
  }
  writer.write(nodeId, patch)
}
