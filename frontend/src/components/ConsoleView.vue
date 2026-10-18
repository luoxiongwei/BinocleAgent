<script setup>
// 工作总览（原「监督台」，2026-10-17 IA 重构改名）：KPI + 处置分布 + 服务状态 + 最近任务 + 人工归档
// 2026-10-19：移除「验收批次」KPI 与「历史验收批次」面板——验收批次以 30 对金标集为前提，
// 交付给用户的新图对没有专家标注，该概念对受众恒为 0 且不可达；批次能力保留在档案管理与 CLI（agent.run_all）。
import { computed } from 'vue'
import { api, dispositionOf, fmtDateTime, pairLabel } from '../api.js'
import SectionHead from './SectionHead.vue'
import KpiCard from './KpiCard.vue'
import EmptyState from './EmptyState.vue'
import DispositionTag from './DispositionTag.vue'
import { IconGear, IconReview } from './icons.js'

const props = defineProps({
  overview: { type: Object, default: () => ({}) },
  agentTasks: { type: Array, default: () => [] }, // :8000 graph_runs 实时归档
  pending: { type: Array, default: () => [] },
  archives: { type: Array, default: () => [] },
  health: { type: Object, default: () => ({}) },
})

const emit = defineEmits(['refresh', 'goto', 'open-archive', 'open-task'])

// 在线研判任务（graph-* 实时归档）并入全量行：KPI 与处置分布不再只有旧批跑数据
const agentRows = computed(() => props.agentTasks || [])
const allRows = computed(() => [
  ...(props.overview.runs?.flatMap((r) => r.rows || []) || []),
  ...agentRows.value,
])

const kpis = computed(() => {
  const rows = allRows.value
  return {
    pending: props.pending.length,
    alerts: rows.filter((r) => r.disposition === 'P1_auto_alert').length,
    done: rows.filter((r) => r.status === 'completed').length,
    agent: agentRows.value.length,
  }
})

// 处置分布（跨全部批次行 + 在线研判任务）
const dist = computed(() => {
  const tally = {}
  for (const row of allRows.value) {
    const key = row.disposition || '未知'
    tally[key] = (tally[key] || 0) + 1
  }
  const total = allRows.value.length || 1
  return Object.entries(tally)
    .sort((a, b) => b[1] - a[1])
    .map(([code, n]) => ({ code, n, pct: (n / total) * 100, meta: dispositionOf(code) }))
})

// 最近研判任务：按 graph_state.json mtime 倒序，点击跳到安全日志生成页
const recentTasks = computed(() =>
  [...agentRows.value].sort((a, b) => String(b.updated).localeCompare(String(a.updated))).slice(0, 8),
)

const apiBase = computed({
  get: () => api.getBase(),
  set: (v) => {
    api.setBase(v)
  },
})

function saveBase() {
  emit('refresh')
}
</script>

