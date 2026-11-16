<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { t } from '@/i18n'
import { createDesktopTabGestures, type DesktopShellState } from '@/desktop/tabs'
import type { DesktopTabDragRect } from '@/desktop'

const props = defineProps<{ state: DesktopShellState | null; pending: readonly string[] }>()
const emit = defineEmits<{ activate: [tabId: string]; open: [] }>()
const strip = ref<HTMLElement | null>(null), tablist = ref<HTMLElement | null>(null)
const openButton = ref<HTMLButtonElement | null>(null)
const failed = ref(false), commanding = ref(false)
const bridge = window.voxariumTabs
const gestures = createDesktopTabGestures({ bridge, state: () => props.state,
  activate: tabId => emit('activate', tabId), error: () => { failed.value = true } })
const openingRoots = computed(() => props.pending.filter(root => !props.state?.tabs.some(tab => tab.project.root === root || tab.tabId === root)))
const pendingTab = (tabId: string, root: string) => props.pending.includes(tabId) || props.pending.includes(root)
let captured: { id: number; node: HTMLButtonElement } | null = null
let observer: ResizeObserver | undefined, disposed = false, geometryQueued = false
const observed = new WeakSet<Element>()

function releaseCapture() {
  const pointer = captured
  captured = null // Normal release's lostpointercapture is not a cancellation.
  if (pointer?.node.hasPointerCapture(pointer.id)) pointer.node.releasePointerCapture(pointer.id)
}
function cancel() { releaseCapture(); return gestures.cancel() }
function pointerdown(event: PointerEvent, tabId: string) {
  const tab = props.state?.tabs.find(item => item.tabId === tabId)
  if (event.button !== 0 || !event.isPrimary || event.pointerType !== 'mouse' || captured || commanding.value
    || !tab || pendingTab(tabId, tab.project.root) || tab.state !== 'ready' || !bridge) return
  const node = event.currentTarget as HTMLButtonElement
  // Focus stays in shell until native terminal/disarm; activating here changes
  // the lease revision and steals native focus into project contents (§5.3.4).
  event.preventDefault(); node.focus()
  try {
    node.setPointerCapture(event.pointerId)
    captured = { id: event.pointerId, node }
    failed.value = false
    if (!gestures.begin(tabId, { x: event.clientX, y: event.clientY })) releaseCapture()
  } catch { failed.value = true; void cancel() }
}
function pointermove(event: PointerEvent) {
  if (captured?.id === event.pointerId) gestures.move({ x: event.clientX, y: event.clientY })
}
function pointerup(event: PointerEvent) {
  if (captured?.id !== event.pointerId) return
  gestures.release({ x: event.clientX, y: event.clientY })
  releaseCapture() // No finish/detach IPC. Only main's native hook can accept release.
}
function pointercancel(event: PointerEvent) { if (captured?.id === event.pointerId) void cancel() }
function click(event: MouseEvent, tabId: string) {
  if (event.detail === 0) releaseCapture()
  void gestures.click(tabId, event.detail !== 0)
}
function escape(event: KeyboardEvent) {
  if (event.key === 'Escape') { void cancel() }
}
async function command(tabId: string, action: 'detach' | 'close' | number = 'detach') {
  if (!bridge || commanding.value) return
  commanding.value = true; failed.value = false
  try {
    if (!await cancel() || disposed) return
    const state = props.state, tab = state?.tabs.find(item => item.tabId === tabId)
    if (!state || !tab || (action === 'close' ? tab.state === 'closing' : tab.state !== 'ready') || tab.ownerWindowId !== state.window.windowId || pendingTab(tabId, tab.project.root)) return
    const request = { windowId: state.window.windowId, windowRevision: state.window.revision, tabId, tabRevision: tab.revision }
    if (action === 'close') {
      if (!bridge.closeTab || !(await bridge.closeTab(request)).closed) {
        if (!disposed) failed.value = true
      }
    } else if (action === 'detach') await bridge.detachTab(request)
    else await bridge.reorderTab({ ...request, index: action })
  } catch { if (!disposed) failed.value = true }
  finally { if (!disposed) commanding.value = false }
}
function navigate(event: KeyboardEvent) {
  const buttons = Array.from(tablist.value?.querySelectorAll<HTMLButtonElement>('[role="tab"]') ?? [])
  const index = buttons.indexOf(event.target as HTMLButtonElement)
  if (index < 0) return
  const tabId = buttons[index]?.dataset.tabId
  if (event.ctrlKey && event.shiftKey && ['ArrowLeft', 'ArrowRight'].includes(event.key)) {
    event.preventDefault()
    const next = Math.max(0, Math.min(buttons.length - 1, index + (event.key === 'ArrowLeft' ? -1 : 1)))
    if (tabId && next !== index) void command(tabId, next)
    return
  }
  if ((event.shiftKey && event.key === 'F10') || event.key === 'ContextMenu') {
    event.preventDefault(); focusDetach(tabId); return
  }
  if (event.ctrlKey || event.altKey || event.shiftKey || captured) return
  let next: number
  switch (event.key) {
    case 'ArrowRight': next = (index + 1) % buttons.length; break
    case 'ArrowLeft': next = (index + buttons.length - 1) % buttons.length; break
    case 'Home': next = 0; break
    case 'End': next = buttons.length - 1; break
    default: return
  }
  event.preventDefault()
  buttons[next]?.focus()
  const id = buttons[next]?.dataset.tabId
  if (id) {
    if (gestures.armed) void cancel().then(disarmed => { if (disarmed && !disposed) emit('activate', id) })
    else emit('activate', id)
  }
}
function open() {
  if (gestures.armed) void cancel().then(disarmed => { if (disarmed && !disposed) emit('open') })
  else emit('open')
}
function focusDetach(tabId?: string) {
  const item = Array.from(strip.value?.querySelectorAll<HTMLButtonElement>('[data-detach-tab-id]') ?? [])
    .find(node => node.dataset.detachTabId === tabId)
  item?.focus()
}
function reportGeometry() {
  const state = props.state, viewport = tablist.value?.getBoundingClientRect()
  if (!bridge || !state || !viewport || disposed) return
  // Clipping is essential for horizontally scrolled tabs. Only role=tab buttons
  // are target zones: close/detach/open/status controls never become native drop zones.
  const tabs: DesktopTabDragRect[] = []
  for (const node of tablist.value?.querySelectorAll<HTMLButtonElement>('[role="tab"]') ?? []) {
    if (observer && !observed.has(node)) { observer.observe(node); observed.add(node) }
    const rect = node.getBoundingClientRect(), x = Math.max(rect.left, viewport.left), y = Math.max(rect.top, viewport.top)
    const width = Math.min(rect.right, viewport.right) - x, height = Math.min(rect.bottom, viewport.bottom) - y
    if (node.dataset.tabId && width > 0 && height > 0) tabs.push({ tabId: node.dataset.tabId, x, y, width, height })
  }
  const openRect = openButton.value?.getBoundingClientRect()
  const stripRect = strip.value?.getBoundingClientRect()
  const append = openRect && stripRect && openRect.width > 0 && openRect.right < stripRect.right
    ? { appendStart: openRect.right, appendEnd: stripRect.right } : {}
  void bridge.setDragGeometry({ windowId: state.window.windowId, windowRevision: state.window.revision, tabs, ...append })
    .catch(() => { /* Stale geometry is rejected by main; next state/resize resends. */ })
}
function scheduleGeometry() {
  if (geometryQueued || disposed) return
  geometryQueued = true
  void nextTick(() => { geometryQueued = false; reportGeometry() })
}
watch(() => props.state, () => { if (gestures.sync()) releaseCapture(); scheduleGeometry() }, { deep: true, flush: 'post' })
watch(() => props.pending, () => {
  const tabId = captured?.node.dataset.tabId, tab = props.state?.tabs.find(item => item.tabId === tabId)
  if (tab && pendingTab(tab.tabId, tab.project.root)) void cancel()
  scheduleGeometry()
}, { deep: true, flush: 'post' })
onMounted(() => {
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(scheduleGeometry)
    if (strip.value) observer.observe(strip.value)
    if (tablist.value) observer.observe(tablist.value)
  }
  window.addEventListener('resize', scheduleGeometry)
  window.visualViewport?.addEventListener('resize', scheduleGeometry)
  document.addEventListener('scroll', scheduleGeometry, true)
  document.addEventListener('keydown', escape)
  scheduleGeometry()
})
onBeforeUnmount(() => {
  disposed = true; releaseCapture(); gestures.dispose(); observer?.disconnect()
  window.removeEventListener('resize', scheduleGeometry)
  window.visualViewport?.removeEventListener('resize', scheduleGeometry)
  document.removeEventListener('scroll', scheduleGeometry, true)
  document.removeEventListener('keydown', escape)
})
const label = (root: string) => root.split(/[\\/]/).filter(Boolean).at(-1) || root
</script>

