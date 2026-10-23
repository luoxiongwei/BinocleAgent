<script setup>
// BentoGrid —— 移植自 React Bits「Magic Bento」的交互层
// 原组件带 6 套内置 artwork（analytics/dashboard/…）与 @hugeicons 依赖，本项目不需要；
// 这里只保留四件真正影响观感与手感的东西：
//   1) 网格级光斑：光标位置换算到每张卡，跨卡接力点亮描边（--near）
//   2) 卡内聚光：指针进入卡内时点亮卡面光斑（--hot）
//   3) 点击波纹：从点击点向外扩散的光环扫过所有卡（--wx/--wy/--wr/--wa）
//   4) 错峰入场 + 轻微磁吸（--tx/--ty），reduced-motion 全关
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  items: { type: Array, required: true },
  gap: { type: Number, default: 14 },
  radius: { type: Number, default: 16 },
  spotlight: { type: Number, default: 260 },
  glow: { type: String, default: '94, 210, 156' }, // "r, g, b"
  tilt: { type: Boolean, default: false },
  magnet: { type: Number, default: 5 }, // px，0 = 关闭
  start: { type: Boolean, default: true }, // false 时等待外部闸门（如影像就绪）再入场
})

const root = ref(null)
let destroy = null
let begin = null

// 外部闸门打开（影像就绪）后开始错峰入场
watch(
  () => props.start,
  (value) => {
    if (value) begin?.()
  },
)

