<script setup>
// 工作总览（原「监督台」，2026-10-17 IA 重构改名）：KPI + 处置分布 + 服务状态 + 最近任务 + 人工归档
// 2026-10-19：移除「验收批次」KPI 与「历史验收批次」面板——验收批次以 30 对金标集为前提，
// 交付给用户的新图对没有专家标注，该概念对受众恒为 0 且不可达；批次能力保留在档案管理与 CLI（agent.run_all）。
import { computed, onMounted, ref, watch } from 'vue'
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

// ---- 经验记忆（反馈汇总 + 验收案例）----
// 数据全部来自 8000：/feedback/summary、/feedback/candidates、/memory/cases
const memory = ref({ summary: null, candidates: [], cases: [], total: 0, error: '' })
const memoryBusy = ref(false)

const VERDICT_LABEL = { accept: '验收通过', correct: '纠正判级', reject: '驳回结果' }
const verdictLabel = (v) => VERDICT_LABEL[v] || '已复核'
const accuracyText = computed(() => {
  const value = memory.value.summary?.accuracy_on_judged
  return typeof value === 'number' ? `${Math.round(value * 100)}%` : '—'
})

async function loadMemory() {
  if (memoryBusy.value) return
  memoryBusy.value = true
  const next = { summary: null, candidates: [], cases: [], total: 0, error: '' }
  try {
    next.summary = await api.feedbackSummary()
  } catch (err) {
    next.error = err?.message || '反馈汇总不可用'
  }
  try {
    next.candidates = (await api.feedbackCandidates()).candidates || []
  } catch {
    /* 候选接口失败不影响主面板 */
  }
  try {
    const result = await api.memoryCases('', 4)
    next.cases = result.cases || []
    next.total = typeof result.total === 'number' ? result.total : next.cases.length
  } catch {
    /* 案例检索失败时保留空列表 */
  }
  memory.value = next
  memoryBusy.value = false
}

onMounted(loadMemory)
// 研判任务增减（跑完一单可能立即产生新反馈/验收）时刷新
watch(() => props.agentTasks.length, loadMemory)

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

  <!-- 经验记忆：人工评价与验收沉淀，区别于复核台（逐条待办）与档案管理（历史归档） -->
  <SectionHead
    eyebrow="EXPERIENCE MEMORY"
    title="经验记忆"
    note="人工反馈与验收沉淀的案例；只作检索参考，不会改写线上判级"
  >
    <button class="btn btn-ghost btn-sm" :disabled="memoryBusy" @click="loadMemory">
      {{ memoryBusy ? '读取中…' : '刷新记忆' }}
    </button>
  </SectionHead>
  <div class="panel"><div class="panel-body stack stack-4">
    <div class="mem-stats">
      <div class="mem-stat">
        <span>累计反馈</span>
        <b class="mono">{{ memory.summary?.total ?? 0 }}</b>
        <em v-if="!memory.summary?.total" class="faint">尚未提交评价</em>
      </div>
      <div class="mem-stat">
        <span>判定准确率</span>
        <b class="mono">{{ accuracyText }}</b>
        <em class="faint">仅统计「正确 / 有误」</em>
      </div>
      <div class="mem-stat">
        <span>已沉淀案例</span>
        <b class="mono">{{ memory.total }}</b>
        <em class="faint">人工验收后写入</em>
      </div>
      <div class="mem-stat">
        <span>校准候选</span>
        <b class="mono" :class="memory.candidates.length ? 'text-warn' : ''">{{ memory.candidates.length }}</b>
        <em class="faint">同类错误 ≥3 条</em>
      </div>
    </div>

    <div v-if="memory.cases.length" class="mem-list">
      <div v-for="c in memory.cases" :key="c.memory_id" class="mem-item">
        <div class="mem-main">
          <span class="mem-title">
            {{ c.work_type || '未标注作业' }}<template v-if="c.site_type"> · {{ c.site_type }}</template>


          </span>
          <span class="mem-meta">{{ fmtDateTime(c.created_at) }} · {{ c.task_id }}</span>
        </div>
        <div class="mem-tags">
          <span class="mem-shift mono">{{ c.agent_risk || '—' }} → {{ c.human_risk || c.agent_risk || '—' }}</span>
          <span class="mem-verdict" :class="`is-${c.verdict || 'reviewed'}`">{{ verdictLabel(c.verdict) }}</span>
        </div>
      </div>
    </div>
    <EmptyState
      v-else
      title="还没有沉淀的案例"
      hint="在安全日志结果区提交「结果评价」或「人工验收」后，案例会进入这里，后续同类作业可检索参考"
    />

    <p v-if="memory.candidates.length" class="mem-hint">
      有 <b>{{ memory.candidates.length }}</b> 组同类错误达到 3 条门槛，可进入离线校准
      <span class="faint">（learning_mode = offline_review_only，不会自动改判级）</span>
    </p>
  </div></div>

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

<style scoped>
.mem-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--sp-3);
}
.mem-stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--line);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.02);
}
.mem-stat > span { font-size: var(--fs-micro); letter-spacing: 0.1em; color: var(--text-dim); }
.mem-stat > b { font-size: 26px; font-weight: 700; line-height: 1.1; }
.mem-stat > em { font-size: var(--fs-micro); font-style: normal; }

.mem-list { display: flex; flex-direction: column; gap: 2px; }
.mem-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-3) var(--sp-2);
  border-bottom: 1px dashed var(--line);
}
.mem-item:last-child { border-bottom: 0; }
.mem-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.mem-title { font-size: var(--fs-small); font-weight: 600; color: var(--text); }
.mem-meta { font-size: var(--fs-micro); color: var(--text-dim); }
.mem-tags { display: flex; align-items: center; gap: var(--sp-2); flex: none; }
.mem-shift { font-size: var(--fs-micro); color: var(--text-dim); }
.mem-verdict {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: var(--fs-micro);
  border: 1px solid var(--line-strong);
  color: var(--text-dim);
}
.mem-verdict.is-accept { border-color: rgba(94, 210, 156, 0.45); color: var(--safe); }
.mem-verdict.is-correct { border-color: rgba(233, 181, 92, 0.45); color: var(--warn); }
.mem-verdict.is-reject { border-color: rgba(244, 115, 106, 0.45); color: var(--danger); }
.mem-hint { font-size: var(--fs-micro); color: var(--text-dim); }

@media (max-width: 900px) {
  .mem-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>

