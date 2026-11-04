<script setup lang="ts">
defineProps<{ title: string; hint?: string }>()
</script>

<template>
  <div class="grid h-full place-items-center p-8 text-center">
    <div class="max-w-sm">
      <slot name="icon" />
      <p class="font-medium">{{ title }}</p>
      <p v-if="hint" class="mt-1 text-muted">{{ hint }}</p>
      <slot />
    </div>
  </div>
</template>
