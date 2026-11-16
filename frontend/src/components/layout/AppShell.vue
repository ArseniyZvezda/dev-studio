<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'

import { useSearchStore } from '@/stores/search'

import ChatWidget from '@/components/chat/ChatWidget.vue'

import NotificationToasts from './NotificationToasts.vue'
import SearchPalette from './SearchPalette.vue'
import SideNav from './SideNav.vue'
import StudioStatusBar from './StudioStatusBar.vue'

const search = useSearchStore()

/**
 * `Ctrl+K` (or `Cmd+K`) opens the search panel from anywhere — the same chord every
 * editor and every app with a palette uses (§6.1). Only while a provider exists: with
 * nothing to answer, the chord does what the browser would have done.
 *
 * The block editor binds the same chord for links and handles it first, on its own
 * element; that arrives here with `defaultPrevented` set, and anything else editable is
 * left to its own devices too.
 */
function onKeydown(event: KeyboardEvent): void {
  if (!(event.ctrlKey || event.metaKey) || event.shiftKey || event.altKey) return
  if (event.key.toLowerCase() !== 'k' || event.defaultPrevented || !search.available) return
  const target = event.target as HTMLElement | null
  if (target?.isContentEditable) return
  event.preventDefault()
  search.show()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="studio-shell">
    <div class="relative flex min-h-0 min-w-0">
      <SideNav />
      <main class="vxs-main min-w-0 flex-1">
        <!-- §6.1, §7: all routes, including plugin pages, share this surface. -->
        <div class="vxs-page-surface">
          <!-- §5.3.2: only changing the pipeline replaces its parameter writer. -->
          <RouterView v-slot="{ Component, route }">
            <component :is="Component" :key="route.name === 'pipeline-editor' ? String(route.params.id) : undefined" />
          </RouterView>
        </div>
      </main>
      <ChatWidget />
    </div>
    <StudioStatusBar />
    <SearchPalette v-if="search.open" @close="search.hide()" />
    <!-- What an automation's *notify* action shows, over every section (§6.5). -->
    <NotificationToasts />
  </div>
</template>
