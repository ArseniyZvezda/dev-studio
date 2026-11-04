<script setup lang="ts">
/** The same chat list in the popup and the Chats page (§10.9). */
import { Plus } from 'lucide-vue-next'
import { computed } from 'vue'
import MemberMark from '@/components/teams/MemberMark.vue'
import { t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'
import { formatTime } from '@/lib/status'
import { useChatStore } from '@/stores/chat'
import { useTeamsStore } from '@/stores/teams'

const chat = useChatStore()
const teams = useTeamsStore()
const agents = computed(() => teams.members.filter(member => member.kind === 'agent'))
const rowMember = (id: string) => teams.memberById.get(id) ?? null
</script>

<template>
  <div class="min-h-0 flex-1 overflow-y-auto">
    <p v-if="chat.error && chat.view === 'list'" role="alert" class="px-3 py-2 text-small text-danger">{{ chat.error }}</p>
    <p v-if="chat.loading && !chat.loaded" class="p-4 text-faint">{{ t('common.loading') }}</p>
    <p v-else-if="!chat.chats.length && !agents.length && teams.members.length" class="p-4 text-faint">
      <I18nT keypath="chat.list.peopleOnly">
        <template #teams>
          <RouterLink to="/teams" class="text-accent hover:underline">
            {{ t('chat.page.teams') }}
          </RouterLink>
        </template>
      </I18nT>
    </p>
    <div v-else-if="!chat.chats.length" class="flex h-full flex-col items-center justify-center gap-3 p-4 text-center">
      <p class="max-w-[16rem] text-faint">{{ t('chat.list.empty') }}</p>
      <button
        v-if="agents.length"
        class="flex items-center gap-1.5 rounded-md border border-line px-3 py-1.5 hover:bg-hover"
        @click="chat.startNew()"
      >
        <Plus class="size-4" />
        <span>{{ t('chat.newChat') }}</span>
      </button>
    </div>
    <ul v-else class="py-1">
      <li v-for="entry in chat.chats" :key="entry.id">
        <button
          class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-hover"
          :class="chat.view === 'chat' && chat.activeChatId === entry.id ? 'bg-accent-soft' : ''"
          :aria-current="chat.view === 'chat' && chat.activeChatId === entry.id ? 'true' : undefined"
          @click="chat.openChat(entry.id)"
        >
          <MemberMark
            :member="rowMember(entry.member_id)"
            variant="badge"
            :corner="false"
            class="shrink-0"
          />
          <span class="min-w-0 flex-1 leading-tight">
            <span class="flex items-baseline gap-2">
              <span class="truncate font-medium">
                {{ rowMember(entry.member_id)?.name ?? entry.member_id }}
              </span>
              <span class="ml-auto shrink-0 text-small text-faint">
                {{ formatTime(entry.last_message_at) }}
              </span>
            </span>
            <span class="flex items-center gap-1.5 text-small text-muted">
              <span
                v-if="entry.is_running"
                class="vxs-dot-live size-1.5 shrink-0 rounded-full bg-accent"
                :title="t('chat.answering')"
              />
              <span
                v-else-if="chat.unread.has(entry.id)"
                class="size-1.5 shrink-0 rounded-full bg-accent"
                :title="t('chat.list.newReply')"
              />
              <span class="truncate">{{ entry.title || t('chat.newChat') }}</span>
              <span v-if="entry.producer_mode" class="shrink-0 rounded-pill bg-accent-soft px-1.5 text-accent">
                {{ t('chat.producer.label') }}
              </span>
            </span>
          </span>
        </button>
      </li>
    </ul>
  </div>

</template>
