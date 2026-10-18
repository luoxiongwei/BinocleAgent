<script setup>
// 处置档位标签：颜色与处置语义一一对应（红=告警 / 琥珀=提示 / 绿=归档 / 青=转人工）
import { computed } from 'vue'
import { dispositionOf } from '../api.js'

const props = defineProps({
  code: { type: String, default: '' },
})

const meta = computed(() => dispositionOf(props.code))
</script>

<template>
  <span :class="['pill', `pill-${meta.tone}`]" :title="meta.label">
    <span class="dot" />
    {{ meta.short }}
  </span>
</template>
