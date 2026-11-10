<script setup lang="ts">
/**
 * Project documentation — the team's second medium next to the task board (spec §10.4).
 *
 * One view, not three. `docs/block-editor-spec.md` replaced the source / preview / split
 * arrangement with a single block editor: what is on screen is the document, and Markdown
 * is what it is stored as rather than what it is edited as. The guarantee that makes this
 * safe is §8 — a document opened and not edited is never written, and one that is edited
 * comes back as the same Markdown a person would have typed.
 *
 * `?path=` is honoured so a task can link straight to the document its agent wrote.
 *
 * Every row of the tree has a context menu (block-editor spec §5.2). A folder's creates a document
 * in it, renames it — in a dialog that shows the links into it being mended, document by document
 * — deletes it, and gives it a colour and an icon; a document's deletes it. The header's Delete
 * opens the same confirmation the menu does.
 *
 * **This page owns the live room** (dev spec §16.4). While the project is in step with its
 * server, the open document is edited in a room shared with everyone else who has it open:
 * the page joins it, hands the session to the editor, and leaves it when the document is
 * closed. The editor is keyed on the session as well as on the path, so joining or leaving a
 * room is a new editor — one that is either bound to the room's document or to the file,
 * never something in between. `bind()` is the only place that decides which.
 */
import {
  AArrowDown,
  AArrowUp,
  FilePlus2,
  FileText,
  PanelRight,
  Rows3,
  Rows4,
  RotateCw,
  Save,
  Search,
  Trash2,
  X,
} from "lucide-vue-next";
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  shallowRef,
  watch,
} from "vue";
import { useRoute, useRouter } from "vue-router";

import BlockEditor from "@/components/docs/BlockEditor.vue";
import DocCreateDialog from "@/components/docs/DocCreateDialog.vue";
import DocDeleteDialog from "@/components/docs/DocDeleteDialog.vue";
import DocSidePanel from "@/components/docs/DocSidePanel.vue";
import DocTree from "@/components/docs/DocTree.vue";
import DocTreeMenu from "@/components/docs/DocTreeMenu.vue";
import FolderRenameDialog from "@/components/docs/FolderRenameDialog.vue";
import SidePane from "@/components/layout/SidePane.vue";
import EmptyState from "@/components/ui/EmptyState.vue";
import { formatNumber, t, type MessageKey } from "@/i18n";
import I18nT from "@/i18n/I18nT.vue";
import { seedUpdate } from "@/lib/collab/seed";
import {
  openSession,
  type CollabSession,
  type CollabUser,
  type EndReason,
} from "@/lib/collab/session";
import type { Heading } from "@/lib/editor/outline";
import { titleOf } from "@/lib/md/title";
import { formatTime } from "@/lib/status";
import {
  FONT_SIZE,
  LINE_HEIGHT,
  docsVcsRoot,
  documentPath,
  useDocsStore,
  type DocTreeNode,
  type SaveState,
} from "@/stores/docs";
import { useSessionStore } from "@/stores/session";
import { useSyncStore } from "@/stores/sync";
import { useThemeStore } from "@/stores/theme";
import { useVcsStore } from "@/stores/vcs";

const docs = useDocsStore();
const theme = useThemeStore();
const session = useSessionStore();
const vcs = useVcsStore();
const sync = useSyncStore();
const route = useRoute();
const router = useRouter();

const showPanel = ref(true);
const headings = ref<Heading[]>([]);
const activeHeading = ref<number | null>(null);

const creating = ref(false);
const newPath = ref("");
const renaming = ref(false);
const renamePath = ref("");
const searchOpen = ref(false);

/** The row whose context menu is open, where it opened, and the row to give focus back to. */
const treeMenu = ref<{
  node: DocTreeNode;
  at: { x: number; y: number };
  row: HTMLElement | null;
} | null>(null);
/** The folder the rename dialog is about, while it is open. */
const renameFolderPath = ref<string | null>(null);
/** The folder a new document is being named in, while that dialog is open. */
const createFolderPath = ref<string | null>(null);
/** The document or folder the delete dialog is about, while it is open. */
const deleting = ref<{ path: string; folder: boolean } | null>(null);

