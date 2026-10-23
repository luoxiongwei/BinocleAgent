<script setup>
// 侧栏：品牌 + 导航 + 服务健康 + 环境注脚
import { IconShield, IconReview, IconLayers, IconPlay, IconActivity } from './icons.js'

defineProps({
  active: { type: String, default: 'console' },
  pendingCount: { type: Number, default: 0 },
  healthAgent: { type: Boolean, default: false }, // 8000 任务服务
  healthConsole: { type: Boolean, default: false }, // 8010 只读控制台
})

const emit = defineEmits(['navigate'])

const items = [
  { key: 'task', label: '安全日志', icon: IconPlay },
  { key: 'console', label: '工作总览', icon: IconShield },
  { key: 'review', label: '人工复核', icon: IconReview },
  { key: 'batches', label: '档案管理', icon: IconLayers },
]
</script>

<template>
  <aside class="side-nav">
    <div class="brand">
      <span class="brand-mark"><img src="/binocle-logo.svg" alt="Binocle" width="26" height="26" /></span>
      <div>
        <div class="brand-name">Binocle</div>
      </div>
    </div>

    <div class="nav-group-label">工作区</div>
    <button
      v-for="item in items"
      :key="item.key"
      :class="['nav-item', { active: active === item.key }]"
      :aria-current="active === item.key ? 'page' : undefined"
      @click="emit('navigate', item.key)"
    >
      <span class="nav-icon"><component :is="item.icon" /></span>
      {{ item.label }}
      <span v-if="item.key === 'review' && pendingCount" class="nav-count">
        {{ pendingCount }}
      </span>
    </button>

    <div class="side-services">
      <div class="side-services-title">服务健康</div>
      <div class="svc-row">
        <span :class="['status-dot', healthAgent ? 'on' : 'off']" />
        任务执行服务
        <span class="svc-port">:8000</span>
      </div>
      <div class="svc-row">
        <span :class="['status-dot', healthConsole ? 'on' : 'off']" />
        只读验收控制台
        <span class="svc-port">:8010</span>
      </div>
      <div class="svc-row" style="margin-top: 8px">
        <IconActivity />
        <span class="faint">LangGraph 13 节点</span>
      </div>
    </div>

  </aside>
</template>
