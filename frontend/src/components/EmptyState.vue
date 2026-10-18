<script setup>
// 空态：教育式文案，说明"这里为什么空、下一步做什么"
defineProps({
  title: { type: String, default: '暂无数据' },
  hint: { type: String, default: '' },
})
</script>

<template>
  <div class="empty-state">
    <div class="empty-icon"><slot name="icon" /></div>
    <b>{{ title }}</b>
    <p v-if="hint">{{ hint }}</p>
    <slot />
  </div>
</template>
