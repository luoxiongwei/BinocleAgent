<script setup>
import { IconActivity, IconCheck, IconImage, IconPlay, IconShield } from './icons.js'

defineEmits(['start'])
</script>

<template>
  <main class="landing-page">
    <div class="landing-orbit landing-orbit-a" />
    <div class="landing-orbit landing-orbit-b" />
    <header class="landing-header">
      <div class="landing-brand"><span class="landing-mark"><IconShield /></span><span>Binocle安全日志Agent</span></div>
      <span class="landing-status"><span class="status-dot on" />双光输入 · 自动研判 · 可追溯归档</span>
    </header>

    <section class="landing-hero">
      <div class="landing-copy">
        <p class="landing-kicker">BINOCLE SAFETY LOG AGENT</p>
        <h1 class="landing-slogan">警示危险<br /><em>拥抱安全</em></h1>
        <h2>让图像说出危险，将危险记成日志</h2>
        <p class="landing-lead">Binocle 是专门生成安全日志的智能体：看见双光图像中的风险，完成研判、处置与归档，让每一次安全判断都有证据可查。</p>
        <button class="landing-cta" @click="$emit('start')"><IconPlay />开始日志</button>
        <p class="landing-note"><IconCheck />任务失败会保留原因、重试轨迹和人工归档，不会静默丢失。</p>
      </div>
      <div class="landing-visual" aria-label="双光安全日志流程示意">
        <div class="lens lens-rgb"><IconImage /><span>RGB</span></div>
        <div class="lens lens-ir"><IconActivity /><span>IR</span></div>
        <div class="beam beam-one" /><div class="beam beam-two" />
        <div class="landing-core"><IconShield /><strong>LOG</strong><small>安全日志</small></div>
      </div>
    </section>

    <section class="landing-features">
      <article><IconImage /><div><b>双光输入</b><span>上传 RGB 与红外图对，支持中文命名与原图留痕。</span></div></article>
      <article><IconActivity /><div><b>自主处置</b><span>Agent 根据质量、识别和对账结果选择重试、规划或转人工。</span></div></article>
      <article><IconCheck /><div><b>安全归档</b><span>风险结论、证据链、工具事件和人工回写统一保存。</span></div></article>
    </section>
  </main>
</template>