onMounted(() => {
  const el = root.value
  if (!el) return
  const cells = Array.from(el.querySelectorAll('.bento-cell'))
  const cards = cells.map((cell) => cell.querySelector('.bento-card'))
  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
  const motion = !reduced

  el.style.setProperty('--bento-glow-rgb', props.glow)
  el.style.setProperty('--bento-radius', `${props.radius}px`)
  el.style.setProperty('--bento-gap', `${props.gap}px`)
  el.style.setProperty('--bento-spot', `${props.spotlight}px`)
  el.style.setProperty('--bento-border', `${Math.round(props.spotlight * 0.75)}px`)

  const states = cards.map(() => ({
    hot: 0,
    near: 0,
    tx: 0,
    ty: 0,
    vx: 0,
    vy: 0,
    written: {},
  }))
  const pointer = { x: -1e5, y: -1e5, inside: false }
  const light = { x: -1e5, y: -1e5, near: 0, ready: false }
  const wave = { x: 0, y: 0, t: -1, duration: 1.05, reach: 800 }
  let bounds = null
  let rects = []
  let raf = 0
  let last = 0
  let visible = true
  let entered = false

  const write = (card, state, name, value) => {
    if (state.written[name] === value) return
    state.written[name] = value
    card.style.setProperty(name, value)
  }

  const measure = () => {
    bounds = el.getBoundingClientRect()
    rects = cards.map((card) => (card ? card.getBoundingClientRect() : null))
  }

  const startWave = (x, y) => {
    if (!motion || !bounds) return
    wave.x = x
    wave.y = y
    wave.t = 0
    wave.reach =
      Math.max(
        Math.hypot(x - bounds.left, y - bounds.top),
        Math.hypot(x - bounds.right, y - bounds.top),
        Math.hypot(x - bounds.left, y - bounds.bottom),
        Math.hypot(x - bounds.right, y - bounds.bottom),
      ) + 120
    wake()
  }

  const frame = (now) => {
    raf = 0
    const dt = Math.min(0.05, (now - last) / 1000 || 0)
    last = now
    let busy = false

    const margin = props.spotlight * 0.5
    const roam =
      pointer.inside && bounds &&
      pointer.x > bounds.left - margin && pointer.x < bounds.right + margin &&
      pointer.y > bounds.top - margin && pointer.y < bounds.bottom + margin
    const nearTarget = roam ? 1 : 0
    if (!light.ready && nearTarget) {
      light.x = pointer.x
      light.y = pointer.y
      light.ready = true
    }
    const follow = 1 - Math.exp(-dt / 0.05)
    light.x += (pointer.x - light.x) * follow
    light.y += (pointer.y - light.y) * follow
    const fade = 1 - Math.exp(-dt / (nearTarget > light.near ? 0.12 : 0.4))
    light.near += (nearTarget - light.near) * fade
    if (Math.abs(nearTarget - light.near) < 0.002) light.near = nearTarget
    else busy = true
    if (Math.abs(pointer.x - light.x) + Math.abs(pointer.y - light.y) > 0.3) busy = true

    let waveRadius = 0
    let waveAlpha = 0
    if (wave.t >= 0) {
      wave.t += dt
      const progress = Math.min(1, wave.t / wave.duration)
      waveRadius = wave.reach * (1 - Math.pow(1 - progress, 2.2))
      waveAlpha = Math.pow(1 - progress, 1.3)
      if (progress >= 1) {
        wave.t = -1
        waveAlpha = 0
      } else busy = true
    }

    cards.forEach((card, index) => {
      const rect = rects[index]
      if (!card || !rect) return
      const state = states[index]
      const px = pointer.x - rect.left
      const py = pointer.y - rect.top
      const inside = pointer.inside && entered && px >= 0 && py >= 0 && px <= rect.width && py <= rect.height

      const hotTarget = inside ? 1 : 0
      state.hot += (hotTarget - state.hot) * (1 - Math.exp(-dt / (hotTarget > state.hot ? 0.14 : 0.32)))
      if (Math.abs(hotTarget - state.hot) < 0.002) state.hot = hotTarget
      else busy = true

      // 磁吸：卡面朝指针方向轻微偏移（不做 3D 倾斜，避免文字发虚）
      const u = (px / Math.max(1, rect.width) - 0.5) * 2
      const v = (py / Math.max(1, rect.height) - 0.5) * 2
      const reach = props.magnet
      const txTarget = inside && motion && reach ? Math.max(-1, Math.min(1, u)) * reach : 0
      const tyTarget = inside && motion && reach ? Math.max(-1, Math.min(1, v)) * reach : 0
      const k = 170
      const damp = 20
      const steps = Math.max(1, Math.ceil(dt / (1 / 240)))
      const h = dt / steps
      for (let s = 0; s < steps; s++) {
        state.vx += (k * (txTarget - state.tx) - damp * state.vx) * h
        state.tx += state.vx * h
        state.vy += (k * (tyTarget - state.ty) - damp * state.vy) * h
        state.ty += state.vy * h
      }
      if (Math.abs(txTarget - state.tx) < 0.05 && Math.abs(tyTarget - state.ty) < 0.05) {
        state.tx = txTarget
        state.ty = tyTarget
        state.vx = 0
        state.vy = 0
      } else busy = true

      write(card, state, '--bx', `${(light.x - rect.left).toFixed(1)}px`)
      write(card, state, '--by', `${(light.y - rect.top).toFixed(1)}px`)
      write(card, state, '--near', light.near.toFixed(3))
      write(card, state, '--hot', state.hot.toFixed(3))
      write(card, state, '--tx', `${state.tx.toFixed(2)}px`)
      write(card, state, '--ty', `${state.ty.toFixed(2)}px`)
      write(card, state, '--wx', `${(wave.x - rect.left).toFixed(1)}px`)
      write(card, state, '--wy', `${(wave.y - rect.top).toFixed(1)}px`)
      write(card, state, '--wr', `${waveRadius.toFixed(1)}px`)
      write(card, state, '--wa', waveAlpha.toFixed(3))
    })

    if (busy && visible) raf = requestAnimationFrame(frame)
  }

  function wake() {
    if (raf || !visible) return
    last = performance.now()
    raf = requestAnimationFrame(frame)
  }

  const onPointerMove = (event) => {
    pointer.x = event.clientX
    pointer.y = event.clientY
    pointer.inside = true
    wake()
  }

  const onPointerOut = (event) => {
    if (event.relatedTarget) return
    pointer.inside = false
    wake()
  }

  const onPointerDown = (event) => {
    if (!el.contains(event.target)) return
    startWave(event.clientX, event.clientY)
  }

  const onScroll = () => {
    measure()
    wake()
  }

  const resizeObserver = new ResizeObserver(() => {
    measure()
    wake()
  })
  resizeObserver.observe(el)

  const visibility = new IntersectionObserver((entries) => {
    visible = entries.some((entry) => entry.isIntersecting)
    if (visible) {
      measure()
      wake()
    }
  })
  visibility.observe(el)

  // 错峰入场：逐张展开（80ms 间隔）；start=false 时挂起，等外部闸门（影像首帧）
  measure()
  const timers = []
  begin = () => {
    if (entered) return
    entered = true
    if (!motion) {
      cells.forEach((cell) => cell.setAttribute('data-state', 'in'))
      return
    }
    cells.forEach((cell, index) => {
      timers.push(window.setTimeout(() => cell.setAttribute('data-state', 'in'), index * 80))
    })
  }
  if (props.start || !motion) begin()

  window.addEventListener('pointermove', onPointerMove, { passive: true })
  window.addEventListener('pointerdown', onPointerDown, { passive: true })
  window.addEventListener('scroll', onScroll, { passive: true, capture: true })
  window.addEventListener('resize', onScroll, { passive: true })
  document.addEventListener('pointerout', onPointerOut)
  wake()

  destroy = () => {
    cancelAnimationFrame(raf)
    timers.forEach((id) => window.clearTimeout(id))
    resizeObserver.disconnect()
    visibility.disconnect()
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerdown', onPointerDown)
    window.removeEventListener('scroll', onScroll, { capture: true })
    window.removeEventListener('resize', onScroll)
    document.removeEventListener('pointerout', onPointerOut)
  }
})

onBeforeUnmount(() => {
  destroy?.()
  destroy = null
})
</script>

<template>
  <div ref="root" class="hero-features bento">
    <div v-for="(item, i) in items" :key="item.title" class="bento-cell" :style="{ '--i': i }">
      <article class="bento-card">
        <div class="bento-head">
          <span class="feat-index">{{ String(i + 1).padStart(2, '0') }}</span>
          <span class="feat-icon"><component :is="item.icon" /></span>
        </div>
        <b class="bento-title">{{ item.title }}</b>
        <span class="feat-desc">{{ item.desc }}</span>
        <span class="bento-light" aria-hidden="true" />
        <span class="bento-rim" aria-hidden="true" />
        <span class="bento-border" aria-hidden="true" />
      </article>
    </div>
  </div>
</template>
