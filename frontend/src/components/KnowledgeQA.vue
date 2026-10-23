<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const props = defineProps({ pairId: { type: String, default: '' }, taskId: { type: String, default: '' } })
const emit = defineEmits(['session'])
const question = ref('')
const pairInput = ref(props.pairId || '')
const sessionId = ref('')
const answer = ref(null)
const busy = ref(false)
const error = ref('')

async function ask() {
  if (!question.value.trim() || busy.value) return
  busy.value = true; error.value = ''
  try {
    answer.value = await api.chat({ message: question.value, session_id: sessionId.value || undefined, pair_id: pairInput.value.trim() || props.pairId || undefined, task_id: props.taskId || undefined })
    sessionId.value = answer.value.session_id
    emit('session', sessionId.value)
    question.value = ''
  } catch (err) { error.value = err.message || '问答失败' } finally { busy.value = false }
}
</script>

<template>
  <section class="qa-card">
    <div class="section-kicker">KNOWLEDGE QA</div>
    <h2>规范问答</h2>
    <p class="muted">先询问条款，再上传图对；同一会话会把已引用条款带入后续安全日志。</p>
    <div class="qa-row">
      <input v-model="pairInput" class="pair-input" placeholder="pair ID（可选）" />
      <input v-model="question" placeholder="例如：安全帽佩戴有哪些要求？" @keyup.enter="ask" />
      <button :disabled="busy || !question.trim()" @click="ask">{{ busy ? '检索中…' : '提问' }}</button>
    </div>
    <p v-if="error" class="qa-error">{{ error }}</p>
    <div v-if="answer" class="qa-answer">
      <div class="qa-session">{{ answer.intent === 'knowledge_qa' ? `会话 ${answer.session_id} · 已引用 ${answer.citations?.length || 0} 条规范` : `已识别意图：${answer.intent}` }}</div>
      <pre>{{ answer.answer }}</pre>
      <div v-if="answer.citations?.length" class="qa-citations">
        <span v-for="(cite, index) in answer.citations" :key="index">{{ cite.doc_id }} · {{ cite.full_path }}</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.qa-card { border: 1px solid var(--line); border-radius: 12px; padding: 20px; margin-bottom: 20px; background:
  linear-gradient(180deg, rgba(255, 255, 255, 0.028), rgba(255, 255, 255, 0.008)), var(--surface); }
.section-kicker { color: var(--accent); letter-spacing: .16em; font-size: 11px; font-weight: 700; }
h2 { margin: 7px 0; }
.muted, .qa-session { color: var(--text-dim); font-size: 13px; }
.qa-row { display: flex; gap: 10px; }
.qa-row input { flex: 1; min-width: 0; }
.qa-row .pair-input { flex: 0 0 150px; }
.qa-row button { white-space: nowrap; }
.qa-answer { margin-top: 16px; border-top: 1px solid var(--line); padding-top: 14px; }
pre { white-space: pre-wrap; font: inherit; line-height: 1.65; color: var(--text); }
.qa-citations { display: flex; flex-wrap: wrap; gap: 6px; }
.qa-citations span { background: var(--surface-3); border-radius: 5px; padding: 4px 7px; font-size: 12px; color: var(--text-dim); }
.qa-error { color: var(--danger); }
</style>
