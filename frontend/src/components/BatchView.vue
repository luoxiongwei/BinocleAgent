<script setup>
// 档案管理（原「在线日志」，2026-10-17 IA 重构）：
//   Tab 1 实时任务流 —— 在线 Agent 任务归档 → 安全日志 + 逐阶段证据链 + 事件流（原在线日志主体）
//   Tab 2 历史日志   —— 统一时间轴三栏：日历(220px) + 当日产物(320px) + 日志详情(弹性)
//     当日产物三类：验收批次（可下钻图对）/ 研判任务 / 待审复核（可跳复核台）
// 批次抽象为时间日期：批次按 summary.json mtime、任务按 graph_state mtime、待审按 created_at，
// 统一经 timeline.buildTimeline 按本地日期归并（与界面 fmtDateTime 同时区语义）。
import { computed, ref, watch } from 'vue'
import { api, dayKey, fmtDateTime, fmtDayHead, pairLabel } from '../api.js'
import { buildTimeline } from '../timeline.js'
import EvidenceChain from './EvidenceChain.vue'
import SafetyLog from './SafetyLog.vue'
import DispositionTag from './DispositionTag.vue'
import EmptyState from './EmptyState.vue'
import CalendarPicker from './CalendarPicker.vue'
import { IconLayers, IconReview } from './icons.js'

const props = defineProps({
  agentTasks: { type: Array, default: () => [] },
  batches: { type: Array, default: () => [] },
  overview: { type: Object, default: () => ({}) }, // /overview：runs 含 rows + metrics
  pending: { type: Array, default: () => [] }, // 待审条目（日历圆点 + 历史日志第三类）
  focus: { type: Object, default: () => ({}) }, // 总览台跳转携带 { tab, run, task, date }
})

const latestBatch = computed(() => props.batches[0] || null)

const emit = defineEmits(['notify', 'goto-review'])

// ---------- Tab ----------
const tab = ref('live')

// ---------- 统一时间轴 ----------
const days = computed(() =>
  buildTimeline({
    runs: props.overview.runs || [],
    tasks: props.agentTasks,
    reviews: props.pending,
  }),
)
const nDays = computed(() => days.value.filter((d) => d.date).length)
const selectedDate = ref('')

const dayItems = computed(() =>
  days.value.find((d) => d.date === selectedDate.value) || { date: '', runs: [], tasks: [], reviews: [] },
)

// 当日选中项（四类互斥选择；日期切换后过期选择自动失效，回退当日第一项）
const selRun = ref('')
const selPair = ref('')
const selTask = ref('')
const selReview = ref('')

// 隐藏式日历（2026-10-18）：不做常驻左栏，挂在当日列表的日期头上——
// 需要挑日期时才点开，避免日历长期占据一栏把日志详情挤窄
const calOpen = ref(false)

const view = computed(() => {
  const day = dayItems.value
  if (selTask.value && day.tasks.some((t) => t.task_id === selTask.value)) {
    return { kind: 'task', id: selTask.value }
  }
  if (selRun.value) {
    const run = day.runs.find((r) => r.run === selRun.value)
    if (run) {
      if (selPair.value && (run.rows || []).some((r) => r.pair_id === selPair.value)) {
        return { kind: 'pair', run: selRun.value, pair: selPair.value }
      }
      return { kind: 'run', run: selRun.value }
    }
  }
  if (selReview.value && day.reviews.some((v) => v.task_id === selReview.value)) {
    return { kind: 'review', id: selReview.value }
  }
  const t = day.tasks[0]
  if (t) return { kind: 'task', id: t.task_id }
  const r = day.runs[0]
  if (r) return { kind: 'run', run: r.run }
  const v = day.reviews[0]
  if (v) return { kind: 'review', id: v.task_id }
  return { kind: 'none' }
})

const runOfView = computed(() =>
  view.value.kind === 'run' || view.value.kind === 'pair'
    ? (props.overview.runs || []).find((r) => r.run === view.value.run) || null
    : null,
)
const taskOfView = computed(() =>
  view.value.kind === 'review' ? props.pending.find((v) => v.task_id === view.value.id) || null : null,
)

function selectRun(run) {
  selTask.value = ''
  selReview.value = ''
  selRun.value = run.run
  selPair.value = ''
}
function selectTask(id) {
  selRun.value = ''
  selPair.value = ''
  selReview.value = ''
  selTask.value = id
}
function selectReview(id) {
  selRun.value = ''
  selPair.value = ''
  selTask.value = ''
  selReview.value = id
}
function selectPair(pair) {
  selPair.value = pair
}

