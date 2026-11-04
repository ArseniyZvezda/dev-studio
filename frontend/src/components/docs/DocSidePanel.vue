<script setup lang="ts">
/**
 * Outline, the link graph and backlinks — the three ways a document sits in a set of
 * documents: its own shape, the picture of the corpus around it, and who points at it.
 *
 * Backlinks come from the backend rather than being derived here: resolving `[[a name]]`
 * to a file needs every document's title and path, and the answer has to match what the
 * renderer decided or the panel and the link would disagree. The graph is the same fact for
 * the whole corpus at once, which is why it arrives as its own lazily fetched payload rather
 * than on the document.
 */
import { CornerUpLeft, Link2Off, ListTree } from 'lucide-vue-next'

import DocLinkGraph from '@/components/docs/DocLinkGraph.vue'
import type { DocBacklink, DocGraph, DocLink } from '@/api/types'
import { t } from '@/i18n'
import type { Heading } from '@/lib/editor/outline'
// Outline entries come from the editor's live document, not from the saved file (§6):
// the panel keeps up with what is being typed, and a heading is identified by its position
// because two sections with the same name are ordinary in a spec.

defineProps<{
  headings: Heading[]
  /** Position of the heading being read, highlighted as the document scrolls (§6). */
  active?: number | null
  /** The open document — the link graph's centre. */
  path: string
  backlinks: DocBacklink[]
  links: DocLink[]
  graph: DocGraph | null
  graphLoading?: boolean
  graphError?: string | null
}>()

const emit = defineEmits<{
  goHeading: [heading: Heading]
  open: [path: string]
  create: [target: string]
  /** The graph section was expanded; the page decides whether to fetch. */
  reveal: []
}>()
</script>

<template>
  <aside class="flex w-64 shrink-0 flex-col overflow-auto border-l border-line bg-surface">
    <section class="border-b border-line-subtle p-3">
      <h2 class="mb-2 flex items-center gap-1.5 text-small tracking-wide text-faint uppercase">
        <ListTree class="size-3" /> {{ t('docs.sidePanel.outline') }}
      </h2>
      <button
        v-for="heading in headings"
        :key="heading.pos"
        class="block w-full truncate rounded-sm py-0.5 text-left hover:bg-hover"
        :style="{ paddingLeft: `${(heading.level - 1) * 10}px` }"
        :class="[
          heading.level === 1 ? 'font-medium' : 'text-muted',
          heading.pos === active && 'bg-accent-soft text-primary',
        ]"
        @click="emit('goHeading', heading)"
      >
        {{ heading.text || '—' }}
      </button>
      <p v-if="!headings.length" class="text-faint">{{ t('docs.sidePanel.noHeadings') }}</p>
    </section>

    <!-- Directly above Backlinks on purpose: the inbound half of the picture is exactly what
         that list names, so the two sit adjacent and agree by colour. `links ?? []` for the
         reason the Unresolved section states below. -->
    <DocLinkGraph
      :graph="graph"
      :path="path"
      :links="links ?? []"
      :loading="graphLoading"
      :error="graphError"
      @open="emit('open', $event)"
      @reveal="emit('reveal')"
    />

    <section class="border-b border-line-subtle p-3">
      <h2 class="mb-2 flex items-center gap-1.5 text-small tracking-wide text-faint uppercase">
        <CornerUpLeft class="size-3" /> {{ t('docs.sidePanel.backlinks') }}
      </h2>
      <div v-for="link in backlinks" :key="link.path" class="mb-2">
        <button class="block w-full truncate text-left hover:underline" @click="emit('open', link.path)">
          {{ link.title }}
        </button>
        <p
          v-for="(context, index) in link.contexts"
          :key="index"
          class="truncate border-l-2 border-line pl-2 text-small text-faint"
          :title="context"
        >
          {{ context }}
        </p>
      </div>
      <p v-if="!backlinks.length" class="text-faint">{{ t('docs.sidePanel.noBacklinks') }}</p>
    </section>

    <!-- `?? []` because a payload without links is a bug worth surviving, not crashing on:
         the panel going quiet is recoverable, a thrown render takes the whole editor down. -->
    <section v-if="(links ?? []).some((link) => !link.resolved)" class="p-3">
      <h2 class="mb-2 flex items-center gap-1.5 text-small tracking-wide text-faint uppercase">
        <Link2Off class="size-3" /> {{ t('docs.sidePanel.unresolved') }}
      </h2>
      <div
        v-for="link in (links ?? []).filter((entry) => !entry.resolved)"
        :key="link.target"
        class="mb-1 flex items-center gap-2"
      >
        <code class="min-w-0 flex-1 truncate font-mono text-small">{{ link.target }}</code>
        <button
          class="shrink-0 rounded-sm border border-line px-1.5 py-0.5 text-small hover:bg-hover"
          @click="emit('create', link.target)"
        >
          {{ t('common.create') }}
        </button>
      </div>
    </section>
  </aside>
</template>
