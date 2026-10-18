<script setup>
// 运行任务：上传图对（支持中文命名）→ 启动研判 → 实时时间线 → 结果证据链
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, fmtTime, toolCn, pairLabel, pairInputs, loadInputs, canonicalPairId } from '../api.js'
import SectionHead from './SectionHead.vue'
import EvidenceChain from './EvidenceChain.vue'
import SafetyLog from './SafetyLog.vue'
import DispositionTag from './DispositionTag.vue'
import RiskTag from './RiskTag.vue'
import { IconPlay, IconUpload, IconSearch, IconAlert, IconReview, IconCheck } from './icons.js'

const emit = defineEmits(['notify', 'goto-review'])

const pairId = ref('pair04')
const pairName = ref('')
const customId = ref('')
const upload = ref({ rgb: null, ir: null })
const uploadResult = ref(null)
const polling = ref(false) // 正在持续观测某个运行中的任务
const result = ref(null)
const events = ref([])
const queryId = ref('')
const error = ref('')
const uploadNext = ref(null)
const resultAnchor = ref(null)
const pipelineStarted = ref(false)
let pollTimer = null
let pollTaskId = ''

const isTerminal = (snap) => ['completed', 'failed'].includes(snap?.status)

// 终态后补取最终快照：checkpointer 与磁盘落盘有极短窗口，失败态也可能先于
// 完整证据落盘可见。最多补取 5 次，直到 disposition 出现或快照不再增长，
// 避免页面把 fuse_qc 中间态当最终日志钉住（2026-10-16 实测复现）。
async function finalizeSnapshot(id) {
  for (let k = 0; k < 5; k += 1) {
    await new Promise((r) => setTimeout(r, 300))
    try {
      const again = await api.task(id)
      if ((again.tool_calls?.length || 0) >= (result.value?.tool_calls?.length || 0)) {
        result.value = again
      }
      if (result.value?.disposition || result.value?.status === 'failed') break
    } catch (err) {
      /* 补取失败下一轮重试 */
    }
  }
}

// 单轮观测：刷新快照 + 事件流。任何异常都在这里吞掉——后端运行中与读侧共享
// sqlite 连接，get_state 偶发锁冲突返回 5xx 属瞬时抖动，绝不能因此终止观测
// 循环（2026-10-17 用户实测：一抛循环即死，页面冻结到任务结束或手动刷新）。
async function pollTick() {
  if (!pollTaskId) return
  try {
    const snap = await api.task(pollTaskId)
    result.value = snap
    await loadEvents(pollTaskId)
    if (isTerminal(snap)) {
      await finalizeSnapshot(pollTaskId)
      stopPolling()
      resultAnchor.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      if (result.value?.status === 'completed') emit('notify', `任务 ${pollTaskId} 已完成`, 'ok')
      else if (result.value?.status === 'failed') emit('notify', `任务 ${pollTaskId} 执行失败`, 'err')
    }
  } catch (err) {
    /* 瞬时失败静默，下一轮重试 */
  }
}

function startPolling(id) {
  pollTaskId = id
  queryId.value = id
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = setInterval(pollTick, 1000)
  polling.value = true
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  pollTaskId = ''
  polling.value = false
}

// 后台标签页定时器会被浏览器降频冻结，恢复可见时立即补拉一次，
// 避免用户切回标签看到的还是几分钟前的陈旧帧（2026-10-16 实测复现）。
function onVisibility() {
  if (document.visibilityState === 'visible' && polling.value) {
    pollTick()
  }
}

onMounted(() => document.addEventListener('visibilitychange', onVisibility))
onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onVisibility)
  stopPolling()
})

// 图对命名管理
const renameTarget = ref('')
const renameValue = ref('')
const renaming = ref(false)
const showRename = ref(false)

// 重命名输入框的自动补全：只列已命名的图对
const pairOptions = computed(() => pairInputs.value.filter((p) => p.display))

// 图对变化时清理残留的自动生成任务 ID（graph-前缀），保留用户手输的自定义 ID，
// 避免「换了图对但任务 ID 还是上一个」导致的撞车
watch(pairId, () => {
  if (customId.value.startsWith('graph-')) customId.value = ''
})

const toolEvents = computed(() =>
  events.value.filter((e) => e.event !== 'task_queued'),
)

// 待机态（2026-10-18 用户要求）：未进入流水线、无结果、无观测任务时，上传表单是本页
// 唯一主角——垂直居中于页面中心，避免顶部孤岛 + 下方大片留白的不和谐感；一旦
// pipelineStarted / result 出现，舞台回顶锚定，把面积让给事件流与研判结果。
const idle = computed(() => !pipelineStarted.value && !result.value && !polling.value)