// 取消选择日期时清空当日选中项（日历组件内已实现「再点一次取消」）
watch(selectedDate, (v) => {
  calOpen.value = false
  if (v) return
  selRun.value = ''
  selPair.value = ''
  selTask.value = ''
  selReview.value = ''
})

// 外部跳转（总览台批次面板 / KPI）：{ tab, run, task, date }
watch(
  () => props.focus,
  (f) => {
    if (!f) return
    if (f.tab) tab.value = f.tab
    if (f.tab !== 'history') return
    if (f.run) {
      const run = (props.overview.runs || []).find((r) => r.run === f.run)
      if (run) selectedDate.value = dayKey(run.updated) || selectedDate.value
      selTask.value = ''
      selReview.value = ''
      selRun.value = f.run
      selPair.value = ''
    }
    if (f.task) {
      const task = props.agentTasks.find((t) => t.task_id === f.task)
      if (task) selectedDate.value = dayKey(task.updated) || selectedDate.value
      selectTask(f.task)
    }
    if (f.date) selectedDate.value = f.date
  },
  { immediate: true, deep: true },
)

// 无选中日期时默认落「最近有数据的一天」，历史日志开箱即用
watch(
  days,
  (list) => {
    if (!selectedDate.value) selectedDate.value = (list.find((d) => d.date) || {}).date || ''
  },
  { immediate: true },
)

// ---------- 任务详情（实时任务流与历史日志共用） ----------
const onlineSelected = ref('')
const onlineDetail = ref(null)
const onlineEvents = ref([])
const loadingOnline = ref(false)

// 在线任务有些旧快照只有 events，没有 tool_calls；统一成 EvidenceChain
// 需要的轨迹字段，保证安全日志详情始终能展示逐阶段日志。
const onlineEvidenceDetail = computed(() => {
  if (!onlineDetail.value) return null
  const calls = onlineDetail.value.tool_calls?.length
    ? onlineDetail.value.tool_calls
    : onlineEvents.value
        .filter((e) => e.tool || e.event)
        .map((e) => ({
          tool: e.tool || e.event,
          status: e.status || 'recorded',
          error: e.error || null,
        }))
  return { ...onlineDetail.value, tool_calls: calls }
})

async function loadOnlineTask(taskId) {
  onlineSelected.value = taskId
  loadingOnline.value = true
  try {
    const [snapshot, stream] = await Promise.all([api.task(taskId), api.taskEvents(taskId)])
    onlineDetail.value = snapshot
    onlineEvents.value = stream.events || []
  } catch (err) {
    emit('notify', `在线日志读取失败：${err.message}`, 'err')
  } finally {
    loadingOnline.value = false
  }
}

// 历史日志选中任务时自动装载详情（与实时任务流共用一套详情状态）
watch(
  view,
  (v) => {
    if (v.kind === 'task' && v.id && onlineSelected.value !== v.id) loadOnlineTask(v.id)
  },
  { immediate: true },
)

// ---------- 图对详情（历史日志 · 批次下钻） ----------
const pairDetail = ref(null)
const pairLoading = ref(false)
const pairError = ref('')

watch(
  () => (view.value.kind === 'pair' ? `${view.value.run}/${view.value.pair}` : ''),
  (key) => {
    if (!key) return
    const [run, pair] = key.split('/')
    pairLoading.value = true
    pairError.value = ''
    pairDetail.value = null
    api
      .pairDetail(run, pair)
      .then((d) => {
        pairDetail.value = d
      })
      .catch((err) => {
        pairError.value = err.message
      })
      .finally(() => {
        pairLoading.value = false
      })
  },
)

// ---------- 验收报告（历史日志 · 批次） ----------
const runReport = ref(null) // { run, markdown }
async function openRunReport(run) {
  if (runReport.value?.run === run) {
    runReport.value = null
    return
  }
  runReport.value = { run, markdown: '' }
  try {
    const r = await api.runReport(run)
    runReport.value = { run, markdown: r.markdown || '（报告为空）' }
  } catch (err) {
    runReport.value = { run, markdown: `读取失败：${err.message}` }
  }
}
</script>

