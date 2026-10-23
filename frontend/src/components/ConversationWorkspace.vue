<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const emit = defineEmits(['session', 'task'])

const sessionId = ref('')
const pairId = ref('')
const input = ref('')
const turns = ref([])
const busy = ref(false)
const error = ref('')
const recentSessions = ref([])
const showRecent = ref(false)

function newSession() {
  sessionId.value = ''
  turns.value = []
  localStorage.removeItem('binocle-session-id')
  error.value = ''
}

async function loadRecent() {
  try { recentSessions.value = (await api.qaSessions(20)).sessions || [] } catch { recentSessions.value = [] }
}

async function openRecent(item) {
  if (!item?.session_id) return
  sessionId.value = item.session_id
  localStorage.setItem('binocle-session-id', sessionId.value)
  const session = await api.qaSession(sessionId.value)
  turns.value = (session.turns || []).flatMap((turn) => [
    { role: 'user', content: turn.question || turn.user_message || '' },
    { role: 'assistant', content: turn.answer || turn.assistant_message || '', trace: turn.trace || [] },
  ]).filter((turn) => turn.content)
  showRecent.value = false
  emit('session', sessionId.value)
}

onMounted(async () => {
  await loadRecent()
  const saved = localStorage.getItem('binocle-session-id')
  if (!saved) return
  try {
    const session = await api.qaSession(saved)
    sessionId.value = session.session_id
    const restored = (session.turns || []).flatMap((turn) => [
      { role: 'user', content: turn.question || turn.user_message || '' },
      { role: 'assistant', content: turn.answer || turn.assistant_message || '', trace: turn.trace || [] },
    ]).filter((turn) => turn.content)
    turns.value = restored.filter((turn, index) =>
      index === 0 || !(turn.role === restored[index - 1].role && turn.content === restored[index - 1].content))
    emit('session', sessionId.value)
  } catch {
    localStorage.removeItem('binocle-session-id')
  }
})

const empty = computed(() => turns.value.length === 0)

async function send() {
  const message = input.value.trim()
  if (!message || busy.value) return
  turns.value.push({ role: 'user', content: message })
  input.value = ''; busy.value = true; error.value = ''
  try {
    const result = await api.chat({ message, session_id: sessionId.value || undefined, pair_id: pairId.value.trim() || undefined })
    sessionId.value = result.session_id || sessionId.value
    if (sessionId.value) {
      localStorage.setItem('binocle-session-id', sessionId.value)
      emit('session', sessionId.value)
      await loadRecent()
    }
    turns.value.push({ role: 'assistant', content: result.answer || '已完成处理。', intent: result.intent, task: result.task, trace: result.trace })
    // 对话里发起研判任务 → 通知工作区切到研判页并加载该任务
    if (result.task?.task_id) emit('task', result.task)
  } catch (err) { error.value = err.message || '请求失败' } finally { busy.value = false }
}
</script>

