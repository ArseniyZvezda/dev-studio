/**
 * Thin typed wrapper over `fetch` for the `/api` surface.
 *
 * All URLs are relative. In production the SPA is served by the same FastAPI process,
 * and in development Vite proxies `/api` and `/ws` to the backend (see
 * `vite.config.ts`) — so there is exactly one URL shape to reason about, no CORS in
 * the picture, and plugin modules load same-origin in both modes.
 */

import { locale, t } from '@/i18n'
import { authHeaders, withTokenParam } from './auth'
import type {
  TaskResultType,
  AutomationInput,
  AutomationRun,
  AutomationsOverview,
  AgentChat,
  ProducerPolicy,
  ProducerEffortMode,
  AgentChatMessage,
  AgentMessage,
  ChatContext,
  ChatCreate,
  ChatDetail,
  AppConfig,
  ApplicationSettingsUpdate,
  CreateEntryRequest,
  DeleteRequest,
  DocDetail,
  DocEntry,
  DocFolderDeleted,
  DocFolderRenameDone,
  DocFolderRenameFrame,
  DocFolderStyle,
  DocFolderStyleUpdate,
  DocGraph,
  DocMoveResult,
  DocSearchResult,
  DirectoryListing,
  EntryPathsResponse,
  LayoutUpdate,
  MemberCreate,
  MemberOptions,
  LoadedPlugin,
  McpOverview,
  McpServerInput,
  NodeTypesResponse,
  PathInspection,
  PickResult,
  PickerStatus,
  RecentProject,
  ProjectTaskCounts,
  RemoteControlLink,
  PipelineDefinition,
  PipelineDetail,
  PipelineSummary,
  PluginPage,
  PreviewResult,
  ProjectInfo,
  MemoryEntryDetail,
  MemoryOverview,
  MemoryWrite,
  PromptSpec,
  RenameRequest,
  RevealResult,
  Run,
  RunDetail,
  RunRequest,
  RunStartedResponse,
  SearchProviderInfo,
  SearchResponse,
  SettingsUpdate,
  SkillDetail,
  SkillsOverview,
  StatusInfo,
  StudioChangelog,
  SyntaxGrammar,
  TaskCreate,
  TaskChanges,
  TaskCommitProposal,
  TaskDetail,
  TaskUpdate,
  Team,
  TeamDetail,
  TaskAttachment,
  TeamMember,
  TeamStats,
  TeamTask,
  TransferRequest,
  TreeNode,
  UpdateStatus,
  VcsCommitRequest,
  VcsCommitResult,
  VcsOverview,
  VcsSettingsUpdate,
  VcsStatus,
  MemberUpdate,
} from './types'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail)
    this.name = 'ApiError'
  }

  /** The backend answers 401 only when a token is configured and ours is wrong. */
  get isAuthFailure(): boolean {
    return this.status === 401
  }
}

/** Uses the same authentication and origin as every other Studio operation (§2.5). */
export function recoverRuntime(frontendVersion: string, backendVersion: string) {
  return request<{ action: 'restart' | 'reload'; version: string }>('POST', '/runtime/recover', {
    body: { frontend_version: frontendVersion, backend_version: backendVersion },
  })
}

type Query = Record<string, string | number | boolean | null | undefined>

/** Doc paths are nested (`features/dash.md`), so slashes must survive but nothing else. */
function encodePath(path: string): string {
  return path
    .split('/')
    .filter(Boolean)
    .map((segment) => encodeURIComponent(segment))
    .join('/')
}

function buildUrl(path: string, query?: Query): string {
  const url = `/api${path}`
  if (!query) return url
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

async function request<T>(
  method: string,
  path: string,
  options: { query?: Query; body?: unknown; cache?: RequestCache } = {},
): Promise<T> {
  const headers: Record<string, string> = { ...authHeaders() }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers,
      cache: options.cache,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    })
  } catch (cause) {
    // Network-level failure: the backend is down, or was restarted mid-request.
    throw new ApiError(0, cause instanceof Error ? cause.message : t('api.networkError'))
  }

  if (!response.ok) {
    throw new ApiError(response.status, await readErrorDetail(response))
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const payload = await response.json()
    const detail = (payload as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
    if (detail) return JSON.stringify(detail)
  } catch {
    /* fall through to the status text */
  }
  return response.statusText || `HTTP ${response.status}`
}

/** Absolute URL for a raw project file — used for `<img>` and download links. */
export function rawFileUrl(path: string): string {
  return buildUrl('/explorer/raw', { path })
}