const editor = ref<InstanceType<typeof BlockEditor> | null>(null);

/** The room the open document is being edited in, when it is (dev spec §16.4). */
const room = shallowRef<CollabSession | null>(null);
/** A room is being asked for. The editor is not drawn until the answer is in. */
const joining = ref(false);
/** Everyone else in the room, for the header. */
const roomPeers = ref<CollabUser[]>([]);
/** Bumped by every `bind()`, so an answer that arrives for a superseded one is dropped. */
let binding = 0;

/**
 * A different document is a different editor (§7) — and so is the same document in a
 * different room, or in none.
 */
const editorKey = computed(
  () => `${docs.open?.path ?? ""}\u0000${room.value?.sid ?? ""}`,
);

/** Leave *session* — after whatever was saved from it has been sent on its way. */
async function leave(session: CollabSession): Promise<void> {
  // Not before the editor bound to it is gone: closing destroys the document under it.
  await nextTick();
  // The editor's last save is marked as the room's own, and has to be: written after the
  // room is left it would count as a write from outside and end the room for the others.
  await docs.settle();
  if (docs.live === session.sid) docs.setLive(null);
  session.close();
}

/**
 * The editor says the user changed *path*. Ignored for a document that is no longer the
 * open one: an editor on its way out must not mark its successor's document as edited.
 */
function onDirty(path: string): void {
  if (docs.open?.path === path) docs.markDirty();
}

/** The editor hands over Markdown for *path* — written only if that is still the open document. */
function onChange(markdown: string, path: string): void {
  if (docs.open?.path === path) void docs.save(markdown);
}

/**
 * Stop editing in *session* and carry on alone with what is on screen.
 *
 * The text is read out of the editor while it is still the room's, before anything that
 * replaces it, and becomes the document edited alone. If the room had moved on from what
 * this disk last saw, that text is owed a save — an ordinary one, since the room is gone.
 */
function dropToSolo(session: CollabSession): void {
  const markdown = session.changed ? editor.value?.takeMarkdown() : undefined;
  room.value = null;
  roomPeers.value = [];
  joining.value = false;
  docs.setLive(null);
  if (markdown !== undefined && markdown !== docs.open?.content)
    docs.adopt(markdown);
  // After the editor bound to it is gone — the key has just changed.
  void nextTick(() => session.close());
}

/** How many times a room that could not be joined is asked for again, and how often. */
const JOIN_RETRIES = 3;
const JOIN_RETRY_MS = 2500;
let retries = 0;
let retryTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * Decide what the open document is edited in — its room, or the file alone.
 *
 * Everything that changes the answer comes through here: another document, the project
 * falling in or out of step with its server, a room ending. The first half is synchronous
 * on purpose. By the time the page renders again the editor must already be gone or hidden;
 * one frame of the new document's editor bound to the old document's room would show — and
 * could save — the wrong text.
 */
function bind(): void {
  const generation = ++binding;
  const previous = room.value;
  const path = docs.open?.path ?? null;
  const wanted = Boolean(path) && sync.live;
  const focused = editor.value?.hasFocus() ?? false;
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = null;

  // The same document, and no room to be had any more: the project fell out of step.
  if (previous && previous.path === path && !wanted) {
    dropToSolo(previous);
    return;
  }

  // An edit made alone and not saved yet goes to the file first: the room is joined from
  // the revision on disk, and the editor about to replace this one starts from it.
  if (!previous) editor.value?.flush();
  room.value = null;
  roomPeers.value = [];
  joining.value = wanted;

  void (async () => {
    if (previous) await leave(previous);
    else await docs.settle();
    if (generation !== binding) return;
    if (!wanted || !path) return;

    const session = await openSession({ path, seed: seedUpdate });
    if (generation !== binding || docs.open?.path !== path) {
      session?.close();
      return;
    }
    if (session) {
      session.onEnd((reason) => void onRoomEnd(session, reason));
      session.onPeers(() => {
        if (room.value === session) roomPeers.value = session.peers();
      });
      docs.setLive(session.sid);
      room.value = session;
      roomPeers.value = session.peers();
    }
    // With no room to be had the document is simply edited alone, as it always was.
    joining.value = false;
    if (focused) void nextTick(() => editor.value?.focus());
    if (session) retries = 0;
    else scheduleRetry(generation);
  })();
}

