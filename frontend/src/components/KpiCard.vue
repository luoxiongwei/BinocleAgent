<script setup>
// KPI 卡：左侧色带 + 衬线大数字（纯展示；跳转由各面板按钮承担，2026-10-18 回退过箭头）
// 2026-10-23：接入 React Bits 的 Count Up（数字滚动）+ Spotlight Card（指针聚光），
// 两者都是纯前端表现，不改数据来源与页面跳转语义。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: '—' },
  sub: { type: String, default: '' },
  color: { type: String, default: '' }, // danger/warn/safe/manual/accent
})

// —— 数字滚动 ——
const shown = ref(props.value)
let raf = 0
let from = 0
let to = 0
let started = 0

const numeric = (v) => {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  const text = String(v ?? '').trim()
  return /^-?\d+$/.test(text) ? Number(text) : null
}

function animate(target) {
  cancelAnimationFrame(raf)
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
  if (reduce) {
    shown.value = target
    return
  }
  from = numeric(shown.value) ?? 0
  to = target
  started = performance.now()
  const step = (now) => {
    const p = Math.min(1, (now - started) / 900)
    const eased = 1 - Math.pow(1 - p, 3)
    shown.value = Math.round(from + (to - from) * eased)
    if (p < 1) raf = requestAnimationFrame(step)
  }
  raf = requestAnimationFrame(step)
}

watch(
  () => props.value,
  (value) => {
    const target = numeric(value)
    if (target === null) {
      cancelAnimationFrame(raf)
      shown.value = value
      return
    }
    animate(target)
  },
)

onMounted(() => {
  const target = numeric(props.value)
  if (target === null) return
  shown.value = 0
  animate(target)
})

onBeforeUnmount(() => cancelAnimationFrame(raf))

// —— 指针聚光（Spotlight Card 极简版）——
const card = ref(null)
function onMove(event) {
  const el = card.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  el.style.setProperty('--mx', `${event.clientX - rect.left}px`)
  el.style.setProperty('--my', `${event.clientY - rect.top}px`)
}
</script>

<template>
  <div
    ref="card"
    class="kpi"
    :style="color ? { '--kpi-color': `var(--${color})` } : null"
    @pointermove="onMove"
  >
    <span class="kpi-label">{{ label }}</span>
    <strong class="kpi-value" :class="color ? `text-${color}` : ''">{{ shown }}</strong>
    <span v-if="sub" class="kpi-sub">{{ sub }}</span>
  </div>
</template>
