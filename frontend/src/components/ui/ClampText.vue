<script setup lang="ts">
/**
 * A paragraph held to a few lines, with the rest one click away.
 *
 * For text that arrives as data and at any length — a plugin's description out of its manifest,
 * a skill's out of its frontmatter — in a list where every row should be findable at a glance:
 * a fourteen-line description turns a list of five things into a page about one of them.
 *
 * The toggle exists only while there is something to show. Whether the text overflows is
 * measured rather than guessed from its length, because that depends on the column's width and
 * the reader's text size, and it is measured again when either changes.
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { t } from '@/i18n'

const props = withDefaults(defineProps<{ text: string; lines?: number }>(), { lines: 3 })

const body = ref<HTMLElement | null>(null)
const open = ref(false)
const overflows = ref(false)

/** Only while clamped: an open paragraph has nothing cut off, and must keep its "Show less". */
function measure() {
  const element = body.value
  if (!element || open.value) return
  overflows.value = element.scrollHeight > element.clientHeight + 1
}

let observer: ResizeObserver | null = null

onMounted(() => {
  measure()
  if (!body.value) return
  observer = new ResizeObserver(measure)
  observer.observe(body.value)
})

onBeforeUnmount(() => observer?.disconnect())

watch(
  () => props.text,
  async () => {
    open.value = false
    await nextTick()
    measure()
  },
)
</script>

<template>
  <div>
    <!-- The line count is handed down as a custom property (level 3 of the styling rules): it is
         the caller's number, known only at runtime. -->
    <p ref="body" :class="{ clamped: !open }" :style="{ '--clamp-lines': lines }">{{ text }}</p>
    <button
      v-if="overflows"
      type="button"
      class="mt-0.5 text-small text-faint hover:text-primary"
      :aria-expanded="open"
      @click="open = !open"
    >
      {{ open ? t('common.showLess') : t('common.showMore') }}
    </button>
  </div>
</template>

<style scoped>
.clamped {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: var(--clamp-lines);
  line-clamp: var(--clamp-lines);
  overflow: hidden;
}
</style>
