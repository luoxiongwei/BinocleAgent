<script setup>
// 统一时间轴日历（2026-10-17 IA 重构）：暗色月历 + 三类产物圆点标注。
//   ● 橙 = 验收批次（历史批跑）   ● 黄 = 研判任务（graph_runs）   ● 蓝 = 待审复核（pending）
// 数据来自 buildTimeline（本地日期分组）；选中日期经 update:modelValue / select 双通道
// 抛出，由父级决定「再点一次取消选择」。月份游标优先落在选中日期，否则最近有数据日期。
import { computed, ref, watch } from 'vue'
import { dayKey } from '../api.js'

const props = defineProps({
  days: { type: Array, default: () => [] }, // buildTimeline() 输出
  modelValue: { type: String, default: '' }, // 选中日期 'YYYY-MM-DD'，'' = 未选
})
const emit = defineEmits(['update:modelValue', 'select'])

const WEEK_CN = ['一', '二', '三', '四', '五', '六', '日']
const pad = (n) => String(n).padStart(2, '0')
const todayKey = () => {
  const d = new Date()
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

const byDate = computed(() => {
  const m = {}
  for (const d of props.days) if (d.date) m[d.date] = d
  return m
})
const dotsOf = (iso) => {
  const d = byDate.value[iso]
  if (!d) return []
  return [
    d.runs.length ? 'run' : '',
    d.tasks.length ? 'task' : '',
    d.reviews.length ? 'review' : '',
  ].filter(Boolean)
}

const cursor = ref({ y: 0, m: 0 })
function jumpTo(iso) {
  const d = iso ? new Date(`${iso}T00:00:00`) : null
  if (d && !Number.isNaN(d.getTime())) {
    cursor.value = { y: d.getFullYear(), m: d.getMonth() + 1 }
    return
  }
  const latest = props.days.find((x) => x.date)
  if (latest) {
    const ld = new Date(`${latest.date}T00:00:00`)
    cursor.value = { y: ld.getFullYear(), m: ld.getMonth() + 1 }
    return
  }
  const t = new Date()
  cursor.value = { y: t.getFullYear(), m: t.getMonth() + 1 }
}
watch(() => props.modelValue, jumpTo, { immediate: true })
watch(
  () => props.days.map((d) => d.date).join(','),
  () => {
    if (!props.modelValue && !byDate.value[`${cursor.value.y}-${pad(cursor.value.m)}`]) jumpTo('')
  },
)

const monthLabel = computed(() => `${cursor.value.y}年${cursor.value.m}月`)
function shiftMonth(delta) {
  const d = new Date(cursor.value.y, cursor.value.m - 1 + delta, 1)
  cursor.value = { y: d.getFullYear(), m: d.getMonth() + 1 }
}

const cells = computed(() => {
  const { y, m } = cursor.value
  const offset = (new Date(y, m - 1, 1).getDay() + 6) % 7 // 周一为一周首日
  const nDays = new Date(y, m, 0).getDate()
  const out = []
  for (let i = 0; i < offset; i += 1) out.push(null)
  for (let d = 1; d <= nDays; d += 1) out.push(`${y}-${pad(m)}-${pad(d)}`)
  return out
})

// 点选语义：点新日期 = 选中；再点一次已选日期 = 取消（空串）。
// 切换判断放在组件内（读 props.modelValue），避免父级 v-model 与 select 双通道竞态。
function pick(iso) {
  const next = props.modelValue === iso ? '' : iso
  emit('update:modelValue', next)
  emit('select', next)
}
function goToday() {
  emit('update:modelValue', today)
  emit('select', today)
}
const today = todayKey()
</script>

<template>
  <div class="cal">
    <div class="cal-head">
      <button class="cal-nav" title="上一个月" @click="shiftMonth(-1)">‹</button>
      <span class="cal-month">{{ monthLabel }}</span>
      <button class="cal-nav" title="下一个月" @click="shiftMonth(1)">›</button>
    </div>
    <div class="cal-week">
      <span v-for="w in WEEK_CN" :key="w">{{ w }}</span>
    </div>
    <div class="cal-grid">
      <template v-for="(iso, i) in cells" :key="i">
        <span v-if="!iso" class="cal-cell is-blank" />
        <button
          v-else
          :class="['cal-cell', {
            'is-today': iso === today,
            'is-selected': iso === modelValue,
            'has-data': Boolean(byDate[iso]),
          }]"
          :title="byDate[iso] ? `${iso}：批次 ${byDate[iso].runs.length} · 任务 ${byDate[iso].tasks.length} · 待审 ${byDate[iso].reviews.length}` : iso"
          @click="pick(iso)"
        >
          <span class="cal-day">{{ Number(iso.slice(8)) }}</span>
          <span v-if="dotsOf(iso).length" class="cal-dots">
            <i v-for="k in dotsOf(iso)" :key="k" :class="['cal-dot', `dot-${k}`]" />
          </span>
        </button>
      </template>
    </div>
    <div class="cal-legend">
      <span><i class="cal-dot dot-run" />验收批次</span>
      <span><i class="cal-dot dot-task" />研判任务</span>
      <span><i class="cal-dot dot-review" />待审复核</span>
    </div>
    <div class="cal-foot">
      <span class="cal-selected">{{ modelValue ? `已选 ${modelValue.slice(5)}` : '未选择日期' }}</span>
      <button class="cal-today" @click="goToday">今天</button>
    </div>
  </div>
