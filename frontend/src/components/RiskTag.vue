<script setup>
// 风险等级标签（宋体展示，与规范文件气质一致）
import { riskTone } from '../api.js'

defineProps({ value: { type: String, default: '' } })
</script>

<template>
  <span :class="['risk-tag', riskTone(value)]">{{ value || '未判定' }}</span>
</template>