<template>
  <section class="conversation-workspace">
    <div class="conversation-head">
      <div><div class="section-kicker">BINOCLE AGENT</div><h2>和 Binocle 对话</h2></div>
      <div class="head-actions"><span v-if="sessionId" class="session-label">会话 {{ sessionId }}</span><button class="new-session" @click="showRecent = !showRecent">最近问答</button><button class="new-session" @click="newSession">新建会话</button></div>
    </div>
    <div v-if="showRecent" class="recent-panel">
      <div class="recent-head"><span>最近问答</span><button class="recent-clear" @click="newSession">清空当前</button></div>
      <button v-for="item in recentSessions" :key="item.session_id" class="recent-item" @click="openRecent(item)">
        <span class="recent-title">{{ item.title }}</span><span class="recent-meta">{{ item.turn_count }} 轮 · {{ item.session_id }}</span>
      </button>
      <div v-if="!recentSessions.length" class="recent-empty">暂无历史对话</div>
    </div>
    <div class="conversation-body" :class="{ empty }">
      <div v-if="empty" class="empty-prompt">你可以直接提问、查询任务，或要求我检查一对 RGB / 红外图像。</div>
      <article v-for="(turn, i) in turns" :key="i" :class="['turn', turn.role]">
        <div class="turn-role">{{ turn.role === 'user' ? '你' : 'Binocle' }}</div>
        <div class="turn-content">{{ turn.content }}</div>
        <div v-if="turn.trace?.length" class="turn-trace">
          <div class="trace-title">执行轨迹 · {{ turn.trace.length }} 次工具调用</div>
          <div v-for="(step, j) in turn.trace" :key="j" class="trace-step">
            <span class="trace-tool">{{ step.tool }}</span>
            <span class="trace-status">已完成</span>
          </div>
        </div>
        <div v-if="turn.task" class="turn-task">任务 {{ turn.task.task_id }} · {{ turn.task.status || 'queued' }}</div>
      </article>
    </div>
    <div class="composer-meta"><input v-model="pairId" placeholder="pair ID（需要检查图像时填写）" /></div>
    <div class="composer"><textarea v-model="input" rows="2" placeholder="输入问题或任务，例如：什么是高处作业？ / 查询 graph-pair04 / 检查 pair04" @keydown.enter.exact.prevent="send" /><button :disabled="busy || !input.trim()" @click="send">{{ busy ? '处理中…' : '发送' }}</button></div>
    <p v-if="error" class="error">{{ error }}</p>
  </section>
</template>

<style scoped>
.conversation-workspace { max-width: 980px; margin: 0 auto; border: 1px solid var(--line); border-radius: 14px; background: var(--surface); overflow: hidden; }
.conversation-head { display:flex; justify-content:space-between; align-items:center; padding:22px 26px; border-bottom:1px solid var(--line); }
.head-actions { display:flex; align-items:center; gap:12px; }.new-session { font-size:12px; padding:6px 10px; }
.recent-panel { border-bottom:1px solid #303842; padding:12px 24px; background:#171c21; max-height:220px; overflow:auto; }
.recent-head { display:flex; justify-content:space-between; color:#aeb8c2; font-size:12px; margin-bottom:8px; }
.recent-clear { border:0; background:none; color:#8d98a5; cursor:pointer; font-size:11px; }
.recent-item { display:flex; justify-content:space-between; width:100%; border:0; border-radius:7px; background:transparent; color:#dce4ea; padding:8px 6px; cursor:pointer; text-align:left; }
.recent-item:hover { background:#252e36; }.recent-title { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:65%; }.recent-meta { color:#7f8995; font-size:11px; }
.recent-empty { color:#7f8995; font-size:12px; padding:8px 6px; }
.section-kicker { color:var(--accent); letter-spacing:.16em; font-size:11px; font-weight:700; }
h2 { margin:6px 0 0; }
.session-label,.composer-meta { color:var(--text-dim); font-size:12px; }
.conversation-body { min-height:330px; max-height:560px; overflow:auto; padding:24px; }
.conversation-body.empty { display:grid; place-items:center; color:var(--text-faint); }
.turn { max-width:82%; margin-bottom:18px; padding:12px 15px; border-radius:10px; line-height:1.65; }
.turn.user { margin-left:auto; background:var(--surface-3); }
.turn.assistant { background:var(--surface-2); border-left:3px solid var(--accent); }
.turn-role { font-size:11px; color:var(--accent); margin-bottom:4px; }
.turn-task { margin-top:8px; color:var(--accent); font-size:12px; }
.turn-trace { margin-top:12px; padding-top:10px; border-top:1px solid var(--line); font-size:12px; }
.trace-title { color:var(--text-dim); margin-bottom:6px; }.trace-step { display:flex; gap:10px; padding:3px 0; }
.trace-tool { color:var(--accent); }.trace-status { color:var(--accent); }
.composer-meta { padding:10px 24px 0; }.composer-meta input { width:220px; }
.composer { display:flex; gap:10px; padding:12px 24px 22px; }.composer textarea { flex:1; resize:vertical; }.composer button { align-self:end; }
.error { color:var(--danger); padding:0 24px 18px; }
</style>