<template>
  <!-- KPI（纯展示：页面跳转由下方各面板自带按钮承担，不在 KPI 重复造轮子） -->
  <div class="kpi-strip">
    <KpiCard label="待人工复核" :value="kpis.pending" sub="系统无法自动裁决" color="manual" />
    <KpiCard label="强制告警" :value="kpis.alerts" sub="P1_auto_alert" color="danger" />
    <KpiCard label="研判完成" :value="kpis.done" sub="在线研判归档" color="safe" />
    <KpiCard label="在线研判" :value="kpis.agent" sub="graph_runs 实时归档" color="warn" />
  </div>

  <!-- 处置分布 -->
  <SectionHead eyebrow="DISPOSITION MIX" title="处置分布" note="二值口径：判不了即转人工" />
  <div class="panel"><div class="panel-body stack stack-4">
    <div class="dist-bar">
      <div
        v-for="seg in dist"
        :key="seg.code"
        class="dist-seg"
        :style="{ width: seg.pct + '%', background: `var(--${seg.meta.tone})` }"
        :title="`${seg.meta.label} ${seg.n}`"
      />
    </div>
    <div class="dist-legend">
      <span v-for="seg in dist" :key="seg.code" class="lg">
        <span class="sw" :style="{ background: `var(--${seg.meta.tone})` }" />
        {{ seg.meta.label }}
        <b class="mono">{{ seg.n }}</b>
      </span>
      <span v-if="!dist.length" class="faint">暂无论证数据</span>
    </div>
  </div></div>

  <!-- 服务与接口 -->
  <div class="panel console-service-panel" style="margin-top: var(--sp-4)">
    <div class="panel-head"><IconGear /><span class="panel-title">服务与接口</span></div>
    <div class="panel-body stack stack-4">
      <div class="facts-grid">
        <div class="fact"><span>任务服务 :8000</span><b :class="health.status ? 'text-safe' : 'text-danger'">{{ health.status ? '在线' : '离线' }}</b></div>
        <div class="fact"><span>控制台 :8010</span><b :class="health.console_mounted ? 'text-safe' : 'text-danger'">{{ health.console_mounted ? '已挂载' : '未挂载' }}</b></div>
        <div class="fact"><span>模型 API 配置</span><b :class="health.config_present ? 'text-safe' : 'text-danger'">{{ health.config_present ? '已就绪' : '待填写' }}</b></div>
        <div class="fact"><span>规范库</span><b>{{ health.schemas?.length ?? '—' }} schema</b></div>
      </div>
      <div class="field">
        <label class="field-label" for="api-base">只读调试 API 基址</label>
        <div class="row">
          <input id="api-base" v-model="apiBase" class="input mono" placeholder="/api" />
          <button class="btn btn-secondary" @click="saveBase">保存并连接</button>
        </div>
        <p class="field-hint">
          <span class="mono">/api</span> → 8010 只读验收控制台 ·
          <span class="mono">/tasks · /health · /inputs</span> → 8000 任务执行服务（前端按路径自动分流）
        </p>
      </div>
    </div>
  </div>

  <!-- 最近研判任务（在线 Agent 实时数据） -->
  <SectionHead
    eyebrow="LIVE TASKS"
    title="最近研判任务"
    note="来自 :8000 任务服务 graph_runs 的实时归档，点击可打开对应安全日志"
  >
    <button class="btn btn-ghost btn-sm" @click="emit('goto', 'task')">生成新日志</button>
  </SectionHead>
  <div class="panel">
    <div v-if="recentTasks.length" class="panel-body flush">
      <button
        v-for="t in recentTasks"
        :key="t.task_id"
        class="data-row"
        style="grid-template-columns: 1fr auto"
        title="在安全日志生成页打开"
        @click="emit('open-task', t.task_id)"
      >
        <span class="grow">
          <span class="row-id" style="display: block">{{ t.task_id }}</span>
          <span class="row-sub" style="display: block">
            {{ pairLabel(t.pair_id) }} · {{ fmtDateTime(t.updated) }} · {{ t.tool_calls?.length || 0 }} 次工具调用
          </span>
        </span>
        <span class="row-meta">
          <DispositionTag v-if="t.disposition" :code="t.disposition" />
          <span v-else class="tag-mono">{{ t.status || '进行中' }}</span>
        </span>
      </button>
    </div>
    <EmptyState v-else title="暂无论研任务" hint="在「安全日志生成」页启动任务后，会自动出现在这里" />
  </div>

  <!-- 人工归档 -->
  <SectionHead
    eyebrow="HUMAN ARCHIVES"
    title="离线人工归档"
    note="转人工任务的留痕记录"
  >
    <button class="btn btn-ghost btn-sm" @click="emit('goto', 'review')">
      <IconReview />去复核
    </button>
  </SectionHead>
  <div class="panel">
      <div v-if="archives.length" class="panel-body flush">
        <button
          v-for="item in archives.slice(0, 6)"
          :key="item.task_id"
          class="data-row"
          style="grid-template-columns: 1fr auto"
          title="在复核台打开"
          @click="emit('open-archive', item.task_id)"
        >
          <span class="grow">
            <span class="row-id" style="display: block">{{ item.task_id }}</span>
            <span class="row-sub" style="display: block">{{ pairLabel(item.pair_id) }} · {{ item.failure_reason_code || '原因未分类' }}</span>
          </span>
          <span class="row-meta"><DispositionTag code="P3_manual_judge" /></span>
        </button>
      </div>
    <EmptyState v-else title="暂无归档" hint="转人工且已完成回写的任务会出现在这里" />
  </div>
</template>
