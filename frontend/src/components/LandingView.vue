<script setup>
// 落地页 · 夜视监视器 Hero（2026-10-23 重构）
// 背景影像走 Mux HLS；离线环境自动退回静态深绿辉光底，不影响进入工作区。
import { onBeforeUnmount, onMounted, ref } from 'vue'
import {
  IconArrowRight,
  IconChat,
  IconClose,
  IconEye,
  IconMenu,
  IconRefresh,
  IconSearch,
} from './icons.js'
import SwarmCursor from './SwarmCursor.vue'
import BentoGrid from './BentoGrid.vue'

const emit = defineEmits(['start', 'navigate'])

const HLS_SRC = 'https://stream.mux.com/tLkHO1qZoaaQOUeVWo8hEBeGQfySP02EPS02BmnNFyXys.m3u8'
const NAV = [
  { key: 'console', label: '工作总览' },
  { key: 'task', label: '安全日志' },
  { key: 'review', label: '人工复核' },
  { key: 'batches', label: '档案管理' },
]

// 四个能力块：编号 + 图标 + 标题 + 一句表述
const FEATURES = [
  { icon: IconChat, title: '智能问答', desc: '对话式追问研判依据，任务可从对话发起与跟踪' },
  { icon: IconEye, title: '低光检测', desc: '红外补盲，夜间与烟尘场景不漏检' },
  { icon: IconSearch, title: '规范检索', desc: '9 部 JGJ/GB 条文级召回，每个答案可溯源' },
  { icon: IconRefresh, title: '反馈进化', desc: '人工复核回写真值，案例记忆持续沉淀' },
]

const videoEl = ref(null)
const mediaOn = ref(false)
// 特效时序：影像先出（playing 或 900ms 超时）→ 文案/辉光 → 粒子 → 能力卡
const heroReady = ref(false)
const menuOpen = ref(false)
// 蚁群粒子：按视口宽度降载（移动端 GPU 弱，粒子数减半再减）
const swarmCount = ref(520)
function syncSwarm() {
  const w = window.innerWidth
  swarmCount.value = w < 640 ? 190 : w < 1100 ? 330 : 520
}
let hls = null

let readyTimer = 0

onMounted(async () => {
  syncSwarm()
  window.addEventListener('resize', syncSwarm)
  // 影像拿不到首帧（离线/被墙）时不拖住整页：900ms 后照常进场
  readyTimer = window.setTimeout(() => (heroReady.value = true), 900)
  const video = videoEl.value
  if (!video) {
    heroReady.value = true
    return
  }
  try {
    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = HLS_SRC
    } else {
      const mod = await import('hls.js')
      const Hls = mod.default || mod.Hls
      if (!Hls || !Hls.isSupported()) return
      // enableWorker:false —— 沙箱/受限环境下 worker 版 hls 易静默失败
      hls = new Hls({ enableWorker: false })
      hls.loadSource(HLS_SRC)
      hls.attachMedia(video)
      hls.on(Hls.Events.ERROR, (_evt, data) => {
        if (data?.fatal) mediaOn.value = false
      })
    }
    video.addEventListener(
      'playing',
      () => {
        mediaOn.value = true
        heroReady.value = true
        window.clearTimeout(readyTimer)
      },
      { once: true },
    )
    const played = video.play?.()
    if (played?.catch) played.catch(() => {})
  } catch {
    mediaOn.value = false
  }
})

onBeforeUnmount(() => {
  window.clearTimeout(readyTimer)
  window.removeEventListener('resize', syncSwarm)
  hls?.destroy()
  hls = null
})

function go(key) {
  menuOpen.value = false
  emit('navigate', key)
}
</script>

<template>
  <main class="hero" :class="{ 'media-on': mediaOn, 'is-ready': heroReady }">
    <div class="hero-media" aria-hidden="true">
      <video ref="videoEl" class="hero-video" muted loop playsinline autoplay preload="metadata" />
      <div class="hero-scrim-left" />
      <div class="hero-scrim-bottom" />
    </div>

    <!-- 粒子蚁群：WebGL2 流体场，跟随光标聚散 / 悬停按钮时绕行 -->
    <div class="hero-swarm" aria-hidden="true">
      <SwarmCursor
        :count="swarmCount"
        :size="2.2"
        color="#a8f0cf"
        accent-color="#5ed29c"
        :spread="118"
        :swirl="0.78"
        :tail="0.55"
        :trail="0.85"
        :wander="0.55"
        :depth="0.8"
        idle="roam"
        click-effect="burst"
        :enabled="heroReady"
      />
    </div>



    <svg class="hero-glow" viewBox="0 0 1200 460" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <filter id="heroGlow" x="-60%" y="-80%" width="220%" height="280%">
          <feGaussianBlur stdDeviation="25" />
        </filter>
      </defs>
      <ellipse cx="600" cy="120" rx="560" ry="120" fill="rgba(94,210,156,0.18)" filter="url(#heroGlow)" />
      <ellipse cx="600" cy="120" rx="300" ry="62" fill="rgba(20,86,66,0.32)" filter="url(#heroGlow)" />
    </svg>

    <header class="hero-nav">
      <a class="hero-brand" href="#home" @click.prevent="menuOpen = false">
        <span class="hero-logo"><img src="/binocle-logo.svg" alt="Binocle" width="32" height="32" /></span>
        <span class="hero-brand-text">BINOCLE<em>safety log</em></span>
      </a>

      <nav class="hero-links" aria-label="主导航">
        <button v-for="item in NAV" :key="item.key" @click="go(item.key)">{{ item.label }}</button>
      </nav>

      <div class="hero-nav-right">
        <span class="hero-live"><i class="live-dot" />13 nodes online</span>
        <button
          class="hero-burger"
          :aria-expanded="menuOpen"
          aria-label="打开导航菜单"
          @click="menuOpen = !menuOpen"
        >
          <IconClose v-if="menuOpen" /><IconMenu v-else />
        </button>
      </div>
    </header>

    <div v-if="menuOpen" class="hero-mobile" @click.self="menuOpen = false">
      <button v-for="item in NAV" :key="item.key" @click="go(item.key)">
        {{ item.label }}<IconArrowRight />
      </button>
    </div>

    <section class="hero-body">
      <div class="hero-stack">
        <p class="hero-eyebrow">Binocle safety log agent</p>
        <h1 class="hero-title">全天守护，<br />安全留痕<span class="dot">。</span></h1>
        <p class="hero-lead">
          上传一对可见光与红外图像，Agent 自动完成配准融合、双源交叉对账与规范条款判级，
          输出带证据链的安全日志与处置结论——每条判断都能被追溯。
        </p>

        <div class="hero-actions">
          <button class="hero-cta" @click="emit('start')">开始研判<IconArrowRight /></button>
          <button class="hero-ghost" @click="go('console')">查看工作总览</button>
        </div>
      </div>
    </section>

    <BentoGrid :items="FEATURES" :start="heroReady" :radius="16" :gap="14" :spotlight="260" glow="94, 210, 156" />
  </main>
</template>
