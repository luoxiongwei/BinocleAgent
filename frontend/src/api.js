// API 层：双端口路由 + 全部端点。
// /api 前缀经 Vite 代理到 8010（只读验收控制台）；
// /tasks /health /inputs 属于 8000（任务执行服务），不走 /api 前缀。
// （历史 bug：所有请求统一加 /api 导致任务接口 404，故按路径分流。）
import { ref } from 'vue'

const AGENT_PATHS = ['/tasks', '/health', '/inputs', '/qa', '/chat', '/feedback', '/memory']

const apiBase = () => (localStorage.getItem('gdyolo-api-base') || '/api').replace(/\/$/, '')

const isAgentPath = (path) =>
  AGENT_PATHS.some((p) => path === p || path.startsWith(`${p}/`) || path.startsWith(`${p}?`))

const resolveRoot = (path) =>
  isAgentPath(path) ? apiBase().replace(/\/api$/, '') : apiBase()

async function request(path, options = {}, attempt = 0) {
  // 10s 超时：后端瞬时抖动（如 sqlite 锁等待）不该让页面无限挂起；
  // GET 失败自动重试一次（POST 不重试，避免重复建任务）。
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 10000)
  // 显式带 /api 前缀的端点（8010 只读控制台）：基址自身已以 /api 结尾时
  // 去掉路径里的重复前缀，否则会拼出 /api/api/...（曾因此无法经 api.js 请求）。
  const root = resolveRoot(path)
  const url = path.startsWith('/api/') && root.endsWith('/api') ? `${root}${path.slice(4)}` : `${root}${path}`
  try {
    const response = await fetch(url, {
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      ...options,
      signal: controller.signal,
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(payload.detail || payload.error || `请求失败 (${response.status})`)
    return payload
  } catch (err) {
    const transient = err?.name === 'AbortError' || err instanceof TypeError
    if (transient && attempt === 0 && (!options.method || options.method === 'GET')) {
      await new Promise((r) => setTimeout(r, 400))
      return request(path, options, attempt + 1)
    }
    if (err?.name === 'AbortError') throw new Error('请求超时（10s），服务可能正忙')
    throw err
  } finally {
    clearTimeout(timer)
  }
}

const enc = encodeURIComponent

export const api = {
  setBase(base) {
    localStorage.setItem('gdyolo-api-base', base.replace(/\/$/, ''))
  },
  getBase: apiBase,

  // —— 8000 任务执行服务 ——
  health: () => request('/health'),
  createTask: (body) => request('/tasks', { method: 'POST', body: JSON.stringify(body) }),
  task: (id) => request(`/tasks/${enc(id)}`),
  taskEvents: (id) => request(`/tasks/${enc(id)}/events`),
  retryTask: (id, body = {}) =>
    request(`/tasks/${enc(id)}/retry`, { method: 'POST', body: JSON.stringify(body) }),
  taskFeedback: (id) => request(`/tasks/${enc(id)}/feedback`),
  submitFeedback: (id, body) =>
    request(`/tasks/${enc(id)}/feedback`, { method: 'POST', body: JSON.stringify(body) }),
  feedbackSummary: () => request('/feedback/summary'),
  feedbackCandidates: () => request('/feedback/candidates'),
  memoryCases: (q = '', limit = 5) => request(`/memory/cases?q=${encodeURIComponent(q)}&limit=${limit}`),
  policyVersion: () => request('/policy/version'),
  submitReview: (id, body) =>
    request(`/tasks/${enc(id)}/review`, { method: 'POST', body: JSON.stringify(body) }),
  submitAcceptance: (id, body) =>
    request(`/tasks/${enc(id)}/acceptance`, { method: 'POST', body: JSON.stringify(body) }),
  batches: () => request('/batches'),
  batch: (id) => request(`/batches/${enc(id)}`),
  qaAsk: (body) => request('/qa/ask', { method: 'POST', body: JSON.stringify(body) }),
  chat: (body) => request('/chat', { method: 'POST', body: JSON.stringify(body) }),
  chatTools: () => request('/chat/tools'),
  qaSession: (id) => request(`/qa/sessions/${enc(id)}`),
  qaSessions: (limit = 20) => request(`/qa/sessions?limit=${limit}`),
  bindQaSession: (body) => request('/qa/sessions/bind', { method: 'POST', body: JSON.stringify(body) }),

  // —— 8010 只读验收控制台 ——
  consoleHealth: () => request('/api/health'),
  overview: () => request('/overview'),
  runs: () => request('/runs'),
  runDetail: (run) => request(`/runs/${enc(run)}`),
  runReport: (run) => request(`/runs/${enc(run)}/report`),
  pairDetail: (run, pair) => request(`/runs/${enc(run)}/pairs/${enc(pair)}`),
  taskArchives: () => request('/task-archives'),
  runPairs: (run) => request(`/runs/${enc(run)}/pairs`),
  graphRunImages: (taskId) => request(`/graph-runs/${enc(taskId)}/images`),
  pending: () => request('/pending'),
  humanArchives: () => request('/human-archives'),

  // —— 图对命名（中文别名层） ——
  listInputs: () => request('/inputs'),
  renamePair: (pairId, displayName) =>
    request('/inputs/rename', {
      method: 'POST',
      body: JSON.stringify({ pair_id: pairId, display_name: displayName }),
    }),

  // 上传图对（FormData，不走 JSON）
  async uploadPair({ pairId, displayName, rgb, ir }) {
    const form = new FormData()
    form.append('pair_id', pairId || '')
    if (displayName) form.append('display_name', displayName)
    form.append('rgb', rgb)
    form.append('ir', ir)
    const response = await fetch(`${resolveRoot('/inputs')}/inputs`, { method: 'POST', body: form })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(payload.detail || '上传失败')
    return payload
  },
}

// —— 图对中文名（别名注册表的应用级缓存） ——
const pairAliases = ref({})   // 中文名 -> pair_id（仅本模块消费：canonicalPairId）
export const pairDisplay = ref({})   // pair_id -> 中文显示名
export const pairInputs = ref([])    // 图对库列表

export async function loadInputs() {
  try {
    const data = await api.listInputs()
    pairAliases.value = data.aliases || {}
    pairInputs.value = data.pairs || []
    const display = {}
    for (const p of data.pairs || []) if (p.display) display[p.pair_id] = p.display
    pairDisplay.value = display
  } catch {
    /* 图对库加载失败不阻断主流程 */
  }
}

// pair_id -> 展示名（有中文名用中文名，否则原样）
export const pairLabel = (pairId) => pairDisplay.value[pairId] || pairId || '—'

// 处置档位语义（二值口径：成功三档 + 失败一律转人工）
export const DISPOSITIONS = {
  P1_auto_alert: { label: '高风险 · 强制告警', short: '强制告警', tone: 'danger' },
  TIP_optional: { label: '低风险 · 可选提示', short: '可选提示', tone: 'warn' },
  P4_archive: { label: '安全 · 记录归档', short: '归档', tone: 'safe' },
  P3_manual_judge: { label: '转人工 · 待裁定', short: '转人工', tone: 'manual' },
  P3_failed_closed: { label: '转人工 · 失败关闭', short: '转人工', tone: 'manual' },
}

export const canonicalPairId = (value) => {
  const raw = String(value || '').trim()
  return pairAliases.value[raw] || raw
}

export const dispositionOf = (code) =>
  DISPOSITIONS[code] || { label: code || '未知', short: code || '—', tone: 'muted' }

export const riskTone = (value) =>
  ({ 高风险: 'danger', 低风险: 'warn', 安全: 'safe' }[value] || 'muted')

// 工具中文名（执行时间线 / 证据链展示用）
export const TOOL_CN = {
  fuse_qc: '双光融合配准',
  vlm_open_detect: 'VLM 开放检测',
  yolo_detect: 'YOLO 第二观察者',
  reconcile: '双源对账',
  verify_equipment_state: '设备状态校验',
  evaluate_risk: '规则引擎研判',
  generate_judgement: '条款判级（LLM+RAG）',
  validate_report: '报告校验',
  planner: '有界规划器',
  recrop_verify: '局部重裁复核',
  yolo_recheck: 'YOLO 复检',
}

export const toolCn = (name) => TOOL_CN[name] || name

// 时间显示统一转本地时区：后端时间戳为带偏移的 UTC（+00:00），
// 直接切字符串会差 8 小时（2026-10-17 用户实测：事件 17:50 vs 界面时钟 01:53）。
const pad2 = (n) => String(n).padStart(2, '0')
const toDate = (iso) => {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

// 解释性注释（SectionHead 的 note）显隐：默认收起，顶栏「注释」开关切换，
// localStorage 持久化（2026-10-17 用户要求：解释性文字可隐藏）。
const NOTES_KEY = 'gdyolo-notes-visible'
export const notesVisible = ref(localStorage.getItem(NOTES_KEY) === '1')
export function toggleNotes() {
  notesVisible.value = !notesVisible.value
  localStorage.setItem(NOTES_KEY, notesVisible.value ? '1' : '0')
}
export const fmtTime = (iso) => {
  const d = toDate(iso)
  return d ? `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}` : '--:--:--'
}
export const fmtDateTime = (iso) => {
  const d = toDate(iso)
  return d ? `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}` : '—'
}
// 本地日期键（YYYY-MM-DD）：统一时间轴/日期分组专用，与 fmtDateTime 同一时区语义
// （后端时间戳为 UTC ISO，直接切字符串会差 8 小时——2026-10-17 复核台日期分组实测）。
export const dayKey = (iso) => {
  const d = toDate(iso)
  return d ? `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}` : ''
}
// 日期键 → 「MM-DD · 周五」组头（时间轴/队列分组用）
const WEEK_CN = ['日', '一', '二', '三', '四', '五', '六']
export const fmtDayHead = (key) => {
  const d = toDate(key)
  return d ? `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} · 周${WEEK_CN[d.getDay()]}` : '时间未知'
}
