<script setup lang="ts">
/**
 * What an automation's *notify* action looks like inside the Studio (§6.5): a short stack of
 * cards in the top-right corner, over every section.
 *
 * Mounted once in `AppShell`, which is also where the store subscribes — a notification is
 * for whoever has the Studio open, whatever page they are on. Above the dialogs (`z-[60]`
 * over their `z-50`): a task reaching review while another task's dialog is open is exactly
 * when the news is wanted, and the stack sits in a corner no dialog's controls reach.
 *
 * A card that names a place is a link to it; the close button is a sibling of that link
 * rather than a child, so dismissing one never navigates.
 */
import { BellRing, X } from 'lucide-vue-next'
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'

import { t } from '@/i18n'
import { notificationText } from '@/lib/automations'
import { useNotificationsStore, type Toast } from '@/stores/notifications'

const notifications = useNotificationsStore()
const router = useRouter()

onMounted(() => notifications.subscribe())

function open(toast: Toast): void {
  const route = toast.notification.route
  notifications.dismiss(toast.id)
  if (route) void router.push(route)
}
</script>

<template>
  <Teleport to="body">
    <!-- `aria-live` on the stack, not on a card: a card that mounts already holding its text
         is not announced, a region that gains a child is.

         A `TransitionGroup` (`vxs-toast-*` in `base.css`'s motion section): a card comes in
         from the edge the stack sits on, leaves through it, and the cards under one that left
         close the gap. The stack is block flow with a margin between cards rather than a flex
         column with a gap, because the leaving card is taken out of the flow to open that gap —
         and in a flex container an absolutely placed child is put back at the container's
         start, so every card but the first would jump to the top to fade. -->
    <TransitionGroup
      tag="div"
      name="vxs-toast"
      class="pointer-events-none fixed right-4 top-4 z-[60] w-80 max-w-[calc(100vw-2rem)] space-y-2"
      role="status"
      aria-live="polite"
    >
      <div
        v-for="toast in notifications.toasts"
        :key="toast.id"
        class="vxs-card pointer-events-auto flex items-start gap-1 rounded-lg border border-card bg-surface"
        @mouseenter="notifications.hold(toast.id)"
        @mouseleave="notifications.release(toast.id)"
      >
        <component
          :is="toast.notification.route ? 'button' : 'div'"
          class="flex min-w-0 flex-1 items-start gap-2.5 rounded-lg p-3 text-left"
          :class="toast.notification.route ? 'hover:bg-hover' : ''"
          @click="toast.notification.route ? open(toast) : undefined"
        >
          <BellRing class="mt-0.5 size-4 shrink-0 text-[var(--vxs-nav-automations)]" />
          <span class="min-w-0 flex-1">
            <span class="block break-words font-medium">{{ notificationText(toast.notification).title }}</span>
            <span
              v-if="notificationText(toast.notification).message"
              class="mt-0.5 line-clamp-3 block whitespace-pre-line break-words text-small text-muted"
            >
              {{ notificationText(toast.notification).message }}
            </span>
            <span v-if="toast.notification.automation_name" class="mt-1 block truncate text-small text-faint">
              {{ toast.notification.automation_name }}
            </span>
          </span>
        </component>
        <button
          class="m-1.5 shrink-0 rounded-md p-1 text-faint hover:bg-hover hover:text-primary"
          :title="t('common.close')"
          :aria-label="t('common.close')"
          @click="notifications.dismiss(toast.id)"
        >
          <X class="size-3.5" />
        </button>
      </div>
    </TransitionGroup>
  </Teleport>
</template>
