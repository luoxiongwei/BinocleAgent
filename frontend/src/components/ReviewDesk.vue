<script setup>
// 人工复核工作台：左队列 + 右证据链 + 人工结论回写
import { computed, onMounted, ref, watch } from 'vue'
import { api, dayKey, fmtDateTime, fmtDayHead, pairLabel } from '../api.js'
import CalendarPicker from './CalendarPicker.vue'
import SectionHead from './SectionHead.vue'
import EmptyState from './EmptyState.vue'
import ImageTriptych from './ImageTriptych.vue'
import EvidenceChain from './EvidenceChain.vue'
import DispositionTag from './DispositionTag.vue'
import RiskTag from './RiskTag.vue'
import { IconReview, IconAlert } from './icons.js'

const props = defineProps({
  pending: { type: Array, default: () => [] },
  focusTaskId: { type: String, default: '' }, // 从任务页跳转时携带
})

const emit = defineEmits(['notify', 'refresh-queue'])

// 外部跳转入场：若目标任务在队列中则直接打开；不在队列（实时转人工任务只进归档台账）则按 ID 直查
watch(
  () => props.focusTaskId,
  (id) => {
    if (!id) return
    const hit = props.pending.find((p) => p.task_id === id)
    if (hit) openTask(hit)
    else openTaskById(id)
  },
)

const selectedId = ref('')
const task = ref(null)
const detailLoading = ref(false)
const detailError = ref('')
const images = ref({})
const submitting = ref(false)
const idQuery = ref('')

const verdict = ref({ verdict: '接受', risk_label: '低风险', notes: '' })

// —— 图像定位：先用 pair_id 在全部批次里反查（批次产物含双光图像）；
// 反查不到时兜底用在线研判任务自身的产物（graph_runs/l2/annotated，2026-10-19） ——
const runPairsCache = new Map()

async function locateImages(pairId, taskId) {
  if (!pairId) return {}
  try {
    const { runs } = await api.runs()
    for (const run of runs) {
      let pairs = runPairsCache.get(run.run)
      if (!pairs) {
        pairs = (await api.runPairs(run.run)).pairs || []
        runPairsCache.set(run.run, pairs)
      }
      if (pairs.includes(pairId)) {
        const base = `/api/runs/${encodeURIComponent(run.run)}/pairs/${encodeURIComponent(pairId)}/image`
        return { visible: `${base}/visible`, infrared: `${base}/infrared`, fused: `${base}/fused` }
      }
    }
  } catch {
    /* 图像定位失败不阻断复核 */
  }
  // 兜底：在线研判任务自身产物（端点只返回实际存在的图，缺哪个格哪个格留空）
  if (taskId) {
    try {
      const { images: own } = await api.graphRunImages(taskId)
      if (own && (own.fused || own.visible || own.infrared)) return own
    } catch {
      /* 非在线研判任务或产物缺失 */
    }
  }
  return {}
}

async function loadTaskDetail(id) {
  task.value = null
  images.value = {}
  detailError.value = ''
  detailLoading.value = true
  try {
    task.value = await api.task(id)
    images.value = await locateImages(task.value.pair_id, task.value.id || id)
  } catch (err) {
    detailError.value = err.message
  } finally {
    detailLoading.value = false
  }
}

// 从队列打开（pending 条目）
function openTask(item) {
  selectedId.value = item.task_id
  verdict.value = { verdict: '接受', risk_label: '低风险', notes: '' }
  loadTaskDetail(item.task_id)
}

// 按 ID 直接打开（实时转人工任务只进 human_archives 不进待审队列，需此入口）
async function openTaskById(id) {
  const key = (id || '').trim()
  if (!key) return
  selectedId.value = key
  idQuery.value = ''
  verdict.value = { verdict: '接受', risk_label: '低风险', notes: '' }
  await loadTaskDetail(key)
}

watch(
  () => props.pending,
  (list) => {
    if (!list.length) {
      selectedId.value = ''
      task.value = null
      return
    }
    if (!list.some((p) => p.task_id === selectedId.value)) {
      const first = newestItem.value || list[0]
      if (first) openTask(first)
    }
  },
)

onMounted(() => {
  const first = newestItem.value || props.pending[0]
  if (first) openTask(first)
})

