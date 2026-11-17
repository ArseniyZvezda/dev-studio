/**
 * Project documentation — the team's shared specs and notes (spec §10.4).
 *
 * Agents write here through their `write_doc` tool, so the list is kept live off
 * `team.doc.changed` rather than only on navigation: watching a designer's spec appear
 * while its task is still running is the point of showing it in the Studio at all.
 *
 * **Dirty means "the user edited it", not "the bytes differ"** (block-editor spec §8.2).
 * The editor's document model is a syntax tree, and serialising it produces this project's
 * Markdown dialect — which is not what most files on disk are written in. Comparing the two
 * would report every document as modified the moment it was opened, and §8.2 requires the
 * opposite: opening ten documents and closing them must leave version control clean. So the
 * flag is raised by the editor when a transaction changes the document, and by nothing else.
 *
 * Saving is debounced by the editor rather than manual. These files are on disk and in git,
 * an agent may be reading one mid-task, and losing an edit to a forgotten Ctrl+S is worse
 * than writing a little too often — but a save that lands on top of someone else's write
 * would destroy it, which is what `expected_updated_at` is for.
 *
 * **A document open in a live room is the exception to that last rule** (dev spec §16.4).
 * There the document on screen is the room's, merged as it is typed, and the file is only
 * its latest Markdown: the room's scribe writes it unconditionally, and a write arriving
 * from somebody else is the same room's text a moment older or newer — never a reason to
 * warn, and never something to load into the editor. `live` is what says so.
 */

import { defineStore } from "pinia";
import { computed, ref, shallowRef } from "vue";

import { api, ApiError, createApiClient } from "@/api/client";
import type {
  DocBacklink,
  DocDetail,
  DocEntry,
  DocFolderRenameDone,
  DocFolderRenameFrame,
  DocFolderStyle,
  DocGraph,
  DocSearchResult,
  DocsConfig,
  DocumentFence,
  DocumentSaveReceipt,
  DocSaved,
  ProjectRemoteEditorLease,
  ProjectRemoteEditorMaterialization,
} from "@/api/types";
import { t } from "@/i18n";
import { titleOf } from "@/lib/md/title";
import { onEvent } from "@/ws/connection";
import { ProjectSession, rendererSession, StaleProjectResponseError } from "@/session/project-session";
import { useSyncStore, StaleRemoteBindingError, type RemoteBindingTicket } from "@/stores/sync";

export interface CapturedDocMutation {
  readonly binding: RemoteBindingTicket | null;
  readonly fence: Readonly<DocumentFence> | undefined;
}

/** Capture before debounce/queue, not when the delayed callback executes (§5.3). */
export interface CapturedDocSave extends CapturedDocMutation {
  readonly path: string | null;
  readonly live: string | null;
  readonly version: number;
  readonly revision: number;
  readonly expectedUpdatedAt: string | null;
  readonly dirty: boolean;
}

export type SaveState = "saved" | "saving" | "dirty" | "failed";

export interface DocTreeNode {
  name: string;
  path: string;
  isFolder: boolean;
  entry: DocEntry | null;
  children: DocTreeNode[];
}

/**
 * Where the documents sit in the **project's** frame — `.voxarium_studio/docs` in every ordinary
 * layout — which is the frame `/api/vcs/status` speaks (§9.6). A document's own path is
 * relative to the documents folder, so a version control lookup needs this in front of it.
 *
 * Derived from the two absolute paths `/api/project` reports rather than restated here, so
 * whatever the backend names the data directory is followed without a second copy of the
 * name. The fallback is the documented default, for a payload that does not carry both.
 */
export function docsVcsRoot(
  root: string | null | undefined,
  dataDir: string | null | undefined,
): string {
  const normalise = (path: string) =>
    path.replace(/\\/g, "/").replace(/\/+$/, "");
  if (root && dataDir) {
    const base = normalise(root);
    const data = normalise(dataDir);
    // Case-insensitively: both come from one backend, but a Windows drive letter is
    // whatever the shell typed, and `D:` and `d:` name the same directory.
    if (data.toLowerCase().startsWith(`${base.toLowerCase()}/`)) {
      return `${data.slice(base.length + 1)}/docs`;
    }
  }
  return ".voxarium_studio/docs";
}

/**
 * *path* as it reads once *folder* is called *renamed*; unchanged outside it (§5.2).
 *
 * The backend's `_moved`, restated for the paths the client holds — the open document, the
 * folded folders — so they follow a rename instead of pointing at a name that is gone.
 */
export function movedPath(path: string, folder: string, renamed: string): string {
  if (path === folder) return renamed;
  if (path.startsWith(`${folder}/`)) return `${renamed}${path.slice(folder.length)}`;
  return path;
}

/**
 * Where a document called *name* goes inside *folder* (`""` for the docs root) — `""` when the
 * name is nothing but blanks and slashes.
 *
 * `.md` is added unless the name already ends in a suffix a document may have. The backend adds
 * it to a path with no suffix at all, but reads `v1.2` as a `.2` it refuses, so the suffix is
 * decided here. Separators around the name are dropped: `notes/` is `notes.md`, not a file
 * called `.md` inside a new folder.
 */
export function documentPath(folder: string, name: string): string {
  const bare = name.trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  if (!bare) return "";
  const path = folder ? `${folder}/${bare}` : bare;
  return /\.(md|markdown|txt)$/i.test(path) ? path : `${path}.md`;
}

/** A folder's look written into the map the tree reads — or taken out, once it is plain. */
function withStyle(
  styles: Record<string, DocFolderStyle>,
  style: DocFolderStyle,
): Record<string, DocFolderStyle> {
  const next = { ...styles };
  if (style.color === null && style.icon === null) delete next[style.path];
  else next[style.path] = style;
  return next;
}

function buildTree(entries: DocEntry[]): DocTreeNode[] {
  const root: DocTreeNode = {
    name: "",
    path: "",
    isFolder: true,
    entry: null,
    children: [],
  };

  for (const entry of entries) {
    const segments = entry.path.split("/");
    let cursor = root;
    segments.forEach((segment, index) => {
      const isLeaf = index === segments.length - 1;
      const path = segments.slice(0, index + 1).join("/");
      let next = cursor.children.find(
        (child) => child.name === segment && child.isFolder !== isLeaf,
      );
      if (!next) {
        next = {
          name: segment,
          path,
          isFolder: !isLeaf,
          entry: isLeaf ? entry : null,
          children: [],
        };
        cursor.children.push(next);
      }
      cursor = next;
    });
  }

  const sort = (nodes: DocTreeNode[]): DocTreeNode[] => {
    nodes.sort((a, b) =>
      a.isFolder === b.isFolder
        ? a.name.localeCompare(b.name)
        : a.isFolder
          ? -1
          : 1,
    );
    for (const node of nodes) if (node.children.length) sort(node.children);
    return nodes;
  };
  return sort(root.children);
}

/**
 * Every folder the tree draws, which is every path prefix some document sits under.
 *
 * Folders are implied by the document paths rather than listed by the backend, so this is
 * the only statement of what a folder path *is*, and `buildTree` walks the same segments.
 */
function folderPaths(entries: DocEntry[]): Set<string> {
  const paths = new Set<string>();
  for (const entry of entries) {
    const segments = entry.path.split("/");
    for (let i = 1; i < segments.length; i += 1) {
      paths.add(segments.slice(0, i).join("/"));
    }
  }
  return paths;
}

