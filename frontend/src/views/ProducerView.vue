<script setup lang="ts">
/** Producer workspace: one pinned conversation and a horizontal live task strip (§10.11). */
import { ChevronLeft, ChevronRight, MessagesSquare, Plus, Trash2 } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import ProducerControls from '@/components/chat/ProducerControls.vue'
import ChatHeaderChips from '@/components/chat/ChatHeaderChips.vue'
import ProducerPlan from '@/components/producer/ProducerPlan.vue'
import TaskConversation from '@/components/producer/TaskConversation.vue'
import { t } from '@/i18n'
import { producerActiveTasks } from '@/lib/producer'
import { useChatStore } from '@/stores/chat'
import { useTeamsStore } from '@/stores/teams'

const chat = useChatStore()
const teams = useTeamsStore()
const route = useRoute()
const router = useRouter()
const loading = ref(true)
let alive = true
let selectionRequest = 0
onBeforeUnmount(() => { alive = false; selectionRequest++ })
const strip = ref<HTMLElement | null>(null)
const producerIds = computed(() => new Set(teams.teams.flatMap(team =>
  team.members.filter(member => member.builtin === 'producer').map(member => member.id))))
const conversations = computed(() => chat.chats.filter(item => producerIds.value.has(item.member_id)))
const selectedMemberId = computed(() => chat.view === 'new' ? chat.newMemberId : chat.activeChat?.member_id)
const team = computed(() => teams.teams.find(entry => entry.members.some(member =>
  member.id === selectedMemberId.value)) ?? teams.team)
const producer = computed(() => team.value?.members.find(member => member.builtin === 'producer'))
const tasks = computed(() => team.value ? producerActiveTasks(team.value.tasks, team.value.id) : [])
const selection = computed(() => chat.view === 'chat' ? chat.activeChatId ?? '' : '')
const changingChat = ref(false)

async function newChat(): Promise<void> {
  if (!producer.value || chat.busy || changingChat.value) return
  const request = ++selectionRequest
  changingChat.value = true
  try {
    const id = await chat.createProducerChat(producer.value.id)
    if (alive && request === selectionRequest && id) {
      void router.replace({ path: '/producer', query: { chat: id } })
    }
  } finally {
    changingChat.value = false
  }
}

async function deleteChat(): Promise<void> {
  const id = selection.value
  if (!id || chat.busy || changingChat.value) return
  ++selectionRequest
  changingChat.value = true
  try {
    const removed = await chat.remove(id)
    if (!alive || !removed) return
    const next = conversations.value[0]
    if (next) await selectChat(next.id)
    else {
      // Leave an empty composer; deleting the last conversation must not create another.
      chat.startNew(producer.value?.id)
      void router.replace({ path: '/producer' })
    }
  } finally {
    changingChat.value = false
  }
}

async function selectChat(id: string): Promise<void> {
  const conversation = conversations.value.find(item => item.id === id)
  if (!conversation) return
  const request = ++selectionRequest
  const owner = teams.teams.find(entry => entry.members.some(member => member.id === conversation.member_id))
  if (owner && teams.activeTeamId !== owner.id) {
    teams.activeTeamId = owner.id
    try {
      await teams.refreshTeam()
    } catch (cause) {
      if (alive && request === selectionRequest) teams.error = cause instanceof Error ? cause.message : String(cause)
      return
    }
  }
  if (!alive || request !== selectionRequest) return
  await chat.openChat(id)
  if (!alive || request !== selectionRequest || chat.activeChatId !== id) return
  if (!await chat.ensureProducerChat(id)) return
  if (!alive || request !== selectionRequest || chat.activeChatId !== id) return
  void router.replace({ path: '/producer', query: { chat: id } })
}