<template>
  <div v-if="latestBatch" class="panel latest-batch-summary">
    <div class="panel-head"><IconLayers /><span class="panel-title">最新批次 · {{ latestBatch.batch_id }}</span><span class="grow" /><span class="faint text-small">{{ latestBatch.total }} 个任务</span></div>
    <div class="panel-body batch-summary-row">
      <span>已完成 {{ latestBatch.counts?.completed || 0 }}</span>
      <span>执行中 {{ latestBatch.counts?.running || 0 }}</span>
      <span>失败 {{ latestBatch.counts?.failed || 0 }}</span>
    </div>
  </div>
  <div class="archive-tabs" role="tablist">
    <button :class="['archive-tab', { on: tab === 'live' }]" role="tab" :aria-selected="tab === 'live'" @click="tab = 'live'">
      实时任务流 <span class="tab-count">{{ agentTasks.length }} 条</span>
    </button>
    <button :class="['archive-tab', { on: tab === 'history' }]" role="tab" :aria-selected="tab === 'history'" @click="tab = 'history'">
      历史日志 <span class="tab-count">{{ nDays }} 日</span>
    </button>
    <span class="grow" />
    <span class="faint text-small">验收批次按日期归档 · 点击左侧日期头打开日历</span>
  </div>

  <!-- ============ Tab 1：实时任务流（原在线日志） ============ -->
  <div v-if="tab === 'live'" class="online-archive-grid">
    <div v-if="agentTasks.length" class="panel agent-task-archive">
      <div class="panel-head"><IconLayers /><span class="panel-title">在线任务</span><span class="grow" /><span class="faint text-small">{{ agentTasks.length }} 条</span></div>
      <div class="panel-body flush">
        <button v-for="task in agentTasks.slice(0, 12)" :key="task.task_id" class="data-row agent-task-row" @click="loadOnlineTask(task.task_id)">
          <span class="grow"><span class="row-id" style="display:block">{{ task.task_id }}</span><span class="row-sub" style="display:block">{{ pairLabel(task.pair_id) }} · {{ task.updated ? fmtDateTime(task.updated) : '—' }}</span></span>
          <span class="row-meta"><span class="tag-mono">{{ task.overall_risk || '未判定' }}</span><DispositionTag :code="task.disposition" /></span>
        </button>
      </div>
    </div>
    <div v-if="onlineSelected" class="panel online-task-detail">
      <div class="panel-head"><span class="panel-title">安全日志 · {{ onlineSelected }}</span><span class="grow" /><span v-if="loadingOnline" class="faint text-small">读取中…</span><button class="btn btn-secondary btn-sm" @click="onlineSelected = ''; onlineDetail = null; onlineEvents = []">返回列表</button></div>
      <div v-if="onlineDetail" class="panel-body stack stack-3">
        <SafetyLog :detail="onlineDetail" />
        <div class="panel online-evidence-panel">
          <div class="panel-head"><span class="panel-title">研判日志 · 逐阶段证据链</span></div>
          <div class="panel-body"><EvidenceChain :detail="onlineEvidenceDetail" /></div>
        </div>
        <div class="online-event-list"><b>执行事件流 · {{ onlineEvents.length }} 个事件</b><div v-for="(e, i) in onlineEvents" :key="i" class="trace-row"><span class="trace-time">{{ fmtDateTime(e.time) }}</span><span class="trace-kind">{{ e.event }}</span><span v-if="e.tool" class="tag-mono">{{ e.tool }}</span><span v-if="e.status" class="faint">{{ e.status }}</span></div></div>
        <details class="json-fold"><summary>安全日志原始 JSON</summary><pre>{{ JSON.stringify(onlineDetail, null, 2) }}</pre></details>
      </div>
    </div>
  </div>

  <!-- ============ Tab 2：历史日志（两栏：当日产物 + 日志详情，排列同在线文档页） ============ -->
  <div v-else class="history-grid">
    <!-- 左：当日产物（日期头 = 隐藏式日历入口） -->
    <div class="panel history-list">
      <div class="panel-head day-head">
        <div class="day-head-wrap">
          <button
            class="day-head-btn"
            :class="{ on: calOpen }"
            title="点击打开日历选择日期"
            @click="calOpen = !calOpen"
            @keydown.esc="calOpen = false"
          >
            <span class="panel-title">{{ selectedDate ? fmtDayHead(selectedDate) : '选择日期' }}</span>
            <span class="day-count">{{ dayItems.runs.length + dayItems.tasks.length + dayItems.reviews.length }} 项</span>
            <span class="day-caret">{{ calOpen ? '▴' : '▾' }}</span>
          </button>
          <div v-if="calOpen" class="cal-drop">
            <CalendarPicker :days="days" v-model="selectedDate" />
          </div>
        </div>
      </div>
      <div v-if="!selectedDate" class="panel-body">
        <EmptyState title="从日历选择一天" hint="点击上方日期头打开日历；圆点标注当天有归档产物——橙色为验收批次、黄色为研判任务、蓝色为待审复核。" />
      </div>
      <div v-else-if="!dayItems.runs.length && !dayItems.tasks.length && !dayItems.reviews.length" class="panel-body">
        <EmptyState title="当日无归档" hint="这一天没有任何验收批次、研判任务或待审条目。" />
      </div>
      <div v-else class="panel-body flush history-list-body">
        <div v-if="dayItems.runs.length" class="day-sec">
          <div class="day-sec-head"><IconLayers /><span>验收批次</span><b class="mono">{{ dayItems.runs.length }}</b></div>
          <button
            v-for="r in dayItems.runs"
            :key="r.run"
            :class="['data-row', { selected: selRun === r.run }]"
            style="grid-template-columns: minmax(0, 1fr) auto"
            @click="selectRun(r)"
          >
            <span class="grow">
              <span class="row-id" style="display: block">{{ r.run }}</span>
              <span class="row-sub" style="display: block">{{ r.metrics?.n ?? 0 }} 对 · 更新于 {{ r.updated ? fmtDateTime(r.updated) : '—' }}</span>
            </span>
            <span class="row-meta"><span class="tag-mono">一致率 {{ Math.round((r.metrics?.exact_rate ?? 0) * 100) }}%</span></span>
          </button>
        </div>
        <div v-if="dayItems.tasks.length" class="day-sec">
          <div class="day-sec-head"><IconLayers /><span>研判任务</span><b class="mono">{{ dayItems.tasks.length }}</b></div>
          <button
            v-for="t in dayItems.tasks"
            :key="t.task_id"
            :class="['data-row', { selected: view.kind === 'task' && view.id === t.task_id }]"
            style="grid-template-columns: minmax(0, 1fr) auto"
            @click="selectTask(t.task_id)"
          >
            <span class="grow">
              <span class="row-id" style="display: block">{{ t.task_id }}</span>
              <span class="row-sub" style="display: block">{{ pairLabel(t.pair_id) }} · {{ t.updated ? fmtDateTime(t.updated) : '—' }}</span>
            </span>
            <span class="row-meta"><DispositionTag v-if="t.disposition" :code="t.disposition" /><span v-else class="tag-mono">{{ t.status || '进行中' }}</span></span>
          </button>
        </div>
        <div v-if="dayItems.reviews.length" class="day-sec">
          <div class="day-sec-head"><IconReview /><span>待审复核</span><b class="mono">{{ dayItems.reviews.length }}</b></div>
          <button
            v-for="v in dayItems.reviews"
            :key="v.task_id"
            :class="['data-row', { selected: view.kind === 'review' && view.id === v.task_id }]"
            style="grid-template-columns: minmax(0, 1fr) auto"
            @click="selectReview(v.task_id)"
          >
            <span class="grow">
              <span class="row-id" style="display: block">{{ v.task_id }}</span>
              <span class="row-sub" style="display: block">{{ v.failure_reason_code || v.failure_reason || '系统未能自动裁决' }}</span>
            </span>
            <span class="row-meta"><DispositionTag code="P3_manual_judge" /></span>
          </button>
        </div>
      </div>
    </div>

    <!-- 右：日志详情 -->
    <div class="history-detail">
      <!-- 批次 -->
      <template v-if="view.kind === 'run' && runOfView">
        <div class="panel">
          <div class="panel-head">
            <span class="panel-title">验收批次 · {{ runOfView.run }}</span>
            <span class="grow" />
            <button v-if="runOfView.has_report" class="btn btn-secondary btn-sm" @click="openRunReport(runOfView.run)">
              {{ runReport?.run === runOfView.run ? '收起报告' : '查看验收报告' }}
            </button>
          </div>
          <div class="panel-body stack stack-4">
            <div class="facts-grid">
              <div class="fact"><span>图对数</span><b>{{ runOfView.metrics?.n ?? 0 }}</b></div>
              <div class="fact"><span>一致率</span><b>{{ Math.round((runOfView.metrics?.exact_rate ?? 0) * 100) }}%</b></div>
              <div class="fact"><span>更新于</span><b>{{ runOfView.updated ? fmtDateTime(runOfView.updated) : '—' }}</b></div>
              <div class="fact"><span>验收报告</span><b>{{ runOfView.has_report ? '已生成' : '未生成' }}</b></div>
            </div>
            <div v-if="runReport?.run === runOfView.run" class="run-report">
              <pre>{{ runReport.markdown }}</pre>
            </div>
            <p class="field-hint" style="margin: 0">点击下图对可查看该图对的研判日志（安全日志 + 证据链）。</p>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head"><span class="panel-title">批次图对 · {{ runOfView.rows?.length ?? 0 }}</span></div>
          <div class="panel-body flush">
            <button
              v-for="row in runOfView.rows || []"
              :key="row.pair_id"
              :class="['data-row', { selected: view.kind === 'pair' && view.pair === row.pair_id }]"
              style="grid-template-columns: minmax(0, 1fr) auto auto auto"
              @click="selectPair(row.pair_id)"
            >
              <span class="row-id">{{ pairLabel(row.pair_id) }}</span>
              <DispositionTag v-if="row.disposition" :code="row.disposition" />
              <span v-else class="tag-mono">{{ row.status }}</span>
              <span class="tag-mono">{{ row.overall_risk || '未判定' }}</span>
              <span class="faint text-small">{{ typeof row.elapsed_s === 'number' ? `${row.elapsed_s}s` : '—' }}</span>
            </button>
          </div>
        </div>
      </template>

      <!-- 图对（批次下钻） -->
      <template v-else-if="view.kind === 'pair'">
        <div class="panel">
          <div class="panel-head">
            <button class="btn btn-ghost btn-sm" @click="selPair = ''">‹ 返回批次 {{ view.run }}</button>
            <span class="grow" />
            <span class="faint text-small">图对 {{ pairLabel(view.pair) }}</span>
          </div>
        </div>
        <div v-if="pairLoading" class="panel"><div class="panel-body stack stack-3"><span class="muted text-small">正在读取图对产物…</span><div class="run-bar" /></div></div>
        <div v-else-if="pairError" class="panel"><div class="panel-body"><div class="alert alert-error" style="margin: 0">{{ pairError }}</div></div></div>
        <template v-else-if="pairDetail">
          <SafetyLog :detail="pairDetail" />
          <div class="panel">
            <div class="panel-head"><span class="panel-title">研判日志 · 逐阶段证据链</span></div>
            <div class="panel-body"><EvidenceChain :detail="pairDetail" /></div>
          </div>
        </template>
      </template>

      <!-- 研判任务 -->
      <template v-else-if="view.kind === 'task'">
        <div class="panel">
          <div class="panel-head">
            <span class="panel-title">研判任务 · {{ view.id }}</span>
            <span class="grow" />
            <span v-if="loadingOnline" class="faint text-small">读取中…</span>
          </div>
        </div>
        <div v-if="onlineDetail" class="stack stack-3">
          <SafetyLog :detail="onlineDetail" />
          <div class="panel">
            <div class="panel-head"><span class="panel-title">研判日志 · 逐阶段证据链</span></div>
            <div class="panel-body"><EvidenceChain :detail="onlineEvidenceDetail" /></div>
          </div>
          <div class="online-event-list"><b>执行事件流 · {{ onlineEvents.length }} 个事件</b><div v-for="(e, i) in onlineEvents" :key="i" class="trace-row"><span class="trace-time">{{ fmtDateTime(e.time) }}</span><span class="trace-kind">{{ e.event }}</span><span v-if="e.tool" class="tag-mono">{{ e.tool }}</span><span v-if="e.status" class="faint">{{ e.status }}</span></div></div>
          <details class="json-fold"><summary>安全日志原始 JSON</summary><pre>{{ JSON.stringify(onlineDetail, null, 2) }}</pre></details>
        </div>
      </template>

      <!-- 待审复核（跳复核台） -->
      <template v-else-if="view.kind === 'review' && taskOfView">
        <div class="panel">
          <div class="panel-head"><IconReview /><span class="panel-title">待审条目 · {{ taskOfView.task_id }}</span></div>
          <div class="panel-body stack stack-4">
            <div class="facts-grid">
              <div class="fact"><span>图对</span><b>{{ pairLabel(taskOfView.pair_id) }}</b></div>
              <div class="fact"><span>入队</span><b>{{ taskOfView.created_at ? fmtDateTime(taskOfView.created_at) : '—' }}</b></div>
              <div class="fact"><span>类型</span><b>{{ taskOfView.kind === 'manual' ? '阻塞性人工介入' : '抽样复核' }}</b></div>
              <div class="fact"><span>状态</span><b>{{ taskOfView.resolved ? '已处理' : '待裁定' }}</b></div>
            </div>
            <div class="alert alert-info" style="margin: 0">
              转人工原因：{{ taskOfView.failure_reason || taskOfView.failure_reason_code || '系统未能自动裁决，需人工判断' }}
            </div>
            <button class="btn btn-primary" @click="emit('goto-review', taskOfView.task_id)">在人工复核台打开</button>
            <p class="field-hint" style="margin: 0">按二值口径，判不了即转人工、不输出风险等级；裁定回写后此处与档案同步更新。</p>
          </div>
        </div>
      </template>

      <div v-else class="panel">
        <EmptyState title="选择一项查看日志" hint="从左侧列表选择当日产物：验收批次可下钻到图对研判日志，研判任务展示完整安全日志，待审条目可跳转复核台。" />
      </div>
    </div>
  </div>
  <!-- 日历浮层打开时：点击外部关闭 -->
  <div v-if="calOpen" class="cal-backdrop" @click="calOpen = false" />
