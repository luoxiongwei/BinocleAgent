<script setup>
// 双光安护理台 · 应用外壳
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api, loadInputs } from './api.js'
import SideNav from './components/SideNav.vue'
import TopBar from './components/TopBar.vue'
import ConsoleView from './components/ConsoleView.vue'
import ReviewDesk from './components/ReviewDesk.vue'
import BatchView from './components/BatchView.vue'
import LandingView from './components/LandingView.vue'
import TaskWorkspace from './components/TaskWorkspace.vue'

const VIEW_META = {
  home: { title: 'Binocle安全日志Agent', eyebrow: 'BINOCLE SAFETY LOG AGENT' },
  console: { title: '工作总览', eyebrow: 'WORK OVERVIEW' },
  review: { title: '人工复核', eyebrow: 'HUMAN REVIEW' },
  batches: { title: '档案管理', eyebrow: 'ARCHIVE MANAGEMENT' },
  task: { title: '安全日志生成', eyebrow: 'SAFETY LOG PIPELINE' },
}

const active = ref('home')
const loading = ref(false)
const autoRefresh = ref(true)
const healthAgent = ref(null) // 8000
const healthConsole = ref(null) // 8010 /api/health
const overview = ref({})
const taskArchives = ref([])
const batches = ref([])
const pending = ref([])
const archives = ref([])
const toasts = ref([])
const reviewFocus = ref('')
const taskView = ref(null) // TaskWorkspace 模板 ref（内部转发到 TaskView.loadTask），总览台下钻用
// 档案管理跳转携带（2026-10-17）：{ tab: 'live'|'history', run, task, date }
const archiveFocus = ref({})

let timer = null

// hash 深链：#home / #console / #review / #batches / #task
function syncFromHash() {
  const key = (location.hash || '').replace('#', '')
  if (key in VIEW_META) active.value = key
}

function syncToHash(key) {
  if (location.hash.replace('#', '') !== key) history.replaceState(null, '', `#${key}`)
}

const pendingCount = computed(() => pending.value.filter((p) => !p.resolved).length)

function notify(message, type = 'info') {
  const id = Date.now() + Math.random()
  toasts.value.push({ id, message, type })
  setTimeout(() => {
    toasts.value = toasts.value.filter((t) => t.id !== id)
  }, 3600)
}

async function refreshAll() {
  loading.value = true
  try {
    const [ha, hc, ov, ta, pd, ar, bs] = await Promise.allSettled([
      api.health(),
      api.consoleHealth(),
      api.overview(),
      api.taskArchives(),
      api.pending(),
      api.humanArchives(),
      api.batches(),
    ])
    healthAgent.value = ha.status === 'fulfilled' ? ha.value : null
    healthConsole.value = hc.status === 'fulfilled' ? hc.value : null
    if (ov.status === 'fulfilled') overview.value = ov.value
    if (ta.status === 'fulfilled') taskArchives.value = ta.value.tasks || []
    if (pd.status === 'fulfilled') pending.value = (pd.value.entries || []).filter((e) => !e.resolved)
    if (ar.status === 'fulfilled') archives.value = ar.value.entries || []
    if (bs.status === 'fulfilled') batches.value = bs.value.batches || []
    if (ha.status === 'rejected') notify('任务服务 :8000 未连接', 'err')
    await loadInputs()
  } finally {
    loading.value = false
  }
}

function navigate(key, focus) {
    // 档案管理允许携带定位信息（Tab / 批次 / 任务 / 日期），供总览台下钻
    if (key === 'batches' && focus) archiveFocus.value = focus
    active.value = key
    syncToHash(key)
    if (key === 'batches' || key === 'console') refreshAll()
}

// ConsoleView 的 open-archive 与 TaskView 的 goto-review 语义相同：跳到复核页并聚焦任务
function gotoReview(taskId) {
  reviewFocus.value = taskId
  active.value = 'review'
  syncToHash('review')
}

function openAgentTask(taskId) {
  // 命令式下钻：KeepAlive 下 TaskView 的 props 在停用期间冻结、watcher 通道不可靠
  // （2026-10-18 探针实测重复下钻同一任务零触发），故激活后用模板 ref 直接调 loadTask。
  // TaskView 首次挂载可能晚于当前 tick，ref 就绪前按 microtask→macrotask 递减重试。
  active.value = 'task'
  syncToHash('task')
  const load = (tries) => {
    if (taskView.value) { taskView.value.loadTask(taskId); return }
    if (tries <= 0) return
    if (tries > 6) setTimeout(() => load(tries - 1), 30)
    else nextTick(() => load(tries - 1))
  }
  load(12)
}

function toggleAuto() {
  autoRefresh.value = !autoRefresh.value
}

onMounted(() => {
  syncFromHash()
  window.addEventListener('hashchange', syncFromHash)
  refreshAll()
  timer = setInterval(() => {
    if (autoRefresh.value && active.value !== 'task') refreshAll()
  }, 30000)
})

onUnmounted(() => {
  window.removeEventListener('hashchange', syncFromHash)
  clearInterval(timer)
})
</script>

<template>
  <LandingView v-if="active === 'home'" @start="navigate('task')" @navigate="navigate" />
  <div v-else class="app-shell">
    <SideNav
      :active="active"
      :pending-count="pendingCount"
      :health-agent="Boolean(healthAgent)"
      :health-console="Boolean(healthConsole)"
      @navigate="navigate"
    />

    <div class="app-main">
      <TopBar
        :title="VIEW_META[active].title"
        :eyebrow="VIEW_META[active].eyebrow"
        :busy="loading"
        :auto-refresh="autoRefresh"
        @refresh="refreshAll"
        @toggle-auto="toggleAuto"
      />

      <!-- 视图缓存：误触侧栏切换再回来时保留各页状态（上传表单/运行中任务/事件流/
           复核台选中项），任务在后端继续跑，轮询定时器在后台持续，回来即最新进度。
           不缓存在 LandingView（home）——落地页每次进入应为初始态。 -->
      <main class="app-content">
        <KeepAlive>
          <ConsoleView
            v-if="active === 'console'"
            :overview="overview"
            :agent-tasks="taskArchives"
            :pending="pending"
            :archives="archives"
            :health="healthConsole || {}"
            @refresh="refreshAll"
            @goto="navigate"
            @open-archive="gotoReview"
            @open-task="openAgentTask"
          />
          <ReviewDesk
            v-else-if="active === 'review'"
            :pending="pending"
            :focus-task-id="reviewFocus"
            @notify="notify"
            @refresh-queue="refreshAll"
          />
          <BatchView
            v-else-if="active === 'batches'"
            :agent-tasks="taskArchives"
            :batches="batches"
            :overview="overview"
            :pending="pending"
            :focus="archiveFocus"
            @notify="notify"
            @goto-review="gotoReview"
          />
          <TaskWorkspace v-else ref="taskView" @notify="notify" @goto-review="gotoReview" />
        </KeepAlive>
      </main>
    </div>

    <div class="toast-host" aria-live="polite">
      <div v-for="t in toasts" :key="t.id" :class="['toast', t.type]">
        {{ t.message }}
      </div>
    </div>
  </div>
</template>