<template>
  <nav ref="strip" class="project-tab-strip" :aria-label="t('desktop.tabs.label')" @dragstart.prevent>
    <div ref="tablist" class="project-tab-strip__tabs" role="tablist" :aria-label="t('desktop.tabs.label')" @keydown="navigate">
      <div v-for="tab in props.state?.tabs ?? []" :key="tab.tabId" class="project-tab-strip__item" role="presentation">
        <button type="button" role="tab" :data-tab-id="tab.tabId" :aria-selected="tab.tabId === props.state?.window.activeTabId"
          :tabindex="tab.tabId === props.state?.window.activeTabId || !props.state?.window.activeTabId ? 0 : -1"
          :aria-busy="pendingTab(tab.tabId, tab.project.root) || tab.state === 'loading'"
          aria-keyshortcuts="Control+Shift+ArrowLeft Control+Shift+ArrowRight Shift+F10"
          :aria-description="t('desktop.tabs.reorderHint')"
          :title="`${tab.project.root} — ${t(`desktop.tabs.${tab.state}`)}`"
          :class="['project-tab-strip__tab', { 'is-active': tab.tabId === props.state?.window.activeTabId, 'is-crashed': tab.state === 'crashed' }]"
          @pointerdown="pointerdown($event, tab.tabId)" @pointermove="pointermove" @pointerup="pointerup"
          @pointercancel="pointercancel" @lostpointercapture="pointercancel"
          @contextmenu.prevent="focusDetach(tab.tabId)" @click="click($event, tab.tabId)">
          <span class="project-tab-strip__name">{{ label(tab.project.root) }}</span>
          <span class="project-tab-strip__status">{{ t(`desktop.tabs.${pendingTab(tab.tabId, tab.project.root) ? 'pending' : tab.state}`) }}</span>
        </button>
        <!-- An inline command, never a popup painted below a WebContentsView. -->
        <button type="button" class="project-tab-strip__detach" :data-detach-tab-id="tab.tabId"
          :disabled="!bridge || commanding || tab.state !== 'ready' || pendingTab(tab.tabId, tab.project.root)"
          :aria-label="`${t('desktop.tabs.detach')}: ${label(tab.project.root)}`" :title="t('desktop.tabs.detach')"
          @click="command(tab.tabId)">↗</button>
        <button type="button" class="project-tab-strip__close" :data-close-tab-id="tab.tabId"
          :disabled="!bridge?.closeTab || commanding || tab.state === 'closing' || pendingTab(tab.tabId, tab.project.root)"
          :aria-label="`${t('desktop.tabs.close')}: ${label(tab.project.root)}`" :title="t('desktop.tabs.close')"
          @click="command(tab.tabId, 'close')">&times;</button>
      </div>
      <span v-for="root in openingRoots" :key="root" role="status" aria-busy="true" class="project-tab-strip__tab" :title="root">
        <span class="project-tab-strip__name">{{ label(root) }}</span>
        <span class="project-tab-strip__status">{{ t('desktop.tabs.pending') }}</span>
      </span>
    </div>
    <span v-if="failed" role="alert" class="project-tab-strip__error" :title="t('desktop.tabs.error')">{{ t('desktop.tabs.error') }}</span>
    <button ref="openButton" type="button" class="project-tab-strip__open" :aria-label="t('desktop.tabs.open')" :title="t('desktop.tabs.open')" aria-haspopup="dialog" @click="open">+</button>
  </nav>