function scrollTasks(direction: number): void {
  if (!strip.value) return
  const card = strip.value.querySelector<HTMLElement>('.producer-task')
  strip.value.scrollBy({ left: direction * ((card?.offsetWidth ?? 320) + 12),
    behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' })
}

onMounted(async () => {
  teams.subscribe()
  await Promise.all([teams.load(), chat.load()])
  if (!alive) return
  const requested = typeof route.query.chat === 'string' ? route.query.chat : chat.activeChatId
  const selected = conversations.value.find(item => item.id === requested) ?? conversations.value[0]
  if (selected) await selectChat(selected.id)
  else chat.startNew(producer.value?.id)
  if (!alive) return
  loading.value = false
})

// Keep a newly created Producer conversation addressable and recover if deleted elsewhere.
watch(() => chat.activeChatId, id => {
  if (!loading.value && chat.view === 'chat' && conversations.value.some(item => item.id === id)) {
    void router.replace({ path: '/producer', query: { chat: id } })
  }
})
watch(() => chat.view, view => {
  if (!loading.value && !changingChat.value && view === 'list') {
    const next = conversations.value[0]
    if (next) void selectChat(next.id)
    else {
      chat.startNew(producer.value?.id)
      void router.replace({ path: '/producer' })
    }
  }
})
watch(() => route.query.chat, id => {
  if (!loading.value && typeof id === 'string' && id !== chat.activeChatId) void selectChat(id)
})
</script>

<template>
  <div class="flex h-full min-h-0 flex-col" data-producer-workspace>
    <p v-if="teams.error" class="px-4 py-2 text-danger" role="alert">{{ teams.error }}</p>
    <p v-if="chat.error && chat.view === 'list'" class="px-4 py-2 text-danger" role="alert">{{ chat.error }}</p>
    <div class="producer-workspace min-h-0 flex-1">
      <section class="producer-pinned flex min-h-0 flex-col border-r border-line bg-surface" :aria-label="t('producerPage.chat')">
        <ProducerControls v-if="!loading && producer && chat.view === 'chat'" toolbar />
        <header class="shrink-0 border-b border-line">
          <div class="flex h-14 items-center gap-2 px-3">
            <select :value="selection" :disabled="loading || chat.busy || changingChat" :aria-label="t('producerPage.selectChat')"
              class="min-w-0 flex-1 rounded-md border border-line bg-inset px-2 py-1.5 outline-none focus:border-accent"
              @change="selectChat(($event.target as HTMLSelectElement).value)">
              <option value="" disabled>{{ t('producerPage.newChat') }}</option>
              <option v-for="item in conversations" :key="item.id" :value="item.id">{{ item.title || t('producerPage.chat') }}</option>
            </select>
            <button class="grid size-8 shrink-0 place-items-center rounded-md border border-line hover:bg-hover disabled:opacity-40"
              :disabled="loading || !producer || chat.busy || changingChat" :title="t('producerPage.newChat')" :aria-label="t('producerPage.newChat')" @click="newChat">
              <Plus class="size-4" />
            </button>
            <button class="grid size-8 shrink-0 place-items-center rounded-md border border-danger/30 bg-danger-soft text-danger hover:bg-danger/20 disabled:opacity-40"
              :disabled="loading || !selection || chat.busy || changingChat" :title="t('producerPage.deleteChat')" :aria-label="t('producerPage.deleteChat')" @click="deleteChat">
              <Trash2 class="size-4 text-danger" />
            </button>
          </div>
          <ChatHeaderChips v-if="!loading && chat.view === 'chat' && producer && chat.activeChat?.member_id === producer.id"
            :chat="chat.activeChat" :member="producer" :messages="chat.activeMessages" :levels="teams.options?.effort_levels" />
        </header>
        <p v-if="loading" class="p-4 text-muted">{{ t('producerPage.loading') }}</p>
        <p v-else-if="!producer" class="p-4 text-muted">{{ t('producerPage.noProducer') }}</p>
        <div class="producer-conversation min-h-0 flex-1" :class="{ hidden: loading || !producer }">
          <ProducerPlan :items="chat.view === 'chat' ? chat.activeChat?.producer_checklist ?? [] : []" />
          <!-- One persistent widget retains attachments and drafts across page navigation. -->
          <div id="producer-chat" class="min-h-0 min-w-0" />
        </div>
      </section>
      <section class="flex min-h-0 min-w-0 flex-col" :aria-label="t('producerPage.tasks')">
        <header class="flex h-14 shrink-0 items-center gap-2 px-4">
          <MessagesSquare class="size-4 text-faint" />
          <h2 class="font-medium">{{ t('producerPage.tasks') }}</h2>
          <span class="rounded-pill bg-inset px-2 text-small tabular-nums text-muted">{{ tasks.length }}</span>
          <div v-if="tasks.length" class="ml-auto flex gap-1">
            <button class="grid size-8 place-items-center rounded-md border border-line hover:bg-hover"
              :aria-label="t('producerPage.previous')" :title="t('producerPage.previous')" @click="scrollTasks(-1)"><ChevronLeft class="size-4" /></button>
            <button class="grid size-8 place-items-center rounded-md border border-line hover:bg-hover"
              :aria-label="t('producerPage.next')" :title="t('producerPage.next')" @click="scrollTasks(1)"><ChevronRight class="size-4" /></button>
          </div>
        </header>
        <div v-if="!tasks.length" class="m-auto max-w-sm p-6 text-center">
          <MessagesSquare class="mx-auto mb-3 size-8 text-faint" />
          <h3 class="font-medium">{{ t('producerPage.emptyTasks') }}</h3>
          <p class="mt-2 text-muted">{{ t('producerPage.emptyTasksHint') }}</p>
        </div>
        <div v-else ref="strip" class="producer-strip min-h-0 flex-1" tabindex="0" :aria-label="t('producerPage.tasks')">
          <TaskConversation v-for="task in tasks" :key="task.id" :task="task"
            :member="team?.members.find(member => member.id === task.assignee_id) ?? null"
            class="producer-task rounded-lg border border-line bg-surface" />
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.producer-workspace { display: grid; grid-template-columns: clamp(480px, calc(26% + 200px), 580px) minmax(0, 1fr); }
.producer-conversation { display: grid; grid-template-rows: minmax(0, 1fr) minmax(0, 2fr); }
.producer-conversation.hidden { display: none; }
.producer-strip { display: flex; gap: 12px; overflow-x: auto; overscroll-behavior-x: contain; padding: 0 0 12px 12px; }
/* §10.11: keep the card proportions, with room for the assignee and usage on one row. */
.producer-task { flex: 0 0 calc((100% + 200px - 24px) / 2.5 - 100px); min-width: 360px; }
@media (max-width: 900px) {
  .producer-workspace { grid-template-columns: minmax(280px, 1fr) minmax(0, 1fr); }
  .producer-task { flex-basis: calc(100% - 24px); }
}
@media (max-width: 600px) {
  .producer-workspace { grid-template-columns: minmax(0, 1fr); overflow-y: auto; }
  .producer-pinned { height: 80vh; border-right: 0; border-bottom: 1px solid var(--vxs-border-subtle); }
  .producer-workspace > section:last-child { height: 65vh; }
}
</style>
