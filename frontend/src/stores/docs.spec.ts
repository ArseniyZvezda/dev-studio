/**
 * The document store (block-editor spec §7, §8.2).
 *
 * This is the last thing between the app and somebody's file, and its rules are stated as
 * refusals: a document nobody edited is never written, a save that would land on top of
 * another write is refused rather than forced, and a link resolves the way the backend
 * resolves it. Each of those has cost a file once, in this project or in the editor above it.
 *
 * The API is a stub, so what is under test is the store's decisions rather than HTTP. Pinia
 * needs no DOM, which is what lets this run in the same node environment as the rest.
 */

import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, createApiClient } from "@/api/client";
import { t } from "@/i18n";
import { LINE_HEIGHT } from "@/stores/docs";
import { useSyncStore } from "@/stores/sync";
import type {
  DocDetail,
  DocEntry,
  DocFolderDeleted,
  DocFolderRenameFrame,
  DocFolderStyle,
  DocumentFence,
  ProjectRemoteStatus,
  ProjectRemoteEditorLease,
} from "@/api/types";

const api = {
  docs: vi.fn(),
  doc: vi.fn(),
  saveDoc: vi.fn(),
  createDoc: vi.fn(),
  deleteDoc: vi.fn(),
  deleteDocFolder: vi.fn(),
  moveDoc: vi.fn(),
  searchDocs: vi.fn(),
  docsGraph: vi.fn(),
  docFolders: vi.fn(),
  setDocFolderStyle: vi.fn(),
  renameDocFolder: vi.fn(),
  updateSettings: vi.fn(),
  projectRemote: vi.fn(),
  materializeRemoteEditor: vi.fn(),
  confirmRemoteEditorMaterialization: vi.fn(),
};

vi.mock("@/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ApiError: actual.ApiError,
    createApiClient: vi.fn(() => api),
    api: new Proxy(
      {},
      { get: (_, key: string) => api[key as keyof typeof api] },
    ),
  };
});

// One socket, owned by the app; the store only subscribes to it. The handlers are kept so a
// check can play an event the way the socket would — `mock` prefix because `vi.mock` is
// hoisted above everything else in the file.
type MockHandler = (payload: Record<string, unknown>, envelope?: { project_id?: string; epoch?: string }) => void;
const mockWsHandlers = new Map<string, MockHandler>();
vi.mock("@/ws/connection", () => ({
  onEvent: (name: string, handler: MockHandler) => {
    mockWsHandlers.set(name, handler);
  },
}));

const mockSession = vi.hoisted(() => ({
  isLegacy: true,
  scope: null as { project_id: string; epoch: string } | null,
  capture: () => () => undefined,
  matches: (scope: { project_id?: string; epoch?: string }) => scope.project_id === 'project_a' && scope.epoch === 'epoch_a',
}));
vi.mock('@/session/project-session', async importOriginal => ({
  ...await importOriginal<typeof import('@/session/project-session')>(),
  rendererSession: mockSession,
}));

const {
  FOLDS_KEY_PREFIX,
  FONT_SIZE,
  GRAPH_REFRESH_MS,
  docsVcsRoot,
  documentPath,
  movedPath,
  useDocsStore,
} = await import("./docs");

function entry(path: string, title = path): DocEntry {
  return {
    path,
    title,
    size_bytes: 10,
    updated_at: "2026-08-04T10:00:00+00:00",
  };
}

function detail(path: string, content = "# Doc\n"): DocDetail {
  return { ...entry(path), content, links: [], backlinks: [] };
}

/** Let anything already queued run — saves are chained, so they start a turn late. */
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  vi.mocked(createApiClient).mockClear();
  mockSession.isLegacy = true;
  mockSession.scope = null;
  setActivePinia(createPinia());
  mockWsHandlers.clear();
  for (const stub of Object.values(api)) stub.mockReset();
  api.docs.mockResolvedValue([entry("features/dash.md", "Dash ability")]);
  api.docsGraph.mockResolvedValue({ nodes: [], edges: [] });
  api.docFolders.mockResolvedValue([]);
});

