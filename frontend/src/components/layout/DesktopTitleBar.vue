<script setup lang="ts">
/** The Electron title bar shares the Studio's theme tokens (dev spec §2.7). */
import { Copy, Minus, PanelBottomClose, Square, X } from 'lucide-vue-next'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { locale, t } from '@/i18n'
import type { DesktopWindowState } from '@/desktop'

import ProjectSwitcher from '@/components/project/ProjectSwitcher.vue'
import { useSessionStore } from '@/stores/session'
const session = useSessionStore()
const controls = window.voxariumWindow
const state = ref<DesktopWindowState>({ maximized: false, focused: true })
watch(locale, value => { void controls?.setLocale?.(value) }, { immediate: true })
let unsubscribe: (() => void) | undefined
onMounted(async () => {
  if (!controls) return
  unsubscribe = controls.onStateChange(value => { state.value = value })
  state.value = await controls.getState()
})
onBeforeUnmount(() => unsubscribe?.())
</script>

<template>
  <header class="desktop-titlebar" :class="{ 'is-inactive': !state.focused }">
    <div v-if="session.project" class="desktop-titlebar__project"><ProjectSwitcher /></div>
    <span class="desktop-titlebar__title"><img src="/studio-logo.png" alt="" aria-hidden="true" class="mr-1.5 inline-block size-5 align-middle" />{{ t('app.name') }}</span>
    <div v-if="controls" class="desktop-titlebar__controls">
      <button v-if="controls.minimizeToTray" type="button" :title="t('desktop.minimizeToTray')" :aria-label="t('desktop.minimizeToTray')" @click="controls.minimizeToTray()">
        <PanelBottomClose aria-hidden="true" />
      </button>
      <button type="button" :title="t('desktop.minimize')" :aria-label="t('desktop.minimize')" @click="controls.minimize()">
        <Minus aria-hidden="true" />
      </button>
      <button type="button"
        :title="t(state.maximized ? 'desktop.restore' : 'desktop.maximize')"
        :aria-label="t(state.maximized ? 'desktop.restore' : 'desktop.maximize')"
        @click="controls.toggleMaximize()">
        <Copy v-if="state.maximized" aria-hidden="true" />
        <Square v-else aria-hidden="true" />
      </button>
      <button type="button" class="desktop-titlebar__close" :title="t('desktop.closeWindow')" :aria-label="t('desktop.closeWindow')" @click="controls.close()">
        <X aria-hidden="true" />
      </button>
    </div>
  </header>
</template>

<style scoped>
.desktop-titlebar {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  height: 44px;
  flex-shrink: 0;
  background: var(--vxs-bg-surface);
  border-bottom: 1px solid var(--vxs-border);
  color: var(--vxs-text-muted);
  user-select: none;
  -webkit-app-region: drag;
}
.desktop-titlebar__title {
  position: absolute;
  inset-inline: 320px 190px;
  text-align: center;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.025em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  pointer-events: none;
}
.is-inactive .desktop-titlebar__title { opacity: 0.55; }
.desktop-titlebar__controls { display: flex; height: 100%; -webkit-app-region: no-drag; }
.desktop-titlebar__controls button {
  display: grid;
  place-items: center;
  width: 46px;
  height: 100%;
  color: var(--vxs-text-muted);
  background: transparent;
  border: 0;
  outline-offset: -3px;
}
.desktop-titlebar__controls button:hover { background: var(--vxs-bg-hover); color: var(--vxs-text-primary); }
.desktop-titlebar__controls button:active { background: var(--vxs-border); }
.desktop-titlebar__controls button.desktop-titlebar__close:hover { background: var(--vxs-danger); color: var(--vxs-white); }
.desktop-titlebar__controls svg { width: 13px; height: 13px; stroke-width: 1.5; }
.desktop-titlebar__project { margin-right: auto; padding: 0 10px; max-width: 310px; -webkit-app-region: no-drag; }
</style>
