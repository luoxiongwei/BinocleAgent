<script setup>
// 图像三联（可见光 / 红外 / 融合），点击放大（灯箱）
// src 存在但加载失败（如产物缺失 404）时回退到「无图像」占位，不显示裂图图标
import { ref, watch } from 'vue'

const props = defineProps({
  images: { type: Object, default: () => ({}) }, // { fused, visible, infrared }
})

const KINDS = [
  { key: 'visible', label: '可见光 VISIBLE' },
  { key: 'infrared', label: '红外 INFRARED' },
  { key: 'fused', label: '融合配准 FUSED' },
]

const zoom = ref(null) // { src, label }
const failed = ref({}) // kind -> true：src 存在但加载 404/解码失败

watch(() => props.images, () => {
  failed.value = {}
})

function onImgError(kind) {
  failed.value = { ...failed.value, [kind]: true }
}

function open(src, label) {
  if (src) zoom.value = { src, label }
}

function onKey(e, src, label) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    open(src, label)
  }
}

function onZoomKey(e) {
  if (e.key === 'Escape' || e.key === 'Enter' || e.key === ' ') zoom.value = null
}
</script>

<template>
  <div class="image-triptych">
    <figure
      v-for="kind in KINDS"
      :key="kind.key"
      class="image-frame"
      :class="{ clickable: images[kind.key] && !failed[kind.key] }"
      :role="images[kind.key] && !failed[kind.key] ? 'button' : undefined"
      :tabindex="images[kind.key] && !failed[kind.key] ? 0 : undefined"
      :aria-label="images[kind.key] && !failed[kind.key] ? `放大 ${kind.label}` : undefined"
      :title="images[kind.key] && !failed[kind.key] ? '点击或按回车放大' : ''"
      @click="open(images[kind.key], kind.label)"
      @keydown="onKey($event, images[kind.key], kind.label)"
    >
      <img
        v-if="images[kind.key] && !failed[kind.key]"
        :src="images[kind.key]"
        :alt="kind.label"
        loading="lazy"
        @error="onImgError(kind.key)"
      />
      <div v-else class="image-missing">无图像</div>
      <figcaption>{{ kind.label }}</figcaption>
    </figure>
  </div>

  <div v-if="zoom" class="lightbox" role="dialog" aria-modal="true" aria-label="图像放大" tabindex="0" @click="zoom = null" @keydown="onZoomKey">
    <img :src="zoom.src" :alt="zoom.label" />
    <span class="lightbox-cap">{{ zoom.label }} · 点击任意处或按 Esc 关闭</span>
  </div>
</template>