const alreadyReviewed = computed(() => Boolean(task.value?.human_verdict))
const needLabel = computed(() => verdict.value.verdict === '修改' || task.value?.disposition === 'P3_manual_judge')

// 队列按本地日期分组（2026-10-17 用户要求：区分批次/时间，不再一锅端）。
// created_at 为 UTC ISO，经 dayKey 转本地日期——与界面 fmtDateTime 同时区语义。
// 无时间戳的早年遗留条目归入「时间未知」桶，排在最后而非丢弃。
const groups = computed(() => {
  const m = new Map()
  for (const item of props.pending) {
    const key = dayKey(item.created_at)
    if (!m.has(key)) m.set(key, [])
    m.get(key).push(item)
  }
  return [...m.entries()]
    .map(([key, items]) => ({ key, items }))
    .sort((a, b) => (b.key || ' ').localeCompare(a.key || ' '))
})
// 默认选中最新一组的第一项（队列文件按时间追加，直接取 [0] 会打开最旧任务）
const newestItem = computed(() => groups.value[0]?.items?.[0] || null)

// 隐藏式日历（2026-10-24，与档案管理同一套交互）：日期头点开月历挑日，
// 选中后队列只保留当天条目；当天没有待审任务时明确提示「该日无档案记录」。
const calOpen = ref(false)
const pickedDate = ref('')

// CalendarPicker 需要 buildTimeline 形状；复核台只喂 reviews 一类
const days = computed(() =>
  groups.value.map((g) => ({ date: g.key, runs: [], tasks: [], reviews: g.items })),
)
const visibleGroups = computed(() =>
  pickedDate.value ? groups.value.filter((g) => g.key === pickedDate.value) : groups.value,
)
const pickedCount = computed(() => visibleGroups.value.reduce((n, g) => n + g.items.length, 0))

watch(pickedDate, () => {
  calOpen.value = false
})

async function submit() {
  if (!task.value?.task_id) return
  submitting.value = true
  try {
    task.value = await api.submitReview(task.value.task_id, verdict.value)
    emit('notify', `人工结论已回写：${verdict.value.verdict}${verdict.value.risk_label ? ' · ' + verdict.value.risk_label : ''}`, 'ok')
    emit('refresh-queue')
  } catch (err) {
    emit('notify', `回写失败：${err.message}`, 'err')
  } finally {
    submitting.value = false
  }
}

const facts = computed(() => {
  const t = task.value
  if (!t) return []
  return [
    ['处置', t.disposition || '—'],
    ['风险', t.overall_risk || '未判定'],
    ['路由', t.route || '—'],
    ['工具调用', `${t.tool_calls?.length || 0} 次`],
    ['告警条目', `${t.n_warnings ?? 0} 条`],
    ['执行步骤', `${t.steps?.length || 0} 步`],
  ]
})
</script>

