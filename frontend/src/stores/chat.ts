import { upsertTranscript } from '@/lib/transcript'
/**
 * Chats with a team member, and the floating popup that holds them (§10.9).
 *
 * Two kinds of state live here and are kept apart on purpose. The **chats** — the list,
 * each one's transcript, whether the member is answering — are the backend's, read over
 * `/api/chats` and kept live off `team.chat.*` events. The **popup's** state — whether it
 * is open, which screen it shows, which chat, what was typed and not sent — is this
 * browser's, and goes to `localStorage` so a page reload or a trip to another section puts
 * the popup back exactly as it was. Nothing of the second kind reaches the project.
 */

import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import { ApiError, api } from '@/api/client'
import type { AgentChat, AgentChatMessage, ChatContext, ProducerEffortMode } from '@/api/types'
import { locale, t } from '@/i18n'
import { parseUtc } from '@/lib/status'
import { onEvent } from '@/ws/connection'
import { rendererSession } from '@/session/project-session'
import { projectStorageKey, readProjectValue, writeProjectValue } from '@/desktop/project-storage'
import { useSessionStore } from './session'

export type ChatView = 'list' | 'new' | 'chat'

const STATE_KEY = 'voxarium_studio.chat.state'
const DRAFT_PREFIX = 'voxarium_studio.chat.draft.'
/** The draft of a chat that does not exist yet — the "new chat" screen's. */
const NEW_DRAFT_KEY = `${DRAFT_PREFIX}new`

export interface PopupState {
  open: boolean
  view: ChatView
  chatId: string | null
  memberId: string | null
}

const DEFAULT_STATE: PopupState = { open: false, view: 'list', chatId: null, memberId: null }

/** The popup as it was left — every field checked, because the store is the browser's. */
export function readPopupState(raw: string | null): PopupState {
  if (!raw) return { ...DEFAULT_STATE }
  try {
    const parsed = JSON.parse(raw) as Partial<PopupState> | null
    if (!parsed || typeof parsed !== 'object') return { ...DEFAULT_STATE }
    const view: ChatView =
      parsed.view === 'chat' || parsed.view === 'new' || parsed.view === 'list'
        ? parsed.view
        : 'list'
    return {
      open: parsed.open === true,
      view,
      chatId: typeof parsed.chatId === 'string' && parsed.chatId ? parsed.chatId : null,
      memberId: typeof parsed.memberId === 'string' && parsed.memberId ? parsed.memberId : null,
    }
  } catch {
    return { ...DEFAULT_STATE }
  }
}

/** Only canonical project roots identify durable drafts; runtime handles do not. */
export function draftKey(root: string | null, view: ChatView, chatId: string | null): string | null {
  if (view === 'new') return projectStorageKey(root, NEW_DRAFT_KEY)
  if (view === 'chat' && chatId) return projectStorageKey(root, `${DRAFT_PREFIX}chat.${chatId}`)
  return null
}

export function readDraft(key: string | null): string {
  return readProjectValue(key) ?? ''
}

export function writeDraft(key: string | null, text: string): void {
  writeProjectValue(key, text || null)
}

/** Newest activity first — the order the list shows and the backend answers in. */
function byActivity(a: AgentChat, b: AgentChat): number {
  return parseUtc(b.last_message_at) - parseUtc(a.last_message_at)
}

/** §10.11: a Producer summary replaces history, including stale fetches/socket replay. */
function compactedMessages(messages: AgentChatMessage[]): AgentChatMessage[] {
  // Keep Python's sub-millisecond precision; REST and WS may differ in the UTC suffix.
  const timestamp = (message: AgentChatMessage) => message.created_at.replace(/(?:Z|\+00:00)$/, '')
  const summary = messages.reduce<AgentChatMessage | undefined>((latest, message) =>
    message.kind === 'producer_compacted' && message.data?.replaces_history === true &&
    (!latest || timestamp(message) > timestamp(latest)) ? message : latest, undefined)
  if (!summary) return messages
  return [summary, ...messages.filter(message =>
    message.id !== summary.id && timestamp(message) > timestamp(summary),
  ).sort((a, b) => timestamp(a).localeCompare(timestamp(b)))]
}

