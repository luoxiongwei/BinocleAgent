<script setup>
// 顶栏：当前视图标题 + 时钟 + 自动刷新指示 + 手动刷新
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { IconEye, IconRefresh } from './icons.js'
import { notesVisible, toggleNotes } from '../api.js'

const props = defineProps({
  title: { type: String, required: true },
  eyebrow: { type: String, default: '' },
  busy: { type: Boolean, default: false },
  autoRefresh: { type: Boolean, default: true },
})

const emit = defineEmits(['refresh', 'toggle-auto'])
const now = ref(new Date())
let timer = null

const clock = computed(() =>
  now.value.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }),
)

onMounted(() => {
  timer = setInterval(() => (now.value = new Date()), 1000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <header class="topbar">
    <div class="topbar-title">
      <span v-if="eyebrow" class="eyebrow">{{ eyebrow }}</span>
      <h1>{{ title }}</h1>
    </div>
    <div class="topbar-right">
      <button
        :class="['btn', 'btn-ghost', 'btn-sm']"
        :aria-pressed="notesVisible"
        :title="notesVisible ? '点击隐藏各区块的解释性注释' : '点击显示各区块的解释性注释'"
        @click="toggleNotes()"
      >
        <IconEye />{{ notesVisible ? '注释开' : '注释关' }}
      </button>
      <button
        :class="['btn', 'btn-ghost', 'btn-sm']"
        :aria-pressed="autoRefresh"
        title="每 30 秒自动刷新队列与服务状态"
        @click="emit('toggle-auto')"
      >
        <span :class="['status-dot', autoRefresh ? 'on' : 'off']" />
        {{ autoRefresh ? '自动刷新中' : '已暂停刷新' }}
      </button>
      <span class="clock">{{ clock }}</span>
      <button class="btn btn-secondary btn-sm" :disabled="busy" @click="emit('refresh')">
        <IconRefresh />{{ busy ? '读取中' : '刷新' }}
      </button>
    </div>
  </header>
</template>
