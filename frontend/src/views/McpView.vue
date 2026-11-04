<script setup lang="ts">
/**
 * MCP servers for the project's Claude agents (§10.7).
 *
 * The page edits `.mcp.json` at the project root — Claude Code's own file, so what is
 * configured here is what the developer's terminal sees too. Two consequences shape the
 * UI: the on/off switch does **not** delete anything (it writes to `config.yaml`, so the
 * terminal keeps the server), and when the file will not parse the form is replaced by
 * the raw editor rather than overwriting whatever is in there.
 *
 * **One entry comes from no file.** The Studio's own `voxarium_studio` server is in-process — it
 * is built out of the running orchestrator, which is what lets its tools obey the same
 * routing rules as a click in the UI (§10.4) — so there is nothing in `.mcp.json` to
 * describe it. It is listed first anyway, because a page called "MCP Servers" that omits
 * the one every agent actually uses reads as if the Studio gave agents nothing. It carries
 * no Edit and no Delete: neither has anything to act on, and the backend refuses both.
 * Disable is the one control it keeps, since that switch was always Studio-side.
 */
import {
  AlertTriangle,
  FileJson,
  Package,
  Pencil,
  Plug,
  Plus,
  Power,
  Trash2,
} from 'lucide-vue-next'
import { computed, onMounted, reactive, ref } from 'vue'

import { api } from '@/api/client'
import type { McpOverview, McpServerEntry, McpServerInput, McpTransport } from '@/api/types'
import EmptyState from '@/components/ui/EmptyState.vue'
import LoadingBars from '@/components/ui/LoadingBars.vue'
import { t, type MessageKey } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'

const overview = ref<McpOverview | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const busy = ref<string | null>(null)

/** The name being edited, `''` while adding, `null` when the form is closed. */
const editing = ref<string | null>(null)
const showRaw = ref(false)
const rawDraft = ref('')

const form = reactive({
  name: '',
  type: 'stdio' as McpTransport,
  command: '',
  args: '',
  env: '',
  url: '',
  headers: '',
})

const servers = computed(() => overview.value?.servers ?? [])
/** What `.mcp.json` holds — the built-in entry is not part of "the file is empty". */
const fileServers = computed(() => servers.value.filter((entry) => !entry.builtin))
const rawDirty = computed(() => rawDraft.value !== (overview.value?.text ?? ''))

onMounted(load)

async function load() {
  loading.value = true
  try {
    apply(await api.mcp())
  } catch (cause) {
    fail(cause)
  } finally {
    loading.value = false
  }
}

function apply(result: McpOverview) {
  overview.value = result
  error.value = null
  if (!showRaw.value || !rawDirty.value) rawDraft.value = result.text
}

function fail(cause: unknown) {
  error.value = cause instanceof Error ? cause.message : String(cause)
}