/**
 * What the header's stepper may set the reading size to, in px — the same bounds the
 * backend's `DocsConfig` validates against, so the control cannot offer a rejected value.
 *
 * There is no default here on purpose. "Nobody chose one" is an *offset* — a point above
 * whatever the §7 density knob makes `--vxs-text-size` — and only the DOM knows what that
 * resolves to, so it is `editor.css` that states it and the view that reads it back.
 */
export const FONT_SIZE = { min: 10, max: 28 } as const;

const clampFontSize = (value: number): number =>
  Math.min(FONT_SIZE.max, Math.max(FONT_SIZE.min, Math.round(value)));

/**
 * What the header's leading stepper may set, unitless — the backend's `DocsConfig` bounds
 * again. `step` is what one click moves by; values are kept to one decimal so `1.1 + 0.1`
 * never persists as `1.2000000000000002`.
 *
 * As with the size, the default is not here: `editor.css` states it and the view reads it
 * back, so there is exactly one place that knows what "nobody chose" renders as.
 */
export const LINE_HEIGHT = { min: 1, max: 2.5, step: 0.1 } as const;

const clampLineHeight = (value: number): number =>
  Math.min(
    LINE_HEIGHT.max,
    Math.max(LINE_HEIGHT.min, Math.round(value * 10) / 10),
  );

/**
 * Where this browser remembers a project's folded folders — the prefix of one key per project,
 * the backend's resolved root after it (block-editor spec §5.2).
 *
 * Per project because a folder is a path, and `features` in one project is not `features` in
 * the next one opened. The root is used verbatim: `ProjectPaths.for_project` resolves it, so
 * one directory always arrives spelled one way.
 */
export const FOLDS_KEY_PREFIX = "voxarium_studio.docs.collapsed.";

/**
 * The folders a stored value says are folded — none, for a value that is absent or that this
 * build cannot read.
 *
 * Whatever build last ran in this browser wrote it, or a person did, so only a list of strings
 * is taken at its word. Whether each one is still a folder needs a listing, which is what
 * `load()` checks the set against.
 */
function parseFolds(raw: string | null): Set<string> {
  if (!raw) return new Set();
  try {
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return new Set();
    return new Set(
      value.filter(
        (path): path is string => typeof path === "string" && path !== "",
      ),
    );
  } catch {
    return new Set();
  }
}

/** One spelling per set, whatever order the folders were folded in. */
const serialiseFolds = (folds: Set<string>): string =>
  JSON.stringify([...folds].sort());

/**
 * A burst of `team.doc.changed` from one agent run should cost one re-fetch, not twenty.
 *
 * Exported so the check pins the value the store actually debounces on, rather than a copy
 * of it that would go on passing after the constant moved.
 */
export const GRAPH_REFRESH_MS = 500;