/**
 * Ask for the room again in a moment, a few times.
 *
 * A room is refused while the file is not yet in step with the server — a save still on
 * its way up, a change still on its way down — and that passes by itself. Editing alone
 * meanwhile is harmless, but staying alone is not: the next save would be a write from
 * outside the room everybody else is in. Not while there is an unsaved edit, though: asking
 * means a new editor, and that is not something to do to someone mid-sentence.
 */
function scheduleRetry(generation: number): void {
  if (retries >= JOIN_RETRIES) return;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    if (generation !== binding || !sync.live || !docs.open) return;
    if (docs.dirty || docs.saving) {
      scheduleRetry(generation);
      return;
    }
    retries += 1;
    bind();
  }, JOIN_RETRY_MS);
}

/** The room ended without this page leaving it. */
async function onRoomEnd(
  session: CollabSession,
  reason: EndReason,
): Promise<void> {
  if (room.value !== session) return;
  if (reason === "reset") {
    // The file was replaced or deleted from outside the room. What the room held no longer
    // describes it; read the file again and join whatever room there is now.
    binding += 1;
    room.value = null;
    roomPeers.value = [];
    joining.value = true;
    docs.setLive(null);
    // After the editor bound to it is gone — `joining` has just hidden it.
    void nextTick(() => session.close());
    await docs.reopen();
    bind();
    return;
  }
  // The connection went away. What is on screen stays on screen.
  binding += 1;
  dropToSolo(session);
}

watch(
  () => docs.open?.path,
  () => {
    // A different document starts its own count of attempts.
    retries = 0;
  },
);
watch([() => docs.open?.path, () => sync.live], bind, { immediate: true });

const requestedPath = computed(() => {
  const value = route.query.path;
  return typeof value === "string" && value ? value : null;
});

const documents = computed(() =>
  docs.entries.map((entry) => ({ path: entry.path, title: entry.title })),
);

/** The documents folder in the project's frame, so a row can ask version control about itself (§9.6). */
const vcsRoot = computed(() =>
  docsVcsRoot(session.project?.root, session.project?.data_dir),
);

/**
 * The app's own body size, as `tokens.css` resolved it — read back from the DOM rather than
 * restated here, because the scale has one home (§7) and a copy in TypeScript would be a
 * second one to keep in step. Re-read when `theme.text_size` moves, which since the density
 * knob was removed is the only thing that changes it.
 */
const baseTextSize = computed(() => {
  void theme.theme.text_size;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(
    "--vxs-text-size",
  );
  return Number.parseFloat(raw) || 13;
});

/** What the document is rendered at: the size somebody picked, or a point above the app. */
const textSize = computed(() => docs.fontSize ?? baseTextSize.value + 1);

/**
 * `editor.css`'s own leading, read back from the DOM the way the text size is — the
 * stylesheet is the one place that states it. The property is on the column, which only
 * exists once a document is open; the fallback keeps the label meaningful before that.
 */
const baseLineHeight = computed(() => {
  void docs.open;
  const column = document.querySelector(".block-editor-column");
  const raw = column
    ? getComputedStyle(column).getPropertyValue("--vxs-doc-line-height-default")
    : "";
  return Number.parseFloat(raw) || 1.5;
});

/** What the document is rendered with: the leading somebody picked, or the stylesheet's. */
const lineHeight = computed(() => docs.lineHeight ?? baseLineHeight.value);
/** One decimal, in the interface language's own separator — `1.5`, or `1,5`. */
const lineHeightLabel = computed(() =>
  formatNumber(lineHeight.value, {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }),
);