export const useChatStore = defineStore('chat', () => {
  const session = useSessionStore()
  // App creates this store before bootstrap. Bind once metadata arrives, never to a
  // mutable selection; old continuations keep their original root (dev spec §5.3.2).
  const storageRoot = ref<string | null>(null)
  let restoring = false
  const initial = { ...DEFAULT_STATE }

  // ---- the popup's own state (this browser's)
  const open = ref(initial.open)
  // Visibility of the full-page chat is independent of the remembered popup (§10.9).
  const pageVisible = ref(false)
  const view = ref<ChatView>(initial.view)
  const activeChatId = ref<string | null>(initial.chatId)
  /** Who the "new chat" screen is addressed to. */
  const newMemberId = ref<string | null>(initial.memberId)
  /** The last text selected on the page outside the popup — sent with the next message. */
  const selection = ref('')

  // ---- the chats (the backend's)
  const chats = ref<AgentChat[]>([])
  const messages = ref<Record<string, AgentChatMessage[]>>({})
  const loaded = ref(false)
  const loading = ref(false)
  const pendingActions = ref(new Set<string>())
  const actionKey = computed(() => view.value === 'chat' && activeChatId.value ? activeChatId.value : view.value)
  const busy = computed(() => pendingActions.value.has(actionKey.value))
  const error = ref<string | null>(null)
  const actionErrors = ref(new Map<string, string>())
  const visibleError = computed(() => error.value ?? actionErrors.value.get(actionKey.value) ?? null)
  /** Chats a member answered in while the popup was not showing them. */
  const unread = ref(new Set<string>())
  // A delayed list must not resurrect even a previously unknown row deleted over WS.
  const deletedChats = new Set<string>()

  const activeChat = computed(
    () => chats.value.find((chat) => chat.id === activeChatId.value) ?? null,
  )
  const activeMessages = computed(() =>
    activeChatId.value ? (messages.value[activeChatId.value] ?? []) : [],
  )
  const unreadCount = computed(() => unread.value.size)

  const currentDraftKey = computed(() => draftKey(storageRoot.value, view.value, activeChatId.value))

  watch(() => session.project?.root, root => {
    if (!root || storageRoot.value !== null) return
    restoring = true
    storageRoot.value = root
    // Old keys contain no trustworthy project ownership. Leave them untouched;
    // neither the first nor a second project may automatically claim their text.
    const state = readPopupState(readProjectValue(projectStorageKey(root, STATE_KEY)))
    open.value = state.open
    view.value = state.view
    activeChatId.value = state.chatId
    newMemberId.value = state.memberId
    restoring = false
  }, { immediate: true, flush: 'sync' })

  watch([open, view, activeChatId, newMemberId], () => {
    if (restoring) return
    writeProjectValue(projectStorageKey(storageRoot.value, STATE_KEY), JSON.stringify({
      open: open.value,
      view: view.value,
      chatId: activeChatId.value,
      memberId: newMemberId.value,
    } satisfies PopupState))
    // Synchronous: closing and immediately reloading must agree.
  }, { flush: 'sync' })

  watch(pageVisible, showing => {
    if (showing && view.value === 'chat' && activeChatId.value) unread.value.delete(activeChatId.value)
  }, { flush: 'sync' })

  // ---- navigation

  let openRequest = 0

  function show(): void {
    open.value = true
    if (view.value === 'chat' && activeChatId.value) unread.value.delete(activeChatId.value)
  }

  function hide(): void {
    open.value = false
  }

  function toggle(): void {
    if (open.value) hide()
    else show()
  }

  function showList(): void {
    openRequest++
    view.value = 'list'
    error.value = null
  }

  function startNew(memberId: string | null = null): void {
    openRequest++
    if (memberId) newMemberId.value = memberId
    view.value = 'new'
    error.value = null
  }

  async function openChat(chatId: string): Promise<void> {
    const request = ++openRequest
    view.value = 'chat'
    activeChatId.value = chatId
    unread.value.delete(chatId)
    error.value = null
    messages.value[chatId] ??= []
    const previous = chats.value.find(chat => chat.id === chatId)
    try {
      const detail = await api.chat(chatId)
      if (request !== openRequest || activeChatId.value !== chatId || view.value !== 'chat') return
      const { messages: fetched, ...chat } = detail
      // The fetch may predate a live update or an action in this same chat.
      if (previous === chats.value.find(entry => entry.id === chatId)) upsert(chat)
      // Events may have appended while the fetch was in flight; keep what they brought.
      const known = messages.value[chatId] ?? []
      const seen = new Set(fetched.map((message) => message.id))
      messages.value = {
        ...messages.value,
        [chatId]: compactedMessages([...fetched, ...known.filter((message) => !seen.has(message.id))]),
      }
    } catch (cause) {
      if (request !== openRequest || activeChatId.value !== chatId || view.value !== 'chat') return
      if (cause instanceof ApiError && cause.status === 404) {
        // Deleted from another tab, or the project changed under us.
        forget(chatId)
        return
      }
      error.value = cause instanceof Error ? cause.message : String(cause)
    }
  }

  // ---- loading

  let loadRequest: Promise<void> | null = null

  /** The page and persistent widget must both await the same list and transcript (§10.11). */
  function load(): Promise<void> {
    if (loadRequest) return loadRequest
    loadRequest = fetchChats().finally(() => { loadRequest = null })
    return loadRequest
  }

  async function fetchChats(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      chats.value = [...(await api.chats())].sort(byActivity)
      loaded.value = true
      subscribe()
      if (view.value === 'chat') {
        if (activeChatId.value && chats.value.some((chat) => chat.id === activeChatId.value)) {
          await openChat(activeChatId.value)
        } else {
          activeChatId.value = null
          view.value = 'list'
        }
      }
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
    } finally {
      loading.value = false
    }
  }

  function upsert(chat: AgentChat): void {
    deletedChats.delete(chat.id)
    const index = chats.value.findIndex((entry) => entry.id === chat.id)
    const next = [...chats.value]
    if (index < 0) next.push(chat)
    else next[index] = chat
    chats.value = next.sort(byActivity)
  }

  // ---- actions

  // §10.11: an HTTP action belongs to the chat that initiated it. A different
  // conversation can send or pause while the first is still waiting on the server.
  async function runAction<T>(fallback: T, work: (action: {
    created: (chat: AgentChat) => void
    selected: () => boolean
  }) => Promise<T>, key = actionKey.value): Promise<T> {
    const origin = actionKey.value
    if (pendingActions.value.has(key)) return fallback
    pendingActions.value.add(key)
    actionErrors.value.delete(origin)
    let request = openRequest
    const member = newMemberId.value
    const selected = () => request === openRequest && (view.value !== 'new' || member === newMemberId.value)
    let createdId: string | null = null
    error.value = null
    function created(chat: AgentChat): void {
      upsert(chat)
      messages.value[chat.id] ??= []
      createdId = chat.id
      pendingActions.value.add(chat.id)
      if (!selected()) return
      request = ++openRequest
      newMemberId.value = chat.member_id
      activeChatId.value = chat.id
      view.value = 'chat'
    }
    try {
      return await work({ selected, created })
    } catch (cause) {
      const target = createdId ?? origin
      if (target === actionKey.value || chats.value.some(row => row.id === target)) {
        actionErrors.value.set(target, cause instanceof Error ? cause.message : String(cause))
      }
      return fallback
    } finally {
      pendingActions.value.delete(key)
      if (createdId) pendingActions.value.delete(createdId)
    }
  }

  // A socket deletion can arrive before a pending action's HTTP response.
  function updateExisting(chat: AgentChat): void {
    if (chats.value.some(entry => entry.id === chat.id)) upsert(chat)
  }

  /** Adopt an existing Producer chat on the workspace without resetting its plan. */
  async function ensureProducerChat(chatId: string): Promise<boolean> {
    const known = chats.value.find(chat => chat.id === chatId)
    if (known?.producer_workspace && known.producer_mode) return true
    return runAction(false, async () => {
      updateExisting(await api.useProducerWorkspace(chatId, locale.value))
      return true
    })
  }

  /** Create an idle, permanently enabled Producer conversation (§10.11). */
  async function createProducerChat(memberId: string): Promise<string | null> {
    if (pendingActions.value.has('create-producer')) return null
    openRequest++
    return runAction(null, async action => {
      const created = await api.createChat({ member_id: memberId, producer_mode: true, locale: locale.value })
      action.created(created)
      return created.id
    }, 'create-producer')
  }

  /**
   * Send what was typed, opening the chat first when the "new chat" screen is showing.
   * Returns whether the message left; the reason it did not is in `error`.
   */
  async function send(text: string, context: ChatContext, attachments: string[] = [], producerMode = false): Promise<boolean> {
    const body = text.trim()
    if (!body && !attachments.length) return false
    return runAction(false, async action => {
      let chatId = activeChatId.value
      if (view.value === 'new' || !chatId) {
        if (!newMemberId.value) {
          error.value = t('chat.errors.pickMember')
          return false
        }
        const created = await api.createChat({ member_id: newMemberId.value,
          ...(producerMode ? { producer_mode: true, locale: locale.value } : {}) })
        action.created(created)
        chatId = created.id
      }
      // An older ordinary turn may have prevented adoption when the page opened.
      // Retry before its next message so this page never sends outside Producer Mode.
      const current = chats.value.find(chat => chat.id === chatId)
      if (producerMode && (!current?.producer_workspace || !current.producer_mode)) {
        updateExisting(await api.useProducerWorkspace(chatId, locale.value))
      }
      try {
        const result = await api.sendChatMessage(chatId, body, context, attachments)
        updateExisting(result.chat)
        appendMessage(result.message)
      } catch (cause) {
        if (cause instanceof ApiError && cause.status === 409) throw new Error(t('chat.errors.stillAnswering'))
        throw cause
      }
      if (action.selected()) selection.value = ''
      return true
    })
  }

  async function stop(chatId: string): Promise<void> {
    try {
      await api.cancelChat(chatId)
    } catch (cause) {
      if (view.value === 'chat' && activeChatId.value === chatId) error.value = cause instanceof Error ? cause.message : String(cause)
    }
  }

  /** §10.11: both chat surfaces use these same actions and durable server state. */
  async function producerAction(action: 'start' | 'stop' | 'approve' | 'pause' | 'resume', policy?: AgentChat['producer_policy'], estimateTaskCostUsd?: number, effortMode?: ProducerEffortMode): Promise<void> {
    if (busy.value) return
    if (action === 'start' && estimateTaskCostUsd !== undefined && (!Number.isFinite(estimateTaskCostUsd) || estimateTaskCostUsd <= 0)) {
      error.value = t('chat.producer.estimateInvalid')
      return
    }
    return runAction(undefined, async request => {
      let current = activeChat.value
      if (action === 'start' && (view.value === 'new' || !current)) {
        if (!newMemberId.value) return
        current = await api.createChat({ member_id: newMemberId.value })
        request.created(current)
      }
      if (!current) return
      const updated = action === 'start'
        ? await api.startProducer(current.id, locale.value, policy, estimateTaskCostUsd, effortMode)
        : action === 'stop'
          ? await api.stopProducer(current.id)
          : action === 'pause'
            ? await api.pauseProducer(current.id)
            : action === 'resume'
              ? await api.resumeProducer(current.id)
              : await api.approveProducerPlan(current.id, current.producer_plan_version ?? 0)
      updateExisting(updated)
    })
  }

  async function setProducerPolicy(policy: NonNullable<AgentChat['producer_policy']>): Promise<void> {
    const current = activeChat.value
    if (!current) return
    return runAction(undefined, async () => updateExisting(await api.setProducerPolicy(current.id, policy)))
  }

  async function setModel(backend: string | null, model: string | null): Promise<void> {
    const current = activeChat.value
    if (!current) return
    return runAction(undefined, async () => updateExisting(await api.setChatModel(current.id, backend, model)))
  }

  async function setProducerEffortMode(mode: ProducerEffortMode): Promise<void> {
    const current = activeChat.value
    if (!current) return
    return runAction(undefined, async () => updateExisting(await api.setProducerEffortMode(current.id, mode)))
  }

  async function setProducerEstimateTaskCost(estimateTaskCostUsd: number): Promise<void> {
    const current = activeChat.value
    if (!current || busy.value) return
    if (!Number.isFinite(estimateTaskCostUsd) || estimateTaskCostUsd <= 0) {
      error.value = t('chat.producer.estimateInvalid')
      return
    }
    return runAction(undefined, async () => updateExisting(await api.setProducerSettings(current.id, estimateTaskCostUsd)))
  }

  async function remove(chatId: string): Promise<boolean> {
    const key = draftKey(storageRoot.value, 'chat', chatId)
    return runAction(false, async () => {
      try {
        await api.deleteChat(chatId)
      } catch (cause) {
        if (!(cause instanceof ApiError && cause.status === 404)) throw cause
      }
      forget(chatId)
      writeDraft(key, '')
      return true
    }, chatId)
  }

  function forget(chatId: string): void {
    deletedChats.add(chatId)
    chats.value = chats.value.filter((chat) => chat.id !== chatId)
    const rest = { ...messages.value }
    delete rest[chatId]
    messages.value = rest
    unread.value.delete(chatId)
    actionErrors.value.delete(chatId)
    if (activeChatId.value === chatId) {
      openRequest++
      activeChatId.value = null
      if (view.value === 'chat') view.value = 'list'
    }
  }

  function setSelection(text: string): void {
    selection.value = text
  }

  function clearSelection(): void {
    selection.value = ''
  }

  // ---- live updates

  function appendMessage(message: AgentChatMessage): void {
    const known = messages.value[message.chat_id]
    if (known || (message.kind === 'producer_compacted' && message.data?.replaces_history === true)) {
      messages.value = { ...messages.value, [message.chat_id]: compactedMessages(upsertTranscript(known ?? [], message)) }
    }
    const chat = chats.value.find((entry) => entry.id === message.chat_id)
    if (chat && parseUtc(message.created_at) > parseUtc(chat.last_message_at)) {
      upsert({ ...chat, last_message_at: message.created_at })
    }
  }

  let subscribed = false

  /** §5.3: gap recovery never navigates, clears a composer or consumes pending attachments. */
  async function resyncData(): Promise<void> {
    const chatId = view.value === 'chat' ? activeChatId.value : null
    const request = openRequest
    const refreshTranscript = async () => {
      if (!chatId) return
      const selected = () => request === openRequest && activeChatId.value === chatId && view.value === 'chat'
      const previous = chats.value.find(chat => chat.id === chatId)
      try {
        const { messages: fetched, ...chat } = await api.chat(chatId)
        if (!selected()) return
        if (previous === chats.value.find(entry => entry.id === chatId)) upsert(chat)
        const merged = (messages.value[chatId] ?? []).reduce(upsertTranscript<AgentChatMessage>, fetched)
        messages.value = { ...messages.value, [chatId]: compactedMessages(merged) }
      } catch (cause) {
        if (selected()) throw cause
      }
    }
    const refreshList = async () => {
      const previous = new Map(chats.value.map(chat => [chat.id, chat]))
      const fetched = await api.chats()
      const current = new Map(chats.value.map(chat => [chat.id, chat]))
      const next = fetched.filter(chat => !deletedChats.has(chat.id) && (!previous.has(chat.id) || current.has(chat.id)))
        .map(chat => current.has(chat.id) && current.get(chat.id) !== previous.get(chat.id) ? current.get(chat.id)! : chat)
      const seen = new Set(next.map(chat => chat.id))
      // Preserve rows created/updated by live events after the read started, not stale deletions.
      chats.value = [...next, ...chats.value.filter(chat => !seen.has(chat.id) && previous.get(chat.id) !== chat)].sort(byActivity)
    }
    const results = await Promise.allSettled([refreshList(), refreshTranscript()])
    for (const result of results) {
      if (result.status === 'rejected') {
        error.value = result.reason instanceof Error ? result.reason.message : String(result.reason)
      }
    }
  }

  let resyncRequest: Promise<void> | null = null
  let resyncPending = false

  function resync(): Promise<void> {
    resyncPending = true
    resyncRequest ??= Promise.resolve().then(async () => {
      do {
        resyncPending = false
        await resyncData()
      } while (resyncPending)
    }).catch(cause => {
      error.value = cause instanceof Error ? cause.message : String(cause)
    }).finally(() => { resyncRequest = null })
    return resyncRequest
  }

  function subscribe(): void {
    if (subscribed) return
    subscribed = true

    onEvent('system.dropped', (payload, envelope) => {
      if (payload.resync && !rendererSession.isLegacy && envelope && rendererSession.matches(envelope)) {
        void resync()
      }
    })

    onEvent('team.chat.updated', (chat) => upsert(chat))

    onEvent('team.chat.deleted', ({ chat_id }) => forget(chat_id))

    onEvent('team.chat.message', (message) => {
      appendMessage(message)
      const showing =
        (open.value || pageVisible.value) && view.value === 'chat' && activeChatId.value === message.chat_id
      if (!showing && message.role === 'assistant' && message.kind === 'text') {
        unread.value.add(message.chat_id)
      }
    })
  }

  return {
    open,
    currentDraftKey,
    pageVisible,
    view,
    activeChatId,
    newMemberId,
    selection,
    chats,
    messages,
    loaded,
    loading,
    busy,
    error: visibleError,
    unread,
    activeChat,
    activeMessages,
    unreadCount,
    show,
    hide,
    toggle,
    showList,
    startNew,
    openChat,
    load,
    createProducerChat,
    ensureProducerChat,
    send,
    stop,
    producerAction,
    setProducerPolicy,
    setModel,
    setProducerEffortMode,
    setProducerEstimateTaskCost,
    remove,
    setSelection,
    clearSelection,
  }
})