</template>

<style scoped>
.cal {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
  padding: 10px;
}
.cal-head { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
.cal-month { flex: 1; text-align: center; font-size: var(--fs-small); font-weight: 700; letter-spacing: 0.04em; }
.cal-nav {
  width: 24px; height: 24px; border: 1px solid var(--line); border-radius: 6px;
  background: transparent; color: var(--text-dim); cursor: pointer; font-size: 14px; line-height: 1;
}
.cal-nav:hover { border-color: var(--line-strong); color: var(--text); }
.cal-week, .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }
.cal-week span { text-align: center; font-size: 11px; color: var(--text-faint); padding: 4px 0; }
.cal-cell {
  position: relative; aspect-ratio: 1; min-width: 0; border: 1px solid transparent; border-radius: 6px;
  background: transparent; color: var(--text-dim); cursor: pointer;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px;
  font-size: 12px; padding: 0;
}
.cal-cell.is-blank { cursor: default; }
.cal-cell.has-data { color: var(--text); font-weight: 600; }
.cal-cell.is-today { border-color: var(--accent-dim); }
.cal-cell.is-selected { background: var(--accent-wash); border-color: var(--accent); color: var(--accent); }
.cal-cell:hover:not(.is-blank) { background: var(--surface-2); }
.cal-cell.is-selected:hover { background: var(--accent-wash); }
.cal-dots { display: flex; gap: 2px; height: 4px; }
.cal-dot { width: 4px; height: 4px; border-radius: 50%; display: inline-block; }
.dot-run { background: var(--accent); }
.dot-task { background: var(--warn); }
.dot-review { background: var(--manual); }
.cal-legend {
  display: flex; flex-wrap: wrap; gap: 4px 10px; margin-top: 10px; padding-top: 8px;
  border-top: 1px solid var(--line); font-size: 11px; color: var(--text-faint);
}
.cal-legend span { display: inline-flex; align-items: center; gap: 4px; }
.cal-foot { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.cal-selected { flex: 1; font-size: 11.5px; color: var(--text-faint); font-family: var(--font-mono); }
.cal-today {
  border: 1px solid var(--line); border-radius: 6px; background: transparent;
  color: var(--text-dim); font-size: 11.5px; padding: 3px 8px; cursor: pointer;
}
.cal-today:hover { border-color: var(--accent); color: var(--accent); }
</style>
