<script setup lang="ts">
/**
 * What a content area shows while its first read is on the way (§7): three grey bars
 * shimmering a step apart, in place of the word "Loading…". The eye gets the shape before the
 * data, which reads as faster than the word does.
 *
 * The bars are `vxs-skeleton` (`base.css`), which also keeps them out of sight for the first
 * 200ms — so a read that answers inside that never shows them, and the page's own content simply
 * arrives. The words stay, for assistive technology, which has no shimmer to see.
 */
import { t } from '@/i18n'
</script>

<template>
  <!-- The words come last: the bars take their shimmer's step from their position. -->
  <div class="max-w-md space-y-2.5 py-1" role="status">
    <span class="vxs-skeleton" />
    <span class="vxs-skeleton w-[70%]" />
    <span class="vxs-skeleton w-[45%]" />
    <span class="sr-only">{{ t('common.loading') }}</span>
  </div>
</template>
