<script setup lang="ts">
/**
 * Who makes the Studio and on what terms (§6.1) — the rail's last entry, under Settings.
 *
 * **Deliberately not localised**, the one exception to §6.4: the window's content is a credits
 * card, shown in English in every language, so its text lives here and not in the locale
 * files. Only the frame around it — the dialog's name and the close button — is translated.
 */
import { Info, X } from 'lucide-vue-next'

import ModalDialog from '@/components/ui/ModalDialog.vue'
import { t } from '@/i18n'

const emit = defineEmits<{ close: [] }>()

const PRODUCT_URL = 'https://voxarium.studio/studio/'
const AUTHOR = { name: 'Voxarium Studio', url: 'https://voxarium.studio/' }
const CEO = 'Arseniy Zvezda'
const CONTRIBUTORS = [{ handle: 'devilast', name: 'Igor Nazarov' }]
</script>

<template>
  <ModalDialog :label="t('nav.about.label')" panel-class="w-[26rem]" @close="emit('close')">
    <header class="flex shrink-0 items-center gap-2 border-b border-line px-4 py-3">
      <Info class="size-4 shrink-0 text-accent" />
      <h2 class="min-w-0 flex-1 font-medium">{{ t('nav.about.label') }}</h2>
      <button
        class="grid size-6 shrink-0 place-items-center rounded-md text-faint hover:bg-hover hover:text-primary"
        :title="t('common.close')"
        @click="emit('close')"
      >
        <X class="size-4" />
      </button>
    </header>

    <div class="min-h-0 flex-1 space-y-5 overflow-y-auto px-6 py-5 text-center">
      <section>
        <a :href="PRODUCT_URL" target="_blank" rel="noopener" class="about-link text-lg font-medium">
          Voxarium Studio
        </a>
        <p class="mt-1 text-small text-muted">Open Source | MIT Licence | Free For All</p>
      </section>

      <section>
        <h3 class="about-heading">Authors</h3>
        <a :href="AUTHOR.url" target="_blank" rel="noopener" class="about-link">{{ AUTHOR.name }}</a>
        <p class="text-secondary">CEO | {{ CEO }}</p>
      </section>

      <section>
        <h3 class="about-heading">Contributors</h3>
        <p v-for="person in CONTRIBUTORS" :key="person.handle" class="text-secondary">
          {{ person.handle }} | {{ person.name }}
        </p>
      </section>
    </div>
  </ModalDialog>
</template>

<style scoped>
.about-heading {
  margin-bottom: 4px;
  font-size: var(--vxs-text-small, 12px);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--vxs-text-faint);
}

.about-link {
  color: var(--vxs-accent);
}

.about-link:hover {
  text-decoration: underline;
}
</style>
