<script setup lang="ts">
/** Application-only shell. No project runtime is imported here (dev spec §5.3.4). */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createDesktopShellController, type DesktopShellController } from '@/desktop/tabs'
import DesktopTitleBar from '@/components/layout/DesktopTitleBar.vue'
import ProjectTabStrip from '@/components/layout/ProjectTabStrip.vue'
import { t } from '@/i18n'
import I18nT from '@/i18n/I18nT.vue'

const props = defineProps<{ controller?: DesktopShellController }>()
const controller = props.controller ?? createDesktopShellController()
const { state, pending, error, menuOpen, recent, listing, selected, picker, busy } = controller
const displayed = ref(false)
const panel = ref<HTMLElement | null>(null)
const draft = ref('')
const showHidden = ref(false)
let returnFocus: HTMLElement | null = null
let menuGeneration = 0
const entries = computed(() => (listing.value?.entries ?? []).filter(entry => showHidden.value || !entry.hidden))
const canOpen = computed(() => !!selected.value?.exists && !!selected.value.is_dir && !busy.value)
const creates = computed(() => canOpen.value && !selected.value?.is_project)
const activeTab = computed(() => state.value?.tabs.find(tab => tab.tabId === state.value?.window.activeTabId))
watch(selected, value => { draft.value = value?.path ?? '' })
watch(menuOpen, open => {
  if (!open && displayed.value) displayed.value = false
})

async function showMenu() {
  if (displayed.value) return
  const generation = ++menuGeneration
  returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  // Wait for main to hide the native project view before rendering above its area.
  await controller.setMenuOpen(true)
  if (generation !== menuGeneration || !menuOpen.value) return
  displayed.value = true
  await nextTick()
  panel.value?.querySelector<HTMLElement>('button, input')?.focus()
  void controller.refreshRecent()
}
async function closeMenu() {
  ++menuGeneration
  await controller.setMenuOpen(false)
  // Keep the dialog visible if a stale revision/native failure rejected close.
  if (!menuOpen.value) { displayed.value = false; returnFocus?.focus() }
}
async function confirm() {
  if (!canOpen.value || !selected.value) return
  await controller.openProject(selected.value.path, !selected.value.is_project)
  if (!error.value) await closeMenu()
}
async function openRecent(root: string) {
  // Recent metadata may be stale. Never infer permission to create from a row click.
  await controller.inspect(root)
  if (selected.value?.path && selected.value.exists && selected.value.is_dir && selected.value.is_project) {
    await controller.openProject(selected.value.path, false)
    if (!error.value) await closeMenu()
  }
}
function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && (displayed.value || menuOpen.value)) { event.preventDefault(); void closeMenu(); return }
  if (event.key !== 'Tab' || !displayed.value) return
  const nodes = Array.from(panel.value?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)') ?? [])
  const first = nodes[0], last = nodes.at(-1)
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(() => { document.addEventListener('keydown', keydown); void controller.start() })
onBeforeUnmount(() => { ++menuGeneration; document.removeEventListener('keydown', keydown); controller.dispose() })
</script>