/** One value per line: an argument may contain spaces, so nothing is split on them. */
function linesToList(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

/** `KEY=value` per line. Split on the first `=` only, so a token may contain one. */
function linesToMap(text: string): Record<string, string> {
  const map: Record<string, string> = {}
  for (const line of linesToList(text)) {
    const at = line.indexOf('=')
    if (at <= 0) throw new Error(t('mcp.form.notKeyValue', { line }))
    map[line.slice(0, at).trim()] = line.slice(at + 1).trim()
  }
  return map
}

/** How many entries a map holds, as a plural message: `2 headers`. */
function count(map: Record<string, string>, key: MessageKey): string {
  return t(key, { count: Object.keys(map).length })
}

function mapToLines(map: Record<string, string>): string {
  return Object.entries(map)
    .map(([key, value]) => `${key}=${value}`)
    .join('\n')
}

function startCreate() {
  Object.assign(form, {
    name: '',
    type: 'stdio' as McpTransport,
    command: '',
    args: '',
    env: '',
    url: '',
    headers: '',
  })
  editing.value = ''
  error.value = null
}

function startEdit(server: McpServerEntry) {
  // The template offers no Edit on the built-in entry; this is the guard that makes that
  // a rule rather than a layout accident — there is no file entry behind it to submit.
  if (server.builtin) return
  Object.assign(form, {
    name: server.name,
    type: server.type as McpTransport,
    command: server.command ?? '',
    args: server.args.join('\n'),
    env: mapToLines(server.env),
    url: server.url ?? '',
    headers: mapToLines(server.headers),
  })
  editing.value = server.name
  error.value = null
}

async function submit() {
  const name = form.name.trim()
  if (!name) {
    error.value = t('mcp.form.nameRequired')
    return
  }

  let body: McpServerInput
  try {
    body =
      form.type === 'stdio'
        ? {
            type: 'stdio',
            command: form.command.trim(),
            args: linesToList(form.args),
            env: linesToMap(form.env),
          }
        : {
            type: form.type,
            url: form.url.trim(),
            headers: linesToMap(form.headers),
          }
  } catch (cause) {
    fail(cause)
    return
  }

  busy.value = name
  try {
    // A rename is a new entry plus a removal — the file keys servers by name, and the
    // backend upserts at the name in the path.
    const previous = editing.value
    apply(await api.saveMcpServer(name, body))
    if (previous && previous !== name) apply(await api.deleteMcpServer(previous))
    editing.value = null
  } catch (cause) {
    fail(cause)
  } finally {
    busy.value = null
  }
}

async function remove(server: McpServerEntry) {
  if (server.builtin) return
  if (!window.confirm(t('mcp.servers.confirmRemove', { name: server.name }))) return
  busy.value = server.name
  try {
    apply(await api.deleteMcpServer(server.name))
    if (editing.value === server.name) editing.value = null
  } catch (cause) {
    fail(cause)
  } finally {
    busy.value = null
  }
}

async function toggle(server: McpServerEntry) {
  busy.value = server.name
  try {
    apply(await api.setMcpServerEnabled(server.name, !server.enabled))
  } catch (cause) {
    fail(cause)
  } finally {
    busy.value = null
  }
}

async function setStrict(value: boolean) {
  busy.value = '*'
  try {
    apply(await api.setMcpStrict(value))
  } catch (cause) {
    fail(cause)
  } finally {
    busy.value = null
  }
}

async function saveRaw() {
  busy.value = '*'
  try {
    const result = await api.saveMcpFile(rawDraft.value)
    rawDraft.value = result.text
    apply(result)
  } catch (cause) {
    fail(cause)
  } finally {
    busy.value = null
  }
}

function openRaw() {
  rawDraft.value = overview.value?.text ?? ''
  showRaw.value = true
}
</script>

<template>
  <div class="h-full overflow-auto">
    <header class="vxs-pane border-b border-pane bg-surface px-pane py-2">
      <h1 class="text-title font-medium">{{ t('mcp.title') }}</h1>
      <p class="truncate text-small text-faint">
        <template v-if="overview && !overview.exists">
          {{ t('mcp.notCreated', { path: overview.path }) }}
        </template>
        <template v-else>{{ overview?.path ?? '.mcp.json' }}</template>
      </p>
    </header>

    <p v-if="error" class="bg-danger-soft px-pane py-1 text-danger">{{ error }}</p>

    <!-- The introduction and each section are the page's bands (`vxs-enter`, §7), arriving
         top-down. Not the add/edit form: it opens on a click, and a band waits its turn. -->
    <div class="max-w-3xl space-y-6 p-pane">
      <LoadingBars v-if="loading" />

      <template v-else>
        <p class="vxs-enter text-muted">{{ t('mcp.intro') }}</p>

        <!-- A document that will not parse: the form is withheld rather than made to
             overwrite it, and the raw editor is offered instead. -->
        <section
          v-if="overview?.error"
          class="vxs-enter rounded-lg border border-danger-soft bg-danger-soft p-3"
        >
          <p class="flex items-center gap-2 font-medium text-danger">
            <AlertTriangle class="size-4 shrink-0" /> {{ t('mcp.parseError.title') }}
          </p>
          <p class="mt-1 font-mono text-small text-danger">{{ overview.error }}</p>
          <p class="mt-1 text-muted">{{ t('mcp.parseError.hint') }}</p>
          <button
            class="mt-2 inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover"
            @click="openRaw"
          >
            <FileJson class="size-3.5" /> {{ t('mcp.parseError.editFile') }}
          </button>
        </section>

        <template v-else>
          <section class="vxs-enter">
            <div class="mb-2 flex items-center justify-between">
              <h2 class="font-medium">{{ t('mcp.servers.title') }}</h2>
              <button
                class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover"
                @click="startCreate"
              >
                <Plus class="size-3.5" /> {{ t('mcp.servers.add') }}
              </button>
            </div>

            <ul class="space-y-2">
              <li
                v-for="server in servers"
                :key="server.name"
                class="rounded-lg border bg-surface p-3"
                :class="[
                  server.builtin ? 'border-accent-border' : 'border-line',
                  server.enabled ? '' : 'opacity-60',
                ]"
              >
                <div class="flex items-start gap-3">
                  <Package v-if="server.builtin" class="mt-0.5 size-5 shrink-0 text-accent" />
                  <Plug v-else class="mt-0.5 size-5 shrink-0 text-accent" />

                  <div class="min-w-0 flex-1">
                    <p class="flex items-baseline gap-2">
                      <span class="truncate font-medium">{{ server.name }}</span>
                      <span
                        class="rounded-sm px-1.5 font-mono text-small"
                        :class="server.builtin ? 'bg-accent-soft text-accent' : 'bg-inset text-faint'"
                      >
                        {{ server.builtin ? t('mcp.servers.builtin') : server.type }}
                      </span>
                      <span v-if="!server.enabled" class="text-small text-faint">
                        {{ t('mcp.servers.disabled') }}
                      </span>
                    </p>
                    <p class="truncate font-mono text-small text-muted">
                      <template v-if="server.builtin">{{ server.tools.join(' · ') }}</template>
                      <template v-else-if="server.type === 'stdio'">
                        {{ [server.command, ...server.args].join(' ') }}
                      </template>
                      <template v-else>{{ server.url }}</template>
                    </p>
                    <p
                      class="mt-1 text-small"
                      :class="server.available === false ? 'text-danger' : 'text-faint'"
                    >
                      <template v-if="server.builtin">{{ t('mcp.servers.builtinDescription') }}</template>
                      <template v-else-if="server.available === false">{{ server.detail }}</template>
                      <template v-else-if="server.available">
                        {{ t('mcp.servers.onPath', { path: server.detail }) }}
                      </template>
                      <template v-else>{{ t('mcp.servers.remote') }}</template>
                      ·
                      <I18nT keypath="mcp.servers.tools">
                        <template #prefix>
                          <span class="font-mono">{{ server.tool_prefix }}*</span>
                        </template>
                      </I18nT>
                      <template v-if="Object.keys(server.env).length">
                        · {{ count(server.env, 'mcp.servers.envCount') }}
                      </template>
                      <template v-if="Object.keys(server.headers).length">
                        · {{ count(server.headers, 'mcp.servers.headerCount') }}
                      </template>
                    </p>
                  </div>

                  <div class="flex shrink-0 gap-2">
                    <!-- Edit and Delete are absent on the built-in entry, not disabled:
                         there is no `.mcp.json` entry behind it to edit or remove, and a
                         greyed button invites a click that can only ever be refused. -->
                    <button
                      v-if="!server.builtin"
                      class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                      :disabled="busy === server.name"
                      @click="startEdit(server)"
                    >
                      <Pencil class="size-3.5" /> {{ t('common.edit') }}
                    </button>
                    <button
                      class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                      :disabled="busy === server.name"
                      :title="
                        server.builtin
                          ? server.enabled
                            ? t('mcp.servers.disableBuiltin')
                            : t('mcp.servers.enableBuiltin')
                          : server.enabled
                            ? t('mcp.servers.disableFile')
                            : t('mcp.servers.enableFile')
                      "
                      @click="toggle(server)"
                    >
                      <Power class="size-3.5" />
                      {{ server.enabled ? t('common.disable') : t('common.enable') }}
                    </button>
                    <button
                      v-if="!server.builtin"
                      class="inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 hover:bg-hover disabled:opacity-50"
                      :disabled="busy === server.name"
                      :title="t('mcp.servers.removeTitle')"
                      @click="remove(server)"
                    >
                      <Trash2 class="size-3.5" />
                    </button>
                  </div>
                </div>
                <p v-if="server.builtin" class="mt-2 text-small text-faint">
                  <I18nT keypath="mcp.servers.builtinNote">
                    <template #file><code class="font-mono">.mcp.json</code></template>
                  </I18nT>
                </p>
              </li>
            </ul>

            <EmptyState
              v-if="!fileServers.length && editing === null"
              :title="t('mcp.servers.emptyTitle')"
              :hint="t('mcp.servers.emptyHint')"
            >
              <template #icon><Plug class="mx-auto mb-3 size-8 text-faint" /></template>
            </EmptyState>

            <!-- Entries the file has and the Studio cannot read. Shown rather than
                 swallowed: they are invisible to agents, and the raw editor is the fix. -->
            <ul v-if="overview?.invalid.length" class="mt-2 space-y-1">
              <li
                v-for="entry in overview.invalid"
                :key="entry.name"
                class="rounded-md border border-line bg-inset p-2 text-small"
              >
                <span class="font-medium">{{ entry.name }}</span>
                <span class="text-danger"> — {{ entry.error }}</span>
                <span class="text-faint"> · {{ t('mcp.servers.notPassed') }}</span>
              </li>
            </ul>
          </section>

          <!-- add / edit form -->
          <section v-if="editing !== null" class="rounded-lg border border-line bg-surface p-3">
            <h2 class="mb-3 font-medium">
              {{ editing ? t('mcp.form.editTitle', { name: editing }) : t('mcp.form.addTitle') }}
            </h2>

            <div class="space-y-3">
              <label class="flex items-center gap-3">
                <span class="w-24 shrink-0 text-muted">{{ t('mcp.form.name') }}</span>
                <input
                  v-model="form.name"
                  class="flex-1 rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
                  placeholder="github"
                />
              </label>
              <p class="pl-27 text-small text-faint">
                <I18nT keypath="mcp.form.prefixHint">
                  <template #prefix>
                    <span class="font-mono">mcp__{{ form.name || 'name' }}__</span>
                  </template>
                </I18nT>
              </p>

              <label class="flex items-center gap-3">
                <span class="w-24 shrink-0 text-muted">{{ t('mcp.form.transport') }}</span>
                <select
                  v-model="form.type"
                  class="rounded-md border border-line bg-inset px-2 py-1 outline-none focus:border-accent"
                >
                  <option value="stdio">{{ t('mcp.form.transports.stdio') }}</option>
                  <option value="http">{{ t('mcp.form.transports.http') }}</option>
                  <option value="sse">{{ t('mcp.form.transports.sse') }}</option>
                </select>
              </label>

              <template v-if="form.type === 'stdio'">
                <label class="flex items-center gap-3">
                  <span class="w-24 shrink-0 text-muted">{{ t('mcp.form.command') }}</span>
                  <input
                    v-model="form.command"
                    class="flex-1 rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
                    placeholder="npx"
                  />
                </label>
                <label class="flex items-start gap-3">
                  <span class="w-24 shrink-0 pt-1 text-muted">{{ t('mcp.form.arguments') }}</span>
                  <textarea
                    v-model="form.args"
                    rows="3"
                    class="flex-1 rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
                    placeholder="-y&#10;@modelcontextprotocol/server-filesystem"
                  />
                </label>
                <p class="pl-27 text-small text-faint">{{ t('mcp.form.argumentsHint') }}</p>
                <label class="flex items-start gap-3">
                  <span class="w-24 shrink-0 pt-1 text-muted">{{ t('mcp.form.environment') }}</span>
                  <textarea
                    v-model="form.env"
                    rows="2"
                    class="flex-1 rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
                    placeholder="API_TOKEN=…"
                  />
                </label>
              </template>

              <template v-else>
                <label class="flex items-center gap-3">
                  <span class="w-24 shrink-0 text-muted">URL</span>
                  <input
                    v-model="form.url"
                    class="flex-1 rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
                    placeholder="https://example.com/mcp"
                  />
                </label>
                <label class="flex items-start gap-3">
                  <span class="w-24 shrink-0 pt-1 text-muted">{{ t('mcp.form.headers') }}</span>
                  <textarea
                    v-model="form.headers"
                    rows="2"
                    class="flex-1 rounded-md border border-line bg-inset px-2 py-1 font-mono outline-none focus:border-accent"
                    placeholder="Authorization=Bearer …"
                  />
                </label>
              </template>

              <p class="pl-27 text-small text-faint">
                <I18nT keypath="mcp.form.keyValueHint">
                  <template #format><span class="font-mono">KEY=value</span></template>
                </I18nT>
              </p>

              <div class="flex gap-2 pl-27">
                <button
                  class="rounded-md border border-line bg-accent-soft px-3 py-1 hover:bg-hover disabled:opacity-50"
                  :disabled="busy !== null"
                  @click="submit"
                >
                  {{ t('common.save') }}
                </button>
                <button
                  class="rounded-md border border-line px-3 py-1 hover:bg-hover"
                  @click="editing = null"
                >
                  {{ t('common.cancel') }}
                </button>
              </div>
            </div>
          </section>
        </template>

        <section class="vxs-enter">
          <h2 class="mb-2 font-medium">{{ t('mcp.agents.title') }}</h2>
          <div class="space-y-3 rounded-lg border border-line bg-surface p-3">
            <label class="flex items-start gap-3">
              <input
                type="checkbox"
                class="mt-1 accent-[var(--vxs-accent)]"
                :checked="overview?.strict ?? true"
                :disabled="busy !== null"
                @change="setStrict(($event.target as HTMLInputElement).checked)"
              />
              <span>
                {{ t('mcp.agents.strict') }}
                <span class="block text-small text-faint">{{ t('mcp.agents.strictHint') }}</span>
              </span>
            </label>
            <p class="text-small text-faint">
              <I18nT keypath="mcp.agents.autoAllowed">
                <template #rule><span class="font-mono">mcp__&lt;name&gt;</span></template>
              </I18nT>
            </p>
          </div>
        </section>

        <section class="vxs-enter">
          <button
            class="inline-flex items-center gap-1.5 text-muted hover:text-primary"
            @click="showRaw ? (showRaw = false) : openRaw()"
          >
            <FileJson class="size-3.5" />
            {{ showRaw ? t('mcp.raw.hide') : t('mcp.raw.show') }}
          </button>

          <div v-if="showRaw" class="mt-2 space-y-2">
            <textarea
              v-model="rawDraft"
              rows="14"
              spellcheck="false"
              class="w-full rounded-md border border-line bg-inset p-2 font-mono text-[12px] outline-none focus:border-accent"
            />
            <div class="flex items-center gap-2">
              <button
                class="rounded-md border border-line bg-accent-soft px-3 py-1 hover:bg-hover disabled:opacity-50"
                :disabled="busy !== null || !rawDirty"
                @click="saveRaw"
              >
                {{ t('mcp.raw.save') }}
              </button>
              <button
                class="rounded-md border border-line px-3 py-1 hover:bg-hover disabled:opacity-50"
                :disabled="!rawDirty"
                @click="rawDraft = overview?.text ?? ''"
              >
                {{ t('mcp.raw.revert') }}
              </button>
              <span class="text-small text-faint">{{ t('mcp.raw.hint') }}</span>
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>
