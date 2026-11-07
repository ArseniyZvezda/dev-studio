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

import { ApiError } from "@/api/client";
import { LINE_HEIGHT } from "@/stores/docs";
import type {
  DocDetail,
  DocEntry,
  DocFolderDeleted,
  DocFolderRenameFrame,
  DocFolderStyle,
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
};

vi.mock("@/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ApiError: actual.ApiError,
    api: new Proxy(
      {},
      { get: (_, key: string) => api[key as keyof typeof api] },
    ),
  };
});

// One socket, owned by the app; the store only subscribes to it. The handlers are kept so a
// check can play an event the way the socket would — `mock` prefix because `vi.mock` is
// hoisted above everything else in the file.
type MockHandler = (payload: Record<string, unknown>) => void;
const mockWsHandlers = new Map<string, MockHandler>();
vi.mock("@/ws/connection", () => ({
  onEvent: (name: string, handler: MockHandler) => {
    mockWsHandlers.set(name, handler);
  },
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
  setActivePinia(createPinia());
  mockWsHandlers.clear();
  for (const stub of Object.values(api)) stub.mockReset();
  api.docs.mockResolvedValue([entry("features/dash.md", "Dash ability")]);
  api.docsGraph.mockResolvedValue({ nodes: [], edges: [] });
  api.docFolders.mockResolvedValue([]);
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