</template>

<style scoped>
.project-tab-strip { display: flex; height: 100%; flex: 1; min-width: 0; gap: 6px; padding: 4px 36px 4px 0; -webkit-app-region: drag; }
.project-tab-strip__tabs { -webkit-app-region: no-drag; display: flex; gap: 6px; min-width: 0; overflow-x: auto; scrollbar-width: none; }
.project-tab-strip__item { display: flex; flex-shrink: 0; min-width: 0; overflow: hidden; border-radius: var(--vxs-radius-lg); background: var(--vxs-bg-elevated); }
.project-tab-strip button { -webkit-app-region: no-drag; outline-offset: -3px; }
.project-tab-strip__tab { display: flex; align-items: center; gap: 10px; min-width: 120px; max-width: 260px; padding: 0 14px; color: var(--vxs-text-muted); user-select: none; touch-action: none; }
.project-tab-strip__tab:hover, .project-tab-strip__open:hover, .project-tab-strip__detach:hover:not(:disabled), .project-tab-strip__close:hover:not(:disabled) { background: var(--vxs-bg-hover); }
.project-tab-strip__item:has(.is-active) { background: var(--vxs-accent-container); }
.project-tab-strip__tab.is-active { color: var(--vxs-text-on-accent-container); background: transparent; }
.project-tab-strip__tab.is-crashed, .project-tab-strip__error { color: var(--vxs-danger); }
.project-tab-strip__name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.project-tab-strip__status { font-size: 11px; }
.project-tab-strip__open { flex-shrink: 0; width: 36px; border-radius: var(--vxs-radius-lg); color: var(--vxs-text-muted); font-size: 24px; }
.project-tab-strip__detach, .project-tab-strip__close { width: 28px; flex-shrink: 0; color: var(--vxs-text-muted); }
.project-tab-strip__detach:disabled, .project-tab-strip__close:disabled { opacity: .35; }
.project-tab-strip__error { align-self: center; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
</style>