/**
 * URL for a picture a document points at, resolved against that document's own folder.
 *
 * `src` travels exactly as it stands in the Markdown — resolving it is the backend's job
 * (`DocStore.asset`), and rewriting it here would be the editor deciding what the file says.
 *
 * The token rides the query string, as it does for the WebSocket: an `<img>` cannot carry a
 * header, so `rawFileUrl`'s shape would 401 under `--token` — the configuration least likely
 * to be the one anybody tests in.
 */
export function docAssetUrl(document: string, src: string): string {
  return withTokenParam(buildUrl('/docs/asset', { path: document, src }))
}

/**
 * Where a review attachment's bytes are (§10.4) — an `<img>` source or a download link, so
 * the token rides the query string as it does for `docAssetUrl`.
 */
export function taskAttachmentUrl(teamId: string, taskId: string, name: string): string {
  return withTokenParam(
    buildUrl(`/teams/${teamId}/tasks/${taskId}/attachments/${encodeURIComponent(name)}`),
  )
}

/**
 * Store a file for a review comment; the review names it afterwards (`requestChanges`).
 * Multipart for the reason `uploadDocAsset` is, and with the same rule about `Content-Type`.
 */
export async function uploadTaskAttachment(
  teamId: string,
  taskId: string,
  file: File,
): Promise<TaskAttachment> {
  const body = new FormData()
  body.append('file', file)
  let response: Response
  try {
    response = await fetch(buildUrl(`/teams/${teamId}/tasks/${taskId}/attachments`), {
      method: 'POST',
      headers: authHeaders(),
      body,
    })
  } catch (cause) {
    throw new ApiError(0, cause instanceof Error ? cause.message : t('api.networkError'))
  }
  if (!response.ok) throw new ApiError(response.status, await readErrorDetail(response))
  return (await response.json()) as TaskAttachment
}

export interface UploadedAttachment extends TaskAttachment { reference: string }

export function attachmentUrl(reference: string): string {
  return withTokenParam(buildUrl(`/attachments/${reference.split('/').map(encodeURIComponent).join('/')}`))
}

export async function uploadAttachment(file: File): Promise<UploadedAttachment> {
  const body = new FormData()
  body.append('file', file)
  const response = await fetch(buildUrl('/attachments'), { method: 'POST', headers: authHeaders(), body })
  if (!response.ok) throw new ApiError(response.status, await readErrorDetail(response))
  return await response.json() as UploadedAttachment
}

/** What `uploadDocAsset` answers with: the picture as the Markdown should reference it. */
export interface DocAssetAdded {
  /** The reference to write into `![](…)` — percent-encoded, so a space survives it. */
  src: string
  /** The file's name without its suffix, which is what the alt text is set to. */
  alt: string
  name: string
  path: string
  size: number
}

/**
 * Put a dropped picture beside a document.
 *
 * Multipart rather than JSON: the file is already bytes here, and base64 would inflate a
 * 20 MiB screenshot by a third on the way out. `Content-Type` is deliberately **not** set —
 * the browser has to write it itself, because only it knows the multipart boundary.
 */
export async function uploadDocAsset(document: string, file: File): Promise<DocAssetAdded> {
  const body = new FormData()
  body.append('path', document)
  body.append('file', file)

  let response: Response
  try {
    response = await fetch(buildUrl('/docs/asset'), {
      method: 'POST',
      headers: authHeaders(),
      body,
    })
  } catch (cause) {
    throw new ApiError(0, cause instanceof Error ? cause.message : t('api.networkError'))
  }
  if (!response.ok) throw new ApiError(response.status, await readErrorDetail(response))
  return (await response.json()) as DocAssetAdded
}

/**
 * Every JSON value in a newline-delimited stream, handed over as each line completes.
 *
 * A chunk ends wherever the network cut it, not at a line, so what follows the last newline
 * is carried into the next read — and a multi-byte character split across two chunks is put
 * back together by the decoder's `stream` mode rather than turning into two replacement
 * characters. A final line with no newline after it still counts.
 */
export async function readNdjson(
  body: ReadableStream<Uint8Array>,
  onValue: (value: unknown) => void,
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let pending = ''
  const flush = (text: string) => {
    const lines = text.split('\n')
    pending = lines.pop() ?? ''
    for (const line of lines) if (line.trim()) onValue(JSON.parse(line))
  }
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    flush(pending + decoder.decode(value, { stream: true }))
  }
  flush(`${pending}${decoder.decode()}\n`)
}

/**
 * Rename a folder of the Documents tree and follow the links being mended (§5.2).
 *
 * The answer is a stream, so this is not a `request`: every frame reaches `onFrame` as it
 * arrives, which is what lets the dialog tick documents off while they are written. A refusal
 * — a taken name, a folder the system will not let go of — comes back as an ordinary status
 * and throws `ApiError` before any frame. Resolves with the `done` frame; a stream that ended
 * without one, or said `error`, throws, because the rename then stopped somewhere in the middle.
 */
