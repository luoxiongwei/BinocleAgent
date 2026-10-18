<script setup>
// 区块标题：英文 eyebrow + 宋体中文标题 + 右侧操作区
// note 为解释性文字，受顶栏「注释」总开关控制（默认收起）
import { notesVisible } from '../api.js'

defineProps({
  eyebrow: { type: String, default: '' },
  title: { type: String, required: true },
  note: { type: String, default: '' },
})
</script>

<template>
  <div class="section-head">
    <div>
      <span v-if="eyebrow" class="eyebrow">{{ eyebrow }}</span>
      <h2>{{ title }}</h2>
    </div>
    <div class="row">
      <span v-if="note && notesVisible" class="section-note">{{ note }}</span>
      <slot />
    </div>
  </div>
</template>