// 工作态 → 待机态（2026-10-18 用户要求「后转前」）：上传区标题栏的「发起新任务」。
// 只清前端工作区状态——后台任务不受影响（继续执行并归档，仅停止前端轮询观测）；
// 表单清空文件选择与显示名（防止旧文件名配新 ID 的错配上传），保留图对 ID 便于同图对重跑。
async function resetToIdle() {
  const abandoned = pollTaskId
  stopPolling()
  pipelineStarted.value = false
  result.value = null
  events.value = []
  error.value = ''
  uploadResult.value = null
  queryId.value = ''
  customId.value = ''
  pairName.value = ''
  upload.value = { rgb: null, ir: null }
  showRename.value = false
  await nextTick()
  window.scrollTo({ top: 0, behavior: 'smooth' })
  // 只有「放弃了仍在跑的任务」这一不可见后果需要明确告知
  if (abandoned) {
    emit('notify', `已回到待机：任务 ${abandoned} 仍在后台执行，完成后可在档案管理查看`, 'info')
  }
}

async function uploadPair() {
  if (!upload.value.rgb || !upload.value.ir || !pairId.value.trim()) return
  try {
    uploadResult.value = await api.uploadPair({
      pairId: pairId.value.trim(),
      displayName: pairName.value.trim(),
      rgb: upload.value.rgb,
      ir: upload.value.ir,
    })
    emit('notify', `已保存为 ${uploadResult.value.pair_id}${uploadResult.value.display ? `（${uploadResult.value.display}）` : ''}`, 'ok')
    await loadInputs()
    // 中文只作为显示名，后续任务始终使用规范 ID。
  } catch (err) {
    emit('notify', `上传失败：${err.message}`, 'err')
  }
}