<template>
  <SectionHead
    eyebrow="HUMAN REVIEW DESK"
    title="人工复核队列"
  />

  <div class="review-desk">
    <!-- 左：待审队列 -->
    <div class="panel" :class="{ 'is-overlay': calOpen }">
      <div class="panel-head">
        <IconReview />
        <span class="panel-title">待审队列 · {{ pending.length }}</span>
        <span class="grow" />
        <div class="day-head-wrap">
          <button
            class="day-head-btn"
            :class="{ on: calOpen }"
            title="点击打开日历，按日期查看待审任务"
            @click="calOpen = !calOpen"
            @keydown.esc="calOpen = false"
          >
            <span class="day-label">{{ pickedDate ? fmtDayHead(pickedDate) : '全部日期' }}</span>
            <span class="day-count">{{ pickedCount }} 项</span>
            <span class="day-caret">{{ calOpen ? '▴' : '▾' }}</span>
          </button>
          <div v-if="calOpen" class="cal-drop">
            <CalendarPicker :days="days" v-model="pickedDate" />
          </div>
        </div>
      </div>
      <div class="panel-body" style="padding: 10px var(--sp-4)">
        <div class="row" style="gap: 8px">
          <input
            v-model="idQuery"
            class="input"
            placeholder="按任务 ID 打开（含归档任务）"
            @keyup.enter="openTaskById(idQuery)"
          />
          <button class="btn btn-secondary btn-sm" :disabled="!idQuery.trim()" @click="openTaskById(idQuery)">打开</button>
        </div>
      </div>
      <div class="queue-list">
        <template v-for="g in visibleGroups" :key="g.key">
          <div class="queue-group-head">
            <span>{{ fmtDayHead(g.key) }}</span>
            <span class="grow" />
            <span class="mono">{{ g.items.length }} 项</span>
          </div>
          <button
            v-for="item in g.items"
            :key="item.task_id"
            :class="['queue-row', { selected: selectedId === item.task_id }]"
            @click="openTask(item)"
          >
            <div class="queue-row-top">
              <span class="row-id">{{ item.task_id }}</span>
              <span class="grow" />
              <DispositionTag :code="item.disposition" />
            </div>
            <div class="queue-row-sub">
              {{ item.failure_reason || item.reason || '系统未能自动裁决，需人工判断' }}
            </div>
            <div class="queue-row-foot">
              <span v-if="item.failure_reason_code" class="tag-mono text-warn">{{ item.failure_reason_code }}</span>
              <span class="tag-mono">{{ pairLabel(item.pair_id) }}</span>
              <span class="grow" />
              <span class="faint" style="font-size: 12px">{{ fmtDateTime(item.created_at) }}</span>
            </div>
          </button>
        </template>
        <EmptyState
          v-if="pickedDate && !pickedCount"
          :title="'该日无档案记录'"
          :hint="`${fmtDayHead(pickedDate)} 没有转人工任务归档。可在日历上换一天，或再点一次该日期清除筛选。`"
        >
          <button class="btn btn-ghost btn-sm" style="margin-top: 8px" @click="pickedDate = ''">
            查看全部日期
          </button>
        </EmptyState>
        <EmptyState
          v-else-if="!pending.length"
          title="队列已清空"
          hint="所有转人工任务均已完成裁定。系统按二值口径运行：无法自动定级的任务一律推人工，不会蒙混归档。"
        />
      </div>
    </div>

    <!-- 右：证据与裁定 -->
    <div class="detail-stack">
      <div v-if="!selectedId" class="panel">
        <EmptyState
          title="从左侧选择一项开始复核"
          hint="这里展示该任务的完整证据链：双光配准、开放检测、双源对账、设备校验、规则命中、规范条款与判级报告。"
        >
          <template #icon><IconReview :size="28" /></template>
        </EmptyState>
      </div>

      <template v-else-if="task">
        <!-- 任务头 -->
        <div class="panel"><div class="panel-body stack stack-4">
          <div class="detail-head">
            <div class="grow">
              <span class="eyebrow">TASK DOSSIER</span>
              <h3>{{ task.task_id }}</h3>
              <div class="row row-wrap" style="gap: 6px; margin-top: 6px">
                <DispositionTag :code="task.disposition" />
                <span v-if="task.overall_risk" class="pill pill-muted">风险 <RiskTag :value="task.overall_risk" /></span>
                <span v-for="r in task.review_reasons || []" :key="r" class="tag-mono text-warn">{{ r }}</span>
              </div>
            </div>
            <div class="stack stack-2" style="text-align: right">
              <span class="faint text-small">图对 {{ pairLabel(task.pair_id) }}</span>
              <span class="faint text-small">状态 {{ task.status }}</span>
            </div>
          </div>

          <div v-if="task.error" class="alert alert-error">
            <IconAlert />{{ task.error }}
          </div>

          <div class="facts-grid">
            <div v-for="[k, v] in facts" :key="k" class="fact"><span>{{ k }}</span><b>{{ v }}</b></div>
          </div>
        </div></div>

        <!-- 图像（始终展示：复核需要图像；无产物时如实说明原因） -->
        <div class="panel">
          <div class="panel-head"><span class="panel-title">现场图像 · 双光三联</span></div>
          <div class="panel-body">
            <ImageTriptych :images="images" />
            <p v-if="!images.fused && !images.visible && !images.infrared" class="field-hint" style="margin: var(--sp-3) 0 0">
              未找到该图对（{{ pairLabel(task.pair_id) }}）的图像产物——
              通常是批跑在生成图像前即失败（如配准重合度低于硬阈值，见证据链 01 节点），
              或该 pair_id 并非真实图对编号（无任何批次收录）。
            </p>
          </div>
        </div>

        <!-- 证据链 -->
        <div class="panel">
          <div class="panel-head">
            <span class="panel-title">证据链 · 从配准到条款</span>
          </div>
          <div class="panel-body"><EvidenceChain :detail="task" /></div>
        </div>

        <!-- 人工结论 -->
        <div class="panel">
          <div class="panel-head">
            <span class="panel-title">人工结论</span>
            <span class="grow" />
            <span v-if="alreadyReviewed" class="pill pill-safe"><span class="dot" />已回写</span>
          </div>
          <div class="panel-body stack stack-4">
            <div v-if="alreadyReviewed" class="alert alert-success">
              <IconReview />
              已裁定：{{ task.human_verdict.verdict }} · {{ task.human_verdict.risk_label || '未改判' }}
              <span v-if="task.human_verdict.notes" class="faint">—— {{ task.human_verdict.notes }}</span>
            </div>

            <div v-else-if="!task.need_human_review" class="alert alert-info">
              <IconReview />
              该任务为自动通道终态（{{ task.disposition || '—' }}），系统已自行闭环，无需人工回写。
            </div>

            <template v-else>
              <div class="verdict-cards">
                <button
                  v-for="v in [
                    { key: '接受', desc: '系统结论成立' },
                    { key: '修改', desc: '改判风险等级' },
                    { key: '驳回', desc: '结论不成立' },
                  ]"
                  :key="v.key"
                  :class="['verdict-card', { on: verdict.verdict === v.key }]"
                  :data-verdict="v.key"
                  @click="verdict.verdict = v.key"
                >
                  {{ v.key }}
                  <small>{{ v.desc }}</small>
                </button>
              </div>

              <div class="grid grid-2">
                <label class="field">
                  <span class="field-label">风险等级{{ needLabel ? '（必填）' : '' }}</span>
                  <select v-model="verdict.risk_label" class="select">
                    <option>安全</option>
                    <option>低风险</option>
                    <option>高风险</option>
                  </select>
                </label>
                <label class="field">
                  <span class="field-label">备注（可选）</span>
                  <input v-model="verdict.notes" class="input" placeholder="裁定依据，留痕于审计记录" />
                </label>
              </div>

              <button class="btn btn-primary" :disabled="submitting" @click="submit">
                {{ submitting ? '回写中…' : '提交人工结论' }}
              </button>
              <p class="field-hint">回写只修正结论与处置，不改变「任务已完成」的事实；同一任务不可重复回写。</p>
            </template>
          </div>
        </div>

        <!-- 原始快照 -->
        <details class="panel json-fold">
          <summary>原始任务快照 JSON</summary>
          <pre>{{ JSON.stringify(task, null, 2) }}</pre>
        </details>
      </template>

      <div v-else-if="detailLoading" class="panel"><div class="panel-body stack stack-3">
        <span class="muted text-small">正在读取任务状态与磁盘产物…</span>
        <div class="run-bar" />
      </div></div>

      <div v-else-if="detailError" class="panel"><div class="panel-body">
        <div class="alert alert-error" style="margin: 0"><IconAlert />{{ detailError }}</div>
      </div></div>
    </div>
  </div>
  <div v-if="calOpen" class="cal-backdrop" @click="calOpen = false" />
</template>

<style scoped>
/* 日期头 = 隐藏式日历入口（与档案管理同一套交互，2026-10-24） */
.day-head-wrap { position: relative; flex: none; }
.day-head-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface-2);
  color: var(--text-dim);
  font: inherit;
  font-size: var(--fs-small);
  cursor: pointer;
  transition: border-color var(--dur) var(--ease-out), color var(--dur) var(--ease-out);
}
.day-head-btn:hover { border-color: var(--line-strong); color: var(--text); }
.day-head-btn.on { border-color: var(--accent); color: var(--accent); }
.day-label { font-weight: 600; }
.day-count { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-faint); }
.day-caret { font-size: 11px; color: var(--text-faint); }
.day-head-btn.on .day-caret { color: var(--accent); }

.cal-drop {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  z-index: 40;
  width: 300px;
  max-width: calc(100vw - 60px);
  filter: drop-shadow(0 12px 28px rgba(0, 0, 0, 0.45));
  animation: cal-in var(--dur) var(--ease-out);
}
@keyframes cal-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
.cal-backdrop { position: fixed; inset: 0; z-index: 30; }
</style>