describe('copied document paths in separate renderers (§5.3.2)', () => {
  it('keeps a delayed read in A out of B even when both open the same path', async () => {
    const path = 'features/dash.md';
    const a = useDocsStore(createPinia());
    const b = useDocsStore(createPinia());
    let finishA!: (document: DocDetail) => void;
    api.doc.mockReturnValueOnce(new Promise<DocDetail>(resolve => { finishA = resolve; }));
    api.doc.mockResolvedValueOnce(detail(path, '# Project B\n'));

    const readingA = a.select(path);
    await b.select(path);
    b.markDirty();
    b.setLiveTitle('B unsaved title');
    const originalB = b.open;

    finishA(detail(path, '# Project A\n'));
    await readingA;
    expect(a.open?.content).toBe('# Project A\n');
    expect(a.dirty).toBe(false);
    expect(b.open).toBe(originalB);
    expect(b.open?.content).toBe('# Project B\n');
    expect(b.dirty).toBe(true);
    expect(b.draftTitle).toBe('B unsaved title');
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it('does not share save queues or apply a delayed A save to the copied path in B', async () => {
    const path = 'features/dash.md';
    const a = useDocsStore(createPinia());
    const b = useDocsStore(createPinia());
    api.doc.mockResolvedValueOnce(detail(path, '# Project A\n'));
    api.doc.mockResolvedValueOnce(detail(path, '# Project B\n'));
    await a.select(path);
    await b.select(path);
    let finishA!: (document: DocDetail) => void;
    api.saveDoc.mockReturnValueOnce(new Promise<DocDetail>(resolve => { finishA = resolve; }));
    api.saveDoc.mockResolvedValueOnce(detail(path, '# B saved\n'));

    a.markDirty();
    const savingA = a.save('# A saved\n');
    await tick();
    expect(a.saving).toBe(true);
    b.markDirty();
    expect(await b.save('# B saved\n')).toBe(true);
    expect(a.saving).toBe(true);
    expect(b.saving).toBe(false);
    b.markDirty();
    b.setLiveTitle('B next unsaved edit');
    const originalB = b.open;

    finishA(detail(path, '# A saved\n'));
    expect(await savingA).toBe(true);
    expect(a.open?.content).toBe('# A saved\n');
    expect(a.dirty).toBe(false);
    expect(b.open).toBe(originalB);
    expect(b.open?.content).toBe('# B saved\n');
    expect(b.dirty).toBe(true);
    expect(b.draftTitle).toBe('B next unsaved edit');
    expect(api.saveDoc.mock.calls.map(call => call.slice(0, 2))).toEqual([
      [path, '# A saved\n'],
      [path, '# B saved\n'],
    ]);
  });
});

describe('scoped gap recovery', () => {
  const scope = { project_id: 'project_a', epoch: 'epoch_a' };
  const gap = () => mockWsHandlers.get('system.dropped')!({ count: 3, resync: true }, scope);

  it('recovers listing, warm graph and clean document without saving', async () => {
    mockSession.isLegacy = false;
    const store = useDocsStore();
    store.subscribe();
    api.doc.mockResolvedValue(detail('one.md'));
    await store.select('one.md');
    await store.loadGraph();
    api.docs.mockResolvedValue([entry('one.md'), entry('new.md')]);
    api.doc.mockResolvedValue(detail('one.md', '# External edit\n'));
    api.docsGraph.mockResolvedValue({ nodes: [{ path: 'new.md' }], edges: [] });
    gap();
    await tick();
    expect(store.entries.map(doc => doc.path)).toEqual(['one.md', 'new.md']);
    expect(store.graph?.nodes).toEqual([{ path: 'new.md' }]);
    expect(store.open?.content).toBe('# External edit\n');
    expect(store.dirty).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it('leaves an unchanged clean document object untouched', async () => {
    mockSession.isLegacy = false;
    const store = useDocsStore();
    store.subscribe();
    api.doc.mockResolvedValue(detail('one.md'));
    await store.select('one.md');
    const original = store.open;
    gap();
    await tick();
    expect(store.open).toBe(original);
    expect(store.dirty).toBe(false);
    expect(await store.save('not an edit')).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it.each(['dirty', 'saving', 'live'] as const)('does not replace a %s document or clear its editor state', async state => {
    mockSession.isLegacy = false;
    const store = useDocsStore();
    store.subscribe();
    api.doc.mockResolvedValue(detail('one.md'));
    await store.select('one.md');
    const original = store.open;
    if (state === 'dirty') store.markDirty();
    if (state === 'saving') store.saving = true;
    if (state === 'live') store.setLive('room-one');
    api.doc.mockResolvedValue(detail('one.md', '# Changed externally'));
    gap();
    await tick();
    expect(store.open).toBe(original);
    // A scoped edit without metadata/registration is fenced, never offered Keep mine.
    expect(store.staleOnDisk).toBe(state === 'saving');
    expect(store.dirty).toBe(state === 'dirty');
    expect(store.saving).toBe(state === 'saving');
    expect(store.live).toBe(state === 'live' ? 'room-one' : null);
    expect(api.doc).toHaveBeenCalledTimes(state === 'live' ? 1 : 2);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it('protects an edit typed during the recovery read', async () => {
    mockSession.isLegacy = false;
    const store = useDocsStore();
    store.subscribe();
    api.doc.mockResolvedValue(detail('one.md'));
    await store.select('one.md');
    const original = store.open;
    let resolve!: (doc: DocDetail) => void;
    api.doc.mockReturnValueOnce(new Promise<DocDetail>(done => { resolve = done; }));
    gap();
    await tick();
    store.markDirty();
    store.setLiveTitle('My draft title');
    resolve(detail('one.md', '# External'));
    await tick();
    expect(store.open).toBe(original);
    expect(store.dirty).toBe(true);
    expect(store.staleOnDisk).toBe(false);
    expect(store.fenceError).not.toBeNull();
    expect(store.draftTitle).toBe('My draft title');
  });

  it('does not navigate back after a delayed recovery or clear a deleted dirty draft', async () => {
    mockSession.isLegacy = false;
    const store = useDocsStore();
    store.subscribe();
    api.doc.mockResolvedValue(detail('one.md'));
    await store.select('one.md');
    let resolve!: (doc: DocDetail) => void;
    api.doc.mockReturnValueOnce(new Promise<DocDetail>(done => { resolve = done; }));
    gap();
    await tick();
    api.doc.mockResolvedValueOnce(detail('two.md'));
    await store.select('two.md');
    resolve(detail('one.md', '# Old selection'));
    await tick();
    expect(store.open?.path).toBe('two.md');
    store.markDirty();
    api.doc.mockRejectedValueOnce(new ApiError(404, 'deleted'));
    gap();
    await tick();
    expect(store.open?.path).toBe('two.md');
    expect(store.dirty).toBe(true);
    expect(store.staleOnDisk).toBe(false);
    expect(store.fenceError).not.toBeNull();
  });

  it('ignores foreign, obsolete, unscoped and legacy gaps', async () => {
    const store = useDocsStore();
    store.subscribe();
    gap();
    mockSession.isLegacy = false;
    const handler = mockWsHandlers.get('system.dropped')!;
    handler({ resync: true }, { ...scope, project_id: 'project_b' });
    handler({ resync: true }, { ...scope, epoch: 'old' });
    handler({ resync: true });
    handler({ resync: false }, scope);
    await tick();
    expect(api.docs).not.toHaveBeenCalled();
  });

  it('coalesces a burst, handles rejection and permits the next recovery', async () => {
    mockSession.isLegacy = false;
    const store = useDocsStore();
    store.subscribe();
    api.docs.mockRejectedValueOnce(new Error('offline'));
    gap(); gap(); gap();
    await tick();
    expect(api.docs).toHaveBeenCalledTimes(1);
    expect(store.error).toBe('offline');
    gap();
    await tick();
    expect(api.docs).toHaveBeenCalledTimes(2);
    expect(store.entries).toHaveLength(1);
  });

  it('queues one follow-up for gaps arriving during a read, without parallel recoveries', async () => {
    mockSession.isLegacy = false;
    const store = useDocsStore();
    store.subscribe();
    let resolve!: (docs: DocEntry[]) => void;
    api.docs.mockReturnValueOnce(new Promise<DocEntry[]>(done => { resolve = done; }));
    gap();
    await tick();
    gap(); gap(); gap();
    await tick();
    expect(api.docs).toHaveBeenCalledTimes(1);
    resolve([entry('old.md')]);
    await tick();
    expect(api.docs).toHaveBeenCalledTimes(2);
    expect(store.entries[0]?.path).toBe('features/dash.md');
  });

  it('only marks a dirty draft stale when disk changed and closes a deleted clean document', async () => {
    mockSession.isLegacy = false;
    const store = useDocsStore();
    store.subscribe();
    api.doc.mockResolvedValue(detail('one.md'));
    await store.select('one.md');
    // This case exercises read recovery, not edit admission without a registered lease.
    store.dirty = true;
    gap();
    await tick();
    expect(store.dirty).toBe(true);
    expect(store.staleOnDisk).toBe(false);
    await store.select('one.md');
    api.doc.mockRejectedValueOnce(new ApiError(404, 'deleted'));
    gap();
    await tick();
    expect(store.open).toBeNull();
    expect(api.saveDoc).not.toHaveBeenCalled();
  });
});

describe("settling saves before desktop close (§5.3)", () => {
  const path = "features/dash.md";

  async function opened() {
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail(path));
    await store.select(path);
    store.setLive("room-before-close");
    return store;
  }

  it("does not write a clean open document or leave its room", async () => {
    const store = await opened();
    const document = store.open;
    await store.settle();
    expect(store.open).toBe(document);
    expect(store.live).toBe("room-before-close");
    expect(store.saveState).toBe("saved");
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it("waits for a delayed save without clearing the room or the dirty state early", async () => {
    const store = await opened();
    let finish!: (document: DocDetail) => void;
    api.saveDoc.mockReturnValueOnce(new Promise<DocDetail>(resolve => { finish = resolve; }));
    store.markDirty();
    const saving = store.save("# Last edit\n");
    let settled = false;
    const settling = store.settle().then(() => { settled = true; });
    await tick();
    expect(settled).toBe(false);
    expect(store.dirty).toBe(true);
    expect(store.saving).toBe(true);
    expect(store.live).toBe("room-before-close");
    expect(api.saveDoc.mock.calls[0][4]).toBe("room-before-close");

    finish(detail(path, "# Last edit\n"));
    await settling;
    expect(await saving).toBe(true);
    expect(store.saveState).toBe("saved");
    expect(store.live).toBe("room-before-close");
  });

  it("also waits for a newer save queued after settling started", async () => {
    const store = await opened();
    let finishFirst!: (document: DocDetail) => void;
    let finishSecond!: (document: DocDetail) => void;
    api.saveDoc.mockReturnValueOnce(new Promise<DocDetail>(resolve => { finishFirst = resolve; }));
    api.saveDoc.mockReturnValueOnce(new Promise<DocDetail>(resolve => { finishSecond = resolve; }));
    store.markDirty();
    void store.save("# First\n");
    let settled = false;
    const settling = store.settle().then(() => { settled = true; });
    await tick();
    store.markDirty();
    void store.save("# First and second\n");
    finishFirst(detail(path, "# First\n"));
    await tick();
    expect(api.saveDoc).toHaveBeenCalledTimes(2);
    expect(settled).toBe(false);
    expect(store.saving).toBe(true);

    finishSecond(detail(path, "# First and second\n"));
    await settling;
    expect(store.open?.content).toBe("# First and second\n");
    expect(store.saveState).toBe("saved");
  });

  it.each([409, 500])("settles a failed %s save without treating it as success or retrying", async status => {
    const store = await opened();
    api.saveDoc.mockRejectedValueOnce(new ApiError(status, "save refused"));
    store.markDirty();
    const saving = store.save("# Only in the editor\n");
    await store.settle();
    expect(await saving).toBe(false);
    expect(store.saving).toBe(false);
    expect(store.dirty).toBe(true);
    expect(store.saveError).not.toBeNull();
    expect(store.saveState).toBe("failed");
    expect(store.live).toBe("room-before-close");
    await store.settle();
    expect(api.saveDoc).toHaveBeenCalledTimes(1);
  });

  it("leaves a newer editor edit dirty even once the preceding save has settled", async () => {
    const store = await opened();
    let finish!: (document: DocDetail) => void;
    api.saveDoc.mockReturnValueOnce(new Promise<DocDetail>(resolve => { finish = resolve; }));
    store.markDirty();
    void store.save("# Earlier edit\n");
    const settling = store.settle();
    await tick();
    store.markDirty();
    finish(detail(path, "# Earlier edit\n"));
    await settling;
    expect(store.dirty).toBe(true);
    expect(store.saving).toBe(false);
    expect(store.saveState).toBe("dirty");
    expect(api.saveDoc).toHaveBeenCalledTimes(1);
  });

  it("keeps newer typing dirty after an explicit conflict overwrite settles", async () => {
    const store = await opened();
    let finish!: (document: DocDetail) => void;
    api.saveDoc.mockReturnValueOnce(new Promise<DocDetail>(resolve => { finish = resolve; }));
    store.markDirty();
    const saving = store.saveOverwriting("# Earlier overwrite\n");
    await tick();
    store.markDirty();
    finish(detail(path, "# Earlier overwrite\n"));
    await store.settle();
    expect(await saving).toBe(true);
    expect(store.dirty).toBe(true);
    expect(store.saveState).toBe("dirty");
    api.saveDoc.mockResolvedValueOnce(detail(path, "# Newer typing\n"));
    expect(await store.save("# Newer typing\n")).toBe(true);
    expect(store.dirty).toBe(false);
  });
});

describe("saving", () => {
  it("refuses to write a document nobody edited (§8.2)", async () => {
    // Opening ten documents and closing them must leave version control clean. The editor
    // guards this too; the invariant belongs here because this is what touches the file.
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail("features/dash.md"));
    await store.select("features/dash.md");

    expect(await store.save("# Anything\n")).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it("writes once the editor says the user changed something", async () => {
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail("features/dash.md"));
    api.saveDoc.mockResolvedValue(detail("features/dash.md", "# Edited\n"));
    await store.select("features/dash.md");
    store.markDirty();

    expect(await store.save("# Edited\n")).toBe(true);
    // The `updated_at` it read goes back as the expectation — that is what makes the write
    // conditional rather than a blind overwrite.
    expect(api.saveDoc).toHaveBeenCalledWith(
      "features/dash.md",
      "# Edited\n",
      null,
      "2026-08-04T10:00:00+00:00",
      // No room: an ordinary save, which sync pushes as an ordinary edit (§16.4).
      null,
    );
    expect(store.dirty).toBe(false);
    expect(store.saveState).toBe("saved");
  });

  it("reports a refused write instead of forcing it (§14.5)", async () => {
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail("features/dash.md"));
    api.saveDoc.mockRejectedValue(new ApiError(409, "changed on disk"));
    await store.select("features/dash.md");
    store.markDirty();

    expect(await store.save("# Edited\n")).toBe(false);
    expect(store.staleOnDisk).toBe(true);
    // Still dirty: the editor's content is the only copy of that edit now.
    expect(store.dirty).toBe(true);
    expect(store.saveState).toBe("failed");
  });

  it("keeps the document dirty when the file could not be written", async () => {
    // A read-only file, or the backend restarting. Nothing is pending in the editor at this
    // point, so `Ctrl+S` asking the store again is the only way back — and it only works
    // because the flag survives the failure.
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail("features/dash.md"));
    api.saveDoc.mockRejectedValue(new ApiError(500, "Access is denied"));
    await store.select("features/dash.md");
    store.markDirty();

    expect(await store.save("# Edited\n")).toBe(false);
    expect(store.dirty).toBe(true);
    expect(store.saveError).toContain("Access is denied");

    api.saveDoc.mockResolvedValue(detail("features/dash.md", "# Edited\n"));
    expect(await store.save("# Edited\n")).toBe(true);
    expect(store.saveState).toBe("saved");
  });

  it("forces a save past the refusal when the user keeps their copy", async () => {
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail("features/dash.md"));
    api.saveDoc.mockResolvedValue(detail("features/dash.md", "# Mine\n"));
    await store.select("features/dash.md");

    expect(await store.saveOverwriting("# Mine\n")).toBe(true);
    // No expectation sent: that is what "overwrite whatever is there now" means.
    expect(api.saveDoc).toHaveBeenCalledWith("features/dash.md", "# Mine\n");
    expect(store.staleOnDisk).toBe(false);
  });

  it("keeps an edit made while the save was in flight (§8.2)", async () => {
    // Type, pause long enough for the autosave, then carry on typing while the write is on
    // its way. Clearing `dirty` when it lands would make the next debounce find a clean
    // document and refuse to write it — and the sentence the user finished exists only on
    // screen until they happen to touch the file again.
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail("features/dash.md"));
    await store.select("features/dash.md");
    store.markDirty();

    let land: (value: DocDetail) => void = () => {};
    api.saveDoc.mockReturnValue(
      new Promise<DocDetail>((resolve) => (land = resolve)),
    );
    const inFlight = store.save("# One\n");
    await tick(); // saves are chained, so the write starts a turn after it is asked for

    store.markDirty(); // the keystroke that arrives mid-round-trip
    land(detail("features/dash.md", "# One\n"));
    await inFlight;

    expect(store.dirty).toBe(true);
    expect(store.saveState).toBe("dirty");

    // …and that second edit does get written when its own debounce fires.
    api.saveDoc.mockResolvedValue(detail("features/dash.md", "# One two\n"));
    expect(await store.save("# One two\n")).toBe(true);
    expect(store.dirty).toBe(false);
  });

  it("chains writes so the second carries the version the first stored", async () => {
    // The expectation a save sends only arrives with the previous save's answer. Two writes
    // inside one round trip would both claim a version that is already gone, and the backend
    // would refuse the second — a "this document changed on disk" about the user's own
    // typing, offering to reload away the edit that caused it.
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail("features/dash.md"));
    await store.select("features/dash.md");
    store.markDirty();

    let land: (value: DocDetail) => void = () => {};
    api.saveDoc.mockReturnValueOnce(
      new Promise<DocDetail>((resolve) => (land = resolve)),
    );
    const first = store.save("# One\n");
    await tick();

    store.markDirty(); // the editor only emits again after another edit
    const second = store.save("# One two\n");
    await tick();
    expect(
      api.saveDoc,
      "the second write waits its turn",
    ).toHaveBeenCalledTimes(1);

    api.saveDoc.mockResolvedValue({
      ...detail("features/dash.md", "# One two\n"),
      updated_at: "2026-08-04T10:00:05+00:00",
    });
    land({
      ...detail("features/dash.md", "# One\n"),
      updated_at: "2026-08-04T10:00:01+00:00",
    });
    await Promise.all([first, second]);

    expect(api.saveDoc).toHaveBeenCalledTimes(2);
    expect(api.saveDoc).toHaveBeenLastCalledWith(
      "features/dash.md",
      "# One two\n",
      null,
      "2026-08-04T10:00:01+00:00",
      null,
    );
  });

  it("does not adopt the answer to a save for a document that is no longer open", async () => {
    // A pending save lands after the user has clicked on to the next document.
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail("features/dash.md"));
    await store.select("features/dash.md");
    store.markDirty();

    api.saveDoc.mockResolvedValue(detail("features/dash.md", "# Edited\n"));
    api.doc.mockResolvedValue(detail("qa/test-plan.md"));
    const pending = store.save("# Edited\n");
    await store.select("qa/test-plan.md");
    await pending;

    expect(store.open?.path).toBe("qa/test-plan.md");
    expect(store.open?.content).toBe("# Doc\n");
  });
});

describe("a document open in a live room (dev spec §16.4)", () => {
  async function inRoom() {
    const store = useDocsStore();
    api.docs.mockResolvedValue([entry("features/dash.md")]);
    api.doc.mockResolvedValue(detail("features/dash.md"));
    await store.select("features/dash.md");
    store.subscribe();
    store.setLive("sid-1");
    return store;
  }
  const changed = (payload: Record<string, unknown>) =>
    mockWsHandlers.get("team.doc.changed")?.(payload);

  it("is written unconditionally, and marked as the room's own", async () => {
    const store = await inRoom();
    api.saveDoc.mockResolvedValue(detail("features/dash.md", "# Room\n"));
    store.markDirty();

    expect(await store.save("# Room\n")).toBe(true);
    // No expectation: the file under it is the same room's Markdown, written by sync from
    // whoever was scribe before, and refusing to replace it would stop the file following.
    expect(api.saveDoc).toHaveBeenCalledWith(
      "features/dash.md",
      "# Room\n",
      null,
      null,
      "sid-1",
    );
  });

  it("still marks a save as the room's when the room is left right after it", async () => {
    const store = await inRoom();
    api.saveDoc.mockResolvedValue(detail("features/dash.md", "# Last\n"));
    store.markDirty();
    // The page saves and then leaves, in that order — and clears the room only once the
    // write has been sent, which is what `settle` is for.
    const saving = store.save("# Last\n");
    await store.settle();
    store.setLive(null);
    await saving;
    expect(api.saveDoc.mock.calls[0][4]).toBe("sid-1");
  });

  it("takes the room's Markdown arriving through sync without touching the editor", async () => {
    const store = await inRoom();
    store.markDirty();
    api.doc.mockResolvedValue({
      ...detail("features/dash.md", "# From the scribe\n"),
      updated_at: "2026-08-04T10:00:09+00:00",
    });

    changed({ path: "features/dash.md", title: "Dash", author: "remote:bob" });
    await vi.waitFor(() =>
      expect(store.open?.updated_at).toBe("2026-08-04T10:00:09+00:00"),
    );
    // Not a warning about a file changed on disk, and the pending save is still owed.
    expect(store.staleOnDisk).toBe(false);
    expect(store.dirty).toBe(true);
  });

  it("lets go of a document deleted from under the room", async () => {
    const store = await inRoom();
    changed({ path: "features/dash.md", title: null, author: "remote:bob", deleted: true });
    expect(store.open).toBeNull();
  });

  it("reopens the file when the room ended, or closes it if the file is gone", async () => {
    const store = await inRoom();
    store.setLive(null);
    store.markDirty();
    api.doc.mockResolvedValue(detail("features/dash.md", "# Replaced by an agent\n"));
    await store.reopen();
    expect(store.open?.content).toBe("# Replaced by an agent\n");
    expect(store.dirty).toBe(false);

    api.doc.mockRejectedValue(new ApiError(404, "gone"));
    await store.reopen();
    expect(store.open).toBeNull();
    expect(store.error).toBeNull();
  });

  it("keeps what was on screen when the room is lost, and owes the file a save", async () => {
    const store = await inRoom();
    store.setLive(null);
    api.saveDoc.mockResolvedValue(detail("features/dash.md", "# On screen\n"));

    store.adopt("# On screen\n");
    expect(store.open?.content).toBe("# On screen\n");
    expect(store.dirty).toBe(true);
    await store.settle();
    // Alone again, so conditional again: a file that moved meanwhile is a real conflict.
    expect(api.saveDoc).toHaveBeenCalledWith(
      "features/dash.md",
      "# On screen\n",
      null,
      "2026-08-04T10:00:00+00:00",
      null,
    );
  });

  it("says where a conflicting version went", async () => {
    const store = await inRoom();
    mockWsHandlers.get("sync.conflict")?.({
      path: "features/dash.md",
      copy: "features/dash (conflict bob).md",
      by: "alice",
    });
    // The copy is a new document in the tree, and the banner survives the re-read.
    await vi.waitFor(() =>
      expect(store.error ?? "").toContain("features/dash (conflict bob).md"),
    );
  });
});

describe("resolving a [[wiki link]] from the list already held", () => {
  beforeEach(() => {
    api.docs.mockResolvedValue([
      entry("features/dash.md", "Dash ability"),
      entry("qa/test-plan.md", "Test plan"),
      entry("notes/Meeting Notes.md", "Meeting notes"),
    ]);
  });

  it("matches a bare name, a path, and a title, case-insensitively", async () => {
    const store = useDocsStore();
    await store.load();

    expect(store.resolveLink("dash")).toBe("features/dash.md");
    expect(store.resolveLink("features/dash.md")).toBe("features/dash.md");
    expect(store.resolveLink("features/dash")).toBe("features/dash.md");
    expect(store.resolveLink("DASH")).toBe("features/dash.md");
    expect(store.resolveLink("Test plan")).toBe("qa/test-plan.md");
    expect(store.resolveLink("Meeting Notes")).toBe("notes/Meeting Notes.md");
  });

  it("answers null for a link to a document that does not exist yet", async () => {
    const store = useDocsStore();
    await store.load();
    expect(store.resolveLink("combat-tuning")).toBeNull();
    expect(store.resolveLink("   ")).toBeNull();
  });
});

describe("the tree the sidebar draws", () => {
  it("nests documents under their folders, folders first", async () => {
    const store = useDocsStore();
    api.docs.mockResolvedValue([
      entry("qa/test-plan.md"),
      entry("README.md"),
      entry("features/dash.md"),
      entry("features/deep/nested.md"),
    ]);
    await store.load();

    expect(store.tree.map((node) => node.name)).toEqual([
      "features",
      "qa",
      "README.md",
    ]);
    const features = store.tree[0];
    expect(features.isFolder).toBe(true);
    expect(features.children.map((node) => node.name)).toEqual([
      "deep",
      "dash.md",
    ]);
  });
});

describe("which folders the sidebar has folded away", () => {
  it("outlives the tree component, which every `v-if` above it destroys", async () => {
    const store = useDocsStore();
    api.docs.mockResolvedValue([entry("features/dash.md")]);
    await store.load();

    expect(store.isCollapsed("features")).toBe(false);
    store.toggleFolder("features");
    expect(store.isCollapsed("features")).toBe(true);

    // What re-mounting `DocTree` used to reset: the list is re-read, nothing folded moved.
    await store.load();
    expect(store.isCollapsed("features")).toBe(true);
  });

  it("forgets a folder that is gone, so one later given the name opens", async () => {
    const store = useDocsStore();
    api.docs.mockResolvedValue([entry("features/dash.md")]);
    await store.load();
    store.toggleFolder("features");

    api.docs.mockResolvedValue([entry("dash.md")]);
    await store.load();
    expect(store.collapsed.size).toBe(0);

    api.docs.mockResolvedValue([entry("features/sprint.md")]);
    await store.load();
    expect(store.isCollapsed("features")).toBe(false);
  });

  it("keeps the fold state when the list could not be re-read", async () => {
    const store = useDocsStore();
    api.docs.mockResolvedValue([entry("features/dash.md")]);
    await store.load();
    store.toggleFolder("features");

    api.docs.mockRejectedValue(new Error("offline"));
    await store.load();
    expect(store.isCollapsed("features")).toBe(true);
  });
});

describe("which folders stay folded across a reload (§5.2)", () => {
  const ROOT = "D:\\Games\\Mine";
  const KEY = `${FOLDS_KEY_PREFIX}${ROOT}`;

  /** `localStorage` as the store reaches for it — this runs in node, which has none. */
  function memoryStorage() {
    const items = new Map<string, string>();
    return {
      items,
      getItem: vi.fn((key: string): string | null => items.get(key) ?? null),
      setItem: vi.fn((key: string, value: string): void => {
        items.set(key, value);
      }),
      removeItem: vi.fn((key: string): void => {
        items.delete(key);
      }),
    };
  }

  let local: ReturnType<typeof memoryStorage>;

  beforeEach(() => {
    local = memoryStorage();
    vi.stubGlobal("window", { localStorage: local });
    api.docs.mockResolvedValue([entry("features/dash.md"), entry("qa/plan.md")]);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** A page load: stores built from nothing, then told which project is open. */
  function reload(root = ROOT) {
    setActivePinia(createPinia());
    const store = useDocsStore();
    store.restoreFolds(root);
    return store;
  }

  it("comes back folded, before the listing has even arrived", async () => {
    const before = reload();
    await before.load();
    before.toggleFolder("features");
    expect(local.items.get(KEY)).toBe('["features"]');

    const after = reload();
    // Folded from the first frame — a tree drawn open and then snapped shut is the bug.
    expect(after.isCollapsed("features")).toBe(true);
    await after.load();
    expect(after.isCollapsed("features")).toBe(true);
    expect(after.isCollapsed("qa")).toBe(false);
  });

  it("is remembered per project, because a folder is only a path", async () => {
    const mine = reload();
    await mine.load();
    mine.toggleFolder("features");

    expect(reload("D:\\Games\\Other").isCollapsed("features")).toBe(false);
    expect(reload().isCollapsed("features")).toBe(true);
  });

  it("prunes a late A listing only under A's root while B keeps the copied folders", async () => {
    const otherRoot = "D:\\Games\\Other";
    const otherKey = `${FOLDS_KEY_PREFIX}${otherRoot}`;
    local.items.set(KEY, '["features","gone"]');
    local.items.set(otherKey, '["qa"]');
    const a = reload();
    let finishA!: (documents: DocEntry[]) => void;
    api.docs.mockReturnValueOnce(new Promise<DocEntry[]>(resolve => { finishA = resolve; }));
    const loadingA = a.load();

    const b = reload(otherRoot);
    await b.load();
    b.toggleFolder("features");
    expect(local.items.get(otherKey)).toBe('["features","qa"]');
    finishA([entry("features/dash.md")]);
    await loadingA;

    expect(local.items.get(KEY)).toBe('["features"]');
    expect(local.items.get(otherKey)).toBe('["features","qa"]');
    expect([...b.collapsed].sort()).toEqual(["features", "qa"]);
    // New store instances restore by canonical root, without any runtime project id.
    expect([...reload().collapsed]).toEqual(["features"]);
    expect([...reload(otherRoot).collapsed].sort()).toEqual(["features", "qa"]);
  });

  it("leaves no key behind once nothing is folded", async () => {
    const store = reload();
    await store.load();
    store.toggleFolder("features");
    store.toggleFolder("qa");
    expect(local.items.get(KEY), "stored in one order, however folded").toBe(
      '["features","qa"]',
    );

    store.toggleFolder("features");
    store.toggleFolder("qa");
    expect(local.items.has(KEY)).toBe(false);
  });

  it("forgets a folder that went while the page was shut", async () => {
    local.items.set(KEY, '["features","gone"]');
    const store = reload();
    await store.load();

    expect(store.isCollapsed("gone")).toBe(false);
    expect(local.items.get(KEY)).toBe('["features"]');
  });

  it("writes nothing for the listing every save re-reads", async () => {
    const store = reload();
    await store.load();
    store.toggleFolder("features");
    local.setItem.mockClear();

    await store.load();
    await store.load();
    expect(local.setItem).not.toHaveBeenCalled();
    expect(local.removeItem).not.toHaveBeenCalled();
  });

  it("follows a folder renamed in another window", async () => {
    api.docs.mockResolvedValue([entry("features/combat/hit.md")]);
    const store = reload();
    store.subscribe();
    await store.load();
    store.toggleFolder("features/combat");

    // The folder event comes before any document the rename rewrote, and before the listing.
    api.docs.mockResolvedValue([entry("mechanics/combat/hit.md")]);
    mockWsHandlers.get("team.doc.folder_changed")?.({
      path: "mechanics",
      previous_path: "features",
      author: null,
    });
    await vi.waitFor(() =>
      expect(store.entries[0]?.path).toBe("mechanics/combat/hit.md"),
    );

    expect(store.isCollapsed("mechanics/combat")).toBe(true);
    expect(local.items.get(KEY)).toBe('["mechanics/combat"]');
  });

  it("takes nothing it cannot read at its word", () => {
    for (const raw of ["not json", '{"features": true}', "42", "null"]) {
      local.items.set(KEY, raw);
      expect(reload().collapsed.size, raw).toBe(0);
    }
    local.items.set(KEY, '["features", 7, "", null]');
    expect([...reload().collapsed]).toEqual(["features"]);
  });

  it("folds on screen when storage refuses, and remembers once it accepts again", async () => {
    local.getItem.mockImplementation(() => {
      throw new Error("SecurityError");
    });
    local.setItem.mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    const store = reload();
    await store.load();

    store.toggleFolder("features");
    expect(store.isCollapsed("features")).toBe(true);
    expect(local.items.has(KEY)).toBe(false);

    local.setItem.mockImplementation((key: string, value: string) => {
      local.items.set(key, value);
    });
    store.toggleFolder("qa");
    expect(local.items.get(KEY)).toBe('["features","qa"]');
  });

  it("touches no storage while it does not know the project", async () => {
    setActivePinia(createPinia());
    const store = useDocsStore();
    await store.load();
    store.toggleFolder("features");

    expect(store.isCollapsed("features")).toBe(true);
    expect(local.getItem).not.toHaveBeenCalled();
    expect(local.setItem).not.toHaveBeenCalled();
  });
});

describe("where the documents sit for version control (§9.6)", () => {
  it("puts the data directory in front of docs, in the project's frame", () => {
    expect(
      docsVcsRoot("D:\\Games\\SampleGame", "D:\\Games\\SampleGame\\.voxarium_studio"),
    ).toBe(".voxarium_studio/docs");
    expect(docsVcsRoot("/home/dev/game", "/home/dev/game/.voxarium_studio/")).toBe(
      ".voxarium_studio/docs",
    );
  });

  it("follows whatever the backend names the data directory, however the drive was typed", () => {
    expect(docsVcsRoot("d:\\Games\\Mine", "D:\\Games\\Mine\\.studio")).toBe(
      ".studio/docs",
    );
  });

  it("falls back to the documented default when the layout cannot be read", () => {
    expect(docsVcsRoot(undefined, undefined)).toBe(".voxarium_studio/docs");
    expect(docsVcsRoot("D:\\Games\\Mine", "E:\\elsewhere\\.voxarium_studio")).toBe(
      ".voxarium_studio/docs",
    );
  });
});

describe("the reading size", () => {
  it("holds no size until somebody picks one", () => {
    const store = useDocsStore();
    expect(store.fontSize).toBeNull();

    store.hydrate({ font_size: 17, line_height: null });
    expect(store.fontSize).toBe(17);

    // Both "this project never chose" and "a Studio too old to have the block at all".
    store.hydrate({ font_size: null, line_height: null });
    expect(store.fontSize).toBeNull();
    store.hydrate({ font_size: 17, line_height: null });
    store.hydrate(undefined);
    expect(store.fontSize).toBeNull();
  });

  it("persists a change to config.yaml rather than to the browser (§7)", async () => {
    const store = useDocsStore();
    api.updateSettings.mockResolvedValue({ docs: { font_size: 16 } });

    await store.setFontSize(16);

    expect(api.updateSettings).toHaveBeenCalledWith({
      docs: { font_size: 16 },
    });
    expect(store.fontSize).toBe(16);
  });

  it("gives the size back to the density knob when asked for null", async () => {
    const store = useDocsStore();
    store.hydrate({ font_size: 16, line_height: null });
    api.updateSettings.mockResolvedValue({ docs: { font_size: null } });

    await store.setFontSize(null);

    expect(api.updateSettings).toHaveBeenCalledWith({
      docs: { font_size: null },
    });
    expect(store.fontSize).toBeNull();
  });

  it("stays inside the bounds the stepper offers, whoever asked", async () => {
    const store = useDocsStore();
    api.updateSettings.mockImplementation(
      (patch: { docs: { font_size: number } }) =>
        Promise.resolve({ docs: patch.docs }),
    );

    await store.setFontSize(FONT_SIZE.max + 40);
    expect(store.fontSize).toBe(FONT_SIZE.max);

    await store.setFontSize(0);
    expect(store.fontSize).toBe(FONT_SIZE.min);

    // Already there — nothing to write, so the round trip is not made either.
    api.updateSettings.mockClear();
    await store.setFontSize(FONT_SIZE.min);
    expect(api.updateSettings).not.toHaveBeenCalled();
  });

  it("goes back to the size still on disk when the write is refused", async () => {
    const store = useDocsStore();
    store.hydrate({ font_size: 14, line_height: null });
    api.updateSettings.mockRejectedValue(new Error("read-only project"));

    await store.setFontSize(15);

    expect(store.fontSize).toBe(14);
    // Not worth an error banner over a document — the text simply does not resize.
    expect(store.error).toBeNull();
  });
});

describe("the line spacing", () => {
  it("holds no leading until somebody picks one, and follows the size everywhere else", async () => {
    const store = useDocsStore();
    expect(store.lineHeight).toBeNull();

    store.hydrate({ font_size: null, line_height: 1.3 });
    expect(store.lineHeight).toBe(1.3);
    store.hydrate({ font_size: null, line_height: null });
    expect(store.lineHeight).toBeNull();

    api.updateSettings.mockImplementation(
      (patch: { docs: { line_height: number | null } }) =>
        Promise.resolve({ docs: patch.docs }),
    );
    await store.setLineHeight(1.4);
    expect(api.updateSettings).toHaveBeenCalledWith({
      docs: { line_height: 1.4 },
    });
    expect(store.lineHeight).toBe(1.4);

    // One click from 1.1 is 1.2, not 1.2000000000000002 — and never past the bounds.
    await store.setLineHeight(1.1 + LINE_HEIGHT.step);
    expect(store.lineHeight).toBe(1.2);
    await store.setLineHeight(LINE_HEIGHT.max + 1);
    expect(store.lineHeight).toBe(LINE_HEIGHT.max);
    await store.setLineHeight(0);
    expect(store.lineHeight).toBe(LINE_HEIGHT.min);

    await store.setLineHeight(null);
    expect(store.lineHeight).toBeNull();
  });

  it("goes back to the leading still on disk when the write is refused", async () => {
    const store = useDocsStore();
    store.hydrate({ font_size: null, line_height: 1.6 });
    api.updateSettings.mockRejectedValue(new Error("read-only project"));

    await store.setLineHeight(1.7);

    expect(store.lineHeight).toBe(1.6);
    expect(store.error).toBeNull();
  });
});

describe("the corpus's link graph", () => {
  it("is never touched by opening a document", async () => {
    // The whole reason the graph is a separate lazy endpoint: `select()` is the call a
    // reader actually waits on, and a whole-corpus walk in front of the text they asked for
    // would be paying for an ornament with the one latency that matters.
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail("features/dash.md"));

    await store.load();
    await store.select("features/dash.md");
    await store.select("qa/test-plan.md");

    expect(api.docsGraph).not.toHaveBeenCalled();
  });

  it("makes one request for two callers in a tick, and none while the copy is fresh", async () => {
    const store = useDocsStore();

    await Promise.all([store.loadGraph(), store.loadGraph()]);
    expect(api.docsGraph).toHaveBeenCalledTimes(1);

    await store.loadGraph();
    expect(api.docsGraph).toHaveBeenCalledTimes(1);
    expect(store.graph).toEqual({ nodes: [], edges: [] });
  });

  it("keeps a failure to itself rather than banner-ing over the document", async () => {
    const store = useDocsStore();
    api.docsGraph.mockRejectedValue(new Error("no such route"));

    await store.loadGraph();

    expect(store.graphError).toContain("no such route");
    // `error` is what raises a strip over the document someone is reading.
    expect(store.error).toBeNull();
    expect(store.graph).toBeNull();
  });

  it("issues nothing for a graph nobody has looked at", async () => {
    // Marking it stale is the whole cost: a graph that was never fetched has nobody
    // watching it, which is the deliberate approximation of "is the section open".
    const store = useDocsStore();

    store.invalidateGraph();
    expect(api.docsGraph).not.toHaveBeenCalled();

    // Stale, though — the first reader to look pays for a fresh copy.
    await store.loadGraph();
    expect(api.docsGraph).toHaveBeenCalledTimes(1);
  });
});

describe("what makes the link graph stale", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** A store holding a graph — the only state in which an invalidation costs a request. */
  async function watching() {
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail("features/dash.md"));
    await store.loadGraph();
    api.docsGraph.mockClear();
    return store;
  }

  it("re-fetches after a save, once for a burst of them", async () => {
    // A run that writes twenty documents raises twenty events; the debounce is what keeps
    // that one request, the same way `explorer.file_changed` is debounced before it reaches
    // version control (§9.6).
    const store = await watching();
    api.saveDoc.mockResolvedValue(detail("features/dash.md", "# Edited\n"));
    await store.select("features/dash.md");

    for (let n = 0; n < 5; n += 1) {
      store.markDirty();
      await store.save(`# Edited ${n}\n`);
    }
    expect(api.docsGraph, "nothing goes out inside the window").not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(GRAPH_REFRESH_MS);
    expect(api.docsGraph).toHaveBeenCalledTimes(1);
  });

  it("re-fetches after a create, a delete and a rename", async () => {
    const store = await watching();
    api.createDoc.mockResolvedValue(detail("features/new.md"));
    api.deleteDoc.mockResolvedValue({ path: "features/new.md", deleted: true });
    api.moveDoc.mockResolvedValue({
      ...entry("features/moved.md"),
      moved_from: "features/dash.md",
      referrers: [],
    });

    for (const act of [
      () => store.create("features/new.md", { open: false }),
      () => store.remove("features/new.md"),
      () => store.rename("features/dash.md", "features/moved.md"),
    ]) {
      api.docsGraph.mockClear();
      await act();
      await vi.advanceTimersByTimeAsync(GRAPH_REFRESH_MS);
      expect(api.docsGraph).toHaveBeenCalledTimes(1);
    }
  });

  it("re-fetches when somebody else writes a document", async () => {
    const store = await watching();
    store.subscribe();

    mockWsHandlers.get("team.doc.changed")?.({ path: "qa/test-plan.md" });
    await vi.advanceTimersByTimeAsync(GRAPH_REFRESH_MS);

    expect(api.docsGraph).toHaveBeenCalledTimes(1);
  });

  it("does not let a request that predates a write report the corpus as fresh", async () => {
    // The answer to this request was computed by the server *before* the write below, so
    // adopting it as fresh would leave the store convinced it holds the current corpus —
    // and every later `loadGraph` returns at its guard without asking. The picture then
    // stays wrong until some unrelated write happens to invalidate it again.
    const store = useDocsStore();
    let answer!: (graph: { nodes: []; edges: [] }) => void;
    api.docsGraph.mockReturnValueOnce(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );

    const inFlight = store.loadGraph();
    store.subscribe();
    mockWsHandlers.get("team.doc.changed")?.({ path: "qa/test-plan.md" });

    answer({ nodes: [], edges: [] });
    await inFlight;

    // The re-fetch is armed by the request that finished, not by the invalidation — which
    // found the slot busy and deliberately left the timer alone.
    api.docsGraph.mockClear();
    await vi.advanceTimersByTimeAsync(GRAPH_REFRESH_MS);
    expect(api.docsGraph).toHaveBeenCalledTimes(1);
  });

  it("does not retry a failing graph on a loop", async () => {
    // A failure leaves the corpus stale by definition. Re-arming on staleness would ask
    // again every 500 ms for as long as the backend is down; the reader's Try again is what
    // covers a failure nobody else invalidated.
    const store = await watching();
    api.docsGraph.mockRejectedValueOnce(new Error("500"));
    store.subscribe();

    mockWsHandlers.get("team.doc.changed")?.({ path: "qa/test-plan.md" });
    await vi.advanceTimersByTimeAsync(GRAPH_REFRESH_MS);
    expect(api.docsGraph).toHaveBeenCalledTimes(1);

    api.docsGraph.mockClear();
    await vi.advanceTimersByTimeAsync(GRAPH_REFRESH_MS * 10);
    expect(api.docsGraph, "nothing retries on its own").not.toHaveBeenCalled();
  });
});

describe("where a path is once a folder is renamed (§5.2)", () => {
  it("moves the folder and what is under it, and nothing that merely shares its name", () => {
    expect(movedPath("features", "features", "mechanics")).toBe("mechanics");
    expect(movedPath("features/combat/dash.md", "features", "mechanics")).toBe(
      "mechanics/combat/dash.md",
    );
    expect(movedPath("features-old/dash.md", "features", "mechanics")).toBe(
      "features-old/dash.md",
    );
    expect(movedPath("qa/features/dash.md", "features", "mechanics")).toBe(
      "qa/features/dash.md",
    );
  });
});

describe("how a folder looks (§5.2)", () => {
  const green: DocFolderStyle = { path: "features", color: "green", icon: "swords" };

  it("arrives with the first listing, and again only when the folders themselves change", async () => {
    const store = useDocsStore();
    api.docFolders.mockResolvedValue([green]);
    await store.load();
    expect(store.folderStyle("features")).toEqual(green);
    expect(store.folderStyle("qa")).toBeNull();

    // Every save re-reads the listing; the same folders ask for no look.
    await store.load();
    expect(api.docFolders).toHaveBeenCalledTimes(1);

    // A folder that was not there before does — and a look that fails to arrive keeps the one
    // the tree had, with no banner.
    api.docs.mockResolvedValue([entry("features/dash.md"), entry("qa/plan.md")]);
    api.docFolders.mockRejectedValue(new Error("offline"));
    await store.load();
    expect(api.docFolders).toHaveBeenCalledTimes(2);
    expect(store.error).toBeNull();
    expect(store.folderStyle("features")).toEqual(green);
  });

  it("changes under the pointer, then takes what the server stored", async () => {
    const store = useDocsStore();
    let answer!: (style: DocFolderStyle) => void;
    api.setDocFolderStyle.mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );

    const written = store.setFolderStyle("features", { color: "green" });
    expect(store.folderStyle("features")).toEqual({
      path: "features",
      color: "green",
      icon: null,
    });
    await tick();
    // Only the key that was picked goes out: the backend leaves the other one alone.
    expect(api.setDocFolderStyle).toHaveBeenCalledWith({ path: "features", color: "green" });

    answer({ path: "features", color: "green", icon: "book-open" });
    expect(await written).toBe(true);
    expect(store.folderStyle("features")?.icon).toBe("book-open");
  });

  it("goes back to the look it had, and says why, when the write is refused", async () => {
    const store = useDocsStore();
    api.docFolders.mockResolvedValue([{ ...green, color: "red" }]);
    await store.load();
    api.setDocFolderStyle.mockRejectedValue(
      new ApiError(409, "vxs-folder.json in 'features' does not parse; fix or delete it"),
    );

    expect(await store.setFolderStyle("features", { color: "blue" })).toBe(false);
    expect(store.folderStyle("features")?.color).toBe("red");
    expect(store.error).toContain("does not parse");
  });

  it("forgets a folder once it is plain again", async () => {
    const store = useDocsStore();
    api.docFolders.mockResolvedValue([{ path: "features", color: "red", icon: null }]);
    await store.load();
    api.setDocFolderStyle.mockResolvedValue({ path: "features", color: null, icon: null });

    await store.setFolderStyle("features", { color: null });
    expect(store.folderStyles).toEqual({});
  });

  it("writes one pick after another, so a slow answer cannot put an older look back", async () => {
    const store = useDocsStore();
    const answers: ((style: DocFolderStyle) => void)[] = [];
    api.setDocFolderStyle.mockImplementation(
      () =>
        new Promise((resolve) => {
          answers.push(resolve);
        }),
    );

    const colour = store.setFolderStyle("features", { color: "green" });
    const icon = store.setFolderStyle("features", { icon: "bug" });
    await tick();
    expect(api.setDocFolderStyle, "the icon waits for the colour").toHaveBeenCalledTimes(1);
    expect(store.folderStyle("features"), "both are on screen already").toEqual({
      path: "features",
      color: "green",
      icon: "bug",
    });

    answers[0]({ path: "features", color: "green", icon: null });
    await colour;
    await tick();
    answers[1]({ path: "features", color: "green", icon: "bug" });
    await icon;
    expect(store.folderStyle("features")?.icon).toBe("bug");
  });

  it("re-reads when somebody else restyles a folder, and not for its own pick", async () => {
    const store = useDocsStore();
    store.subscribe();
    api.docFolders.mockClear();

    mockWsHandlers.get("team.doc.folder_changed")?.({
      path: "qa",
      previous_path: null,
      author: null,
    });
    await tick();
    expect(api.docFolders).toHaveBeenCalledTimes(1);

    api.docFolders.mockClear();
    let answer!: (style: DocFolderStyle) => void;
    api.setDocFolderStyle.mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    const written = store.setFolderStyle("features", { color: "green" });
    await tick();
    mockWsHandlers.get("team.doc.folder_changed")?.({
      path: "features",
      previous_path: null,
      author: null,
    });
    answer(green);
    await written;
    expect(api.docFolders).not.toHaveBeenCalled();
  });
});

describe("renaming a folder (§5.2)", () => {
  const renamed: DocFolderRenameFrame = {
    type: "renamed",
    path: "mechanics",
    previous_path: "features",
    moved: 2,
    documents: [{ path: "qa/plan.md", previous_path: null, title: "Plan", links: 1 }],
  };
  const mended: DocFolderRenameFrame = {
    type: "document",
    path: "qa/plan.md",
    previous_path: null,
    title: "Plan",
    links: 1,
    status: "updated",
    error: null,
  };
  const done: DocFolderRenameFrame = {
    type: "done",
    path: "mechanics",
    previous_path: "features",
    moved: 2,
    updated: 1,
    links: 1,
    failed: 0,
  };

  /** A store with a document open inside `features` and a folder folded under it. */
  async function reading() {
    const store = useDocsStore();
    api.docs.mockResolvedValue([entry("features/dash.md"), entry("features/combat/hit.md")]);
    api.doc.mockResolvedValue(detail("features/dash.md"));
    await store.load();
    await store.select("features/dash.md");
    store.toggleFolder("features/combat");
    // What the backend lists once the folder has moved.
    api.docs.mockResolvedValue([entry("mechanics/dash.md"), entry("mechanics/combat/hit.md")]);
    api.doc.mockResolvedValue(detail("mechanics/dash.md"));
    return store;
  }

  it("hands over each frame as it comes, then follows the folder with everything it holds", async () => {
    const store = await reading();
    api.renameDocFolder.mockImplementation(
      async (_path: string, _name: string, onFrame: (frame: DocFolderRenameFrame) => void) => {
        for (const frame of [renamed, mended, done]) onFrame(frame);
        return done;
      },
    );
    const heard: string[] = [];

    const answer = await store.renameFolder("features", "mechanics", (frame) =>
      heard.push(frame.type),
    );

    expect(heard).toEqual(["renamed", "document", "done"]);
    expect(answer).toEqual(done);
    expect(store.open?.path).toBe("mechanics/dash.md");
    expect(store.isCollapsed("mechanics/combat"), "the fold moved with the folder").toBe(true);
    expect(store.entries.map((document) => document.path)).toContain("mechanics/dash.md");
  });

  it("holds on to everything when the rename is refused before the folder moved", async () => {
    const store = await reading();
    api.doc.mockClear();
    api.docs.mockClear();
    api.renameDocFolder.mockRejectedValue(new ApiError(409, "'qa' already exists"));

    await expect(store.renameFolder("features", "qa", () => {})).rejects.toThrow(
      "already exists",
    );

    expect(store.open?.path).toBe("features/dash.md");
    expect(api.docs, "nothing moved, so nothing is re-read").not.toHaveBeenCalled();
    expect(api.doc).not.toHaveBeenCalled();
  });

  it("still follows the folder when the stream was cut off after it moved", async () => {
    const store = await reading();
    api.renameDocFolder.mockImplementation(
      async (_path: string, _name: string, onFrame: (frame: DocFolderRenameFrame) => void) => {
        onFrame(renamed);
        throw new ApiError(0, "network");
      },
    );

    await expect(store.renameFolder("features", "mechanics", () => {})).rejects.toThrow(
      "network",
    );
    expect(store.open?.path).toBe("mechanics/dash.md");
  });

  it("follows a folder another window renamed, but never re-reads unsaved edits away", async () => {
    const store = await reading();
    store.subscribe();

    mockWsHandlers.get("team.doc.folder_changed")?.({
      path: "mechanics",
      previous_path: "features",
      author: null,
    });
    await vi.waitFor(() => expect(store.open?.path).toBe("mechanics/dash.md"));

    // The same again, with an edit on screen that has not been written yet.
    api.doc.mockClear();
    store.markDirty();
    mockWsHandlers.get("team.doc.folder_changed")?.({
      path: "systems",
      previous_path: "mechanics",
      author: null,
    });
    await vi.waitFor(() => expect(store.staleOnDisk).toBe(true));
    expect(store.open?.path).toBe("mechanics/dash.md");
    expect(api.doc).not.toHaveBeenCalled();
  });

  it("waits for a save already on its way before anything else happens", async () => {
    const store = await reading();
    let written!: (document: DocDetail) => void;
    api.saveDoc.mockReturnValue(
      new Promise((resolve) => {
        written = resolve;
      }),
    );
    store.markDirty();
    void store.save("# Edited\n");

    let settled = false;
    const settling = store.settle().then(() => {
      settled = true;
    });
    await tick();
    expect(settled).toBe(false);

    written(detail("mechanics/dash.md", "# Edited\n"));
    await settling;
    expect(settled).toBe(true);
  });
});

describe("where a new document goes (§5.2)", () => {
  it("inside the folder, with .md added unless the name ends in a document's suffix", () => {
    expect(documentPath("features", "combat tuning")).toBe("features/combat tuning.md");
    expect(documentPath("features/combat", "plan.txt")).toBe("features/combat/plan.txt");
    expect(documentPath("features", "Notes.MD")).toBe("features/Notes.MD");
    // A dot is not a suffix a document may have — the backend would refuse `.2`.
    expect(documentPath("features", "v1.2")).toBe("features/v1.2.md");
    expect(documentPath("", "README")).toBe("README.md");
  });

  it("keeps separators around the name out of the path, and has nothing for no name", () => {
    expect(documentPath("features", " /notes/ ")).toBe("features/notes.md");
    expect(documentPath("features", "combat\\hit")).toBe("features/combat/hit.md");
    expect(documentPath("features", "   ")).toBe("");
    expect(documentPath("features", "//")).toBe("");
  });
});

describe("creating a document (§5.2)", () => {
  it("never writes over a document already there", async () => {
    const store = useDocsStore();
    api.createDoc.mockRejectedValue(new ApiError(409, "'features/dash.md' already exists"));

    await expect(store.createDocument("features/dash.md")).rejects.toThrow("already exists");
    expect(api.saveDoc, "a save would have written over it").not.toHaveBeenCalled();
    expect(store.error, "the dialog says it under the field").toBeNull();

    // The header's New document and a `[[link]]` have only the banner to say it in.
    expect(await store.create("features/dash.md")).toBeNull();
    expect(store.error).toContain("already exists");
  });

  it("heads the document with its name, and opens it only when asked", async () => {
    const store = useDocsStore();
    api.createDoc.mockResolvedValue(detail("features/combat-tuning.md"));

    const created = await store.createDocument("features/combat-tuning.md", { open: false });

    expect(created).toBe("features/combat-tuning.md");
    expect(api.createDoc).toHaveBeenCalledWith("features/combat-tuning.md", "# combat tuning\n\n");
    expect(api.doc).not.toHaveBeenCalled();
    expect(store.open).toBeNull();
  });
});

describe("deleting a document or a folder (§5.2)", () => {
  const gone: DocFolderDeleted = {
    path: "features",
    documents: ["features/combat/hit.md", "features/dash.md"],
  };
  const deletedElsewhere = { path: "features", previous_path: null, author: null, deleted: true };

  /** A store reading `features/dash.md`, with `features/combat` folded. */
  async function reading() {
    const store = useDocsStore();
    api.docs.mockResolvedValue([
      entry("features/dash.md"),
      entry("features/combat/hit.md"),
      entry("qa/plan.md"),
    ]);
    api.doc.mockResolvedValue(detail("features/dash.md"));
    await store.load();
    await store.select("features/dash.md");
    store.toggleFolder("features/combat");
    return store;
  }

  it("lets go of the open document once it is deleted", async () => {
    const store = await reading();
    api.deleteDoc.mockResolvedValue({ path: "features/dash.md", deleted: true });
    api.docs.mockResolvedValue([entry("features/combat/hit.md"), entry("qa/plan.md")]);

    await store.remove("features/dash.md");

    expect(store.open).toBeNull();
    expect(store.entries.map((document) => document.path)).toEqual([
      "features/combat/hit.md",
      "qa/plan.md",
    ]);
  });

  it("keeps the document when its delete is refused, and leaves the telling to the dialog", async () => {
    const store = await reading();
    api.deleteDoc.mockRejectedValue(new ApiError(500, "Access is denied"));

    await expect(store.remove("features/dash.md")).rejects.toThrow("Access is denied");
    expect(store.open?.path).toBe("features/dash.md");
    expect(store.error).toBeNull();
  });

  it("waits for a save already on its way, which would write the file back", async () => {
    for (const act of [
      (store: ReturnType<typeof useDocsStore>) => store.remove("features/dash.md"),
      (store: ReturnType<typeof useDocsStore>) => store.deleteFolder("features"),
    ]) {
      setActivePinia(createPinia());
      const store = await reading();
      let written!: (document: DocDetail) => void;
      api.saveDoc.mockReturnValue(
        new Promise((resolve) => {
          written = resolve;
        }),
      );
      api.deleteDoc.mockReset().mockResolvedValue({ path: "features/dash.md", deleted: true });
      api.deleteDocFolder.mockReset().mockResolvedValue(gone);
      store.markDirty();
      void store.save("# Edited\n");
      await tick();

      const deleting = act(store);
      await tick();
      expect(
        api.deleteDoc.mock.calls.length + api.deleteDocFolder.mock.calls.length,
        "nothing is deleted under the save",
      ).toBe(0);

      written(detail("features/dash.md", "# Edited\n"));
      await deleting;
      expect(api.deleteDoc.mock.calls.length + api.deleteDocFolder.mock.calls.length).toBe(1);
    }
  });

  it("lets go of everything a deleted folder took, unsaved edits included", async () => {
    const store = await reading();
    store.markDirty(); // asked for over the document: there is nothing to keep them for
    api.deleteDocFolder.mockResolvedValue(gone);
    api.docs.mockResolvedValue([entry("qa/plan.md")]);

    expect(await store.deleteFolder("features")).toEqual(gone.documents);

    expect(api.deleteDocFolder).toHaveBeenCalledWith("features");
    expect(store.open).toBeNull();
    expect(store.staleOnDisk).toBe(false);
    expect(store.collapsed.size, "the fold went with its folder").toBe(0);
    expect(store.entries.map((document) => document.path)).toEqual(["qa/plan.md"]);
  });

  it("keeps a document outside the deleted folder open", async () => {
    const store = await reading();
    api.deleteDocFolder.mockResolvedValue({
      path: "features/combat",
      documents: ["features/combat/hit.md"],
    });
    api.docs.mockResolvedValue([entry("features/dash.md"), entry("qa/plan.md")]);

    await store.deleteFolder("features/combat");

    expect(store.open?.path).toBe("features/dash.md");
  });

  it("re-reads nothing when the delete was refused before anything went", async () => {
    const store = await reading();
    api.docs.mockClear();
    api.deleteDocFolder.mockRejectedValue(
      new ApiError(409, "Could not delete 'features': something has the folder open"),
    );

    await expect(store.deleteFolder("features")).rejects.toThrow("Could not delete");

    expect(api.docs).not.toHaveBeenCalled();
    expect(store.open?.path).toBe("features/dash.md");
    expect(store.isCollapsed("features/combat")).toBe(true);
  });

  it("goes by the listing when a delete stopped part-way, and keeps what it put back", async () => {
    const store = await reading();
    api.deleteDocFolder.mockRejectedValue(
      new ApiError(500, "Could not delete everything in 'features'. What is left is back where it was."),
    );
    // `combat/hit.md` went before it stopped; `dash.md` is back under its name.
    api.docs.mockResolvedValue([entry("features/dash.md"), entry("qa/plan.md")]);

    await expect(store.deleteFolder("features")).rejects.toThrow("What is left");

    expect(store.open?.path).toBe("features/dash.md");
    expect(store.isCollapsed("features/combat")).toBe(false);
    expect(store.entries.map((document) => document.path)).toEqual([
      "features/dash.md",
      "qa/plan.md",
    ]);
  });

  it("lets go of a document deleted in another window, unless it holds unsaved edits", async () => {
    const store = await reading();
    store.subscribe();
    const deleted = { path: "features/dash.md", title: null, author: null, deleted: true };

    store.markDirty();
    mockWsHandlers.get("team.doc.changed")?.(deleted);
    expect(store.staleOnDisk, "Keep mine writes it back").toBe(true);
    expect(store.open?.path).toBe("features/dash.md");

    await store.select("features/dash.md"); // the edits are dropped
    api.doc.mockClear();
    mockWsHandlers.get("team.doc.changed")?.(deleted);
    expect(store.open).toBeNull();
    expect(api.doc, "reading a deleted document again is a 404").not.toHaveBeenCalled();
  });

  it("lets go of what a folder deleted in another window took, unless it holds unsaved edits", async () => {
    const store = await reading();
    store.subscribe();
    api.docs.mockResolvedValue([entry("qa/plan.md")]);

    store.markDirty();
    mockWsHandlers.get("team.doc.folder_changed")?.(deletedElsewhere);
    await vi.waitFor(() => expect(store.staleOnDisk).toBe(true));
    expect(store.open?.path).toBe("features/dash.md");

    await store.select("features/dash.md");
    mockWsHandlers.get("team.doc.folder_changed")?.(deletedElsewhere);
    await vi.waitFor(() => expect(store.open).toBeNull());
    expect(store.entries.map((document) => document.path)).toEqual(["qa/plan.md"]);
  });

  it("does not hear its own delete coming back as somebody else's", async () => {
    const store = await reading();
    store.subscribe();
    store.markDirty();
    let answer!: (deleted: DocFolderDeleted) => void;
    api.deleteDocFolder.mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    api.docs.mockResolvedValue([entry("qa/plan.md")]);

    const deleting = store.deleteFolder("features");
    await tick();
    // The socket usually beats the answer.
    mockWsHandlers.get("team.doc.folder_changed")?.(deletedElsewhere);
    await tick();
    expect(store.staleOnDisk, "no 'changed on disk' over a delete asked for here").toBe(false);

    answer(gone);
    await deleting;
    expect(store.open).toBeNull();
  });
});


describe("captured document mutation fences (runtime integration I8)", () => {
  const path = "features/dash.md";
  const fence: DocumentFence = { backend_epoch: "backend-a", expected_binding_generation: 4,
    editor_id: "editor-a", client_instance_id: "client-a", sid: "sid-a", version: 10 };
  const remote = (generation = 4): ProjectRemoteStatus => ({ project_id: "project_a", epoch: "epoch_a",
    backend_epoch: "backend-a", persistent_id: "copy-id", identity_revision: 1,
    config_fingerprint: "fp", url: "", enabled: false, has_token: false,
    binding_generation: generation, state: "disabled", active_rooms: 0, pending_writes: 0 });

  async function metadata(generation = 4) {
    api.projectRemote.mockResolvedValue(remote(generation));
    expect(await useSyncStore().loadRemote()).toBe(true);
  }

  async function editing() {
    mockSession.isLegacy = false;
    mockSession.scope = { project_id: "project_a", epoch: "epoch_a" };
    await metadata();
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail(path));
    await store.select(path);
    store.setLive("sid-a");
    store.setEditorFence(fence);
    return store;
  }

  function saved(version: number, sequence = 38) {
    return { ...detail(path, "# Saved\n"), editor_id: "editor-a", backend_epoch: "backend-a",
      binding_generation: 4, version, last_saved_version: sequence };
  }

  it("captures immutable path/live/version/fence before debounce, never latest G", async () => {
    const store = await editing();
    const ticket = store.markDirty()!;
    expect(ticket.version).toBe(11);
    expect(Object.isFrozen(ticket)).toBe(true);
    expect(Object.isFrozen(ticket.fence)).toBe(true);
    expect(Object.isFrozen(ticket.binding)).toBe(true);
    store.setLive(null);
    store.markDirty();
    api.saveDoc.mockResolvedValue(saved(11));
    expect(await store.save("# Saved\n", ticket)).toBe(true);
    expect(api.saveDoc).toHaveBeenCalledWith(path, "# Saved\n", null, null, "sid-a",
      { ...fence, version: 11 });
    expect(store.dirty, "a later version is still owed").toBe(true);
    expect(store.lastSaveReceipt).toEqual({ editor_id: "editor-a", backend_epoch: "backend-a",
      binding_generation: 4, version: 11, last_saved_version: 38 });
    expect(Object.isFrozen(store.lastSaveReceipt)).toBe(true);
  });

  it("keeps the registration ticket for final edits during a same-G pending refresh", async () => {
    const store = await editing();
    const sync = useSyncStore();
    const registered = sync.capture();
    // This is captured before registration/room attachment awaits, then handed back
    // while same-G draining metadata is pending. Fresh admission must still refuse.
    sync.remotePending = true;
    expect(() => sync.capture()).toThrow("no longer current");
    store.setEditorFence(fence, registered);
    const capture = vi.spyOn(sync, "capture");
    const ticket = store.markDirty()!;
    expect(ticket).not.toBeNull();
    expect(ticket.binding).toBe(registered);
    expect(store.captureSave().binding).toBe(registered);
    expect(capture).not.toHaveBeenCalled();
    api.saveDoc.mockResolvedValue(saved(ticket.version));
    expect(await store.save("# Final scribe Markdown\n", ticket)).toBe(true);
    expect(api.saveDoc).toHaveBeenCalledWith(path, "# Final scribe Markdown\n", null, null, "sid-a",
      { ...fence, version: ticket.version });
    expect(store.lastSaveReceipt?.version).toBe(ticket.version);
    expect(store.fenceError).toBeNull();
    expect(capture).not.toHaveBeenCalled();
    await expect(store.createDocument("not-new-admission.md")).rejects.toThrow("no longer current");
    expect(capture).toHaveBeenCalledTimes(1);
    capture.mockRestore();
  });

  it("captures the compatibility registration ticket once, not again for each edit", async () => {
    const store = await editing();
    const sync = useSyncStore();
    const capture = vi.spyOn(sync, "capture");
    store.setEditorFence(fence);
    expect(capture).toHaveBeenCalledTimes(1);
    const registered = store.captureSave().binding;
    sync.remotePending = true;
    expect(store.markDirty()?.binding).toBe(registered);
    expect(store.captureSave().binding).toBe(registered);
    expect(capture).toHaveBeenCalledTimes(1);
    capture.mockRestore();
  });

  it("refuses an invalidated registration ticket after G changes without recapturing latest G", async () => {
    const store = await editing();
    const sync = useSyncStore();
    const registered = sync.capture();
    store.setEditorFence(fence, registered);
    const oldSave = store.markDirty()!;
    await metadata(5);
    const capture = vi.spyOn(sync, "capture");
    expect(() => store.captureSave()).toThrow("no longer current");
    expect(store.markDirty()).toBeNull();
    expect(await store.save("# Old-G callback\n", oldSave)).toBe(false);
    expect(await store.save("# No latest-G substitution\n")).toBe(false);
    expect(() => store.setEditorFence({ ...fence, expected_binding_generation: 5 }, registered))
      .toThrow("no longer current");
    expect(capture).not.toHaveBeenCalled();
    expect(api.saveDoc).not.toHaveBeenCalled();
    expect(store.lastSaveReceipt).toBeNull();
    expect(store.dirty).toBe(true);
    expect(store.staleOnDisk).toBe(false);
    capture.mockRestore();
  });

  const refresh = (store: ReturnType<typeof useDocsStore>, method: "select" | "reopen") =>
    method === "select" ? store.select(path) : store.reopen();

  it.each(["select", "reopen"] as const)("retains the same-path lease after clean %s but rejects old save callbacks", async method => {
    const store = await editing();
    const old = store.markDirty()!;
    api.saveDoc.mockResolvedValue(saved(old.version));
    expect(await store.save("# Saved before refresh\n", old)).toBe(true);
    api.saveDoc.mockClear();
    const registered = store.captureSave().binding;
    const capture = vi.spyOn(useSyncStore(), "capture");
    api.doc.mockResolvedValue(detail(path, "# Fresh server read\n"));
    await refresh(store, method);
    expect(store.open?.content).toBe("# Fresh server read\n");
    expect(store.dirty).toBe(false);
    expect(store.fenceError).toBeNull();
    expect(await store.save("# Old debounce\n", old)).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
    useSyncStore().remotePending = true;
    const next = store.markDirty()!;
    expect(next.binding).toBe(registered);
    expect(next.fence).toEqual({ ...fence, version: old.version + 1 });
    expect(next.revision).not.toBe(old.revision);
    expect(capture).not.toHaveBeenCalled();
    api.saveDoc.mockResolvedValue(saved(next.version));
    expect(await store.save("# Edit after refresh\n", next)).toBe(true);
    expect(api.saveDoc).toHaveBeenCalledWith(path, "# Edit after refresh\n", null, null, "sid-a", next.fence);
    expect(store.lastSaveReceipt?.version).toBe(next.version);
    capture.mockRestore();
  });

  it.each(["select", "reopen"] as const)("preserves the current document/edit/lease when same-path %s fails", async method => {
    const store = await editing();
    const old = store.markDirty()!;
    const document = store.open;
    store.saveError = "existing save failure";
    store.staleOnDisk = true;
    api.doc.mockRejectedValue(new ApiError(503, "read unavailable"));
    await refresh(store, method);
    expect(store.open).toBe(document);
    expect(store.dirty).toBe(true);
    expect(store.saveError).toBe("existing save failure");
    expect(store.staleOnDisk).toBe(true);
    expect(store.fenceError).toBeNull();
    expect(await store.save("# Superseded callback\n", old)).toBe(false);
    const next = store.captureSave();
    expect(next.binding).toBe(old.binding);
    expect(next.revision).not.toBe(old.revision);
    api.saveDoc.mockResolvedValue(saved(next.version));
    expect(await store.save("# Still owned by this lease\n", next)).toBe(true);
  });

  it.each(["select", "reopen"] as const)("does not clear or escape a typed fence through explicit %s", async method => {
    const store = await editing();
    const old = store.markDirty()!;
    const document = store.open;
    const refusal = new ApiError(409, "editor expired", "editor_lease_expired");
    api.saveDoc.mockRejectedValue(refusal);
    expect(await store.save("# Must stay visible\n", old)).toBe(false);
    api.doc.mockClear();
    api.doc.mockResolvedValue(detail(path, "# Disk is not recovery permission\n"));
    await refresh(store, method);
    expect(api.doc).not.toHaveBeenCalled();
    expect(store.open).toBe(document);
    expect(store.fenceError).toBe(refusal);
    expect(store.saveError).toBe(t("docs.remoteEditor.blocked"));
    expect(store.dirty).toBe(true);
    expect(() => store.captureSave()).toThrow(refusal);
    expect(await store.save("# No replay after reload\n", old)).toBe(false);
    expect(api.saveDoc).toHaveBeenCalledTimes(1);
  });

  it.each(["select", "reopen"] as const)("does not revive a stale-G registration on same-path %s", async method => {
    const store = await editing();
    const document = store.open;
    await metadata(5);
    api.doc.mockResolvedValue(detail(path, "# New-G disk read\n"));
    await refresh(store, method);
    expect(store.open).toBe(document);
    expect(store.fenceError).not.toBeNull();
    expect(store.markDirty()).toBeNull();
    expect(await store.save("# No latest-G adoption\n")).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it.each(["select", "reopen"] as const)("preserves an edit and typed failure arriving during same-path %s", async method => {
    const store = await editing();
    const document = store.open;
    let finish!: (value: DocDetail) => void;
    api.doc.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const reading = refresh(store, method);
    const ticket = store.markDirty()!;
    const refusal = new ApiError(409, "lease fenced during read", "unknown_editor");
    api.saveDoc.mockRejectedValue(refusal);
    expect(await store.save("# Typed during refresh\n", ticket)).toBe(false);
    finish(detail(path, "# Delayed disk read\n"));
    await reading;
    expect(store.open).toBe(document);
    expect(store.dirty).toBe(true);
    expect(store.fenceError).toBe(refusal);
    expect(store.saveError).toBe(t("docs.remoteEditor.blocked"));
    expect(await store.save("# Still not a retry\n", ticket)).toBe(false);
    expect(api.saveDoc).toHaveBeenCalledTimes(1);
  });

  it("lets the parent capture a move fence before flush/release awaits without recapturing", async () => {
    const store = await editing();
    const mutation = store.captureMutation();
    expect(Object.isFrozen(mutation)).toBe(true);
    expect(Object.isFrozen(mutation.fence)).toBe(true);
    expect(mutation.fence).toEqual({ backend_epoch: "backend-a", expected_binding_generation: 4 });
    store.setEditorFence(null);
    useSyncStore().remotePending = true;
    const capture = vi.spyOn(useSyncStore(), "capture");
    api.moveDoc.mockResolvedValue({ path: "moved.md", referrers: [] });
    await store.rename("other.md", "moved.md", mutation);
    expect(api.moveDoc).toHaveBeenCalledWith("other.md", "moved.md", mutation.fence);
    expect(capture).not.toHaveBeenCalled();
    capture.mockRestore();
  });

  it("rejects a pre-flush move ticket after G changes without substituting current G", async () => {
    const store = await editing();
    const mutation = store.captureMutation();
    await metadata(5);
    store.setEditorFence(null);
    const capture = vi.spyOn(useSyncStore(), "capture");
    expect(await store.rename("other.md", "moved.md", mutation)).toEqual([]);
    expect(api.moveDoc).not.toHaveBeenCalled();
    expect(capture).not.toHaveBeenCalled();
    expect(store.fenceError).not.toBeNull();
    capture.mockRestore();
  });

  it("rejects a queued old-G callback without invoking HTTP or borrowing the new registration", async () => {
    const store = await editing();
    let finish!: (value: ReturnType<typeof saved>) => void;
    api.saveDoc.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const firstTicket = store.markDirty()!;
    const first = store.save("# One\n", firstTicket);
    await tick();
    const secondTicket = store.markDirty()!;
    const second = store.save("# Two\n", secondTicket);
    await metadata(5);
    store.setEditorFence({ ...fence, expected_binding_generation: 5, editor_id: "editor-b" });
    finish(saved(firstTicket.version));
    expect(await first).toBe(false);
    expect(await second).toBe(false);
    expect(api.saveDoc).toHaveBeenCalledTimes(1);
    expect(store.lastSaveReceipt).toBeNull();
    expect(store.dirty).toBe(true);
    expect(store.staleOnDisk).toBe(false);
    expect(store.fenceError).not.toBeNull();
  });

  it.each(["reload_required", "stale_binding_generation", "unknown_sid", "pending_edits",
    "editor_lease_expired", "mutation_busy", "future_admission_code"])(
    "keeps typed 409 %s separate from disk conflict and refuses Keep mine/adopt", async code => {
      const store = await editing();
      const ticket = store.markDirty()!;
      const refusal = new ApiError(409, "admission refused", code);
      api.saveDoc.mockRejectedValue(refusal);
      expect(await store.save("# Mine\n", ticket)).toBe(false);
      expect(store.fenceError).toBe(refusal);
      expect(store.staleOnDisk).toBe(false);
      expect(store.dirty).toBe(true);
      expect(await store.saveOverwriting("# Mine\n")).toBe(false);
      const content = store.open?.content;
      store.adopt("# Mine\n", ticket);
      await store.settle();
      expect(store.open?.content).toBe(content);
      expect(api.saveDoc).toHaveBeenCalledTimes(1);
      expect(store.lastSaveReceipt).toBeNull();
    },
  );

  it.each(["editor_id", "backend_epoch", "binding_generation", "version", "last_saved_version"] as const)(
    "does not acknowledge missing or mismatched server receipt %s", async field => {
      const store = await editing();
      const ticket = store.markDirty()!;
      const result: Record<string, unknown> = saved(ticket.version);
      delete result[field];
      api.saveDoc.mockResolvedValue(result);
      expect(await store.save("# Saved\n", ticket)).toBe(false);
      expect(store.lastSaveReceipt).toBeNull();
      expect(store.dirty).toBe(true);
      expect(store.staleOnDisk).toBe(false);
    },
  );

  it("still permits a real disk conflict overwrite with the same captured lease/version/sid", async () => {
    const store = await editing();
    const ticket = store.markDirty()!;
    api.saveDoc.mockRejectedValueOnce(new ApiError(409, "disk conflict"));
    expect(await store.save("# Mine\n", ticket)).toBe(false);
    expect(store.staleOnDisk).toBe(true);
    expect(store.fenceError).toBeNull();
    api.saveDoc.mockResolvedValueOnce(saved(ticket.version));
    expect(await store.saveOverwriting("# Mine\n", ticket)).toBe(true);
    expect(api.saveDoc).toHaveBeenLastCalledWith(path, "# Mine\n", null, null, "sid-a", ticket.fence);
    expect(store.lastSaveReceipt?.last_saved_version).toBe(38);
  });

  it("adopts with the captured lease rather than replacing it after a binding change", async () => {
    const store = await editing();
    const ticket = store.markDirty()!;
    await metadata(5);
    store.setEditorFence({ ...fence, expected_binding_generation: 5, editor_id: "editor-b" });
    store.adopt("# Kept local\n", ticket);
    await store.settle();
    expect(api.saveDoc).not.toHaveBeenCalled();
    expect(store.dirty).toBe(true);
    expect(store.lastSaveReceipt).toBeNull();
  });

  it("does not redirect a debounce ticket after navigation to a copied path/reloaded revision", async () => {
    const store = await editing();
    const ticket = store.markDirty()!;
    await store.select(path);
    store.markDirty();
    expect(await store.save("# Old debounce\n", ticket)).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  const actions = ["createDocument", "create", "remove", "deleteFolder", "rename", "renameFolder", "setFolderStyle"] as const;
  async function act(store: ReturnType<typeof useDocsStore>, action: typeof actions[number]) {
    switch (action) {
      case "createDocument": return store.createDocument("new.md", { open: false });
      case "create": return store.create("new.md", { open: false });
      case "remove": return store.remove("other.md");
      case "deleteFolder": return store.deleteFolder("other");
      case "rename": return store.rename("other.md", "moved.md");
      case "renameFolder": return store.renameFolder("other", "moved", () => {});
      case "setFolderStyle": return store.setFolderStyle("other", { color: "blue" });
    }
  }
  function mutationStubs() {
    api.createDoc.mockResolvedValue(detail("new.md"));
    api.deleteDoc.mockResolvedValue({ deleted: true });
    api.deleteDocFolder.mockResolvedValue({ documents: [] });
    api.moveDoc.mockResolvedValue({ path: "moved.md", referrers: [] });
    api.renameDocFolder.mockResolvedValue({ type: "done" });
    api.setDocFolderStyle.mockResolvedValue({ path: "other", color: "blue", icon: null });
  }
  const mutationCalls = () => [api.createDoc, api.deleteDoc, api.deleteDocFolder,
    api.moveDoc, api.renameDocFolder, api.setDocFolderStyle].flatMap(stub => stub.mock.calls);

  it.each(["remove", "deleteFolder", "rename", "renameFolder"] as const)(
    "makes %s wait for a save enqueued before its pending dirty heartbeat", async action => {
      const store = await editing();
      mutationStubs();
      let finishHeartbeat!: () => void;
      const heartbeat = new Promise<void>(resolve => { finishHeartbeat = resolve; });
      const ticket = store.markDirty()!;
      api.saveDoc.mockResolvedValue(saved(ticket.version));
      const saving = store.save("# Before navigation\n", ticket, heartbeat);
      await tick();
      const mutation = act(store, action);
      await tick();
      expect(api.saveDoc).not.toHaveBeenCalled();
      expect(mutationCalls()).toHaveLength(0);
      finishHeartbeat();
      expect(await saving).toBe(true);
      await mutation;
      expect(api.saveDoc).toHaveBeenCalledTimes(1);
      expect(mutationCalls()).toHaveLength(1);
    });

  it("orders explicit overwrite behind its captured heartbeat and keeps settle aware of it", async () => {
    const store = await editing();
    let finishHeartbeat!: () => void;
    const heartbeat = new Promise<void>(resolve => { finishHeartbeat = resolve; });
    const ticket = store.markDirty()!;
    api.saveDoc.mockResolvedValue(saved(ticket.version));
    const saving = store.saveOverwriting("# Explicit disk-conflict resolution\n", ticket, heartbeat);
    let settled = false;
    const waiting = store.settle().then(() => { settled = true; });
    await tick();
    expect(settled).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
    finishHeartbeat();
    expect(await saving).toBe(true);
    await waiting;
    expect(api.saveDoc.mock.calls[0].at(-1)).toEqual(ticket.fence);
  });

  it.each(actions)("passes captured binding fence through %s without a document-specific lease", async action => {
    const store = await editing();
    mutationStubs();
    await act(store, action);
    expect(mutationCalls()).toHaveLength(1);
    expect(mutationCalls()[0].at(-1)).toEqual({ backend_epoch: "backend-a", expected_binding_generation: 4 });
  });

  it.each(actions)("does not send unfenced %s from a scoped renderer with unavailable metadata", async action => {
    mockSession.isLegacy = false;
    mockSession.scope = { project_id: "project_a", epoch: "epoch_a" };
    const store = useDocsStore();
    mutationStubs();
    try { await act(store, action); } catch { /* Dialog actions intentionally propagate refusals. */ }
    expect(mutationCalls()).toHaveLength(0);
    expect(store.fenceError).not.toBeNull();
  });

  it.each(actions)("classifies a typed %s refusal as a fence, not a disk conflict", async action => {
    const store = await editing();
    mutationStubs();
    const refusal = new ApiError(409, "wrong generation", "stale_binding_generation");
    for (const stub of [api.createDoc, api.deleteDoc, api.deleteDocFolder,
      api.moveDoc, api.renameDocFolder, api.setDocFolderStyle]) stub.mockRejectedValue(refusal);
    try { await act(store, action); } catch { /* Field dialogs propagate the typed refusal. */ }
    expect(store.fenceError).toBe(refusal);
    expect(store.staleOnDisk).toBe(false);
    expect(await store.saveOverwriting("# Not an escape\n")).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it("refuses saves before registration, retaining dirty text and exposing no receipt", async () => {
    const store = await editing();
    store.setEditorFence(null);
    expect(store.markDirty()).toBeNull();
    expect(await store.save("# Pending registration\n")).toBe(false);
    expect(store.dirty).toBe(true);
    expect(store.staleOnDisk).toBe(false);
    expect(store.lastSaveReceipt).toBeNull();
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it("cannot use an old document's editor lease for another document before registration", async () => {
    const store = await editing();
    api.doc.mockResolvedValue(detail("other.md"));
    await store.select("other.md");
    expect(store.markDirty()).toBeNull();
    expect(await store.save("# Different document\n")).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it("invalidates an old same-G editor callback when its registration is released or replaced", async () => {
    const store = await editing();
    const ticket = store.markDirty()!;
    store.setEditorFence(null);
    store.setEditorFence({ ...fence, editor_id: "replacement", sid: "replacement-room" });
    expect(await store.save("# Old room debounce\n", ticket)).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
    expect(store.dirty).toBe(true);
    expect(store.lastSaveReceipt).toBeNull();
  });

  it("never redisplays Keep mine for a disk notification after a typed admission refusal", async () => {
    const store = await editing();
    store.setLive(null);
    store.subscribe();
    store.markDirty();
    api.saveDoc.mockRejectedValue(new ApiError(409, "reload", "reload_required"));
    await store.save("# My draft\n");
    mockWsHandlers.get("team.doc.changed")?.({ path, title: "Other", author: null });
    expect(store.staleOnDisk).toBe(false);
    expect(store.dirty).toBe(true);
  });

  it.each(["remove", "deleteFolder"] as const)("captures %s before waiting for earlier saves", async action => {
    const store = await editing();
    mutationStubs();
    let finish!: (value: ReturnType<typeof saved>) => void;
    api.saveDoc.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const ticket = store.markDirty()!;
    const saving = store.save("# Saved\n", ticket);
    await tick();
    const removing = act(store, action);
    // Install a rejection handler before the event loop can report an unhandled refusal.
    const rejected = expect(removing).rejects.toThrow("no longer current");
    await metadata(5);
    finish(saved(ticket.version));
    await saving;
    await rejected;
    expect(api.deleteDoc).not.toHaveBeenCalled();
    expect(api.deleteDocFolder).not.toHaveBeenCalled();
    expect(store.open?.path).toBe(path);
  });

  it("captures queued folder style changes before the first write completes", async () => {
    const store = await editing();
    let finish!: (value: DocFolderStyle) => void;
    api.setDocFolderStyle.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const first = store.setFolderStyle("other", { color: "blue" });
    await tick();
    const second = store.setFolderStyle("other", { icon: "book" });
    await metadata(5);
    finish({ path: "other", color: "blue", icon: null });
    expect(await first).toBe(false);
    expect(await second).toBe(false);
    expect(api.setDocFolderStyle).toHaveBeenCalledTimes(1);
  });

  it("does not fall back to legacy once a renderer has used an activated fence", async () => {
    const store = await editing();
    store.setEditorFence(null);
    mockSession.isLegacy = true;
    mockSession.scope = null;
    useSyncStore().remote = null;
    mutationStubs();
    await expect(store.createDocument("new.md")).rejects.toThrow();
    expect(api.createDoc).not.toHaveBeenCalled();
  });
});

describe("materialized editor proof (I8)", () => {
  const path = "features/dash.md";
  const sid = "sid-a";
  const fence: DocumentFence = { backend_epoch: "backend-a", expected_binding_generation: 4,
    editor_id: "editor-a", client_instance_id: "client-a", sid, version: 10 };
  const lease = (changes: Partial<ProjectRemoteEditorLease> = {}): ProjectRemoteEditorLease => ({
    editor_id: "editor-a", backend_epoch: "backend-a", binding_generation: 4,
    document_path: path, client_instance_id: "client-a", version: 10, revision: 73,
    last_saved_version: null, lease_expires_at: 12345, dirty: true, confirmed: false, ...changes,
  });
  const body = { backend_epoch: "backend-a", expected_binding_generation: 4,
    document_path: path, client_instance_id: "client-a", sid, version: 10, revision: 73 };
  const content = "# Собственный snapshot\r\n\r\n  exact bytes\n";
  const confirmed = () => lease({ dirty: false, confirmed: true, last_saved_version: 91 });

  async function editing(dirty = false) {
    mockSession.isLegacy = false;
    mockSession.scope = { project_id: "project_a", epoch: "epoch_a" };
    api.projectRemote.mockResolvedValue({ project_id: "project_a", epoch: "epoch_a",
      backend_epoch: "backend-a", binding_generation: 4, state: "connected" });
    expect(await useSyncStore().loadRemote()).toBe(true);
    const store = useDocsStore();
    api.doc.mockResolvedValue(detail(path));
    await store.select(path);
    store.setLive(sid);
    store.setEditorFence(fence);
    if (dirty) store.markDirty();
    const version = store.captureSave().version;
    store.setEditorLease(lease({ version }));
    api.materializeRemoteEditor.mockResolvedValue({ ...lease({ version }), proof_id: "proof-a" });
    api.confirmRemoteEditorMaterialization.mockResolvedValue({ ...confirmed(), version });
    return store;
  }

  it.each([false, true])("proves exact captured server revision without writing or marking clean (dirty=%s)", async dirty => {
    const store = await editing(dirty);
    const captured = store.captureSave();
    const snapshot = vi.fn();
    const original = store.open;
    useSyncStore().remotePending = true; // same-G drain retains the original ticket
    const capture = vi.spyOn(useSyncStore(), "capture");
    const result = await store.proveMaterialization(captured, sid, content, snapshot);
    const expected = { ...body, version: captured.version };
    expect(api.materializeRemoteEditor).toHaveBeenCalledExactlyOnceWith("editor-a", expected);
    expect(api.confirmRemoteEditorMaterialization).toHaveBeenCalledExactlyOnceWith("editor-a", {
      ...expected, proof_id: "proof-a", content,
    });
    expect(vi.mocked(createApiClient).mock.calls[0]![0]?.scope).toEqual(mockSession.scope);
    expect(capture).not.toHaveBeenCalled();
    expect(snapshot.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(result).toEqual({ ...confirmed(), version: captured.version });
    expect(store.dirty).toBe(dirty);
    expect(store.open).toBe(original);
    expect(store.lastSaveReceipt).toBeNull();
    expect(store.saving).toBe(false);
    expect(store.staleOnDisk).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
    expect(api.docs).not.toHaveBeenCalled();
    expect(api.doc).toHaveBeenCalledTimes(1);
  });

  it("takes the lease revision before any await, not CapturedDocSave.revision", async () => {
    const store = await editing();
    const captured = store.captureSave();
    expect(captured.revision).not.toBe(73);
    let finish!: (value: unknown) => void;
    api.materializeRemoteEditor.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const pending = store.proveMaterialization(captured, sid, content, () => undefined);
    store.setEditorLease(lease({ revision: 74 }));
    finish({ ...lease(), proof_id: "proof-a" });
    await expect(pending).rejects.toMatchObject({ code: "stale_editor_version" });
    expect(api.materializeRemoteEditor.mock.calls[0]![1]).toEqual(body);
    expect(api.confirmRemoteEditorMaterialization).not.toHaveBeenCalled();
    expect(store.fenceError).toBeNull();
  });

  it.each(["backend_epoch", "expected_binding_generation", "editor_id", "client_instance_id", "sid", "version", "path", "live", "revision"])(
    "rejects an altered captured %s before requesting proof", async field => {
      const store = await editing();
      const captured = store.captureSave();
      const changed = ["path", "live", "revision"].includes(field)
        ? { ...captured, [field]: field === "revision" ? 999 : "other" }
        : { ...captured, fence: { ...captured.fence!, [field]: ["version", "expected_binding_generation"].includes(field) ? 999 : "other" } };
      await expect(store.proveMaterialization(changed, sid, content, () => undefined)).rejects.toBeInstanceOf(Error);
      expect(api.materializeRemoteEditor).not.toHaveBeenCalled();
      expect(api.confirmRemoteEditorMaterialization).not.toHaveBeenCalled();
    });

  it.each(["ticket", "confirm"])("rechecks frozen snapshot after %s await", async phase => {
    const store = await editing();
    let current = true;
    const assertSnapshot = () => { if (!current) throw new Error("snapshot changed"); };
    const method = phase === "ticket" ? api.materializeRemoteEditor : api.confirmRemoteEditorMaterialization;
    method.mockImplementationOnce(async () => {
      current = false;
      return phase === "ticket" ? { ...lease(), proof_id: "proof-a" } : confirmed();
    });
    await expect(store.proveMaterialization(store.captureSave(), sid, content, assertSnapshot)).rejects.toThrow("snapshot changed");
    if (phase === "ticket") expect(api.confirmRemoteEditorMaterialization).not.toHaveBeenCalled();
    expect(store.lastSaveReceipt).toBeNull();
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it.each(["ticket", "confirm"])("cannot update or fence a replacement owner after %s await", async phase => {
    const store = await editing();
    const method = phase === "ticket" ? api.materializeRemoteEditor : api.confirmRemoteEditorMaterialization;
    method.mockImplementationOnce(async () => {
      store.setEditorFence({ ...fence, editor_id: "replacement" });
      store.setEditorLease(lease({ editor_id: "replacement", revision: 90 }));
      return phase === "ticket" ? { ...lease(), proof_id: "proof-a" } : confirmed();
    });
    await expect(store.proveMaterialization(store.captureSave(), sid, content, () => undefined)).rejects.toMatchObject({ code: "stale_editor_version" });
    expect(store.fenceError).toBeNull();
    expect(store.captureSave().fence?.editor_id).toBe("replacement");
    expect(store.lastSaveReceipt).toBeNull();
  });

  it.each(["ticket", "confirm"])("rejects a late %s success after binding invalidation", async phase => {
    const store = await editing();
    const captured = store.captureSave();
    const method = phase === "ticket" ? api.materializeRemoteEditor : api.confirmRemoteEditorMaterialization;
    method.mockImplementationOnce(async () => {
      api.projectRemote.mockResolvedValue({ project_id: "project_a", epoch: "epoch_a",
        backend_epoch: "backend-a", binding_generation: 5, state: "connected" });
      await useSyncStore().loadRemote();
      return phase === "ticket" ? { ...lease(), proof_id: "proof-a" } : confirmed();
    });
    await expect(store.proveMaterialization(captured, sid, content, () => undefined)).rejects.toBeInstanceOf(Error);
    expect(store.fenceError).toBeNull();
    if (phase === "ticket") expect(api.confirmRemoteEditorMaterialization).not.toHaveBeenCalled();
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it.each(["pending_edits", "unknown_sid", "unknown_editor", "editor_mismatch", "editor_lease_expired",
    "stale_editor_version", "stale_backend_epoch", "stale_binding_generation", "mutation_busy", null]
    .flatMap(code => (["ticket", "confirm"] as const).map(phase => ({ code, phase }))))(
    "$phase $code proof refusal never offers Keep mine or retries/saves", async ({ code, phase }) => {
      const store = await editing(true);
      const refusal = new ApiError(409, "proof refused", code);
      (phase === "ticket" ? api.materializeRemoteEditor : api.confirmRemoteEditorMaterialization).mockRejectedValueOnce(refusal);
      await expect(store.proveMaterialization(store.captureSave(), sid, content, () => undefined)).rejects.toMatchObject({
        status: 409, code: code ?? "pending_edits", isDiskConflict: false,
      });
      if (["pending_edits", "mutation_busy", "stale_editor_version"].includes(code ?? "pending_edits")) {
        expect(store.fenceError).toBeNull();
      } else expect(store.fenceError).not.toBeNull();
      expect(store.staleOnDisk).toBe(false);
      expect(store.dirty).toBe(true);
      expect(await store.saveOverwriting(content)).toBe(false);
      expect(api.saveDoc).not.toHaveBeenCalled();
      expect(api.materializeRemoteEditor).toHaveBeenCalledTimes(1);
      expect(api.confirmRemoteEditorMaterialization).toHaveBeenCalledTimes(phase === "ticket" ? 0 : 1);
    });

  it.each(["editor_id", "backend_epoch", "binding_generation", "document_path", "client_instance_id", "version", "revision", "proof_id"])(
    "rejects a ticket with mismatched %s", async field => {
      const store = await editing();
      api.materializeRemoteEditor.mockResolvedValueOnce({ ...lease(), proof_id: "proof-a", [field]: field === "proof_id" ? "" : 999 });
      await expect(store.proveMaterialization(store.captureSave(), sid, content, () => undefined)).rejects.toMatchObject({ code: "stale_editor_version" });
      expect(api.confirmRemoteEditorMaterialization).not.toHaveBeenCalled();
    });

  it.each([{ editor_id: "other" }, { revision: 74 }, { version: 11 }, { confirmed: false },
    { dirty: true }, { last_saved_version: null }, { last_saved_version: 0 }])(
    "refuses malformed confirmation %j without adopting it", async changed => {
      const store = await editing();
      api.confirmRemoteEditorMaterialization.mockResolvedValueOnce({ ...confirmed(), ...changed });
      await expect(store.proveMaterialization(store.captureSave(), sid, content, () => undefined)).rejects.toMatchObject({ code: "stale_editor_version" });
      expect(store.lastSaveReceipt).toBeNull();
      expect(store.staleOnDisk).toBe(false);
      expect(api.saveDoc).not.toHaveBeenCalled();
    });

  it("propagates network/confirm errors without saving, normalizing, or retrying", async () => {
    const store = await editing(true);
    const failure = new Error("offline");
    api.confirmRemoteEditorMaterialization.mockRejectedValueOnce(failure);
    await expect(store.proveMaterialization(store.captureSave(), sid, content, () => undefined)).rejects.toBe(failure);
    expect(store.dirty).toBe(true);
    expect(store.staleOnDisk).toBe(false);
    expect(store.lastSaveReceipt).toBeNull();
    expect(api.materializeRemoteEditor).toHaveBeenCalledTimes(1);
    expect(api.confirmRemoteEditorMaterialization).toHaveBeenCalledTimes(1);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it.each(["pending_edits", "mutation_busy", "stale_editor_version"]
    .flatMap(code => (["ticket", "confirm"] as const).map(phase => ({ code, phase }))))(
    "recovers from $phase $code with a newer server lease and own proof, never a save", async ({ code, phase }) => {
      const store = await editing(true);
      const captured = store.captureSave();
      const document = store.open;
      (phase === "ticket" ? api.materializeRemoteEditor : api.confirmRemoteEditorMaterialization)
        .mockRejectedValueOnce(new ApiError(409, "peer-final-save not ready", code));
      await expect(store.proveMaterialization(captured, sid, content, () => undefined)).rejects.toMatchObject({ code });
      expect(store.fenceError).toBeNull();
      expect(store.staleOnDisk).toBe(false);
      expect(await store.saveOverwriting(content)).toBe(false);
      // The caller waits for peer-final-save/ACK and refreshes the neutral server DTO.
      const fresh = lease({ version: captured.version, revision: 74 });
      store.setEditorLease(fresh);
      api.materializeRemoteEditor.mockResolvedValueOnce({ ...fresh, proof_id: "own-proof-after-peer-save" });
      api.confirmRemoteEditorMaterialization.mockResolvedValueOnce({ ...fresh,
        dirty: false, confirmed: true, last_saved_version: 92 });
      const result = await store.proveMaterialization(captured, sid, content, () => undefined);
      expect(result.last_saved_version).toBe(92);
      expect(api.materializeRemoteEditor).toHaveBeenLastCalledWith("editor-a", {
        ...body, version: captured.version, revision: 74,
      });
      expect(api.confirmRemoteEditorMaterialization).toHaveBeenLastCalledWith("editor-a", {
        ...body, version: captured.version, revision: 74, proof_id: "own-proof-after-peer-save", content,
      });
      expect(store.open).toBe(document);
      expect(store.dirty).toBe(true);
      expect(store.lastSaveReceipt).toBeNull();
      expect(store.fenceError).toBeNull();
      expect(api.saveDoc).not.toHaveBeenCalled();
    });

  it.each(["clean", "dirty", "offline", "fenced"] as const)(
    "retains the scoped %s live editor on external delete and exposes a terminal typed fence", async state => {
      const store = await editing(state !== "clean");
      store.subscribe();
      if (state === "offline") store.saveError = "offline";
      if (state === "fenced") {
        api.materializeRemoteEditor.mockRejectedValueOnce(new ApiError(409, "expired", "editor_lease_expired"));
        await expect(store.proveMaterialization(store.captureSave(), sid, content, () => undefined)).rejects.toThrow();
      }
      const document = store.open;
      const dirty = store.dirty;
      const previousFence = store.fenceError;
      mockWsHandlers.get("team.doc.changed")?.({ path, title: null, author: "remote:peer", deleted: true });
      expect(store.open).toBe(document);
      expect(store.open?.content).toBe(document?.content);
      expect(store.dirty).toBe(dirty);
      expect(store.live).toBe(sid);
      expect(store.fenceError).toMatchObject({ status: 409, code: previousFence ? "editor_lease_expired" : "unknown_sid" });
      expect(store.staleOnDisk).toBe(false);
      expect(store.lastSaveReceipt).toBeNull();
      expect(await store.saveOverwriting(content)).toBe(false);
      expect(api.saveDoc).not.toHaveBeenCalled();
      await tick();
      expect(store.open).toBe(document);
    });

  it("does not replace retained scoped live source with a metadata read arriving after delete/reset", async () => {
    const store = await editing(true);
    store.subscribe();
    const document = store.open;
    let finish!: (fresh: DocDetail) => void;
    api.doc.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    mockWsHandlers.get("team.doc.changed")?.({ path, title: "peer-save", author: "remote:peer", deleted: false });
    mockWsHandlers.get("team.doc.changed")?.({ path, title: null, author: "remote:peer", deleted: true });
    finish(detail(path, "# Deleted source from an obsolete read\n"));
    await tick();
    expect(store.open).toBe(document);
    expect(store.dirty).toBe(true);
    expect(store.live).toBe(sid);
    expect(store.fenceError).toMatchObject({ code: "unknown_sid" });
    expect(store.staleOnDisk).toBe(false);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  type RetainedState = "live" | "fenced" | "save-error" | "draining" | "paused" | "post-fence";
  const phase = (state: "draining" | "paused" | "post-fence") => {
    useSyncStore().bindingChange = { change_id: "change-a", expires_at: 9999999999,
      state: state === "post-fence" ? "paused" : state,
      binding_generation: state === "post-fence" ? 5 : 4, expected_generation: 4,
      fenced: state === "post-fence", required_editor_acks: ["editor-a"], missing_acks: ["editor-a"], reason: null };
  };
  async function retained(state: RetainedState) {
    const store = await editing();
    if (state !== "live") {
      store.setLive(null);
      store.setEditorFence({ ...fence, sid: null });
    }
    if (state === "fenced") store.fenceError = new ApiError(409, "expired", "editor_lease_expired");
    else if (state === "save-error") store.saveError = "offline";
    else if (state !== "live") phase(state);
    store.subscribe();
    return store;
  }
  const externalFolder = (action: "rename" | "delete") => mockWsHandlers.get("team.doc.folder_changed")?.(
    action === "rename"
      ? { path: "moved", previous_path: "features", author: "remote:peer", deleted: false }
      : { path: "features", previous_path: null, author: "remote:peer", deleted: true });

  it.each((["live", "fenced", "save-error", "draining", "paused", "post-fence"] as const)
    .flatMap(state => (["rename", "delete"] as const).map(action => ({ state, action }))))(
    "retains clean/unconfirmed $state source on external folder $action with a typed blocker", async ({ state, action }) => {
      const store = await retained(state);
      const document = store.open;
      const originalSid = store.live;
      api.docs.mockResolvedValue(action === "rename" ? [entry("moved/dash.md")] : []);
      externalFolder(action);
      await tick();
      expect(store.open).toBe(document);
      expect(store.open?.path).toBe(path);
      expect(store.open?.content).toBe(document?.content);
      expect(store.dirty).toBe(false);
      expect(store.live).toBe(originalSid);
      expect(store.fenceError).toMatchObject({ status: 409,
        code: state === "fenced" ? "editor_lease_expired" : state === "live" ? "unknown_sid" : "editor_mismatch" });
      expect(store.staleOnDisk).toBe(false);
      expect(api.doc).toHaveBeenCalledTimes(1);
      expect(await store.saveOverwriting(content)).toBe(false);
      expect(api.saveDoc).not.toHaveBeenCalled();
    });

  it.each((["draining", "paused", "fenced", "save-error"] as const)
    .flatMap(state => [false, true].map(deleted => ({ state, deleted }))))(
    "does not reload/close ordinary scoped $state source on document event (deleted=$deleted)", async ({ state, deleted }) => {
      const store = await retained(state);
      const document = store.open;
      mockWsHandlers.get("team.doc.changed")?.({ path, title: null, author: "remote:peer", deleted });
      await tick();
      expect(store.open).toBe(document);
      expect(store.dirty).toBe(false);
      expect(store.live).toBeNull();
      expect(store.fenceError).not.toBeNull();
      expect(store.staleOnDisk).toBe(false);
      expect(api.doc).toHaveBeenCalledTimes(1);
      expect(api.saveDoc).not.toHaveBeenCalled();
    });

  it.each(["rename", "delete"] as const)("retains dirty scoped live RAM on external folder %s without Keep mine", async action => {
    const store = await retained("live");
    store.markDirty();
    const document = store.open;
    api.docs.mockResolvedValue(action === "rename" ? [entry("moved/dash.md")] : []);
    externalFolder(action);
    await tick();
    expect(store.open).toBe(document);
    expect(store.dirty).toBe(true);
    expect(store.live).toBe(sid);
    expect(store.fenceError).toMatchObject({ code: "unknown_sid" });
    expect(store.staleOnDisk).toBe(false);
    expect(await store.saveOverwriting(content)).toBe(false);
    expect(api.doc).toHaveBeenCalledTimes(1);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it.each(["rename", "delete"] as const)("rechecks phase after external folder %s listing await", async action => {
    const store = await editing();
    store.setLive(null);
    store.setEditorFence({ ...fence, sid: null });
    store.subscribe();
    const document = store.open;
    let finish!: (entries: DocEntry[]) => void;
    api.docs.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    externalFolder(action);
    phase("draining");
    finish(action === "rename" ? [entry("moved/dash.md")] : []);
    await tick();
    expect(store.open).toBe(document);
    expect(store.dirty).toBe(false);
    expect(store.live).toBeNull();
    expect(store.fenceError).toMatchObject({ code: "editor_mismatch" });
    expect(store.staleOnDisk).toBe(false);
    expect(api.doc).toHaveBeenCalledTimes(1);
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it.each(["draining", "paused", "fenced", "save-error", "post-fence"] as const)(
    "gap recovery leaves retained ordinary scoped %s source untouched without a read", async state => {
      const store = await retained(state);
      const document = store.open;
      mockWsHandlers.get("system.dropped")?.({ count: 1, resync: true }, mockSession.scope!);
      await tick();
      expect(store.open).toBe(document);
      expect(store.dirty).toBe(false);
      expect(store.live).toBeNull();
      expect(store.staleOnDisk).toBe(false);
      expect(api.doc).toHaveBeenCalledTimes(1);
      expect(api.saveDoc).not.toHaveBeenCalled();
    });

  it.each((["document", "gap"] as const).flatMap(channel =>
    (["draining", "paused"] as const).flatMap(state => [false, true].map(deleted => ({ channel, state, deleted })))))(
    "discards pretransition $channel read when $state starts before response (deleted=$deleted)", async ({ channel, state, deleted }) => {
      const store = await editing();
      store.setLive(null);
      store.setEditorFence({ ...fence, sid: null });
      store.subscribe();
      const document = store.open;
      let finish!: (document: DocDetail) => void;
      let refuse!: (error: Error) => void;
      api.doc.mockImplementationOnce(() => new Promise((resolve, reject) => { finish = resolve; refuse = reject; }));
      if (channel === "document") {
        mockWsHandlers.get("team.doc.changed")?.({ path, title: "changed", author: "remote:peer", deleted: false });
      } else mockWsHandlers.get("system.dropped")?.({ count: 1, resync: true }, mockSession.scope!);
      await tick();
      phase(state);
      if (deleted) refuse(new ApiError(404, "deleted"));
      else finish(detail(path, "# Incoming source must not replace frozen RAM\n"));
      await tick();
      expect(store.open).toBe(document);
      expect(store.dirty).toBe(false);
      expect(store.live).toBeNull();
      expect(store.staleOnDisk).toBe(false);
      expect(store.fenceError).toMatchObject({ code: "editor_mismatch" });
      expect(api.saveDoc).not.toHaveBeenCalled();
    });

  it.each(["rename", "delete"] as const)("still applies explicit own folder %s after releasing the lease", async action => {
    const store = await editing();
    store.setLive(null);
    store.setEditorFence(null);
    if (action === "delete") {
      api.deleteDocFolder.mockResolvedValue({ path: "features", documents: [path] });
      api.docs.mockResolvedValue([]);
      await store.deleteFolder("features");
      expect(store.open).toBeNull();
    } else {
      api.renameDocFolder.mockImplementationOnce(async (_path, _name, receive) => {
        receive({ type: "renamed", path: "moved", previous_path: "features", moved: 1, documents: [] });
        return { type: "done", path: "moved", previous_path: "features", moved: 1, updated: 0, links: 0, failed: 0 };
      });
      api.docs.mockResolvedValue([entry("moved/dash.md")]);
      api.doc.mockResolvedValue(detail("moved/dash.md"));
      await store.renameFolder("features", "moved", () => undefined);
      expect(store.open?.path).toBe("moved/dash.md");
    }
    expect(store.fenceError).toBeNull();
    expect(api.saveDoc).not.toHaveBeenCalled();
  });

  it("requires a server lease DTO and refuses a foreign or regressed lease", async () => {
    const store = await editing();
    expect(() => store.setEditorLease(lease({ editor_id: "other" }))).toThrow();
    expect(() => store.setEditorLease(lease({ revision: 72 }))).toThrow();
    store.setEditorFence(fence);
    await expect(store.proveMaterialization(store.captureSave(), sid, content, () => undefined)).rejects.toMatchObject({ code: "stale_editor_version" });
    expect(api.materializeRemoteEditor).not.toHaveBeenCalled();
  });
});