async function goToPipeline() {
  if (!uploadResult.value) return
  pipelineStarted.value = true
  result.value = null
  events.value = []
  error.value = ''
  queryId.value = ''
  await nextTick()
  uploadNext.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

async function submitRename() {
  const target = renameTarget.value.trim()
  const name = renameValue.value.trim()
  if (!target || !name) return
  renaming.value = true
  try {
    const r = await api.renamePair(target, name)
    emit('notify', `${r.pair_id} 已命名为「${r.display}」`, 'ok')
    renameTarget.value = ''
    renameValue.value = ''
    await loadInputs()
  } catch (err) {
    emit('notify', `重命名失败：${err.message}`, 'err')
  } finally {
    renaming.value = false
  }
}

async function loadEvents(id) {
  try {
    events.value = (await api.taskEvents(id)).events || []
  } catch (err) {
    if (!String(err.message).includes('404')) emit('notify', err.message, 'err')
  }
}

async function createTask() {
  if (polling.value) return
  error.value = ''
  result.value = null
  events.value = []
  pipelineStarted.value = true
  try {
    const canonical = canonicalPairId(pairId.value)
    const queued = await api.createTask({
      pair_id: canonical,
      task_id: customId.value.trim() || undefined,
    })
    customId.value = queued.task_id
    emit('notify', `任务 ${queued.task_id} 已排队（图对 ${pairLabel(queued.pair_id)}）`, 'ok')
    // 排队即开始持续观测：快照与事件流每秒自动刷新，直到终态
    startPolling(queued.task_id)
    await loadEvents(queued.task_id)
  } catch (err) {
    error.value = err.message
    emit('notify', `任务失败：${err.message}`, 'err')
  }
}

async function queryTask() {
  if (!queryId.value.trim()) return
  try {
    const snap = await api.task(queryId.value.trim())
    result.value = snap
    await loadEvents(queryId.value.trim())
    error.value = ''
    // 运行中的任务（手动查询 / 监督台跳转进入）同样持续观测，
    // 否则页面钉死在打开那一刻的快照，只能靠手动刷新（2026-10-17 用户实测）。
    if (!isTerminal(snap)) {
      startPolling(snap.task_id || queryId.value.trim())
    } else {
      stopPolling()
    }
  } catch (err) {
    error.value = err.message
    emit('notify', err.message, 'err')
  }
}

// 命令式加载指定任务（总览台/档案台下钻入口）。
// 2026-10-18：原实现是 props.initialTaskId + watcher，但 TaskView 在 KeepAlive 下
// 停用期间 props 冻结、激活时 immediate watcher 不补跑——「后转前→再前转同一任务」
// 时 watcher 不触发、页面静默留在待机态（无头探针实测两次下钻零 [WATCH] 日志）。
// 故改为 App 持模板 ref 直接调用本函数，彻底绕开 prop 响应式。
function loadTask(id) {
  if (!id) return
  pipelineStarted.value = true
  queryId.value = id
  queryTask()
}
defineExpose({ loadTask })
</script>

<template>
  <div class="task-stage" :class="{ 'is-idle': idle }">
  <div class="task-workspace">
  <!-- 上传 -->
  <SectionHead eyebrow="INPUT UPLOAD" title="上传双光图对" note="支持中文命名：填名称即自动登记，重复上传为替换">
    <!-- 后转前（2026-10-18）：工作态时才出现，一键收起流水线/结果回到居中待机舞台 -->
    <button v-if="!idle" class="btn btn-ghost btn-sm" title="结束当前工作区，回到待机状态发起新任务" @click="resetToIdle">
      <IconPlay />发起新任务
    </button>
  </SectionHead>
  <div class="panel"><div class="panel-body grid grid-3" style="align-items: end">
    <label class="field">
      <span class="field-label">图对 ID / 中文名</span>
      <input v-model="pairId" class="input" placeholder="pair04 或 智感筑安" />
    </label>
    <label class="field">
      <span class="field-label">显示名（选填，可随时重命名）</span>
      <input v-model="pairName" class="input" placeholder="智感筑安·东侧作业面" />
    </label>
    <div class="grid" style="grid-template-columns: 1fr 1fr; gap: var(--sp-2)">
      <label class="file-btn">
        <IconUpload />
        <span class="file-name">{{ upload.rgb ? upload.rgb.name : 'RGB 图像' }}</span>
        <input type="file" accept="image/*" @change="upload.rgb = $event.target.files[0]" />
      </label>
      <label class="file-btn">
        <IconUpload />
        <span class="file-name">{{ upload.ir ? upload.ir.name : '红外图像' }}</span>
        <input type="file" accept="image/*" @change="upload.ir = $event.target.files[0]" />
      </label>
    </div>
  </div><div class="panel-body" style="padding-top: 0">
    <button class="btn btn-secondary" :disabled="!upload.rgb || !upload.ir || !pairId.trim()" @click="uploadPair">上传图像对</button>
    <div v-if="uploadResult" class="alert alert-info" style="margin: var(--sp-3) 0 0">
      <IconCheck />
      已存为 <b class="mono">{{ uploadResult.pair_id }}</b>
      <span v-if="uploadResult.display">（{{ uploadResult.display }}）</span>
      <span v-if="uploadResult.origin" class="faint">
        · 原始文件 {{ uploadResult.origin.rgb }} / {{ uploadResult.origin.ir }}
      </span>
      <span v-if="uploadResult.replaced?.length" class="faint">· 已替换 {{ uploadResult.replaced.join('、') }}</span>
      <button class="btn btn-primary btn-sm" @click="goToPipeline">下一步：生成安全日志</button>
    </div>
  </div></div>

  <template v-if="pipelineStarted">
  <!-- 图对命名管理：默认收起，避免占据主工作区 -->
  <div class="task-mini-row">
    <div><span class="eyebrow">PAIR LIBRARY</span><b>图对命名</b><span class="faint text-small">规范 ID 不变，中文名可随时改</span></div>
    <button class="btn btn-secondary btn-sm" @click="showRename = !showRename">{{ showRename ? '收起' : '重命名图对' }}</button>
  </div>
  <div v-if="showRename" class="panel">
    <div class="panel-body row" style="align-items: end; gap: var(--sp-3)">
      <label class="field grow">
        <span class="field-label">图对（ID 或中文名）</span>
        <input v-model="renameTarget" class="input" placeholder="pair04 或 智感筑安" list="pair-library-options" />
        <datalist id="pair-library-options">
          <option v-for="p in pairOptions" :key="p.pair_id" :value="p.pair_id">{{ p.display || '' }}</option>
        </datalist>
      </label>
      <label class="field grow">
        <span class="field-label">新显示名</span>
        <input v-model="renameValue" class="input" placeholder="东侧塔吊基座" />
      </label>
      <button class="btn btn-primary" :disabled="renaming || !renameTarget.trim() || !renameValue.trim()" @click="submitRename">
        {{ renaming ? '保存中…' : '重命名' }}
      </button>
    </div>
  </div>

  <!-- 启动任务 -->
  <div ref="uploadNext" />
  <SectionHead eyebrow="LIVE PIPELINE" title="运行一个任务" note="图对 ID 或中文名均可 · 融合 → VLM → YOLO → 对账 → 规则 → 判级，约 1–3 分钟" />
  <div class="panel"><div class="panel-body grid grid-2" style="align-items: end">
    <label class="field">
      <span class="field-label">图对 ID / 中文名</span>
      <input v-model="pairId" class="input" placeholder="pair04 或 智感筑安" />
    </label>
    <label class="field">
      <span class="field-label">任务 ID（可选）</span>
      <input v-model="customId" class="input" placeholder="graph-pair04" />
    </label>
  </div><div class="panel-body" style="padding-top: 0">
    <button class="btn btn-primary" :disabled="polling || !pairId.trim()" @click="createTask">
      <IconPlay />{{ polling ? '执行中 · 实时刷新' : '启动实际任务' }}
    </button>
  </div>
    <div v-if="polling" class="panel-body flush"><div class="run-bar" /></div>
  </div>

  <!-- 查询 -->
  <div class="panel"><div class="panel-body row" style="align-items: end">
    <label class="field grow">
      <span class="field-label">查询已有任务</span>
      <input v-model="queryId" class="input" placeholder="输入 task_id，如 batch-pair16" />
    </label>
    <button class="btn btn-secondary" @click="queryTask"><IconSearch />查询</button>
  </div></div>

  <!-- 错误 -->
  <div v-if="error" class="alert alert-error"><IconAlert />{{ error }}</div>

  <!-- 实时时间线 -->
  <div v-if="toolEvents.length" class="panel">
    <div class="panel-head">
      <span class="panel-title">执行时间线 · {{ toolEvents.length }} 个事件</span>
      <span class="grow" />
      <span v-if="polling" class="row" style="gap: 7px"><span class="status-dot busy" /><span class="faint text-small">实时刷新中</span></span>
    </div>
    <div class="panel-body flush">
      <div v-for="(e, i) in toolEvents" :key="i" class="trace-row">
        <span class="trace-time">{{ fmtTime(e.time) }}</span>
        <span class="trace-kind">{{ e.event }}</span>
        <span class="truncate">
          <span v-if="e.tool" class="tag-mono">{{ toolCn(e.tool) }}</span>
          <span v-if="e.error" class="text-danger text-small"> · {{ e.error }}</span>
          <span v-else-if="e.disposition" class="faint text-small"> · {{ e.disposition }}</span>
        </span>
        <span class="trace-dur">{{ e.duration_ms ? `${e.duration_ms} ms` : '' }}</span>
      </div>
    </div>
  </div>

  <!-- 结果 -->
  <div ref="resultAnchor">
  <template v-if="result">
    <SectionHead eyebrow="RESULT" title="研判结果" />
    <div class="panel"><div class="panel-body stack stack-4">
      <div class="detail-head">
        <div class="grow">
          <span class="eyebrow">TASK</span>
          <h3>{{ result.task_id }}</h3>
          <div class="row row-wrap" style="gap: 6px; margin-top: 6px">
            <span class="tag-mono">{{ pairLabel(result.pair_id) }}</span>
            <DispositionTag :code="result.disposition" />
            <span v-if="result.overall_risk" class="pill pill-muted">风险 <RiskTag :value="result.overall_risk" /></span>
            <span v-if="result.need_human_review" class="pill pill-manual"><span class="dot" />需人工裁定</span>
          </div>
        </div>
        <button v-if="result.need_human_review && !result.human_verdict" class="btn btn-primary" @click="emit('goto-review', result.task_id)">
          <IconReview />去复核
        </button>
      </div>
      <div class="facts-grid">
        <div class="fact"><span>状态</span><b>{{ result.status }}</b></div>
        <div class="fact"><span>处置</span><b>{{ result.disposition || '—' }}</b></div>
        <div class="fact"><span>路由</span><b>{{ result.route || '—' }}</b></div>
        <div class="fact"><span>工具调用</span><b>{{ result.tool_calls?.length || 0 }} 次</b></div>
      </div>
    </div>
      </div>
    <!-- 安全日志文档：结论先行的正式交付形态 -->
    <SafetyLog :detail="result" />
    <div class="panel">
        <div class="panel-head">
          <span class="panel-title">研判日志 · 逐阶段证据链</span>
        </div>
        <div class="panel-body" style="padding-top: 0"><EvidenceChain :detail="result" /></div>
      <details class="json-fold">
        <summary>原始任务快照 JSON</summary>
        <pre>{{ JSON.stringify(result, null, 2) }}</pre>
      </details>
    </div>
  </template>
  </div>
  </template>
  </div>
  <p v-if="idle" class="task-idle-hint">上传一对双光图像，自动完成配准融合、VLM / YOLO 双源检测、对账校验与规范条款判级，约 1–3 分钟产出安全日志</p>
  </div>
</template>
