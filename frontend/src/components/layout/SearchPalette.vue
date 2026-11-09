<script setup lang="ts">
/**
 * The search field's panel (spec §6.1): one input, every provider's hits under it.
 *
 * Opened from the rail's search control or `Ctrl+K`, over whichever page is showing. The
 * Studio indexes nothing itself — the hits come from plugin search providers (§9.7), and
 * the panel renders what they answer: a ranked list as you type, and on `Enter` with no row
 * highlighted the provider's *deep* answer, which for the code graph is `graphify query`'s
 * traversal in prose.
 *
 * A hit with a `path` opens that file in the Explorer; one without (a file outside the
 * project) is shown but cannot be opened, and says so. `Escape` and a click outside are
 * `ModalDialog`'s.
 */
import { Loader2, Search } from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import type { SearchGroup, SearchResult } from '@/api/types'
import ModalDialog from '@/components/ui/ModalDialog.vue'
import CodeCopyButton from '@/components/ui/CodeCopyButton.vue'
import { t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { useSearchStore } from '@/stores/search'
import { useSessionStore } from '@/stores/session'

const emit = defineEmits<{ close: [] }>()

const search = useSearchStore()
const session = useSessionStore()
const router = useRouter()

/**
 * Where the team's documents sit, project-relative and with the trailing slash — `.voxarium_studio/docs/`.
 * A hit under it opens in the Documents section rather than the Explorer, which hides the
 * data directory by default and has no editor for what is in it.
 */
const docsPrefix = computed(() => {
  const dataDir = session.project?.data_dir ?? ''
  const name = dataDir.split(/[\\/]/).filter(Boolean).pop() ?? '.voxarium_studio'
  return `${name}/docs/`
})

const field = ref<HTMLInputElement | null>(null)
const list = ref<HTMLElement | null>(null)
const text = ref(search.query)
/** Index into `hits`, or -1 for "nothing highlighted" — where `Enter` asks the graph. */
const active = ref(-1)
let debounce: number | null = null

const DEBOUNCE_MS = 180

const groups = computed<SearchGroup[]>(() => search.response?.providers ?? [])

/** Every hit across every group, in the order drawn — what the arrow keys walk. */
const hits = computed(() =>
  groups.value.flatMap((group) => group.results.map((hit) => ({ group, hit }))),
)

/** Where each group's first hit sits in `hits`, so a row knows its own index. */
const offsets = computed(() => {
  const map = new Map<string, number>()
  let offset = 0
  for (const group of groups.value) {
    map.set(group.id, offset)
    offset += group.results.length
  }
  return map
})

const hasQuery = computed(() => text.value.trim().length > 0)
const answered = computed(() => search.response !== null && search.response.query === text.value.trim())
const nothing = computed(
  () => answered.value && !search.loading && hits.value.length === 0 && groups.value.every((g) => !g.answer),
)

onMounted(async () => {
  await nextTick()
  field.value?.focus()
  field.value?.select()
})

onBeforeUnmount(() => {
  if (debounce !== null) window.clearTimeout(debounce)
})

function onInput(): void {
  active.value = -1
  if (debounce !== null) window.clearTimeout(debounce)
  debounce = window.setTimeout(() => {
    debounce = null
    void search.run(text.value)
  }, DEBOUNCE_MS)
}

/** `Enter`: open the highlighted hit, or ask the provider for its deep answer. */
function submit(): void {
  if (debounce !== null) {
    window.clearTimeout(debounce)
    debounce = null
  }
  const chosen = hits.value[active.value]
  if (chosen) {
    void open(chosen.hit)
    return
  }
  if (hasQuery.value) void search.run(text.value, true)
}

function move(step: number): void {
  const count = hits.value.length
  if (!count) return
  let next = active.value + step
  if (next < -1) next = count - 1
  if (next >= count) next = -1
  active.value = next
}

// Keep the highlighted row in view as the arrows walk past the edge of the scroller.
watch(active, async () => {
  await nextTick()
  list.value?.querySelector<HTMLElement>('[data-active="true"]')?.scrollIntoView({ block: 'nearest' })
})

async function open(hit: SearchResult): Promise<void> {
  if (!hit.path) return
  if (hit.path.startsWith(docsPrefix.value)) {
    await router.push({ path: '/documents', query: { path: hit.path.slice(docsPrefix.value.length) } })
  } else {
    await router.push({ path: '/explorer', query: { path: hit.path } })
  }
  emit('close')
}

function indexOf(group: SearchGroup, position: number): number {
  return (offsets.value.get(group.id) ?? 0) + position
}
</script>

<template>
  <!-- `instant`: the palette is opened from the keyboard, all day, with the query already on its
       way — it neither arrives nor leaves (§7). -->
  <ModalDialog
    :label="t('common.search')"
    panel-class="mt-[8vh] w-[44rem] max-w-full self-start"
    instant
    @close="emit('close')"
  >
    <form class="flex items-center gap-2 border-b border-line px-3 py-2" @submit.prevent="submit">
      <Search class="size-4 shrink-0 text-faint" />
      <input
        ref="field"
        v-model="text"
        type="text"
        class="min-w-0 flex-1 bg-transparent py-1 outline-none placeholder:text-faint"
        :placeholder="t('search.placeholder')"
        autocomplete="off"
        spellcheck="false"
        @input="onInput"
        @keydown.down.prevent="move(1)"
        @keydown.up.prevent="move(-1)"
        @keydown.enter.prevent="submit"
      />
      <Loader2 v-if="search.loading" class="size-4 shrink-0 animate-spin text-faint" />
      <kbd class="shrink-0 font-mono text-small text-faint">Esc</kbd>
    </form>

    <div ref="list" class="max-h-[60vh] min-h-0 overflow-y-auto">
      <p v-if="search.error" class="px-4 py-3 text-danger">{{ search.error }}</p>

      <p v-if="!hasQuery" class="px-4 py-6 text-center text-muted">
        <I18nT keypath="search.hint">
          <template #arrows><kbd class="font-mono text-small">↑↓</kbd></template>
          <template #enter><kbd class="font-mono text-small">Enter</kbd></template>
        </I18nT>
      </p>

      <template v-for="group in groups" :key="group.id">
        <p
          v-if="groups.length > 1"
          class="px-4 pt-3 pb-1 text-small tracking-wide text-faint uppercase"
        >
          {{ group.label }}
        </p>
        <p v-if="group.error" class="px-4 py-2 text-small text-danger">{{ group.error }}</p>

        <ul v-if="group.results.length" class="py-1">
          <li v-for="(hit, position) in group.results" :key="position">
            <button
              type="button"
              class="flex w-full items-center gap-3 px-4 py-1.5 text-left disabled:opacity-60"
              :class="indexOf(group, position) === active ? 'bg-hover' : 'hover:bg-hover'"
              :data-active="indexOf(group, position) === active"
              :disabled="!hit.path"
              :title="
                hit.path
                  ? hit.path.startsWith(docsPrefix)
                    ? t('search.openInDocuments', { path: hit.path.slice(docsPrefix.length) })
                    : t('search.openInExplorer', { path: hit.path })
                  : t('search.outsideProject')
              "
              @click="open(hit)"
              @pointermove="active = indexOf(group, position)"
            >
              <span class="w-14 shrink-0 rounded-sm bg-inset px-1.5 py-0.5 text-center text-small text-faint">
                {{ hit.kind ?? '' }}
              </span>
              <span class="flex min-w-0 flex-1 flex-col">
                <span
                  class="truncate"
                  :class="hit.kind === 'note' || hit.kind === 'document' || hit.kind === 'concept' ? '' : 'font-mono text-[12px]'"
                >
                  {{ hit.title }}
                </span>
                <!-- What the documents pass wrote about the hit — a one-line summary — under
                     its title; code hits have none. -->
                <span v-if="hit.snippet" class="truncate text-small text-faint">{{ hit.snippet }}</span>
              </span>
              <span v-if="hit.subtitle" class="max-w-[45%] shrink-0 truncate text-small text-faint">
                {{ hit.subtitle }}
              </span>
            </button>
          </li>
        </ul>

        <div v-if="group.answer" class="mx-4 my-3 rounded-md border border-line bg-inset">
          <p class="flex items-center gap-2 border-b border-line px-3 py-1 text-small text-faint">
            <span class="min-w-0 flex-1">{{ t('search.answer', { provider: group.label }) }}</span>
            <CodeCopyButton :text="group.answer" inline />
          </p>
          <pre class="max-h-72 overflow-auto p-3 font-mono text-[12px] leading-relaxed whitespace-pre-wrap">{{
            group.answer
          }}</pre>
        </div>

        <p v-if="group.note" class="px-4 pb-2 text-small text-faint">{{ group.note }}</p>
      </template>

      <p v-if="nothing" class="px-4 py-4 text-center text-muted">{{ t('search.nothing') }}</p>
    </div>

    <footer class="flex items-center gap-4 border-t border-line px-4 py-1.5 text-small text-faint">
      <span>
        <I18nT keypath="search.footer.move">
          <template #keys><kbd class="font-mono">↑↓</kbd></template>
        </I18nT>
      </span>
      <span>
        <I18nT keypath="search.footer.open">
          <template #key><kbd class="font-mono">Enter</kbd></template>
        </I18nT>
      </span>
      <span>
        <I18nT keypath="search.footer.ask">
          <template #key><kbd class="font-mono">Enter</kbd></template>
        </I18nT>
      </span>
    </footer>
  </ModalDialog>
</template>