/** Both knobs hand the editor a CSS property; an unset one is left to `editor.css`. */
const editorStyle = computed(() => ({
  "--vxs-doc-text-size": `${textSize.value}px`,
  "--vxs-doc-line-height":
    docs.lineHeight === null ? undefined : String(docs.lineHeight),
}));

/** The indicator's words, as keys — resolved while rendering, so they follow the language. */
const SAVE_LABELS: Record<SaveState, MessageKey> = {
  saved: "docs.saveState.saved",
  saving: "common.saving",
  dirty: "docs.saveState.dirty",
  failed: "docs.saveState.failed",
};

onMounted(async () => {
  docs.subscribe();
  // §9.6: the tree colours documents by version control status, refreshed by the same
  // debounced `explorer.file_changed` the Explorer uses — an agent's `write_doc` is a file
  // write like any other, and the Explorer would show it a moment later anyway.
  vcs.subscribe();
  void vcs.refresh();
  await docs.load();
  if (requestedPath.value) await select(requestedPath.value);
});

// Leaving the view with a pending edit still in the editor would lose it.
onBeforeUnmount(() => {
  editor.value?.flush();
  binding += 1;
  if (retryTimer) clearTimeout(retryTimer);
  const session = room.value;
  room.value = null;
  if (session) void leave(session);
});

watch(requestedPath, async (path) => {
  if (path && path !== docs.open?.path) await select(path);
});

// The address follows the open document wherever it went — including when a folder rename,
// here or in another window, moved it out from under the page — and lets go of it once a
// delete, here or in another window, closed it.
watch(
  () => docs.open?.path,
  (path) => {
    if (path) {
      if (path !== requestedPath.value)
        void router.replace({ name: "docs", query: { path } });
    } else if (requestedPath.value) {
      void router.replace({ name: "docs" });
    }
  },
);

async function select(path: string): Promise<void> {
  // The outgoing document is written before the incoming one replaces it (§7).
  editor.value?.flush();
  await docs.select(path);
  headings.value = [];
  activeHeading.value = null;
}

async function open(path: string) {
  searchOpen.value = false;
  if (docs.open?.path !== path) await select(path);
  await router.replace({ name: "docs", query: { path } });
}

async function create(rawPath: string) {
  const path = documentPath("", rawPath);
  if (!path) return;
  const created = await docs.create(path);
  creating.value = false;
  newPath.value = "";
  if (created) await router.replace({ name: "docs", query: { path: created } });
}

/**
 * §5: `[[` found nothing, so the document is created "in the current folder".
 *
 * Beside the one being edited, and without opening it — the link is already written and the
 * user is still typing the sentence it sits in.
 */
async function createBeside(name: string) {
  const folder = (docs.open?.path ?? "").split("/").slice(0, -1).join("/");
  const path = documentPath(folder, name);
  if (path) await docs.create(path, { open: false });
}

/** A `[[link]]` to something that does not exist yet offers to create it (§5). */
async function followLink(target: string) {
  const path = docs.resolveLink(target);
  if (path) {
    await open(path);
    return;
  }
  creating.value = true;
  newPath.value = target;
}

async function rename() {
  const source = docs.open?.path;
  const target = renamePath.value.trim();
  renaming.value = false;
  if (!source || !target || target === source) return;
  editor.value?.flush();
  const referrers = await docs.rename(source, target);
  if (referrers.length) {
    docs.error = t("docs.banner.renamedReferrers", {
      count: referrers.length,
      paths: referrers.map((entry) => entry.path).join(", "),
    });
  }
  if (docs.open)
    await router.replace({ name: "docs", query: { path: docs.open.path } });
}

function startRename() {
  renamePath.value = docs.open?.path ?? "";
  renaming.value = true;
}

/** A right-click on a row — a folder's menu, or a document's. */
function openTreeMenu(node: DocTreeNode, event: MouseEvent) {
  const row =
    (event.target as HTMLElement | null)?.closest<HTMLElement>(
      "[role='button']",
    ) ?? null;
  let at = { x: event.clientX, y: event.clientY };
  // The context-menu key and Shift+F10 raise the event with no pointer to open at.
  if (!at.x && !at.y && row) {
    const rect = row.getBoundingClientRect();
    at = { x: rect.left + 24, y: rect.bottom };
  }
  treeMenu.value = { node, at, row };
}