</template>

<style scoped>
.archive-tabs {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: var(--sp-4);
}
.archive-tab {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface);
  color: var(--text-dim);
  font-size: var(--fs-small);
  font-weight: 600;
  padding: 9px 14px;
  cursor: pointer;
  transition: border-color var(--dur) var(--ease-out), color var(--dur) var(--ease-out);
}
.archive-tab:hover { border-color: var(--line-strong); color: var(--text); }
.archive-tab.on { border-color: var(--accent); background: var(--accent-wash); color: var(--accent); }
.tab-count { font-family: var(--font-mono); font-size: 11.5px; font-weight: 400; opacity: 0.8; }

/* 两栏排列与在线文档页（online-archive-grid）保持一致：窄列表 + 宽日志详情 */
.history-grid {
  display: grid;
  grid-template-columns: minmax(250px, .3fr) minmax(0, 1fr);
  gap: var(--sp-4);
  align-items: start;
}
.history-list-body { max-height: calc(100vh - 260px); overflow-y: auto; }
.day-sec + .day-sec { border-top: 1px solid var(--line); }
.day-sec-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px var(--sp-4) 6px;
  font-size: var(--fs-small);
  font-weight: 700;
  letter-spacing: 0.05em;
  color: var(--text-dim);
  position: sticky;
  top: 0;
  background: var(--surface);
  z-index: 1;
}
.day-sec-head b { margin-left: auto; color: var(--text-faint); font-weight: 400; }
.history-detail { display: flex; flex-direction: column; gap: var(--sp-4); min-width: 0; }
.run-report { max-height: 320px; overflow-y: auto; border: 1px solid var(--line); border-radius: 8px; }
.run-report pre { margin: 0; padding: var(--sp-3); font-size: 12px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }

/* 日期头 = 隐藏式日历入口（渐进披露：需要挑日期才点开，不常驻占栏） */
.day-head { padding: 0; }
.day-head-wrap { position: relative; flex: 1; min-width: 0; }
.day-head-btn {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: var(--sp-3) var(--sp-4); border: 0; border-radius: 10px 10px 0 0;
  background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer;
}
.day-head-btn:hover { background: var(--surface-2); }
.day-head-btn .panel-title { color: var(--text); }
.day-count { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-faint); }
.day-caret { margin-left: auto; font-size: 11px; color: var(--text-faint); }
.day-head-btn.on .day-caret { color: var(--accent); }
.cal-drop {
  position: absolute; top: calc(100% + 8px); left: 0; z-index: 40;
  width: 300px; max-width: calc(100vw - 60px);
  filter: drop-shadow(0 12px 28px rgba(0, 0, 0, 0.45));
  animation: cal-in var(--dur) var(--ease-out);
}
@keyframes cal-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
.cal-backdrop { position: fixed; inset: 0; z-index: 30; }

@media (max-width: 900px) {
  .history-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
