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
 * One editor is mounted at a time, so one writer is enough.
 */

type ParamWriter = (nodeId: string, patch: Record<string, unknown>) => void

let writer: ParamWriter | null = null

/** Called by the pipeline editor; the return value withdraws the writer on unmount. */
export function provideNodeParamWriter(fn: ParamWriter): () => void {
  writer = fn
  return () => {
    if (writer === fn) writer = null
  }
}

/**
 * Merge `patch` into a node's params, exactly as the config panel would.
 *
 * A no-op with a warning when no editor is mounted: a plugin calling this from a settings
 * page has made a mistake, and a thrown error inside a plugin's event handler is a worse
 * way to find out than a line in the console.
 */
export function setNodeParams(nodeId: string, patch: Record<string, unknown>): void {
  if (!writer) {
    console.warn(`setNodeParams(${nodeId}) ignored: no pipeline editor is mounted`)
    return
  }
  writer(nodeId, patch)
}