export async function renameDocFolder(
  path: string,
  name: string,
  onFrame: (frame: DocFolderRenameFrame) => void,
): Promise<DocFolderRenameDone> {
  let response: Response
  try {
    response = await fetch(buildUrl('/docs/folders/rename'), {
      method: 'POST',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, name }),
    })
  } catch (cause) {
    throw new ApiError(0, cause instanceof Error ? cause.message : t('api.networkError'))
  }
  if (!response.ok) throw new ApiError(response.status, await readErrorDetail(response))
  if (!response.body) throw new ApiError(0, t('api.renameNoBody'))

  // Filled in by the callback, which is why it is an object: a `let` assigned inside a closure
  // stays narrowed to its initial `null` for the type checker.
  const outcome: { done: DocFolderRenameDone | null; error: string | null } = {
    done: null,
    error: null,
  }
  try {
    await readNdjson(response.body, (value) => {
      const frame = value as DocFolderRenameFrame
      if (frame.type === 'done') outcome.done = frame
      if (frame.type === 'error') outcome.error = frame.error
      onFrame(frame)
    })
  } catch (cause) {
    // The connection dropped part-way. The backend carries on mending regardless — it runs
    // apart from this request — so what is on disk is ahead of what was heard here.
    throw new ApiError(0, cause instanceof Error ? cause.message : t('api.streamCutOff'))
  }
  if (outcome.error !== null) throw new ApiError(500, outcome.error)
  if (outcome.done === null) {
    throw new ApiError(0, t('api.renameUnfinished'))
  }
  return outcome.done
}

/**
 * Bytes of a project file. Plugin previewers parse binary formats client-side, and
 * unlike `rawFileUrl` this carries the auth header, so it also works in token mode.
 */
export async function rawFileBytes(path: string): Promise<ArrayBuffer> {
  let response: Response
  try {
    response = await fetch(rawFileUrl(path), { headers: authHeaders() })
  } catch (cause) {
    throw new ApiError(0, cause instanceof Error ? cause.message : t('api.networkError'))
  }
  if (!response.ok) throw new ApiError(response.status, await readErrorDetail(response))
  return await response.arrayBuffer()
}