function closeTreeMenu() {
  const row = treeMenu.value?.row;
  treeMenu.value = null;
  row?.focus({ preventScroll: true });
}

function restyleFolder(patch: { color?: string | null; icon?: string | null }) {
  if (treeMenu.value?.node.isFolder)
    void docs.setFolderStyle(treeMenu.value.node.path, patch);
}

/**
 * The menu's row, with the menu closed for one of its dialogs.
 *
 * Anything typed is sent on its way now: a dialog is about to cover the editor, and the rename and
 * the delete wait for that save to land before they touch the tree — a save arriving after either
 * would put the old path back.
 */
function leaveMenu(): DocTreeNode | null {
  const node = treeMenu.value?.node ?? null;
  treeMenu.value = null;
  editor.value?.flush();
  return node;
}

function startFolderRename() {
  const node = leaveMenu();
  if (node) renameFolderPath.value = node.path;
}

function startCreate() {
  const node = leaveMenu();
  if (node) createFolderPath.value = node.path;
}

function deleteFromMenu() {
  const node = leaveMenu();
  if (node) deleting.value = { path: node.path, folder: node.isFolder };
}

/** The header's Delete — the open document, through the same confirmation as the menu's. */
function deleteOpen() {
  const path = docs.open?.path;
  if (!path) return;
  editor.value?.flush();
  deleting.value = { path, folder: false };
}

async function onCreated(path: string) {
  const folder = createFolderPath.value;
  createFolderPath.value = null;
  // A folder folded away would hide the document just made in it.
  if (folder && docs.isCollapsed(folder)) docs.toggleFolder(folder);
  await open(path);
}

function onHeadings(next: Heading[]) {
  headings.value = next;
  // The title follows the first heading as it is typed, which is what the tree and the
  // header show — the backend derives the same thing from the file once it is saved.
  docs.setLiveTitle(next[0]?.text ?? titleOf("", docs.open?.path ?? ""));
}

/**
 * `Ctrl+S` (§4.8). The store refuses a document nobody edited, so this is safe to call on a
 * clean one — and it is the only way back from a save that failed, since nothing is pending
 * for the autosave to flush.
 */
async function saveNow() {
  const content = editor.value?.takeMarkdown();
  if (content !== undefined) await docs.save(content);
}

/** Reload from disk, discarding what is in the editor — the answer to a 409 (§14.5). */
async function reload() {
  const path = docs.open?.path;
  if (!path) return;
  await docs.select(path);
  // Said out loud, because the editor refuses to drop unsaved edits on its own: a document
  // arriving through `source` is normally a save's own answer, and older than the screen.
  editor.value?.reload();
}

async function forceSave() {
  const content = editor.value?.takeMarkdown();
  if (content !== undefined) await docs.saveOverwriting(content);
}
</script>

