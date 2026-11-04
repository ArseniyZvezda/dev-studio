<script setup lang="ts">
import { defineAsyncComponent, onBeforeUnmount, onMounted, ref } from 'vue'
import { PopoverRoot, PopoverTrigger, PopoverPortal, PopoverContent } from 'reka-ui'
import { ChevronDown } from 'lucide-vue-next'
import { useSessionStore } from '@/stores/session'
import { t } from '@/i18n'

const ProjectMenuPanel = defineAsyncComponent(() => import('./ProjectMenuPanel.vue'))
const session = useSessionStore()
const open = ref(false)
const show = () => { open.value = true }
onMounted(() => window.addEventListener('studio:open-project', show))
onBeforeUnmount(() => window.removeEventListener('studio:open-project', show))
</script>

<template>
  <PopoverRoot v-model:open="open">
    <PopoverTrigger class="flex max-w-full items-center gap-2 rounded-md px-2 py-1.5 text-primary hover:bg-hover focus-visible:outline-2 focus-visible:outline-accent" :title="`${session.project?.root ?? ''}\n${t('nav.switchProject')}`" :aria-label="`${session.projectName}: ${t('nav.switchProject')}`">
      <img src="/studio-logo.png" alt="" aria-hidden="true" class="size-6 shrink-0" />
      <span class="truncate font-medium">{{ session.projectName }}</span>
      <ChevronDown class="size-3.5 shrink-0 text-muted" />
    </PopoverTrigger>
    <PopoverPortal>
      <PopoverContent align="start" :side-offset="8" :collision-padding="12" class="z-50 overflow-hidden rounded-lg border border-line bg-surface text-primary shadow-lg" :aria-label="t('project.open.title')" @interact-outside="session.switching && $event.preventDefault()">
        <ProjectMenuPanel @close="open = false" />
      </PopoverContent>
    </PopoverPortal>
  </PopoverRoot>
</template>