export const api = {
  // ------------------------------------------------------------ project & settings
  project: () => request<ProjectInfo>('GET', '/project', { cache: 'no-store' }),
  status: () => request<StatusInfo>('GET', '/status'),
  settings: () => request<AppConfig>('GET', '/settings'),
  updateSettings: (body: SettingsUpdate) => request<AppConfig>('PUT', '/settings', { body }),
  updateApplicationSettings: (body: ApplicationSettingsUpdate) =>
    request<AppConfig>('PUT', '/settings/application', { body }),
  /** The built-in text behind every line the Studio says to an agent, in send order. */
  promptSpecs: () => request<Record<string, PromptSpec>>('GET', '/settings/prompts'),
  reloadSettings: () =>
    request<{ config: AppConfig; plugins: LoadedPlugin[] }>('POST', '/settings/reload'),
  /** What each release changed, as Markdown. The Studio's file, not the project's (§12.1). */
  changelog: () => request<StudioChangelog>('GET', '/changelog'),

  // ------------------------------------------------------------ updating the Studio
  // The Studio's own repository, never the managed project's (§12.1).
  updates: () => request<UpdateStatus>('GET', '/updates'),
  /** Fetches now instead of waiting for the next scheduled check. */
  checkForUpdates: () => request<UpdateStatus>('POST', '/updates/check'),
  /**
   * Pull, rebuild, restart. Answers as soon as it starts — the rest arrives as
   * `update.status` and `update.log` events, and the server goes away at the end of it.
   * A 409 means the Studio refused: uncommitted changes, or work a restart would kill.
   */
  applyUpdate: (force = false) => request<UpdateStatus>('POST', '/updates/apply', { body: { force } }),

  // ------------------------------------------------------------ opening a project
  recentProjects: () => request<RecentProject[]>('GET', '/project/recent'),
  projectTaskCounts: () => request<ProjectTaskCounts | null>('GET', '/project/task-counts'),
  forgetRecentProject: (path: string) =>
    request<RecentProject[]>('DELETE', '/project/recent', { query: { path } }),
  /** Swaps the whole backend context. Everything fetched before it is stale afterwards. */
  openProject: (path: string, create = true) =>
    request<ProjectInfo>('POST', '/project/open', { body: { path, create } }),

  browseDirectory: (path = '') =>
    request<DirectoryListing>('GET', '/fs/browse', { query: { path } }),
  inspectPath: (path: string) => request<PathInspection>('GET', '/fs/inspect', { query: { path } }),
  pickerStatus: () => request<PickerStatus>('GET', '/fs/picker'),
  /**
   * Opens the machine's own folder dialog and resolves when the user answers it — so
   * this call is pending for as long as that window is on screen, by design.
   */
  pickDirectory: (startPath?: string) =>
    request<PickResult>('POST', '/fs/pick', { body: { start_path: startPath ?? null } }),

  // ------------------------------------------------------------ explorer
  tree: (path = '', depth = 1) => request<TreeNode>('GET', '/explorer/tree', { query: { path, depth } }),
  preview: (path: string) => request<PreviewResult>('GET', '/explorer/file', { query: { path } }),

  // Operations on the project's own files. None of them publish an event of their own —
  // the file watcher sees the change and `explorer.file_changed` follows, which is the
  // same path an external edit or a pipeline node takes.
  createEntry: (body: CreateEntryRequest) =>
    request<EntryPathsResponse>('POST', '/explorer/create', { body }),
  renameEntry: (body: RenameRequest) =>
    request<EntryPathsResponse>('POST', '/explorer/rename', { body }),
  moveEntries: (body: TransferRequest) =>
    request<EntryPathsResponse>('POST', '/explorer/move', { body }),
  copyEntries: (body: TransferRequest) =>
    request<EntryPathsResponse>('POST', '/explorer/copy', { body }),
  deleteEntries: (body: DeleteRequest) =>
    request<EntryPathsResponse>('POST', '/explorer/delete', { body }),
  /** Show the entry in the machine's file manager. Loopback-only — 409 from elsewhere. */
  revealEntry: (path: string) =>
    request<RevealResult>('POST', '/explorer/reveal', { body: { path } }),

  // ------------------------------------------------------------ version control
  vcs: () => request<VcsOverview>('GET', '/vcs'),
  vcsStatus: () => request<VcsStatus>('GET', '/vcs/status'),
  updateVcs: (body: VcsSettingsUpdate) => request<VcsOverview>('PUT', '/vcs/settings', { body }),
  vcsCommit: (body: VcsCommitRequest) => request<VcsCommitResult>('POST', '/vcs/commit', { body }),
  vcsDiff: (path?: string) =>
    request<{ diff: string }>('GET', '/vcs/diff', { query: { path: path ?? null } }),

  // ------------------------------------------------------------ pipelines
  nodeTypes: () => request<NodeTypesResponse>('GET', '/node-types'),
  pipelines: () => request<PipelineSummary[]>('GET', '/pipelines'),
  pipeline: (id: string) => request<PipelineDetail>('GET', `/pipelines/${id}`),
  createPipeline: (body: PipelineDefinition) =>
    request<PipelineDefinition>('POST', '/pipelines', { body }),
  savePipeline: (id: string, body: PipelineDefinition) =>
    request<PipelineDetail>('PUT', `/pipelines/${id}`, { body }),
  deletePipeline: (id: string) => request<void>('DELETE', `/pipelines/${id}`),
  runPipeline: (id: string, body: RunRequest = {}) =>
    request<RunStartedResponse>('POST', `/pipelines/${id}/run`, { body }),

  // ------------------------------------------------------------ runs
  runs: (query: { pipeline_id?: string; limit?: number } = {}) =>
    request<Run[]>('GET', '/runs', { query }),
  run: (runId: string) => request<RunDetail>('GET', `/runs/${runId}`),
  nodeLog: (runId: string, nodeId: string) =>
    request<{ run_id: string; node_id: string; text: string; exists: boolean }>(
      'GET',
      `/runs/${runId}/nodes/${nodeId}/log`,
    ),
  resumeRun: (runId: string) =>
    request<{ run_id: string; resumed: boolean }>('POST', `/runs/${runId}/resume`),
  cancelRun: (runId: string) =>
    request<{ run_id: string; cancelled: boolean }>('POST', `/runs/${runId}/cancel`),
  clearAssetCache: () =>
    request<{ removed: number; bytes: number; kept: number; failed: number }>('DELETE', '/asset-cache'),
  clearNodeCache: (nodeType?: string) =>
    request<{ removed: number }>('DELETE', '/node-cache', { query: { node_type: nodeType } }),

  // ------------------------------------------------------------ plugins
  plugins: () => request<LoadedPlugin[]>('GET', '/plugins'),
  pluginPages: () => request<PluginPage[]>('GET', '/plugins/pages'),
  /** Languages plugins taught the Studio to highlight — TextMate grammars (§6.2). */
  pluginGrammars: () => request<SyntaxGrammar[]>('GET', '/plugins/grammars'),
  enablePlugin: (id: string) => request<LoadedPlugin>('POST', `/plugins/${id}/enable`),
  disablePlugin: (id: string) => request<LoadedPlugin>('POST', `/plugins/${id}/disable`),
  pluginSettings: (id: string) =>
    request<{ id: string; settings: Record<string, unknown> }>('GET', `/plugins/${id}/settings`),
  savePluginSettings: (id: string, settings: Record<string, unknown>) =>
    request<{ id: string; settings: Record<string, unknown> }>('PUT', `/plugins/${id}/settings`, {
      body: { settings },
    }),
  installPluginEnv: (id: string, force = false) =>
    request<{ id: string; provisioned: boolean; python?: string; log: string[]; detail?: string }>(
      'POST',
      `/plugins/${id}/install-env`,
      { query: { force } },
    ),
  /**
   * Call an action a plugin registered — its own endpoint (§9.7). The answer is whatever
   * the handler returned, unwrapped: the plugin's page knows its own shapes.
   */
  pluginAction: (id: string, action: string, payload?: object) =>
    request<unknown>('POST', `/plugins/${id}/actions/${action}`, { body: payload ?? {} }),

  // ------------------------------------------------------------ search
  /** Who answers the rail's search field. Empty means the field is not shown (§6.1). */
  searchProviders: () => request<SearchProviderInfo[]>('GET', '/search/providers'),
  /** `deep` is Enter rather than a keystroke — the provider may take seconds and answer in prose. */
  search: (q: string, options: { limit?: number; deep?: boolean } = {}) =>
    request<SearchResponse>('GET', '/search', {
      query: { q, limit: options.limit, deep: options.deep ? true : undefined },
    }),

  // ------------------------------------------------------------ chats (§10.9)
  /** Every chat, newest activity first. */
  chats: () => request<AgentChat[]>('GET', '/chats'),
  chat: (chatId: string) => request<ChatDetail>('GET', `/chats/${chatId}`),
  createChat: (body: ChatCreate) => request<AgentChat>('POST', '/chats', { body }),
  useProducerWorkspace: (chatId: string, locale: string) =>
    request<AgentChat>('POST', `/chats/${chatId}/producer/workspace`, { body: { locale } }),
  startProducer: (chatId: string, locale: 'en' | 'ru', policy?: ProducerPolicy, estimateTaskCostUsd?: number, effortMode?: ProducerEffortMode) =>
    request<AgentChat>('POST', `/chats/${chatId}/producer/start`, { body: { locale, policy, estimate_task_cost_usd: estimateTaskCostUsd, effort_mode: effortMode } }),
  setProducerSettings: (chatId: string, estimateTaskCostUsd: number) =>
    request<AgentChat>('PATCH', `/chats/${chatId}/producer/settings`, { body: { estimate_task_cost_usd: estimateTaskCostUsd } }),
  setProducerPolicy: (chatId: string, policy: ProducerPolicy) =>
    request<AgentChat>('PATCH', `/chats/${chatId}/producer/policy`, { body: { policy } }),
  setProducerEffortMode: (chatId: string, mode: ProducerEffortMode) =>
    request<AgentChat>('PATCH', `/chats/${chatId}/producer/effort`, { body: { mode } }),
  stopProducer: (chatId: string) => request<AgentChat>('POST', `/chats/${chatId}/producer/stop`),
  pauseProducer: (chatId: string) => request<AgentChat>('POST', `/chats/${chatId}/producer/pause`),
  resumeProducer: (chatId: string) => request<AgentChat>('POST', `/chats/${chatId}/producer/resume`),
  approveProducerPlan: (chatId: string, version: number) =>
    request<AgentChat>('POST', `/chats/${chatId}/producer/approve`, { body: { version } }),
  deleteChat: (chatId: string) =>
    request<{ chat_id: string; deleted: boolean }>('DELETE', `/chats/${chatId}`),
  /**
   * Say something to the member. Answers as soon as the turn has started (202); the reply
   * streams as `team.chat.message` events. A 409 means the member is still answering.
   */
  sendChatMessage: (chatId: string, text: string, context: ChatContext, attachments: string[] = []) =>
    request<{ chat: AgentChat; message: AgentChatMessage }>(
      'POST',
      `/chats/${chatId}/messages`,
      { body: { text, context, attachments } },
    ),
  cancelChat: (chatId: string) =>
    request<{ chat_id: string; cancelled: boolean }>('POST', `/chats/${chatId}/cancel`),

  // ------------------------------------------------------------ teams
  /** Note: the list endpoint returns members but no tasks — fetch a team for those. */
  teams: () => request<Team[]>('GET', '/teams'),
  team: (teamId: string) => request<TeamDetail>('GET', `/teams/${teamId}`),
  setAutostartTasks: (teamId: string, enabled: boolean) =>
    request<TeamDetail>('PATCH', `/teams/${teamId}`, { body: { autostart_tasks: enabled } }),
  startAllTasks: (teamId: string) =>
    request<{ team_id: string; task_ids: string[] }>('POST', `/teams/${teamId}/tasks/start-all`, {
      body: { summary_locale: locale.value },
    }),
  /** What the team's work has cost, over time and by member — archived tasks included. */
  teamStats: (teamId: string) => request<TeamStats>('GET', `/teams/${teamId}/stats`),
  memberOptions: () => request<MemberOptions>('GET', '/teams/member-options'),
  updateMember: (teamId: string, memberId: string, body: MemberUpdate) =>
    request<TeamMember>('PATCH', `/teams/${teamId}/members/${memberId}`, { body }),
  saveLayout: (teamId: string, positions: LayoutUpdate['positions']) =>
    request<{ team_id: string; updated: number }>('PATCH', `/teams/${teamId}/layout`, {
      body: { positions },
    }),
  createMember: (teamId: string, body: MemberCreate) =>
    request<TeamMember>('POST', `/teams/${teamId}/members`, { body }),
  deleteMember: (teamId: string, memberId: string, reassignToDirector = false) =>
    request<{ member_id: string; removed: boolean }>(
      'DELETE',
      `/teams/${teamId}/members/${memberId}`,
      { query: { reassign_to_director: reassignToDirector } },
    ),
  createTask: (teamId: string, body: TaskCreate) =>
    request<TeamTask>('POST', `/teams/${teamId}/tasks`, {
      body: { ...body, summary_locale: locale.value },
    }),
  tasks: (teamId: string, status?: string) =>
    request<TeamTask[]>('GET', `/teams/${teamId}/tasks`, { query: { status } }),
  task: (teamId: string, taskId: string) =>
    request<TaskDetail>('GET', `/teams/${teamId}/tasks/${taskId}`),
  updateTask: (teamId: string, taskId: string, body: TaskUpdate) =>
    request<TeamTask>('PATCH', `/teams/${teamId}/tasks/${taskId}`, {
      // Release captures the reader's language for the deferred run (§10.4).
      body: { ...body, ...(body.status === 'pending' ? { summary_locale: locale.value } : {}) },
    }),
  deleteTask: (teamId: string, taskId: string, actorId: string | null = null) =>
    request<{ task_id: string; deleted: boolean }>(
      'DELETE',
      `/teams/${teamId}/tasks/${taskId}`,
      { query: { actor_id: actorId } },
    ),
  startTask: (teamId: string, taskId: string) =>
    request<{ task_id: string; status: string }>('POST', `/teams/${teamId}/tasks/${taskId}/start`, {
      body: { summary_locale: locale.value },
    }),
  /**
   * Wipe the session, transcript, summary and cost, keeping the task and its brief (§10.4).
   * The task comes back in `todo`, so the next run starts from nothing.
   */
  resetTask: (teamId: string, taskId: string, actorId: string | null = null) =>
    request<TeamTask>('POST', `/teams/${teamId}/tasks/${taskId}/reset`, {
      body: { actor_id: actorId },
    }),
  compressTask: (teamId: string, taskId: string, actorId: string | null = null) =>
    request<TeamTask>('POST', `/teams/${teamId}/tasks/${taskId}/compress`, {
      body: { actor_id: actorId },
    }),
  approveTask: (teamId: string, taskId: string, comment = '', actorId: string | null = null) =>
    request<TeamTask>('POST', `/teams/${teamId}/tasks/${taskId}/approve`, {
      body: { comment, actor_id: actorId },
    }),
  requestChanges: (
    teamId: string,
    taskId: string,
    comment: string,
    actorId: string | null = null,
    attachments: string[] = [],
  ) =>
    request<TeamTask>('POST', `/teams/${teamId}/tasks/${taskId}/request-changes`, {
      body: { comment, actor_id: actorId, attachments, summary_locale: locale.value },
    }),
  /** Take back a file uploaded for a review that was not sent with it. */
  removeTaskAttachment: (teamId: string, taskId: string, name: string) =>
    request<void>(
      'DELETE',
      `/teams/${teamId}/tasks/${taskId}/attachments/${encodeURIComponent(name)}`,
    ),

  // Human members: claim / note / complete stand in for an agent's session lifecycle.
  claimTask: (teamId: string, taskId: string, actorId: string | null = null) =>
    request<TeamTask>('POST', `/teams/${teamId}/tasks/${taskId}/claim`, {
      body: { actor_id: actorId },
    }),
  addNote: (teamId: string, taskId: string, text: string, actorId: string | null = null) =>
    request<AgentMessage>('POST', `/teams/${teamId}/tasks/${taskId}/notes`, {
      body: { text, actor_id: actorId },
    }),
  completeTask: (
    teamId: string,
    taskId: string,
    summary = '',
    actorId: string | null = null,
    resultType: TaskResultType = 'done',
  ) =>
    request<TeamTask>('POST', `/teams/${teamId}/tasks/${taskId}/complete`, {
      body: { summary, actor_id: actorId, result_type: resultType },
    }),
  cancelTask: (teamId: string, taskId: string) =>
    request<{ task_id: string; cancelled: boolean }>(
      'POST',
      `/teams/${teamId}/tasks/${taskId}/cancel`,
    ),
  // Remote Control: the running session, driven from claude.ai/code or the phone (§10.8).
  enableRemoteControl: (teamId: string, taskId: string) =>
    request<RemoteControlLink>('POST', `/teams/${teamId}/tasks/${taskId}/remote-control`),
  disableRemoteControl: (teamId: string, taskId: string) =>
    request<RemoteControlLink>('DELETE', `/teams/${teamId}/tasks/${taskId}/remote-control`),
  /** What this task changed — its own checkout where it has one, its tool calls where not. */
  taskChanges: (teamId: string, taskId: string) =>
    request<TaskChanges>('GET', `/teams/${teamId}/tasks/${taskId}/changes`),
  /** One file of that, as a patch to render. `path` is as `taskChanges` reported it. */
  taskPatch: (teamId: string, taskId: string, path: string) =>
    request<{ path: string; patch: string }>(
      'GET',
      `/teams/${teamId}/tasks/${taskId}/changes/patch`,
      { query: { path } },
    ),
  /** The commit drafted for the task when it reached review (§10.4). */
  taskCommit: (teamId: string, taskId: string) =>
    request<TaskCommitProposal>('GET', `/teams/${teamId}/tasks/${taskId}/commit`),
  /** Draft it again — what arriving in review does, on request. */
  regenerateTaskCommit: (teamId: string, taskId: string) =>
    request<TaskCommitProposal>('POST', `/teams/${teamId}/tasks/${taskId}/commit/regenerate`),
  /** Commit through the project's version control; `committed: false` means nothing was left. */
  commitTask: (teamId: string, taskId: string, body: { message?: string; files?: string[] }) =>
    request<VcsCommitResult>('POST', `/teams/${teamId}/tasks/${taskId}/commit`, { body }),
  removeWorktree: (teamId: string, taskId: string, force = false) =>
    request<{ task_id: string; removed: boolean }>(
      'DELETE',
      `/teams/${teamId}/tasks/${taskId}/worktree`,
      { query: { force } },
    ),

  // ------------------------------------------------------------ project docs
  docs: () => request<DocEntry[]>('GET', '/docs'),
  doc: (path: string) => request<DocDetail>('GET', `/docs/${encodePath(path)}`),
  /**
   * `expectedUpdatedAt` makes the write conditional — the backend answers 409 rather than
   * overwriting a file that changed since it was read. The editor sends it; anything
   * creating a document from scratch has nothing to compare against, and uses `createDoc`.
   */
  saveDoc: (
    path: string,
    content: string,
    author: string | null = null,
    expectedUpdatedAt: string | null = null,
  ) =>
    request<DocDetail>('PUT', `/docs/${encodePath(path)}`, {
      body: { content, author, expected_updated_at: expectedUpdatedAt },
    }),
  /**
   * A new document, refused with 409 when anything is already at the path. A write with no
   * expectation replaces what is there, which is right for a save and loses a document for a
   * "New document" given a name that is taken.
   */
  createDoc: (path: string, content: string) =>
    request<DocDetail>('PUT', `/docs/${encodePath(path)}`, {
      body: { content, exclusive: true },
    }),
  deleteDoc: (path: string) =>
    request<{ path: string; deleted: boolean }>('DELETE', `/docs/${encodePath(path)}`),
  moveDoc: (source: string, target: string) =>
    request<DocMoveResult>('POST', '/docs/move', { body: { source, target } }),
  searchDocs: (q: string) => request<DocSearchResult[]>('GET', '/docs/search', { query: { q } }),
  /**
   * The whole corpus's link graph, for the side panel's picture (§5). Declared before the
   * `/{path:path}` catch-all on the backend, exactly as `/search` is, or the route would
   * be read as a document called `graph`.
   */
  docsGraph: () => request<DocGraph>('GET', '/docs/graph'),
  /** The folders of the tree that have a colour or an icon — no others are listed (§5.2). */
  docFolders: () => request<DocFolderStyle[]>('GET', '/docs/folders'),
  setDocFolderStyle: (body: DocFolderStyleUpdate) =>
    request<DocFolderStyle>('POST', '/docs/folders/style', { body }),
  renameDocFolder,
  /**
   * Delete a folder with everything in it. 409 means the system would not let go of it and
   * nothing was removed; a 500 may have stopped part-way.
   */
  deleteDocFolder: (path: string) =>
    request<DocFolderDeleted>('POST', '/docs/folders/delete', { body: { path } }),

  // ------------------------------------------------------------ MCP servers (§10.7)
  /**
   * Every call answers with the whole page. `.mcp.json` is edited by hand and by the
   * `claude` CLI as well as from here, so patching local state from a narrower response
   * would drift from the file within one edit.
   */
  mcp: () => request<McpOverview>('GET', '/mcp'),
  saveMcpServer: (name: string, body: McpServerInput) =>
    request<McpOverview>('PUT', `/mcp/servers/${encodeURIComponent(name)}`, { body }),
  deleteMcpServer: (name: string) =>
    request<McpOverview>('DELETE', `/mcp/servers/${encodeURIComponent(name)}`),
  /** Withholds the server from agent sessions; it stays in the file for the terminal. */
  setMcpServerEnabled: (name: string, enabled: boolean) =>
    request<McpOverview>(
      'POST',
      `/mcp/servers/${encodeURIComponent(name)}/${enabled ? 'enable' : 'disable'}`,
    ),
  saveMcpFile: (content: string) => request<McpOverview>('PUT', '/mcp/file', { body: { content } }),
  setMcpStrict: (strict: boolean) =>
    request<McpOverview>('PATCH', '/mcp/settings', { body: { strict } }),

  // ------------------------------------------------------------ skills (§10.10)
  /**
   * `voxarium_studio/skills/` with who has been given what. The folder is changed by copies and
   * checkouts as often as from here, so the calls that change it answer with the whole page.
   */
  skills: () => request<SkillsOverview>('GET', '/skills'),
  /** A new skill folder with its `SKILL.md`. Given to nobody — that is the member editor's. */
  createSkill: (name: string, description: string) =>
    request<SkillsOverview>('POST', '/skills', { body: { name, description } }),
  skill: (name: string) => request<SkillDetail>('GET', `/skills/${encodeURIComponent(name)}`),
  /** 409 when `SKILL.md` changed on disk since `expectedUpdatedAt` was read. */
  saveSkill: (name: string, content: string, expectedUpdatedAt: string | null) =>
    request<SkillDetail>('PUT', `/skills/${encodeURIComponent(name)}`, {
      body: { content, expected_updated_at: expectedUpdatedAt },
    }),
  /** The folder with everything in it, and the name off every member who had it. */
  deleteSkill: (name: string) =>
    request<SkillsOverview>('DELETE', `/skills/${encodeURIComponent(name)}`),

  // ------------------------------------------------------------ project memory (§10.12)
  /** `voxarium_studio/memory/` with the index agents are sent; the switch and cap are team settings. */
  memory: () => request<MemoryOverview>('GET', '/memory'),
  memoryEntry: (name: string) =>
    request<MemoryEntryDetail>('GET', `/memory/${encodeURIComponent(name)}`),
  /** 409 when `create` finds the name taken, or the entry changed since `expected_updated_at`. */
  saveMemoryEntry: (name: string, body: MemoryWrite) =>
    request<MemoryEntryDetail>('PUT', `/memory/${encodeURIComponent(name)}`, { body }),
  deleteMemoryEntry: (name: string) =>
    request<MemoryOverview>('DELETE', `/memory/${encodeURIComponent(name)}`),

  // ------------------------------------------------------------ automations (§6.5)
  /**
   * `voxarium_studio/automations.yaml` with what it has done. Every write answers with the whole page:
   * the file is edited by hand and by merges too, so the list is never patched client-side.
   */
  automations: () => request<AutomationsOverview>('GET', '/automations'),
  createAutomation: (body: AutomationInput) =>
    request<AutomationsOverview>('POST', '/automations', { body }),
  updateAutomation: (id: string, body: AutomationInput) =>
    request<AutomationsOverview>('PUT', `/automations/${encodeURIComponent(id)}`, { body }),
  /** The card's switch — its own route, so a toggle never writes back a stale form. */
  setAutomationEnabled: (id: string, enabled: boolean) =>
    request<AutomationsOverview>('PATCH', `/automations/${encodeURIComponent(id)}`, {
      body: { enabled },
    }),
  deleteAutomation: (id: string) =>
    request<AutomationsOverview>('DELETE', `/automations/${encodeURIComponent(id)}`),
  /** The action, now, with nothing having happened — works on a disabled automation too. */
  runAutomation: (id: string) =>
    request<{ run: AutomationRun }>('POST', `/automations/${encodeURIComponent(id)}/run`),
  clearAutomationRuns: () => request<AutomationsOverview>('DELETE', '/automations/runs'),
}
