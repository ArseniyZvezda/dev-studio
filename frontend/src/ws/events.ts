/**
 * The WebSocket event taxonomy (spec §11.2), typed to match what the backend actually
 * publishes in `voxarium_studio/ws/events.py`.
 */

import type {
  AgentChat,
  AutomationNotification,
  AutomationRun,
  AgentChatMessage,
  LoadedPlugin,
  NodeProgress,
  NodeStatus,
  RunStatus,
  TaskStatus,
  SyncStatus,
  UpdateStatus,
} from '@/api/types'

export interface EventPayloads {
  'system.hello': { project: string; resumed_from: number | null }
  'system.pong': Record<string, never>
  /**
   * This client fell behind and the bus evicted events to avoid stalling the producer,
   * so its stream has a gap. Sent only to the subscriber that lost them, and without a
   * `seq` — refetch over REST rather than trusting what is on screen.
   */
  'system.dropped': { count: number; resync: boolean }

  'run.started': {
    run_id: string
    pipeline_id: string
    pipeline_name: string
    start_node_id: string | null
    node_ids: string[]
  }
  /** The crash path publishes this without `status`/`failed_nodes`, hence the optionals. */
  'run.completed': {
    run_id: string
    pipeline_id: string
    status?: RunStatus
    failed_nodes?: string[]
    error?: string | null
  }
  'run.failed': {
    run_id: string
    pipeline_id: string
    status?: RunStatus
    failed_nodes?: string[]
    error?: string | null
  }
  'run.cancelled': { run_id: string; pipeline_id: string }
  /**
   * A node with `pause_on_complete` finished and the run is waiting to be continued.
   * `node_id` is the one that just stopped; `node_ids` is everything holding the run,
   * since two nodes finishing in the same level both hold it and one Continue frees both.
   */
  'run.paused': { run_id: string; node_id: string; node_ids: string[] }
  'run.resumed': { run_id: string; node_ids: string[] }

  'node.status_changed': {
    run_id: string
    node_id: string
    status: NodeStatus
    from_cache: boolean
    error: string | null
    outputs: Record<string, unknown> | null
  }
  'node.log': {
    run_id: string
    node_id: string
    stream: 'stdout' | 'stderr'
    line: string
  }
  /**
   * A running node saying how far along it is. Advisory and unpersisted — nothing
   * re-sends the last frame, so treat a missed one as "the next will say".
   */
  'node.progress': {
    run_id: string
    node_id: string
    progress: NodeProgress
  }

  'team.updated': { team_id: string; autostart_tasks: boolean }
  'team.task.created': {
    task_id: string
    team_id: string
    assignee_id: string
    title: string
    created_by: string | null
  }
  'team.task.status_changed': {
    task_id: string
    team_id: string
    assignee_id: string
    status: TaskStatus
    /**
     * What it was. The event is re-sent when nothing moved — a summary landing does it — so
     * "changed" is `previous !== status`. Absent from a backend older than the Automations page.
     */
    previous?: TaskStatus
    error: string | null
  }
  /** Fields other than the status were edited; a column move arrives as a status change. */
  'team.task.updated': {
    task_id: string
    team_id: string
    assignee_id: string
    title: string
    status: TaskStatus
  }
  'team.task.deleted': { task_id: string; team_id: string; assignee_id: string }
  /**
   * The task's commit draft moved — generation started or ended, or the Commit button
   * landed (§10.4). The draft itself is read back over REST.
   */
  'team.task.commit_changed': {
    task_id: string
    team_id: string
    generating: boolean
    error: string | null
  }
  'team.member.message': {
    id: string
    task_id: string
    member_id: string
    role: 'user' | 'human' | 'assistant' | 'system' | 'tool' | 'result'
    kind: string
    text: string
    data: Record<string, unknown>
    created_at: string
  }
  /**
   * A chat with a member was created or changed — its title, whether the member is answering,
   * its totals (`teams/chat.py`). The whole chat minus its messages, so a missed frame is
   * corrected by the next one.
   */
  'team.chat.updated': AgentChat
  'team.chat.deleted': { chat_id: string; member_id: string }
  /** One transcript entry of a chat — `team.member.message`'s shape with `chat_id` for `task_id`. */
  'team.chat.message': AgentChatMessage
  /** A document was written — or deleted, when `deleted` is set, with no title left to send. */
  'team.doc.changed': { path: string; title: string | null; author: string | null; deleted?: boolean }
  /**
   * A project memory entry was written or, with `deleted`, removed — by an agent's `remember` /
   * `forget` or from the Memory page (§10.12). A hand edit arrives as `explorer.file_changed`.
   */
  'team.memory.changed': { name: string; author: string | null; deleted?: boolean }
  /**
   * A folder of the Documents tree changed its look, its name when `previous_path` is set, or went
   * with everything in it when `deleted` is (block-editor spec §5.2). The documents a rename
   * rewrote arrive as `team.doc.changed`; the documents a delete took do not arrive one by one.
   */
  'team.doc.folder_changed': {
    path: string
    previous_path: string | null
    author: string | null
    deleted?: boolean
  }

  'explorer.file_changed': {
    changes: { change: 'added' | 'modified' | 'deleted'; path: string; is_dir: boolean }[]
    truncated: boolean
    /** Whose paths these are: two watchers share the bus while a project is switched (§5.1). */
    root?: string
  }

  /** `.voxarium_studio/automations.yaml` was written through the API (§6.5). */
  'automation.changed': Record<string, never>
  /** An automation's action ran, failed or was refused — or, `running`, is still at it. The log row. */
  'automation.fired': AutomationRun
  /** A row already in the log changed: its work reported progress, or ended. Same `id`. */
  'automation.run_updated': AutomationRun
  /** The *notify* action: drawn as a toast, and as a desktop notification if the reader asked. */
  'automation.notification': AutomationNotification

  'plugin.changed': { plugins: LoadedPlugin[]; node_type_count: number }
  /** A plugin's environment started or finished building — the whole entry, to replace its row. */
  'plugin.env': { plugin: LoadedPlugin }
  /**
   * Something a plugin's own backend said (`PluginHost.publish`, §9.7). One type for every
   * plugin: a plugin's page filters on `plugin_id` and `name`, and `data` is its own shape.
   */
  'plugin.event': { plugin_id: string; name: string; data: Record<string, unknown> }

  /**
   * Everything `GET /api/updates` returns, published whenever any of it changes — a check
   * starting or finishing, a newer release appearing, a step of an update beginning. One
   * event carrying the whole answer, so a client renders the state rather than replaying a
   * sequence of verbs, and a missed frame is corrected by the next one (§12.1).
   */
  'update.status': UpdateStatus
  /** One line of an update's transcript, as the command it came from printed it. */
  'update.log': { line: string; step: string | null }

  /** Everything `GET /api/sync` returns, whenever any of it changes (§16.5). */
  'sync.status': SyncStatus
  /**
   * A document changed here and on the server at once, outside a live room. The server's
   * version has the path; this Studio's was kept beside it as `copy` (§16.3).
   */
  'sync.conflict': { path: string; copy: string; by: string | null }

  /**
   * The backend was pointed at a different managed project. Every store is now holding
   * another project's data, so the only correct response is to reload the page — which
   * is also how the tab that did *not* click Open finds out.
   */
  'project.opened': { root: string; name: string; data_dir: string; created: boolean }
}

export type EventType = keyof EventPayloads

/** The envelope every frame arrives in. `channel` is advisory metadata only. */
export interface EventEnvelope<T extends EventType = EventType> {
  type: T
  seq: number
  ts: string
  data: EventPayloads[T]
  channel?: string
}
