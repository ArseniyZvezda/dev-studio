/**
 * Live run state for the pipeline canvas (spec §8.2).
 *
 * Statuses and log lines arrive over the WebSocket while a run is in flight; the REST
 * endpoints are only used to seed state (opening a finished run from history) and as a
 * safety net if a status event was missed. Logs are kept per node and capped, because a
 * chatty ML script can emit tens of thousands of lines and the DOM is the bottleneck,
 * not the socket.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { NodeProgress, NodeStatus, Run, RunDetail } from '@/api/types'
import { onEvent } from '@/ws/connection'

const MAX_LOG_LINES = 2000

export interface LogLine {
  stream: 'stdout' | 'stderr'
  line: string
}

export const useRunsStore = defineStore('runs', () => {
  const currentRun = ref<Run | null>(null)
  const nodeStatus = ref<Record<string, NodeStatus>>({})
  const nodeError = ref<Record<string, string | null>>({})
  const fromCache = ref<Record<string, boolean>>({})
  /**
   * What each node actually produced, keyed by node id.
   *
   * Kept because a node's *result* can be something to look at rather than something to
   * read: `preview3d.view` renders the file it was handed, and the only place the resolved
   * path exists is here. `node.status_changed` already carries the outputs, so a finished
   * node paints without waiting for the REST re-fetch that ends the run.
   */
  const nodeOutputs = ref<Record<string, Record<string, unknown>>>({})
  /**
   * How far along each still-running node says it is.
   *
   * Live-only, and dropped the moment a node reaches a terminal status: a percentage
   * left on a finished node claims a run is still going. Nothing re-sends it, so a
   * reopened run starts empty and fills again from the next frame.
   */
  const nodeProgress = ref<Record<string, NodeProgress>>({})
  const logs = ref<Record<string, LogLine[]>>({})
  /**
   * Nodes holding the run open, waiting for Continue.
   *
   * Live state on the backend too — the runner derives it from the jobs actually waiting
   * rather than from a stored column — so it arrives both ways: on `run.paused`/`run.resumed`
   * while the page is watching, and on `paused_nodes` in the REST payload for a page that
   * opened mid-pause.
   */
  const pausedNodes = ref<string[]>([])
  const history = ref<Run[]>([])
  const starting = ref(false)
  const error = ref<string | null>(null)

  /**
   * The run whose events we accept, tracked separately from `currentRun` because it is
   * known the instant the run is submitted, whereas `currentRun` only arrives after a
   * REST round-trip. Matching on `currentRun` dropped every status and log line emitted
   * in between, and a fast first node could finish inside that window.
   */
  const trackedRunId = ref<string | null>(null)
  /** Which pipeline the open editor is showing, so runs started elsewhere for a
   *  different pipeline do not take over this canvas. */
  const watchedPipelineId = ref<string | null>(null)

  let subscribed = false

  const isRunning = computed(
    () =>
      currentRun.value?.status === 'running' ||
      currentRun.value?.status === 'pending' ||
      // A paused run is still in the job engine holding its place in the graph. Excluding it
      // would put a Run button beside a run that has not ended, and take Cancel away from the
      // one state in which someone is most likely to want it.
      currentRun.value?.status === 'paused',
  )

  const isPaused = computed(() => currentRun.value?.status === 'paused')

  function watchPipeline(pipelineId: string): void {
    watchedPipelineId.value = pipelineId
  }

  function reset(): void {
    currentRun.value = null
    trackedRunId.value = null
    nodeStatus.value = {}
    nodeError.value = {}
    fromCache.value = {}
    nodeOutputs.value = {}
    nodeProgress.value = {}
    pausedNodes.value = []
    logs.value = {}
  }

  function applyDetail(detail: RunDetail): void {
    currentRun.value = detail.run
    const statuses: Record<string, NodeStatus> = {}
    const errors: Record<string, string | null> = {}
    const cached: Record<string, boolean> = {}
    const outputs: Record<string, Record<string, unknown>> = {}
    for (const node of detail.nodes) {
      statuses[node.node_id] = node.status
      errors[node.node_id] = node.error
      cached[node.node_id] = node.from_cache
      outputs[node.node_id] = node.outputs
    }
    nodeStatus.value = statuses
    nodeError.value = errors
    fromCache.value = cached
    nodeOutputs.value = outputs
    // Nothing in the REST payload describes a node mid-flight, and a node this detail
    // reports as finished has no progress to keep.
    nodeProgress.value = Object.fromEntries(
      Object.entries(nodeProgress.value).filter(([id]) => statuses[id] === 'running'),
    )
    pausedNodes.value = detail.paused_nodes ?? []
  }

  async function openRun(runId: string): Promise<void> {
    // Claim the run before awaiting, so events arriving mid-fetch are kept.
    if (trackedRunId.value !== runId) {
      trackedRunId.value = runId
      logs.value = {}
    }
    applyDetail(await api.run(runId))
  }

  async function loadHistory(pipelineId: string): Promise<void> {
    history.value = await api.runs({ pipeline_id: pipelineId, limit: 30 })
  }

  async function start(
    pipelineId: string,
    options: { startNodeId?: string | null; useCache?: boolean } = {},
  ): Promise<void> {
    starting.value = true
    error.value = null
    reset()
    try {
      const response = await api.runPipeline(pipelineId, {
        start_node_id: options.startNodeId ?? null,
        use_cache: options.useCache ?? true,
      })
      // Seed from REST so the run exists in the UI even before the first WS frame.
      // `openRun` claims the id synchronously, so nothing emitted during the fetch is
      // discarded.
      await openRun(response.run_id)
      await loadHistory(pipelineId)
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
    } finally {
      starting.value = false
    }
  }

  async function cancel(): Promise<void> {
    if (!currentRun.value) return
    await api.cancelRun(currentRun.value.id)
  }

  /** Let a paused run carry on. The `run.resumed` event is what repaints the canvas. */
  async function resume(): Promise<void> {
    if (!currentRun.value) return
    error.value = null
    try {
      await api.resumeRun(currentRun.value.id)
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
    }
  }

  async function fetchNodeLog(nodeId: string): Promise<void> {
    if (!currentRun.value) return
    const response = await api.nodeLog(currentRun.value.id, nodeId)
    if (!response.exists) return
    logs.value = {
      ...logs.value,
      [nodeId]: response.text
        .split('\n')
        .filter(Boolean)
        .slice(-MAX_LOG_LINES)
        .map((line) => ({ stream: 'stdout' as const, line })),
    }
  }

  function appendLog(nodeId: string, entry: LogLine): void {
    const existing = logs.value[nodeId] ?? []
    const next = existing.length >= MAX_LOG_LINES ? existing.slice(-MAX_LOG_LINES + 1) : existing.slice()
    next.push(entry)
    logs.value = { ...logs.value, [nodeId]: next }
  }

  function subscribe(): void {
    if (subscribed) return
    subscribed = true

    onEvent('run.started', (payload) => {
      if (trackedRunId.value === payload.run_id) return
      // A run can also be started from elsewhere (the CLI, another tab). Adopt it only
      // when it belongs to the pipeline currently on screen.
      if (payload.pipeline_id !== watchedPipelineId.value) return
      void openRun(payload.run_id)
    })

    const finish = (payload: { run_id: string }) => {
      if (trackedRunId.value !== payload.run_id) return
      void api.run(payload.run_id).then(applyDetail)
    }
    onEvent('run.completed', finish)
    onEvent('run.failed', finish)
    onEvent('run.cancelled', finish)

    onEvent('node.status_changed', (payload) => {
      if (trackedRunId.value !== payload.run_id) return
      nodeStatus.value = { ...nodeStatus.value, [payload.node_id]: payload.status }
      nodeError.value = { ...nodeError.value, [payload.node_id]: payload.error }
      fromCache.value = { ...fromCache.value, [payload.node_id]: payload.from_cache }
      // Only a finished node carries outputs; every earlier transition sends null, and
      // adopting that would blank what the node produced on its previous run.
      if (payload.outputs) {
        nodeOutputs.value = { ...nodeOutputs.value, [payload.node_id]: payload.outputs }
      }
      // A node that is no longer running has nothing left to be part-way through, and a
      // stale bar under a finished node reads as a run that never ended.
      if (payload.status !== 'running' && payload.node_id in nodeProgress.value) {
        const { [payload.node_id]: _dropped, ...rest } = nodeProgress.value
        nodeProgress.value = rest
      }
    })

    onEvent('run.paused', (payload) => {
      if (trackedRunId.value !== payload.run_id) return
      pausedNodes.value = payload.node_ids
      // The run row says `paused` from this moment, and the toolbar reads `currentRun` for
      // which of Run / Cancel / Continue to offer. Patched rather than re-fetched: the REST
      // round trip would leave the run looking still-running for as long as it takes.
      if (currentRun.value) currentRun.value = { ...currentRun.value, status: 'paused' }
    })

    onEvent('run.resumed', (payload) => {
      if (trackedRunId.value !== payload.run_id) return
      pausedNodes.value = []
      if (currentRun.value) currentRun.value = { ...currentRun.value, status: 'running' }
    })

    onEvent('node.progress', (payload) => {
      if (trackedRunId.value !== payload.run_id) return
      nodeProgress.value = { ...nodeProgress.value, [payload.node_id]: payload.progress }
    })

    onEvent('node.log', (payload) => {
      if (trackedRunId.value !== payload.run_id) return
      appendLog(payload.node_id, { stream: payload.stream, line: payload.line })
    })
  }

  return {
    currentRun,
    nodeStatus,
    nodeError,
    fromCache,
    nodeOutputs,
    nodeProgress,
    pausedNodes,
    logs,
    history,
    starting,
    error,
    isRunning,
    isPaused,
    trackedRunId,
    watchPipeline,
    reset,
    openRun,
    loadHistory,
    start,
    cancel,
    resume,
    fetchNodeLog,
    subscribe,
  }
})