<template>
  <div class="desktop-shell">
    <DesktopTitleBar>
      <ProjectTabStrip :state="state" :pending="pending" @activate="controller.activate" @open="showMenu" />
    </DesktopTitleBar>
    <main class="desktop-shell__body">
      <p v-if="error" role="alert" class="text-danger">{{ t('desktop.tabs.error') }}: {{ error }}</p>
      <button v-if="error && !state" type="button" class="rounded border border-line px-3 py-2" @click="controller.start()">{{ t('common.retry') }}</button>
      <p v-if="activeTab?.state === 'crashed'" role="alert" class="text-danger">{{ t('desktop.tabs.crashed') }}: {{ activeTab.project.root }}</p>
      <p v-if="pending.length" role="status">{{ t('desktop.tabs.pending') }}</p>
      <template v-if="!state?.tabs.length">
        <p>{{ t('desktop.tabs.empty') }}</p>
        <button type="button" class="vxs-btn-primary px-4 py-2" @click="showMenu">{{ t('desktop.tabs.open') }}</button>
      </template>
    </main>
    <div v-if="displayed" class="desktop-shell__backdrop" @pointerdown.self="closeMenu">
      <section ref="panel" role="dialog" aria-modal="true" :aria-label="t('project.open.title')" class="desktop-shell__menu rounded-lg border border-line bg-surface text-primary shadow-lg" :aria-busy="busy">
        <header class="flex items-center justify-between border-b border-line px-4 py-3">
          <h1 class="font-medium">{{ t('project.open.title') }}</h1>
          <button type="button" class="rounded px-2 py-1 hover:bg-hover" @click="closeMenu">{{ t('common.cancel') }}</button>
        </header>
        <div class="space-y-3 p-4">
          <p v-if="error" role="alert" class="text-danger">{{ error }}</p>
          <button type="button" class="vxs-btn-primary px-3 py-2" :disabled="busy || !picker?.available" @click="controller.pick()">{{ t('project.open.browseComputer') }}</button>
          <p v-if="picker && !picker.available" class="text-small text-muted">{{ t('project.open.pickerUnavailable', { reason: picker.reason }) }}</p>
          <h2 class="text-small text-muted">{{ t('project.open.recent') }}</h2>
          <ul class="max-h-40 overflow-y-auto">
            <li v-for="project in recent" :key="project.root" class="flex items-center gap-2">
              <button type="button" class="min-w-0 flex-1 truncate rounded px-2 py-2 text-left hover:bg-hover" :disabled="busy" :title="project.root" @click="openRecent(project.root)">{{ project.name }} <span v-if="!project.exists" class="text-muted">({{ t('project.open.missing') }})</span></button>
              <button type="button" class="rounded px-2 py-1 text-small text-muted hover:bg-hover" :disabled="busy" :aria-label="`${t('project.open.forget')}: ${project.name}`" @click="controller.forgetRecent(project.root)">{{ t('project.open.forget') }}</button>
            </li>
          </ul>
          <details @toggle="(event) => { if ((event.target as HTMLDetailsElement).open && !listing) controller.browse('') }">
            <summary class="cursor-pointer text-muted">{{ t('project.open.browse') }}</summary>
            <label class="my-2 flex gap-2 text-small"><input v-model="showHidden" type="checkbox" />{{ t('project.open.showHidden') }}</label>
            <div class="rounded border border-line">
              <div class="flex items-center gap-2 border-b border-line p-2">
                <button type="button" :disabled="busy || !listing || listing.parent === null" @click="controller.browse(listing?.parent ?? '')">{{ t('project.open.up') }}</button>
                <span class="truncate font-mono text-small" :title="listing?.path">{{ listing?.path || listing?.name }}</span>
              </div>
              <ul class="max-h-40 overflow-y-auto">
                <li v-for="entry in entries" :key="entry.path"><button type="button" class="w-full px-3 py-2 text-left hover:bg-hover" :disabled="busy" @click="controller.browse(entry.path)">{{ entry.name }} <span v-if="entry.is_project" class="text-small text-accent">{{ t('project.open.projectTag') }}</span></button></li>
                <li v-if="listing && !entries.length" class="p-2 text-muted">{{ t('project.open.noSubfolders') }}</li>
              </ul>
              <p v-if="listing?.truncated" class="p-2 text-small text-muted">{{ t('project.open.truncated') }}</p>
            </div>
          </details>
          <form class="flex items-end gap-2" @submit.prevent="controller.inspect(draft.trim())">
            <label class="min-w-0 flex-1"><span class="mb-1 block text-small text-muted">{{ t('project.open.folder') }}</span><input v-model="draft" spellcheck="false" class="w-full rounded border border-line bg-inset px-3 py-2 font-mono" :disabled="busy" /></label>
            <button type="submit" class="rounded border border-line px-3 py-2 hover:bg-hover" :disabled="busy || !draft.trim()">{{ t('project.open.browse') }}</button>
          </form>
          <p v-if="selected && (!selected.exists || !selected.is_dir)" role="status" class="text-warning">{{ t('project.open.notAFolder', { path: selected.path }) }}</p>
          <p v-else-if="creates" class="text-warning"><I18nT keypath="project.open.willInitialise" :params="{ name: selected?.name ?? '' }"><template #dir><code>.voxarium_studio/</code></template></I18nT></p>
          <p v-else-if="selected?.is_project" class="text-muted">{{ t('project.open.opensExisting', { name: selected.name }) }}</p>
        </div>
        <footer class="flex justify-end border-t border-line px-4 py-3"><button type="button" class="vxs-btn-primary px-3 py-2 disabled:opacity-40" :disabled="!canOpen || draft.trim() !== selected?.path" @click="confirm">{{ creates ? t('project.open.createAndOpen') : t('common.open') }}</button></footer>
      </section>
    </div>
  </div>
</template>

<style scoped>
.desktop-shell { height: 100vh; display: flex; flex-direction: column; color: var(--vxs-text-primary); }
.desktop-shell__body { flex: 1; padding: 32px; display: flex; flex-direction: column; align-items: center; gap: 16px; color: var(--vxs-text-muted); }
.desktop-shell__backdrop { position: fixed; inset: var(--vxs-titlebar-height) 0 0; z-index: 50; background: rgb(0 0 0 / 25%); padding: 12px; overflow: auto; -webkit-app-region: no-drag; }
.desktop-shell__menu { width: min(600px, 100%); margin: 0 auto; }
</style>
