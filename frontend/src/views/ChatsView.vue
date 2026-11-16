<script setup lang="ts">
import { useRouter } from 'vue-router'
import { setDesktopPageSelector, useDesktopPageTarget } from '@/desktop/page-state'
import { useChatStore } from '@/stores/chat'

const router = useRouter()
// §5.3.6: wait for the persistent widget's shared loader before acknowledging
// a restored conversation. No popup/draft/File state enters the descriptor.
useDesktopPageTarget('/chats', async (page, current) => {
  const chat = useChatStore()
  await chat.load()
  current()
  const id = page.query?.chat
  if (id) {
    await chat.openChat(id)
    current()
    if (chat.view !== 'chat' || chat.activeChatId !== id || chat.error) return false
  } else chat.showList()
  setDesktopPageSelector('/chats', 'chat', id ?? null)
  if (router.currentRoute.value.query.chat) await router.replace({ path: '/chats' })
  return !chat.error
})
</script>

<template>
  <!-- ChatWidget stays mounted in AppShell and teleports here after this view renders.
       The page and popup share drafts, uploads and the active conversation (§10.9). -->
  <div id="chats-page" class="h-full min-h-0" />
</template>