export const useDocsStore = defineStore("docs", () => {
  // Capture the owning Pinia dependency before queue/await. Another mounted
  // editor may become active while this one's final save is awaiting its tail.
  const sync = useSyncStore();
  const entries = ref<DocEntry[]>([]);
  const open = ref<DocDetail | null>(null);
  const loading = ref(false);
  const saving = ref(false);
  const dirty = ref(false);
  const error = ref<string | null>(null);
  /** Why the last save failed, kept apart from `error` so the indicator can show it (§7). */
  const saveError = ref<string | null>(null);
  /** True when the file changed underneath us and the save was refused (§14.5). */
  const staleOnDisk = ref(false);
  const fenceError = shallowRef<Error | null>(null);
  const lastSaveReceipt = shallowRef<Readonly<DocumentSaveReceipt> | null>(null);
  let editorFence: Readonly<DocumentFence> | null = null;
  let editorBinding: RemoteBindingTicket | null = null;
  let editorLease: Readonly<ProjectRemoteEditorLease> | null = null;
  let materializationPending = false;
  let saveRevision = 0;
  let editorRevision = -1;
  let editorPath: string | null = null;
  let fenced = false;
  const savedRevisions = new Map<number, string>();

  function setEditorFence(fence: DocumentFence | null, binding?: RemoteBindingTicket): void {
    // Registration owns admission. Reuse its pre-await ticket even while a same-G
    // draining event refreshes metadata; a final save is not a new editor admission.
    let registeredBinding: RemoteBindingTicket | null = null;
    if (fence) {
      try {
        registeredBinding = binding ?? sync.capture();
        registeredBinding.assertCurrent();
        if (fence.backend_epoch !== registeredBinding.backend_epoch
          || fence.expected_binding_generation !== registeredBinding.expected_binding_generation) {
          throw new ApiError(409, t("docs.remoteEditor.blocked"), "reload_required");
        }
      } catch (cause) { recordFenceError(cause); throw cause; }
    }
    if (editorFence?.editor_id !== fence?.editor_id || editorFence?.sid !== fence?.sid
      || editorFence?.backend_epoch !== fence?.backend_epoch
      || editorFence?.client_instance_id !== fence?.client_instance_id
      || editorFence?.expected_binding_generation !== fence?.expected_binding_generation) saveRevision += 1;
    if (fence) {
      fenced = true;
      if (editorFence?.editor_id !== fence.editor_id) {
        edits = fence.version ?? 0;
        fenceError.value = null;
      }
      else edits = Math.max(edits, fence.version ?? 0);
    }
    editorFence = fence ? Object.freeze({ ...fence }) : null;
    editorLease = null;
    materializationPending = false;
    editorBinding = registeredBinding;
    editorRevision = documentRequest;
    editorPath = open.value?.path ?? null;
    lastSaveReceipt.value = null;
  }

  function captureMutation(): CapturedDocMutation {
    return captureDocumentMutation(false);
  }

  function captureDocumentMutation(editor: boolean): CapturedDocMutation {
    try { return captureMutationTicket(editor); }
    catch (cause) { recordFenceError(cause); throw cause; }
  }

  function captureMutationTicket(editor: boolean): CapturedDocMutation {
    if (editor && fenceError.value) throw fenceError.value;
    // Only a genuinely unactivated legacy renderer can omit the new contract.
    if (rendererSession.isLegacy && !sync.remote && !fenced) {
      return Object.freeze({ binding: null, fence: undefined });
    }
    fenced = true;
    const binding = editor ? editorBinding : sync.capture();
    if (!binding || (editor && (!editorFence?.editor_id
      || editorRevision !== documentRequest || editorPath !== open.value?.path
      || editorFence.backend_epoch !== binding.backend_epoch
      || editorFence.expected_binding_generation !== binding.expected_binding_generation))) {
      throw new ApiError(409, t("docs.remoteEditor.blocked"), "reload_required");
    }
    binding.assertCurrent();
    const fence = Object.freeze(editor ? { ...editorFence!, version: edits } : {
      backend_epoch: binding.backend_epoch,
      expected_binding_generation: binding.expected_binding_generation,
    });
    return Object.freeze({ binding, fence });
  }

  function captureSave(): CapturedDocSave {
    return Object.freeze({ ...captureDocumentMutation(true), path: open.value?.path ?? null,
      live: live.value, version: edits, revision: saveRevision,
      expectedUpdatedAt: open.value?.updated_at ?? null, dirty: dirty.value });
  }

  /** Propagate server registration/heartbeat evidence, never synthesize its CAS revision. */
  function setEditorLease(lease: ProjectRemoteEditorLease): void {
    if (!editorFence || !editorBinding || editorRevision !== documentRequest
      || editorPath !== open.value?.path || lease.document_path !== editorPath
      || lease.editor_id !== editorFence.editor_id
      || lease.backend_epoch !== editorFence.backend_epoch
      || lease.binding_generation !== editorFence.expected_binding_generation
      || lease.client_instance_id !== editorFence.client_instance_id
      || !Number.isSafeInteger(lease.revision) || lease.revision < 0
      || (editorLease && lease.revision < editorLease.revision)) {
      throw new ApiError(409, t("docs.remoteEditor.blocked"), "editor_mismatch");
    }
    editorBinding.assertCurrent();
    editorLease = Object.freeze({ ...lease });
  }

  /** I8 / studio-design §5.2–5.3: prove this frozen editor, without a Markdown write.
   * The caller settles dirty announcements/room frames first and retains ACK ownership.
   */
  async function proveMaterialization(captured: CapturedDocSave, sid: string, content: string,
    assertSnapshot: () => void): Promise<ProjectRemoteEditorLease> {
    const owner = editorFence;
    const lease = editorLease;
    const binding = captured.binding;
    const fence = captured.fence;
    const stale = () => new ApiError(409, t("docs.remoteEditor.blocked"), "stale_editor_version");
    const assertCurrent = () => {
      binding?.assertCurrent();
      if (!binding || binding !== editorBinding || !owner || owner !== editorFence || !fence
        || fenceError.value || !lease || !editorLease || !sid || !captured.path
        || !fence.editor_id || !fence.client_instance_id
        || captured.revision !== saveRevision || captured.path !== open.value?.path
        || editorRevision !== documentRequest || editorPath !== captured.path
        || captured.version !== edits || fence.version !== captured.version
        || captured.live !== sid || live.value !== sid || fence.sid !== sid || owner.sid !== sid
        || fence.editor_id !== owner.editor_id || fence.client_instance_id !== owner.client_instance_id
        || fence.backend_epoch !== binding.backend_epoch || fence.backend_epoch !== owner.backend_epoch
        || fence.expected_binding_generation !== binding.expected_binding_generation
        || fence.expected_binding_generation !== owner.expected_binding_generation
        || lease.version !== captured.version || editorLease.version !== lease.version
        || editorLease.revision !== lease.revision) throw stale();
      assertSnapshot();
    };
    assertCurrent();
    // A separate immutable project client prevents any legacy/global fallback or late rebind.
    const session = new ProjectSession(binding!.runtime_scope.project_id);
    session.bind(binding!.runtime_scope);
    const client = createApiClient(session);
    const body: Readonly<ProjectRemoteEditorMaterialization> = Object.freeze({
      backend_epoch: fence!.backend_epoch, expected_binding_generation: fence!.expected_binding_generation,
      document_path: captured.path!, client_instance_id: fence!.client_instance_id!, sid,
      version: captured.version, revision: lease!.revision,
    });
    const exactLease = (result: ProjectRemoteEditorLease) => result.editor_id === fence!.editor_id
      && result.backend_epoch === body.backend_epoch
      && result.binding_generation === body.expected_binding_generation
      && result.document_path === body.document_path && result.client_instance_id === body.client_instance_id
      && result.version === body.version && result.revision === body.revision;
    try {
      let ticket;
      try { ticket = await client.materializeRemoteEditor(fence!.editor_id!, body); }
      finally { assertCurrent(); }
      if (!exactLease(ticket) || typeof ticket.proof_id !== "string" || !ticket.proof_id) throw stale();
      let confirmed;
      try {
        confirmed = await client.confirmRemoteEditorMaterialization(fence!.editor_id!, {
          ...body, proof_id: ticket.proof_id, content,
        });
      } finally { assertCurrent(); }
      if (!exactLease(confirmed) || confirmed.dirty || !confirmed.confirmed
        || !Number.isSafeInteger(confirmed.last_saved_version) || confirmed.last_saved_version! <= 0) throw stale();
      editorLease = Object.freeze({ ...confirmed });
      materializationPending = false;
      return confirmed;
    } catch (cause) {
      // Proof conflicts are never disk conflicts or permission for Keep mine.
      const refusal = cause instanceof ApiError && cause.status === 409 && cause.code === null
        ? new ApiError(409, cause.message, "pending_edits") : cause;
      try { assertCurrent(); } catch { throw refusal; }
      if (refusal instanceof ApiError && refusal.status === 409
        && ["pending_edits", "mutation_busy", "stale_editor_version"].includes(refusal.code ?? "")) {
        // A peer's final save or a newer server CAS may still arrive during old-G drain.
        // Keep this editor retryable, but this is never a disk overwrite capability.
        materializationPending = true;
        staleOnDisk.value = false;
      } else recordFenceError(refusal);
      throw refusal;
    }
  }

  function recordFenceError(cause: unknown): void {
    if ((cause instanceof ApiError && cause.status === 409 && cause.code !== null)
      || cause instanceof StaleRemoteBindingError || cause instanceof StaleProjectResponseError) {
      fenced = true;
      fenceError.value = cause;
      staleOnDisk.value = false;
    }
  }

  /** External observations cannot replace RAM owned by an admitted/frozen editor (I8). */
  function retainsScopedSource(): boolean {
    const change = sync.bindingChange;
    const transitioning = !!editorBinding && !!change
      && (change.binding_generation === editorBinding.expected_binding_generation
        || change.expected_generation === editorBinding.expected_binding_generation)
      && (change.fenced || ["draining", "ready", "blocked", "paused"].includes(change.state));
    return !!(editorBinding && (live.value || transitioning || fenceError.value || saveError.value));
  }

  function blockExternalReplacement(): void {
    editorLease = null;
    lastSaveReceipt.value = null;
    recordFenceError(fenceError.value ?? new ApiError(409, t("docs.remoteEditor.blocked"),
      live.value ? "unknown_sid" : "editor_mismatch"));
  }

  async function mutate<T>(captured: CapturedDocMutation, run: () => Promise<T>): Promise<T> {
    try {
      return captured.binding ? await sync.run(captured.binding, run) : await run();
    } catch (cause) {
      recordFenceError(cause);
      throw cause;
    }
  }
  /**
   * The session id of the live room the open document is being edited in, or `null` when it
   * is edited alone (dev spec §16.4). Set by the page that owns the session, which is also
   * what clears it — after the last save written from the room has been sent.
   */
  const live = ref<string | null>(null);

  /** The document's heading as the editor currently has it, for the header and the tree. */
  const liveTitle = ref("");

  /**
   * The reading size somebody picked for this project's documents, in px — `null` while
   * nobody has, which the editor renders as a point above the app's own text size.
   *
   * §7: appearance lives in the project's `config.yaml`, not in browser storage, so it
   * travels with the project rather than with the browser that set it.
   */
  const fontSize = ref<number | null>(null);
  /** The leading somebody picked, unitless — `null` while nobody has (same home as the size). */
  const lineHeight = ref<number | null>(null);

  const query = ref("");
  const results = ref<DocSearchResult[]>([]);
  const searching = ref(false);

  /**
   * The corpus's link graph (§5), fetched lazily and held apart from `open` on purpose.
   *
   * The picture is a property of the *set* of documents, not of the one being read, so
   * clicking through ten documents re-centres it client-side without a single request — and
   * nothing on the document-open path touches it. `select()` is what a reader actually waits
   * on, and putting a whole-corpus walk in front of the text they asked for would be paying
   * for an ornament with the one latency that matters.
   */
  const graph = ref<DocGraph | null>(null);
  const graphLoading = ref(false);
  /** Why the picture is missing — kept out of `error`, which raises a banner over the document. */
  const graphError = ref<string | null>(null);
  /** The corpus changed under the copy we hold; the next ask re-fetches. */
  let graphStale = true;
  /** The request in flight, so two callers in one tick make one call. */
  let graphRequest: Promise<void> | null = null;
  let graphTimer: ReturnType<typeof setTimeout> | null = null;
  /**
   * Bumped by every invalidation, and compared across a request's own round trip — the same
   * device `edits` is for a save, and here for the same reason: an answer computed before a
   * write cannot be allowed to report the corpus as fresh.
   */
  let graphGeneration = 0;

  let subscribed = false;
  /**
   * Bumped by every edit the editor reports. A save compares it across its own round trip:
   * an edit made while the write was in flight must not be cleared by that write's success,
   * or its debounce finds a clean document and refuses to save it — and the last thing the
   * user typed only ever existed on screen.
   */
  let edits = 0;
  /** Paths this client is writing right now, so its own change events are not news. */
  const writing = new Set<string>();

  const tree = computed(() => buildTree(entries.value));

  /**
   * Folders the user has folded away, by path.
   *
   * It lives here, and not in `DocTree`, for the reason the Explorer keeps `expanded` in
   * its own store: that component is recursive and every `v-if` above it tears it down --
   * opening the search results, leaving the section, or folding a parent -- so a set held
   * in the component comes back empty and the tree springs open again.
   *
   * Collapsed rather than expanded because the whole corpus arrives in one payload with
   * nothing to fetch lazily: a folder is open unless somebody closed it, which keeps the
   * sidebar from looking emptier than the project is.
   *
   * It outlives the page as well: `restoreFolds` reads it back from `localStorage`, per
   * project, and every change after that is written there by `setCollapsed`. Which folders one
   * reader keeps shut is worth nothing to the project and travels with nobody — the reasoning
   * `TaskDetail.vue` gives for the verbosity dial, not §7's for the theme.
   */
  const collapsed = ref<Set<string>>(new Set());
  /** The key the fold state is remembered under — `null` until a project is known. */
  let foldsKey: string | null = null;
  /** What that key holds as this store last read or wrote it, so a no-op writes nothing. */
  let foldsStored = serialiseFolds(new Set());

  /**
   * How the folders of the tree look, by path — only the ones with a colour or an icon (§5.2).
   *
   * Fetched beside the listing rather than inside it: `GET /api/docs` is a list of documents
   * other callers read too, and a folder's look is not a document. A look that fails to
   * arrive is not worth a banner either — the tree simply keeps the one it had.
   */
  const folderStyles = ref<Record<string, DocFolderStyle>>({});
  /**
   * The folders the look was last fetched for, as one string — `null` before the first listing.
   *
   * `load()` runs after every save and on every `team.doc.changed`, and a document written
   * changes no folder's look. What does is a folder appearing or going (a rename, a styled
   * folder pruned with its last document), and a restyle, which has an event of its own — so a
   * listing that brings the same folders asks for nothing more. An agent writing twenty
   * documents costs twenty listings, as it did, and not forty requests.
   */
  let styledFolders: string | null = null;
  /** Folders this client is restyling right now, so the events for its own writes are not news. */
  const styling = new Set<string>();
  /** Style writes run one after another, for the reason saves do — see `chained`. */
  let styleChain: Promise<unknown> = Promise.resolve();
  /**
   * A folder rename this client started is on its way. Its own folder event is ignored until it
   * ends, because the rename adopts the new paths itself, once, when it knows how it went.
   */
  let renamingFolder = false;
  /** Folders this client is deleting right now — the same arrangement, for `deleteFolder`. */
  const deletingFolders = new Set<string>();

  const backlinks = computed<DocBacklink[]>(() => open.value?.backlinks ?? []);

  const saveState = computed<SaveState>(() => {
    if (saveError.value) return "failed";
    if (saving.value) return "saving";
    return dirty.value ? "dirty" : "saved";
  });

  const draftTitle = computed(() =>
    open.value ? liveTitle.value || titleOf("", open.value.path) : "",
  );

  function isCollapsed(path: string): boolean {
    return collapsed.value.has(path);
  }

  function toggleFolder(path: string): void {
    const next = new Set(collapsed.value);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    setCollapsed(next);
  }

  /**
   * Take up the folds this browser remembers for the project at *root*.
   *
   * The session calls it with the project, before any view renders. Until then the set lives
   * in memory alone — which is all a store that never learns its project keeps.
   */
  function restoreFolds(root: string): void {
    foldsKey = `${FOLDS_KEY_PREFIX}${root}`;
    let raw: string | null = null;
    try {
      raw = window.localStorage.getItem(foldsKey);
    } catch {
      // Site data the browser will not hand over remembers nothing, and the tree opens.
    }
    collapsed.value = parseFolds(raw);
    foldsStored = serialiseFolds(collapsed.value);
  }

  /**
   * The only way the set changes once it is restored: the new set, and then storage.
   *
   * A set of nothing folded removes the key rather than writing `[]`, so a project nobody folds
   * anything in leaves no trace in the browser. A write storage refuses (full, or site data
   * blocked) costs the fold after a reload, never the fold on screen — and is tried again with
   * the next change, since `foldsStored` only moves when the write landed.
   */
  function setCollapsed(next: Set<string>): void {
    collapsed.value = next;
    if (foldsKey === null) return;
    const value = serialiseFolds(next);
    if (value === foldsStored) return;
    try {
      if (next.size) window.localStorage.setItem(foldsKey, value);
      else window.localStorage.removeItem(foldsKey);
      foldsStored = value;
    } catch {
      // See above: remembered next time, if storage takes it then.
    }
  }

  /**
   * Resolve a `[[target]]` the way the backend does, but from the list we already hold —
   * every wiki link in the document asks on every render and cannot go to the server.
   */
  function resolveLink(target: string): string | null {
    const wanted = target.trim().replace(/^\/+/, "").toLowerCase();
    if (!wanted) return null;
    const all = entries.value;
    const exact = all.find((entry) => entry.path.toLowerCase() === wanted);
    if (exact) return exact.path;
    const withSuffix = all.find((entry) =>
      [".md", ".markdown", ".txt"].some(
        (suffix) => entry.path.toLowerCase() === wanted + suffix,
      ),
    );
    if (withSuffix) return withSuffix.path;
    const byStem = all.find((entry) => {
      const stem = (entry.path.split("/").pop() ?? "").replace(/\.[^.]+$/, "");
      return stem.toLowerCase() === wanted;
    });
    if (byStem) return byStem.path;
    return (
      all.find((entry) => entry.title.toLowerCase() === wanted)?.path ?? null
    );
  }

  /** Adopt the size that came back with `GET /api/project`. */
  function hydrate(config: DocsConfig | undefined | null): void {
    const chosen = config?.font_size;
    fontSize.value = typeof chosen === "number" ? clampFontSize(chosen) : null;
    const leading = config?.line_height;
    lineHeight.value =
      typeof leading === "number" ? clampLineHeight(leading) : null;
  }

  /**
   * Optimistic, like the theme store: the text resizes under the cursor and the write to
   * `config.yaml` follows. `null` gives the size back to the density knob.
   *
   * A failed write is not worth an error banner over a document the user is reading — the
   * size simply goes back to what is still on disk.
   */
  async function setFontSize(next: number | null): Promise<void> {
    const previous = fontSize.value;
    const size = next === null ? null : clampFontSize(next);
    if (size === previous) return;
    fontSize.value = size;
    try {
      const config = await api.updateSettings({ docs: { font_size: size } });
      const stored = config.docs?.font_size;
      fontSize.value =
        typeof stored === "number" ? clampFontSize(stored) : null;
    } catch {
      fontSize.value = previous;
    }
  }

  /** The leading's twin of `setFontSize` — optimistic, bounded, and reverted on refusal. */
  async function setLineHeight(next: number | null): Promise<void> {
    const previous = lineHeight.value;
    const leading = next === null ? null : clampLineHeight(next);
    if (leading === previous) return;
    lineHeight.value = leading;
    try {
      const config = await api.updateSettings({
        docs: { line_height: leading },
      });
      const stored = config.docs?.line_height;
      lineHeight.value =
        typeof stored === "number" ? clampLineHeight(stored) : null;
    } catch {
      lineHeight.value = previous;
    }
  }

  async function load(): Promise<void> {
    loading.value = true;
    error.value = null;
    // The first listing fetches the look beside it rather than after it: there is nothing yet
    // to compare against, and the tree should not wait on how its folders look.
    const beside = styledFolders === null ? loadFolderStyles() : null;
    let restyle: Promise<void> | null = null;
    try {
      entries.value = await api.docs();
      // Only against a listing that arrived: a folder whose last document moved away is
      // gone, and holding its path would fold whatever later takes the name. That includes
      // a fold remembered from an earlier visit, for a folder removed while the page was shut.
      const folders = folderPaths(entries.value);
      const kept = [...collapsed.value].filter((path) => folders.has(path));
      // `load()` runs after every save, so the set is replaced only when a fold actually went.
      if (kept.length !== collapsed.value.size) setCollapsed(new Set(kept));
      const signature = [...folders].sort().join("\n");
      if (beside === null && signature !== styledFolders) restyle = loadFolderStyles();
      styledFolders = signature;
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause);
    } finally {
      loading.value = false;
    }
    await (beside ?? restyle);
  }

  async function loadFolderStyles(): Promise<void> {
    try {
      const styles = await api.docFolders();
      folderStyles.value = Object.fromEntries(
        styles.map((style) => [style.path, style]),
      );
    } catch {
      // Kept as it was: a folder drawn in yesterday's colour beats a banner over the document.
    }
  }

  function folderStyle(path: string): DocFolderStyle | null {
    return folderStyles.value[path] ?? null;
  }

  /**
   * Change a folder's colour or icon (§5.2) — `null` for either gives it back to the default,
   * and a key left out of `patch` is left alone.
   *
   * Optimistic, like the reading size: the row changes under the pointer and the write follows.
   * Unlike the reading size, a refusal *is* worth a banner — somebody picked a colour, and a
   * `vxs-folder.json` that does not parse is exactly the case they need to hear about.
   */
  function setFolderStyle(
    path: string,
    patch: { color?: string | null; icon?: string | null },
  ): Promise<boolean> {
    let captured: CapturedDocMutation;
    try { captured = captureMutation(); }
    catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause);
      return Promise.resolve(false);
    }
    const previous = folderStyle(path);
    folderStyles.value = withStyle(folderStyles.value, {
      path,
      color: previous?.color ?? null,
      icon: previous?.icon ?? null,
      ...patch,
    });
    const run = async (): Promise<boolean> => {
      styling.add(path);
      try {
        const stored = await mutate(captured, () => captured.fence
          ? api.setDocFolderStyle({ path, ...patch }, captured.fence)
          : api.setDocFolderStyle({ path, ...patch }));
        folderStyles.value = withStyle(folderStyles.value, stored);
        return true;
      } catch (cause) {
        folderStyles.value = withStyle(
          folderStyles.value,
          previous ?? { path, color: null, icon: null },
        );
        error.value = cause instanceof Error ? cause.message : String(cause);
        return false;
      } finally {
        styling.delete(path);
      }
    };
    const next = styleChain.then(run, run);
    styleChain = next;
    return next;
  }

  /**
   * Rename a folder on disk and mend the links into it (§5.2), passing every progress frame to
   * `onFrame` as it arrives.
   *
   * Throws what the rename threw — a refusal before anything moved, or a stream cut off part
   * way — because the dialog showing it has to tell the two apart, and a banner cannot. However
   * it ends, once the folder has moved the new paths are adopted here: the listing, the folded
   * folders and the open document all follow it.
   */
  async function renameFolder(
    path: string,
    name: string,
    onFrame: (frame: DocFolderRenameFrame) => void,
  ): Promise<DocFolderRenameDone> {
    const captured = captureMutation();
    const moved = { to: null as string | null };
    renamingFolder = true;
    try {
      await settle();
      const receive = (frame: DocFolderRenameFrame) => {
        captured.binding?.assertCurrent();
        if (frame.type === "renamed") moved.to = frame.path;
        onFrame(frame);
      };
      return await mutate(captured, () => captured.fence
        ? api.renameDocFolder(path, name, receive, captured.fence)
        : api.renameDocFolder(path, name, receive));
    } finally {
      renamingFolder = false;
      if (moved.to !== null) {
        // A late old-G stream cannot navigate/remount the current editor.
        let current = true;
        try { captured.binding?.assertCurrent(); } catch { current = false; }
        if (current) await adoptFolderRename(path, moved.to);
      }
    }
  }

  /** The paths this client holds, moved to where a folder rename put them. */
  async function adoptFolderRename(from: string, to: string): Promise<void> {
    const document = open.value;
    const path = document ? movedPath(document.path, from, to) : null;
    if (document && path !== document.path && retainsScopedSource()) blockExternalReplacement();
    // Before `load()`, which would otherwise prune the old paths as folders that are gone.
    setCollapsed(
      new Set([...collapsed.value].map((folder) => movedPath(folder, from, to))),
    );
    await load();
    invalidateGraph();
    if (!document || open.value !== document || !path) return;
    // Outside the folder, there is nothing to follow. If its links were rewritten, that arrives
    // as `team.doc.changed` like any other write, and the handler above re-reads it.
    if (path === document.path) return;
    if (retainsScopedSource()) { blockExternalReplacement(); return; }
    // An unsaved edit stays where it is: re-reading a moved document remounts the editor under
    // a new key, which throws the edit away outright.
    if (dirty.value || saving.value) {
      staleOnDisk.value = !fenceError.value;
      return;
    }
    await select(path, true);
  }

  /**
   * Wait for saves to drain, including a newer save queued while the first was in flight.
   * Resolution is not success: callers deciding whether to close must still check dirty,
   * saving and saveError (dev spec §5.3), without retrying/overwriting a refused save.
   */
  async function settle(): Promise<void> {
    let pending: Promise<unknown>;
    do {
      pending = chain;
      await pending.catch(() => undefined);
    } while (pending !== chain);
  }

  let documentRequest = 0;

  async function select(path: string | null, external = false): Promise<void> {
    if (external && retainsScopedSource()) { blockExternalReplacement(); return; }
    const samePath = path !== null && open.value?.path === path;
    // A read is not a new registration and cannot recover a typed admission refusal.
    if (samePath && fenceError.value) return;
    const before = documentRequest;
    const version = edits;
    saveRevision += 1;
    const request = ++documentRequest;
    if (samePath && editorPath === path && editorRevision === before) editorRevision = request;
    if (!path) {
      closeDocument();
      return;
    }
    error.value = null;
    try {
      const fresh = await api.doc(path);
      if (request !== documentRequest) return;
      if (external && retainsScopedSource()) { blockExternalReplacement(); return; }
      if (samePath) {
        if (fenceError.value || edits !== version) return;
        editorBinding?.assertCurrent();
      }
      open.value = fresh;
      liveTitle.value = open.value.title;
      resetEditState();
    } catch (cause) {
      if (request !== documentRequest) return;
      if (external && retainsScopedSource()) {
        if (cause instanceof ApiError && cause.status === 404) blockExternalReplacement();
        return;
      }
      recordFenceError(cause);
      error.value = cause instanceof Error ? cause.message : String(cause);
    }
  }

  function setLive(session: string | null): void {
    live.value = session;
  }

  /**
   * Read the open document again, or let go of it if it is no longer there — what a page
   * does when the room it was editing in ended because the file was replaced or deleted.
   */
  async function reopen(): Promise<void> {
    const path = open.value?.path;
    if (!path || fenceError.value) return;
    const before = documentRequest;
    const version = edits;
    saveRevision += 1;
    const request = ++documentRequest;
    if (editorPath === path && editorRevision === before) editorRevision = request;
    try {
      const fresh = await api.doc(path);
      if (request !== documentRequest || open.value?.path !== path) return;
      if (fenceError.value || edits !== version) return;
      editorBinding?.assertCurrent();
      open.value = fresh;
      liveTitle.value = fresh.title;
      resetEditState();
    } catch (cause) {
      if (request !== documentRequest || open.value?.path !== path) return;
      recordFenceError(cause);
      if (cause instanceof ApiError && cause.status === 404) closeDocument();
      else error.value = cause instanceof Error ? cause.message : String(cause);
    }
  }

  /**
   * Take *content* as the open document's text and owe the file a save for it.
   *
   * For an editor that has just dropped out of a live room with something on screen its
   * own disk has not seen: the text stays, is edited alone from here on, and is written.
   */
  function adopt(content: string, captured?: CapturedDocSave): void {
    if (!open.value || fenceError.value) return;
    // Transition cleanup must never recapture a newer lease/G as a solo save.
    const ticket = captured ?? markDirty();
    if (!ticket || ticket.path !== open.value.path || ticket.revision !== saveRevision) return;
    open.value = { ...open.value, content };
    dirty.value = true;
    void save(content, Object.freeze({ ...ticket, dirty: true }));
  }

  /** The latest facts about the open document, without touching what is being edited. */
  async function refreshOpen(path: string): Promise<void> {
    const document = open.value;
    const session = live.value;
    const request = documentRequest;
    const owner = editorFence;
    try {
      const fresh = await api.doc(path);
      // Still the same document, still in a room: only then is "leave the editor alone,
      // take the rest" the right thing to do with this answer.
      if (open.value === document && open.value?.path === path && live.value === session
        && documentRequest === request && editorFence === owner && !fenceError.value) {
        open.value = editorBinding ? { ...fresh, content: document!.content } : fresh;
      }
    } catch {
      // A read that failed changes nothing here; a delete has its own event.
    }
  }

  function resetEditState(): void {
    dirty.value = false;
    if (!fenceError.value) saveError.value = null;
    staleOnDisk.value = false;
    lastSaveReceipt.value = null;
    materializationPending = false;
    savedRevisions.clear();
  }

  /** No document open — what is left once the open one was deleted. */
  function closeDocument(): void {
    saveRevision += 1;
    documentRequest += 1;
    open.value = null;
    resetEditState();
  }

  /** Raised by the editor on a transaction that changed the document, and by nothing else. */
  function markDirty(): CapturedDocSave | null {
    dirty.value = true;
    edits += 1;
    saveError.value = null;
    try { return captureSave(); }
    catch (cause) { failedSave(cause); return null; }
  }

  function setLiveTitle(title: string): void {
    liveTitle.value = title;
  }

  /**
   * Run a write after any write already on its way.
   *
   * Saves are **chained, never parallel**, and the reason is the conditional write itself:
   * the expectation a save sends is the `updated_at` of the version it read, and that value
   * only arrives with the *previous* save's answer. Two writes started inside one round trip
   * therefore both claim a version that is already gone, and the second is refused — a 409
   * that says "this document changed on disk" about the user's own keystrokes, offering to
   * reload away the very edit that caused it.
   */
  let chain: Promise<unknown> = Promise.resolve();

  function chained<T>(run: () => Promise<T>, fallback: T): Promise<T> {
    const next = chain.then(run, run).catch(() => fallback);
    chain = next;
    return next;
  }

  /**
   * Write the content the editor produced.
   *
   * The conditional write is the point: `expected_updated_at` is what the document was when
   * it was read, so a `write_doc` from an agent — or a branch switch in Plastic — that
   * landed in between is answered with a 409 instead of being overwritten. The editor keeps
   * its state either way (§7): nothing here touches what the user has on screen.
   */
  function save(content: string, captured?: CapturedDocSave, prerequisite?: Promise<unknown>): Promise<boolean> {
    try {
      const ticket = captured ?? captureSave();
      // Enqueue immediately, before waiting on the editor's dirty heartbeat.
      // Folder/move/delete settle() must see this save, not an empty store queue.
      return chained(async () => {
        try { await prerequisite; }
        catch (cause) { failedSave(cause); return false; }
        return writeDocument(content, ticket);
      }, false);
    } catch (cause) {
      failedSave(cause);
      return Promise.resolve(false);
    }
  }

  function failedSave(cause: unknown): void {
    recordFenceError(cause);
    if (fenceError.value) saveError.value = t("docs.remoteEditor.blocked");
    else if (cause instanceof ApiError && cause.isDiskConflict) {
      staleOnDisk.value = !fenceError.value;
      saveError.value = t("docs.saveState.conflict");
    } else saveError.value = cause instanceof Error ? cause.message : String(cause);
  }

  function saveReceipt(saved: DocSaved, ticket: CapturedDocSave): void {
    const fence = ticket.fence;
    if (!fence?.editor_id) return;
    // Keep only exact server evidence. Neither a clean flag nor the client version is a receipt.
    if (saved.path !== ticket.path || saved.editor_id !== fence.editor_id
      || saved.backend_epoch !== fence.backend_epoch
      || saved.binding_generation !== fence.expected_binding_generation
      || saved.version !== ticket.version || !Number.isSafeInteger(saved.last_saved_version)
      || saved.last_saved_version! < 0) {
      throw new ApiError(409, t("docs.remoteEditor.receiptRequired"), "stale_editor_version");
    }
    lastSaveReceipt.value = Object.freeze({ editor_id: saved.editor_id,
      backend_epoch: saved.backend_epoch, binding_generation: saved.binding_generation,
      version: saved.version, last_saved_version: saved.last_saved_version! });
  }

  async function writeDocument(content: string, ticket: CapturedDocSave, overwrite = false): Promise<boolean> {
    if (!ticket.path || (!ticket.dirty && !overwrite)) return false;
    // Navigation/reload is not permission to save old Markdown under the new open document.
    if (saveRevision !== ticket.revision || open.value?.path !== ticket.path) return false;
    if (fenceError.value) return false;
    saving.value = true;
    saveError.value = null;
    lastSaveReceipt.value = null;
    writing.add(ticket.path);
    try {
      const saved = await mutate(ticket, () => {
        const expected = savedRevisions.get(ticket.revision) ?? ticket.expectedUpdatedAt;
        // Preserve the old unactivated wire call; activated writes always carry the exact fence.
        if (overwrite && !ticket.fence) return api.saveDoc(ticket.path!, content);
        const args = [ticket.path!, content, null,
          overwrite || ticket.live ? null : expected, ticket.live] as const;
        return ticket.fence ? api.saveDoc(...args, ticket.fence) : api.saveDoc(...args);
      });
      if (saveRevision !== ticket.revision || open.value?.path !== ticket.path) return false;
      saveReceipt(saved, ticket);
      savedRevisions.set(ticket.revision, saved.updated_at);
      open.value = edits === ticket.version ? saved : { ...saved, content: open.value.content };
      if (edits === ticket.version) {
        dirty.value = false;
        staleOnDisk.value = false;
      }
      await load();
      invalidateGraph();
      return true;
    } catch (cause) {
      failedSave(cause);
      return false;
    } finally {
      saving.value = false;
      writing.delete(ticket.path);
    }
  }

  /** Only an untyped disk conflict permits Keep mine, never an admission refusal. */
  function saveOverwriting(content: string, captured?: CapturedDocSave, prerequisite?: Promise<unknown>): Promise<boolean> {
    if (fenceError.value || materializationPending) return Promise.resolve(false);
    try {
      const ticket = captured ?? captureSave();
      return chained(async () => {
        try { await prerequisite; }
        catch (cause) { failedSave(cause); return false; }
        return writeDocument(content, ticket, true);
      }, false);
    } catch (cause) {
      failedSave(cause);
      return Promise.resolve(false);
    }
  }

  /**
   * Create a document — never over one already there (§5.2). The write is exclusive, so a taken
   * name comes back as a 409 instead of that document emptied down to a heading.
   *
   * `open` is false when a `[[link]]` conjured it (§5): the file has to exist so the link
   * resolves, but the user is mid-sentence in another document and must not be navigated
   * away from it.
   *
   * Throws what the backend refused, for a dialog with a field to show it under; `create` is the
   * same for callers that have only the banner.
   */
  async function createDocument(
    path: string,
    { title, open: openIt = true }: { title?: string; open?: boolean } = {},
  ): Promise<string> {
    const captured = captureMutation();
    if (openIt) await settle();
    const name = title ?? titleOf("", path);
    const created = await mutate(captured, () => captured.fence
      ? api.createDoc(path, `# ${name}\n\n`, captured.fence)
      : api.createDoc(path, `# ${name}\n\n`));
    await load();
    // A new node, and it may resolve somebody's dangling link into a real edge.
    invalidateGraph();
    if (openIt) await select(created.path);
    return created.path;
  }

  /** `createDocument`, with a refusal raised as the banner over the document. */
  async function create(
    path: string,
    options: { title?: string; open?: boolean } = {},
  ): Promise<string | null> {
    saving.value = true;
    error.value = null;
    try {
      return await createDocument(path, options);
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause);
      return null;
    } finally {
      saving.value = false;
    }
  }

  /**
   * Delete a document (§5.2). Throws what the backend refused, for the dialog that asked.
   *
   * A save already on its way lands first: arriving after the delete, it would write the file
   * straight back. The page flushes the editor before it asks, so nothing is pending there.
   */
  async function remove(path: string): Promise<void> {
    const captured = captureMutation();
    await settle();
    // Its own event is not news — for an open document with unsaved edits it would raise
    // "changed on disk" until the answer below closes it.
    writing.add(path);
    try {
      await mutate(captured, () => captured.fence ? api.deleteDoc(path, captured.fence) : api.deleteDoc(path));
    } finally {
      writing.delete(path);
    }
    if (open.value?.path === path) closeDocument();
    await load();
    // A node and every edge touching it are gone.
    invalidateGraph();
  }

  /**
   * Delete a folder with everything in it (§5.2), then let go of what went — the open document
   * if it was inside; the folds and the look follow the listing. Throws what the backend refused,
   * for the dialog that asked.
   *
   * A save already on its way lands first, for `remove`'s reason, and here it would put the
   * folder itself back around one document. A refusal (4xx) removed nothing and re-reads
   * nothing; anything else may have stopped part-way, so the listing is asked whatever it said.
   */
  async function deleteFolder(path: string): Promise<string[]> {
    const captured = captureMutation();
    await settle();
    deletingFolders.add(path);
    let removed = true;
    try {
      return (await mutate(captured, () => captured.fence
        ? api.deleteDocFolder(path, captured.fence) : api.deleteDocFolder(path))).documents;
    } catch (cause) {
      removed = !(cause instanceof StaleRemoteBindingError || cause instanceof StaleProjectResponseError
        || (cause instanceof ApiError && cause.status >= 400 && cause.status < 500));
      throw cause;
    } finally {
      deletingFolders.delete(path);
      if (removed) await forgetFolder(path, { keepEdits: false });
    }
  }

  /**
   * Let go of the open document a deleted folder took — once the listing agrees it is gone, since
   * a delete that stopped part-way put back what it could not remove.
   *
   * `keepEdits` is for a delete made elsewhere: a document with unsaved edits stays on screen,
   * marked as changed on disk, so "Keep mine" can write it back — the rule the
   * `team.doc.changed` handler keeps for a write. A delete made here was asked for over it.
   */
  async function forgetFolder(
    folder: string,
    { keepEdits }: { keepEdits: boolean },
  ): Promise<void> {
    const document = open.value;
    if (keepEdits && document?.path.startsWith(`${folder}/`) && retainsScopedSource()) {
      blockExternalReplacement();
    }
    await load();
    invalidateGraph();
    if (!document?.path.startsWith(`${folder}/`) || open.value !== document) return;
    if (keepEdits && retainsScopedSource()) { blockExternalReplacement(); return; }
    if (entries.value.some((entry) => entry.path === document.path)) return;
    if (keepEdits && (dirty.value || saving.value)) staleOnDisk.value = !fenceError.value;
    else closeDocument();
  }

  /** Rename or move. Returns the documents left pointing at the old name, if any. */
  async function rename(
    source: string,
    target: string,
    captured?: CapturedDocMutation,
  ): Promise<DocBacklink[]> {
    error.value = null;
    try {
      const ticket = captured ?? captureMutation();
      await settle();
      const moved = await mutate(ticket, () => ticket.fence
        ? api.moveDoc(source, target, ticket.fence) : api.moveDoc(source, target));
      await load();
      // The node's path changed, and `referrers` may be stranded on the old name.
      invalidateGraph();
      if (open.value?.path === source) await select(moved.path);
      return moved.referrers;
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause);
      return [];
    }
  }

  async function search(value: string): Promise<void> {
    query.value = value;
    if (!value.trim()) {
      results.value = [];
      return;
    }
    searching.value = true;
    try {
      results.value = await api.searchDocs(value);
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause);
    } finally {
      searching.value = false;
    }
  }

  /**
   * Fetch the graph if what we hold is missing or stale.
   *
   * Safe to call on every expansion of the panel section: a fresh copy answers from memory,
   * and two callers in one tick share one request. Nothing awaits it — its only caller is a
   * template handler, exactly as `setFontSize` is.
   */
  async function loadGraph(): Promise<void> {
    if (graph.value && !graphStale) return;
    if (graphRequest) return graphRequest;
    graphLoading.value = true;
    graphError.value = null;
    // What this request will be able to say about. A write that lands while it is in flight
    // bumps the counter, and the answer — computed by the server *before* that write — then
    // arrives describing a corpus that has already moved. Clearing `graphStale` on it would
    // leave the store believing it holds a fresh copy, and every later `loadGraph` returns
    // at the guard above without a request: the picture stays wrong until some unrelated
    // write happens to invalidate it again.
    const generation = ++graphGeneration;
    graphRequest = (async () => {
      try {
        graph.value = await api.docsGraph();
        if (generation === graphGeneration) graphStale = false;
      } catch (cause) {
        // A picture that failed to load is not worth a banner over a document somebody is
        // reading — the same call `setFontSize` makes when its write is refused. The section
        // says so in its own box, and offers to try again.
        graphError.value = cause instanceof Error ? cause.message : String(cause);
      } finally {
        graphLoading.value = false;
        graphRequest = null;
        // A write that landed mid-flight left the timer unarmed, because arming it while a
        // request is running would only have it short-circuit at the guard above. Now that
        // the slot is free, it is armed here instead.
        //
        // The condition is the *generation*, never `graphStale`: a failure leaves the corpus
        // stale by definition, so re-arming on that would retry every 500 ms for as long as
        // the backend stays down. Only a change that arrived during this request deserves
        // another one, and the reader's Try again is what covers the rest.
        if (generation !== graphGeneration && graph.value) scheduleGraphRefresh();
      }
    })();
    return graphRequest;
  }

  /**
   * The corpus changed, so the graph we hold is out of date.
   *
   * Kept warm only for a reader who has already looked: a graph that was never fetched has
   * nobody watching it, and marking it stale is then the whole cost. That is a deliberate
   * approximation of "is the section open" — cheaper than a retain/release protocol between
   * a presentational component and the store, and its only cost is one background request
   * for someone who expanded the section once and folded it away again.
   *
   * Debounced, because a run that writes twenty documents raises twenty events — the same
   * reason `explorer.file_changed` is debounced before it reaches version control (§9.6).
   */
  function invalidateGraph(): void {
    graphStale = true;
    graphGeneration += 1;
    if (!graph.value) return;
    // A refresh armed now would only reach `loadGraph`'s in-flight guard and return; the
    // `finally` above re-arms it once the slot is free, which is the same timer either way.
    if (graphRequest) return;
    scheduleGraphRefresh();
  }

  function scheduleGraphRefresh(): void {
    if (graphTimer) clearTimeout(graphTimer);
    graphTimer = setTimeout(() => {
      graphTimer = null;
      void loadGraph();
    }, GRAPH_REFRESH_MS);
  }

  /** §5.3: recover a gap without remounting an editor or writing its serialised Markdown. */
  async function resyncOpen(): Promise<void> {
    const document = open.value;
    if (!document || live.value || retainsScopedSource()) return;
    const path = document.path;
    const request = documentRequest;
    const generation = edits;
    const protectedEdit = dirty.value || saving.value;
    const current = () => documentRequest === request && open.value === document && open.value.path === path && !live.value;
    const protectedNow = () => protectedEdit || dirty.value || saving.value || edits !== generation;
    try {
      const fresh = await api.doc(path);
      if (!current()) return;
      const changed = fresh.updated_at !== document.updated_at || fresh.content !== document.content;
      if (retainsScopedSource()) {
        if (changed) blockExternalReplacement();
        return;
      }
      if (protectedNow()) {
        if (changed) staleOnDisk.value = !fenceError.value;
        return;
      }
      // Even an identical assignment can reset the editor/cursor. An unchanged read is a no-op.
      if (!changed) return;
      open.value = fresh;
      liveTitle.value = fresh.title;
    } catch (cause) {
      if (!current()) return;
      if (retainsScopedSource()) {
        if (cause instanceof ApiError && cause.status === 404) blockExternalReplacement();
        return;
      }
      if (cause instanceof ApiError && cause.status === 404) {
        if (protectedNow()) staleOnDisk.value = !fenceError.value;
        else closeDocument();
      } else error.value = cause instanceof Error ? cause.message : String(cause);
    }
  }

  let resyncRequest: Promise<void> | null = null;
  let resyncPending = false;

  function resync(): Promise<void> {
    resyncPending = true;
    resyncRequest ??= Promise.resolve().then(async () => {
      do {
        resyncPending = false;
        invalidateGraph();
        if (graphTimer) clearTimeout(graphTimer);
        graphTimer = null;
        await Promise.all([load(), loadFolderStyles(), resyncOpen(), graph.value ? loadGraph() : undefined]);
      } while (resyncPending);
    }).catch(cause => {
      error.value = cause instanceof Error ? cause.message : String(cause);
    }).finally(() => { resyncRequest = null; });
    return resyncRequest;
  }

  function subscribe(): void {
    if (subscribed) return;
    subscribed = true;

    onEvent("system.dropped", (payload, envelope) => {
      if (payload.resync && !rendererSession.isLegacy && envelope && rendererSession.matches(envelope)) {
        void resync();
      }
    });

    onEvent("team.doc.changed", (payload) => {
      // Our own write comes back as an event too, and it is not news: reporting it as "this
      // document changed on disk" is a warning about the user's own keystrokes, and adopting
      // it re-fetches what we already hold. Which of the two it would have been depends only
      // on whether the socket beat the HTTP response.
      if (writing.has(payload.path)) return;
      void load();
      // After the guard, deliberately: our own write's graph is invalidated by the
      // save itself, so invalidating before it would cost two fetches per save — and
      // invalidating only here would never refresh the picture for the user's own edits.
      invalidateGraph();
      if (open.value?.path !== payload.path) return;
      if (live.value) {
        // In a room this is the room's own Markdown arriving through sync — written by its
        // scribe, already on screen here. Nothing to warn about and nothing to load: the
        // header's facts are refreshed and the editor is left exactly as it is.
        // External delete/reset is not proof that an admitted editor's RAM can be lost.
        if (payload.deleted) {
          if (!rendererSession.isLegacy || editorBinding || dirty.value || saving.value
            || saveError.value || fenceError.value) {
            blockExternalReplacement();
          } else closeDocument();
        } else if (!fenceError.value) void refreshOpen(payload.path);
        return;
      }
      if (retainsScopedSource()) { blockExternalReplacement(); return; }
      // Someone else's write. With nothing local to lose, adopt it — or, for a delete, let the
      // document go, since reading it again is a 404; otherwise say so and let the user
      // decide — the save would be refused anyway (§14.5), and "Keep mine" writes it back.
      if (dirty.value || saving.value) staleOnDisk.value = !fenceError.value;
      else if (payload.deleted) closeDocument();
      else void select(payload.path, true);
    });

    onEvent("sync.conflict", (payload) => {
      // Not lost, but moved: the person has to know their version is now another document.
      // Said after the listing is re-read, because re-reading it clears the banner.
      void load().finally(() => {
        error.value = t("sync.conflict", {
          path: payload.path,
          copy: payload.copy,
        });
      });
    });

    onEvent("team.doc.folder_changed", (payload) => {
      // A rename this client started adopts its own paths when it ends, a look this client is
      // writing is already on screen, and a delete this client asked for lets go of what went
      // itself; none of them coming back over the socket is news.
      if (renamingFolder || styling.has(payload.path) || deletingFolders.has(payload.path)) {
        return;
      }
      if (payload.deleted) void forgetFolder(payload.path, { keepEdits: true });
      else if (payload.previous_path === null) void loadFolderStyles();
      else void adoptFolderRename(payload.previous_path, payload.path);
    });
  }

  return {
    entries,
    tree,
    collapsed,
    open,
    loading,
    saving,
    dirty,
    saveState,
    error,
    saveError,
    staleOnDisk,
    fenceError,
    lastSaveReceipt,
    setEditorFence,
    setEditorLease,
    proveMaterialization,
    captureSave,
    captureMutation,
    live,
    setLive,
    reopen,
    adopt,
    draftTitle,
    backlinks,
    query,
    results,
    searching,
    graph,
    graphLoading,
    graphError,
    fontSize,
    lineHeight,
    folderStyles,
    load,
    folderStyle,
    setFolderStyle,
    renameFolder,
    settle,
    hydrate,
    setFontSize,
    setLineHeight,
    select,
    markDirty,
    setLiveTitle,
    save,
    saveOverwriting,
    createDocument,
    create,
    remove,
    deleteFolder,
    rename,
    search,
    isCollapsed,
    toggleFolder,
    restoreFolds,
    resolveLink,
    loadGraph,
    invalidateGraph,
    subscribe,
  };
});