<template>
  <div class="flex h-full">
    <SidePane
      :title="t('docs.title')"
      storage-key="voxarium_studio.docs.pane-width"
      :default-width="256"
      :max="480"
    >
      <template #actions>
        <button
          class="grid size-6 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
          :title="t('common.search')"
          @click="((searchOpen = !searchOpen), (creating = false))"
        >
          <Search class="size-3.5" />
        </button>
        <button
          class="grid size-6 place-items-center rounded-sm text-faint hover:bg-hover hover:text-primary"
          :title="t('docs.newDocument')"
          @click="((creating = !creating), (searchOpen = false))"
        >
          <FilePlus2 class="size-3.5" />
        </button>
      </template>

      <!-- Both fields keep their `autofocus`: the elements are still created and destroyed by
           these `v-if`s in this component's scope, and slot content is not cached across a
           toggle. -->
      <template #top>
        <form
          v-if="creating"
          class="border-b border-line p-2"
          @submit.prevent="create(newPath)"
        >
          <input
            v-model="newPath"
            autofocus
            placeholder="features/dash.md"
            class="w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
          />
          <p class="mt-1 text-small text-faint">
            <I18nT keypath="docs.pane.createHint">
              <template #dir><code class="font-mono">.voxarium_studio/docs/</code></template>
              <template #ext><code class="font-mono">.md</code></template>
            </I18nT>
          </p>
        </form>

        <div v-if="searchOpen" class="border-b border-line p-2">
          <input
            :value="docs.query"
            autofocus
            :placeholder="t('docs.pane.searchPlaceholder')"
            class="w-full rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
            @input="docs.search(($event.target as HTMLInputElement).value)"
          />
        </div>
      </template>

      <!-- The scroller stays in this view rather than inside `SidePane`, as it does in the
           Explorer: slot content is compiled here, so a wrapper with `overflow-auto` in the
           container would move the scroll off the element the caller owns. -->
      <div class="min-h-0 flex-1 overflow-auto py-1">
        <template v-if="searchOpen && docs.query.trim()">
          <button
            v-for="result in docs.results"
            :key="result.path"
            class="vxs-row block w-full px-3 py-1.5 text-left hover:bg-hover"
            @click="open(result.path)"
          >
            <span class="block truncate font-medium">{{ result.title }}</span>
            <span
              v-for="match in result.matches"
              :key="match.line"
              class="block truncate text-small text-faint"
              >{{ match.text }}</span
            >
          </button>
          <p
            v-if="!docs.results.length && !docs.searching"
            class="px-3 py-2 text-faint"
          >
            {{ t("docs.pane.noMatches") }}
          </p>
        </template>

        <template v-else>
          <DocTree
            :nodes="docs.tree"
            :selected="docs.open?.path ?? null"
            :vcs-root="vcsRoot"
            @open="open"
            @context="openTreeMenu"
          />
          <p
            v-if="!docs.entries.length && !docs.loading"
            class="px-3 py-2 text-faint"
          >
            <I18nT keypath="docs.pane.empty">
              <template #tool><code class="font-mono">write_doc</code></template>
            </I18nT>
          </p>
        </template>
      </div>
    </SidePane>

    <section class="flex min-w-0 flex-1 flex-col">
      <p
        v-if="docs.error"
        class="flex shrink-0 items-center gap-2 bg-danger-soft px-pane py-1 text-danger"
      >
        <span class="min-w-0 flex-1">{{ docs.error }}</span>
        <button
          class="shrink-0"
          :title="t('docs.banner.dismiss')"
          @click="docs.error = null"
        >
          <X class="size-3.5" />
        </button>
      </p>

      <!-- §14.5: the file moved under the editor. Nothing is decided for the user, and
           nothing they have on screen is lost either way. -->
      <p
        v-if="docs.staleOnDisk"
        class="flex shrink-0 items-center gap-2 bg-warning-soft px-pane py-1"
      >
        <span class="min-w-0 flex-1">
          {{ t("docs.banner.changedOnDisk") }}
        </span>
        <button
          class="flex shrink-0 items-center gap-1 rounded-sm border border-line px-1.5 py-0.5 hover:bg-hover"
          @click="reload"
        >
          <RotateCw class="size-3" /> {{ t("common.reload") }}
        </button>
        <button
          class="flex shrink-0 items-center gap-1 rounded-sm border border-line px-1.5 py-0.5 hover:bg-hover"
          @click="forceSave"
        >
          <Save class="size-3" /> {{ t("docs.banner.keepMine") }}
        </button>
      </p>

      <template v-if="docs.open">
        <header
          class="vxs-pane flex shrink-0 items-center gap-2 border-b border-pane px-pane py-2"
        >
          <div class="min-w-0 flex-1">
            <form v-if="renaming" @submit.prevent="rename">
              <input
                v-model="renamePath"
                autofocus
                class="w-full rounded-md border border-line bg-inset px-2 py-0.5 font-mono outline-none focus:border-accent"
                @keydown.esc="renaming = false"
                @blur="rename"
              />
            </form>
            <template v-else>
              <p class="truncate font-medium">{{ docs.draftTitle }}</p>
              <button
                class="truncate font-mono text-small text-faint hover:text-muted"
                :title="t('docs.header.renameOrMove')"
                @click="startRename"
              >
                {{ docs.open.path }}
              </button>
            </template>
          </div>

          <!-- §7: the indicator says what the file is doing, next to when it last changed. -->
          <span
            class="shrink-0 text-small"
            :class="docs.saveState === 'failed' ? 'text-danger' : 'text-faint'"
            :title="docs.saveError ?? formatTime(docs.open.updated_at)"
          >
            {{ t(SAVE_LABELS[docs.saveState]) }}
            <template v-if="docs.saveState === 'saved'">
              · {{ formatTime(docs.open.updated_at) }}</template
            >
          </span>

          <!-- Who else is editing this document right now (dev spec §16.4). Each in the
               colour their cursor is drawn in, so a caret in the text can be put to a name. -->
          <div
            v-if="room"
            class="flex shrink-0 items-center gap-1"
            role="group"
            :aria-label="t('sync.room.label')"
          >
            <span
              class="flex items-center gap-1 text-small text-faint"
              :title="t('sync.room.liveHint')"
            >
              <span class="size-1.5 rounded-full bg-success" />
              {{ t("sync.room.live") }}
            </span>
            <span
              v-for="peer in roomPeers"
              :key="peer.id"
              class="collab-avatar"
              :style="{ backgroundColor: peer.color }"
              :title="peer.name"
            >
              {{ peer.name.slice(0, 1).toUpperCase() }}
            </span>
          </div>

          <!-- The reading size, next to the document it applies to rather than buried in
               Settings — it is changed while reading, not while configuring. -->
          <div
            class="flex h-7 shrink-0 items-center rounded-md border border-line"
            role="group"
            :aria-label="t('docs.textSize.label')"
          >
            <button
              class="grid size-7 place-items-center rounded-l-md text-faint hover:bg-hover hover:text-primary disabled:pointer-events-none disabled:opacity-40"
              :disabled="textSize <= FONT_SIZE.min"
              :title="t('docs.textSize.smaller')"
              @click="docs.setFontSize(textSize - 1)"
            >
              <AArrowDown class="size-3.5" />
            </button>
            <button
              class="w-8 self-stretch text-small tabular-nums hover:text-primary"
              :class="docs.fontSize === null ? 'text-faint' : 'text-muted'"
              :title="
                docs.fontSize === null
                  ? t('docs.textSize.following', { size: textSize })
                  : t('docs.textSize.picked', { size: textSize })
              "
              @click="docs.setFontSize(null)"
            >
              {{ textSize }}
            </button>
            <button
              class="grid size-7 place-items-center rounded-r-md text-faint hover:bg-hover hover:text-primary disabled:pointer-events-none disabled:opacity-40"
              :disabled="textSize >= FONT_SIZE.max"
              :title="t('docs.textSize.larger')"
              @click="docs.setFontSize(textSize + 1)"
            >
              <AArrowUp class="size-3.5" />
            </button>
          </div>

          <!-- The leading, beside the size it multiplies. -->
          <div
            class="flex h-7 shrink-0 items-center rounded-md border border-line"
            role="group"
            :aria-label="t('docs.lineSpacing.label')"
          >
            <button
              class="grid size-7 place-items-center rounded-l-md text-faint hover:bg-hover hover:text-primary disabled:pointer-events-none disabled:opacity-40"
              :disabled="lineHeight <= LINE_HEIGHT.min"
              :title="t('docs.lineSpacing.tighter')"
              @click="docs.setLineHeight(lineHeight - LINE_HEIGHT.step)"
            >
              <Rows4 class="size-3.5" />
            </button>
            <button
              class="w-8 self-stretch text-small tabular-nums hover:text-primary"
              :class="docs.lineHeight === null ? 'text-faint' : 'text-muted'"
              :title="
                docs.lineHeight === null
                  ? t('docs.lineSpacing.following', { value: lineHeightLabel })
                  : t('docs.lineSpacing.picked', { value: lineHeightLabel })
              "
              @click="docs.setLineHeight(null)"
            >
              {{ lineHeightLabel }}
            </button>
            <button
              class="grid size-7 place-items-center rounded-r-md text-faint hover:bg-hover hover:text-primary disabled:pointer-events-none disabled:opacity-40"
              :disabled="lineHeight >= LINE_HEIGHT.max"
              :title="t('docs.lineSpacing.looser')"
              @click="docs.setLineHeight(lineHeight + LINE_HEIGHT.step)"
            >
              <Rows3 class="size-3.5" />
            </button>
          </div>

          <button
            class="grid size-7 shrink-0 place-items-center rounded-md border border-line"
            :class="showPanel ? 'bg-accent-soft' : 'text-faint hover:bg-hover'"
            :title="t('docs.header.sidePanel')"
            @click="showPanel = !showPanel"
          >
            <PanelRight class="size-3.5" />
          </button>
          <button
            class="grid size-7 shrink-0 place-items-center rounded-md border border-line text-faint hover:bg-hover hover:text-danger"
            :title="t('common.delete')"
            @click="deleteOpen"
          >
            <Trash2 class="size-3.5" />
          </button>
        </header>

        <!-- The page's one band (`vxs-enter`, §7): the document arrives under its header on a
             navigation here. Around the editor rather than on it, which the `:key` re-mounts
             for every document switched to — that is not a page arriving. -->
        <div class="vxs-enter flex min-h-0 flex-1">
          <!-- Not drawn while a room is being asked for (dev spec §16.4): whichever editor
               comes next — the room's or the file's — must be the first one typed into. -->
          <div v-if="joining" class="min-w-0 flex-1" aria-busy="true" />
          <BlockEditor
            v-else
            ref="editor"
            :key="editorKey"
            :source="docs.open.content"
            :path="docs.open.path"
            :documents="documents"
            :collab="room"
            class="min-w-0 flex-1"
            :style="editorStyle"
            @dirty="onDirty"
            @change="onChange"
            @save="saveNow"
            @headings="onHeadings"
            @active="activeHeading = $event"
            @navigate="followLink"
            @create-document="createBeside"
            @failed="docs.error = $event"
          />
          <DocSidePanel
            v-if="showPanel"
            :headings="headings"
            :active="activeHeading"
            :path="docs.open.path"
            :backlinks="docs.backlinks"
            :links="docs.open.links"
            :graph="docs.graph"
            :graph-loading="docs.graphLoading"
            :graph-error="docs.graphError"
            @go-heading="editor?.scrollTo($event)"
            @open="open"
            @create="followLink"
            @reveal="docs.loadGraph()"
          />
        </div>
      </template>

      <EmptyState
        v-else
        class="vxs-enter"
        :title="t('docs.emptyState.title')"
        :hint="t('docs.emptyState.hint')"
      >
        <template #icon
          ><FileText class="mx-auto mb-3 size-8 text-faint"
        /></template>
      </EmptyState>
    </section>

    <DocTreeMenu
      v-if="treeMenu"
      :at="treeMenu.at"
      :node="treeMenu.node"
      :current="
        treeMenu.node.isFolder ? docs.folderStyle(treeMenu.node.path) : null
      "
      @close="closeTreeMenu"
      @create="startCreate"
      @rename="startFolderRename"
      @delete="deleteFromMenu"
      @restyle="restyleFolder"
    />

    <FolderRenameDialog
      v-if="renameFolderPath"
      :path="renameFolderPath"
      @close="renameFolderPath = null"
    />

    <DocCreateDialog
      v-if="createFolderPath"
      :folder="createFolderPath"
      @close="createFolderPath = null"
      @created="onCreated"
    />

    <DocDeleteDialog
      v-if="deleting"
      :path="deleting.path"
      :folder="deleting.folder"
      @close="deleting = null"
    />
  </div>
</template>
