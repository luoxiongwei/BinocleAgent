<script setup>
// 安全日志工作区：双 Tab（研判任务 / 智能问答）
// 2026-10-23：原先对话区与上传区上下堆叠 → 同屏两套输入互相争夺视线，
// 改为与「档案管理」一致的 Tab 切换，同一时刻只出现一套输入。
import { nextTick, ref } from 'vue'
import ConversationWorkspace from './ConversationWorkspace.vue'
import TaskView from './TaskView.vue'

const emit = defineEmits(['notify', 'goto-review'])

const tab = ref('chat')
const taskRef = ref(null)
const qaSessionId = ref('')

function loadTask(taskId) {
  // 从总览/档案下钻时，若当前停在问答页需切回研判页，否则会加载到隐藏面板里
  tab.value = 'judge'
  nextTick(() => taskRef.value?.loadTask(taskId))
}

// 对话里发起任务后自动切到研判页并加载该任务
function onChatTask(task) {
  const id = task?.task_id || task?.id
  if (!id) return
  tab.value = 'judge'
  nextTick(() => loadTask(id))
}

defineExpose({ loadTask })
</script>

<template>
  <div class="task-shell">
    <div class="ws-tabs" role="tablist" aria-label="安全日志视图">
      <button
        :class="['ws-tab', { on: tab === 'chat' }]"
        role="tab"
        :aria-selected="tab === 'chat'"
        @click="tab = 'chat'"
      >
        智能问答
      </button>
      <button
        :class="['ws-tab', { on: tab === 'judge' }]"
        role="tab"
        :aria-selected="tab === 'judge'"
        @click="tab = 'judge'"
      >
        研判任务
      </button>
    </div>

    <div v-show="tab === 'chat'" class="ws-pane">
      <ConversationWorkspace @session="qaSessionId = $event" @task="onChatTask" />
    </div>

    <div v-show="tab === 'judge'" class="ws-pane">
      <TaskView
        ref="taskRef"
        :qa-session-id="qaSessionId"
        @notify="emit('notify', $event)"
        @goto-review="emit('goto-review', $event)"
      />
    </div>
  </div>
</template>

<style scoped>
.ws-tabs {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: var(--sp-4);
}

.ws-tab {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface);
  color: var(--text-dim);
  font: inherit;
  font-size: var(--fs-small);
  font-weight: 600;
  padding: 9px 16px;
  cursor: pointer;
  transition: border-color var(--dur) var(--ease-out), color var(--dur) var(--ease-out),
    background var(--dur) var(--ease-out);
}

.ws-tab:hover { border-color: var(--line-strong); color: var(--text); }
.ws-tab.on {
  border-color: var(--accent);
  background: var(--accent-wash);
  color: var(--accent);
}

.ws-pane { animation: pane-in 200ms var(--ease-out); }

/* 两个 pane 等宽：对话卡片默认 980 居中，与研判卡片左缘不齐 → 拉满同宽 */
.ws-pane :deep(.conversation-workspace) { max-width: none; }

@keyframes pane-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: none; }
}

@media (prefers-reduced-motion: reduce) {
  .ws-pane { animation: none; }
}
</style>
