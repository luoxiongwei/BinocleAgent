<script setup>
// KPI 卡：左侧色带 + 宋体大数字（纯展示；页面跳转由各面板自带按钮承担，
// 2026-10-18 回退：KPI 上的跳转箭头与面板按钮重复造轮子）
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: '—' },
  sub: { type: String, default: '' },
  color: { type: String, default: '' }, // danger/warn/safe/manual/accent
})
</script>

<template>
  <div class="kpi" :style="color ? { '--kpi-color': `var(--${color})` } : null">
    <span class="kpi-label">{{ label }}</span>
    <strong class="kpi-value" :class="color ? `text-${color}` : ''">{{ value }}</strong>
    <span v-if="sub" class="kpi-sub">{{ sub }}</span>
  </div>
</template>
