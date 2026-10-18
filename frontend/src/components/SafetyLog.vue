<script setup>
// 安全日志：把终态快照渲染为一份可读的研判日志文档（结论先行 → 过程佐证）。
// 数据源与 EvidenceChain 相同（任务快照的 evidence 子对象或扁平图对详情）。
import { computed } from 'vue'
import { dispositionOf, fmtDateTime, pairLabel, riskTone } from '../api.js'
import DispositionTag from './DispositionTag.vue'
import RiskTag from './RiskTag.vue'
import { IconAlert, IconCheck, IconReview } from './icons.js'

const props = defineProps({
  detail: { type: Object, default: () => ({}) },
})

// 统一取字段：任务快照在 .evidence 下，图对详情为扁平
const d = computed(() => {
  const raw = props.detail || {}
  return raw.evidence && Object.keys(raw.evidence).length ? { ...raw, ...raw.evidence } : raw
})

const report = computed(() => d.value.report || {})
const warnings = computed(() => report.value.warnings || d.value.warnings || [])
const evidenceCheck = computed(() => d.value.evidence_check || null)
const risk = computed(() => d.value.risk_assessment?.overall_risk || report.value.overall_risk || d.value.overall_risk || '')
const disposition = computed(() => d.value.disposition || '')
const verdict = computed(() => d.value.human_verdict || null)
const meta = computed(() => report.value._meta || {})

// 播报文本：优先 warning_text，其次按 warnings 组装
const broadcast = computed(() => {
  if (report.value.warning_text) return report.value.warning_text
  if (!warnings.value.length) return ''
  return warnings.value.map((w, i) => `${i + 1}）${w.violation}`).join(' ')
})

const checkTone = computed(() => {
  if (!evidenceCheck.value) return 'skip'
  return evidenceCheck.value.valid === false ? 'error' : 'ok'
})

// 运行中任务：文档随事件流渐进生成，结论带如实显示「进行中」而非「未知」
const isRunning = computed(() => ['queued', 'running'].includes(String(d.value.status || '')))

// 判级报告缺失（规划器直接收尾且补跑失败 / 已转人工）：五大内容区块全不渲染时，
// 如实说明原因，不留空白谜团（2026-10-17 graph-pair42 空壳日志用户报障）
const reportMissing = computed(() => {
  const r = report.value
  if (isRunning.value) return false
  return !(r.work_description || r.situation_assessment || r.clause_analysis
           || r.warning_text || (r.warnings || []).length)
})
// 转人工路由（manual_route）：报告缺失是设计形态（二值化铁律：判不了不输出等级），
// 与「规划器直接收尾但规则引擎有等级」的降级缺失区分文案
const manualRoute = computed(() => (report.value._meta || {}).manual_route === true)
</script>

<template>
  <article v-if="d.task_id || report.work_description" class="panel safety-log">
    <!-- 文档头：结论先行 -->
    <header class="safety-log-head">
      <div class="grow">
        <span class="eyebrow">SAFETY LOG · 安全日志</span>
        <h3 class="safety-log-title">{{ pairLabel(d.pair_id) }} 安全研判日志</h3>
        <div class="row row-wrap" style="gap: 6px; margin-top: 8px">
          <span class="tag-mono">{{ d.task_id }}</span>
          <DispositionTag :code="disposition" />
          <span v-if="risk" class="pill pill-muted">风险 <RiskTag :value="risk" /></span>
          <span v-if="d.route" class="pill pill-muted">路由 <b class="mono">{{ d.route }}</b></span>
          <span v-if="meta.generated_at" class="faint text-small">{{ fmtDateTime(meta.generated_at) }}</span>
        </div>
      </div>
      <span v-if="d.need_human_review" class="pill pill-manual"><span class="dot" />需人工裁定</span>
    </header>

    <!-- 结论带 -->
    <div v-if="isRunning" class="safety-log-verdict is-running">
      <span class="status-dot busy" />
      <span>研判进行中</span>
      <span class="faint text-small">—— 文档随执行事件流实时生成，结论待任务完成</span>
    </div>
    <div v-else class="safety-log-verdict" :class="disposition === 'P3_manual_judge' ? 'is-manual' : dispositionOf(disposition).tone">
      <IconAlert v-if="disposition === 'P1_auto_alert'" />
      <IconReview v-else-if="disposition === 'P3_manual_judge'" />
      <IconCheck v-else />
      <span>{{ dispositionOf(disposition).label }}</span>
      <span class="faint text-small">—— {{ risk ? `风险等级：${risk}` : '风险等级：未判定' }}</span>
    </div>

    <div class="safety-log-body">
      <!-- 判级报告缺失说明：转人工任务不生成判级报告（设计形态）；
           规划器直接收尾且补跑失败时如实告知降级，不留空白谜团 -->
      <section v-if="reportMissing" class="log-section">
        <h4 class="log-section-title">判级说明</h4>
        <p class="field-hint" style="margin: 0">
          <template v-if="manualRoute">
            本任务已转人工裁定（{{ d.judgement?.reason || '系统未能自动定级' }}）：
            按二值口径，判不了即转人工、不输出风险等级，故不生成 LLM 判级报告。
            请在人工复核队列完成裁定，回写后此处显示最终结论。
          </template>
          <template v-else>
            本任务未生成 LLM 判级报告：感知链与规则引擎结论仍然有效，处置不受影响；
            播报文本、场景描述与条款分析缺失。规划器直接收尾的任务可在复核台请求重新研判。
          </template>
        </p>
      </section>

      <!-- 场景概述 -->
      <section v-if="report.work_description" class="log-section">
        <h4 class="log-section-title">场景概述</h4>
        <p class="log-prose">{{ report.work_description }}</p>
      </section>

      <!-- 态势评估 -->
      <section v-if="report.situation_assessment" class="log-section">
        <h4 class="log-section-title">态势评估</h4>
        <p class="log-prose">{{ report.situation_assessment }}</p>
      </section>

      <!-- 风险记录与规范条款 -->
      <section v-if="warnings.length" class="log-section">
        <h4 class="log-section-title">风险记录与规范条款 <span class="faint text-small">{{ warnings.length }} 条</span></h4>
        <div v-for="(w, i) in warnings" :key="i" class="log-warning">
          <div class="row row-wrap" style="gap: 6px">
            <span class="pill" :class="riskTone(w.risk_level) === 'danger' ? 'pill-danger' : 'pill-warn'">
              <span class="dot" />{{ w.level || '疑似' }} · {{ w.risk_level }}
            </span>
            <span v-if="w.confidence != null" class="faint text-small mono">置信 {{ w.confidence }}</span>
          </div>
          <p class="log-warning-text">{{ w.violation }}</p>
          <div v-if="w.clause_ids?.length || w.clause_sources?.length" class="clause-quote">
            <span class="clause-ids">{{ (w.clause_ids || []).join(' / ') }}</span>
            《{{ (w.clause_sources || []).join('》《') }}》
          </div>
          <div v-if="w.evidence?.length" class="row row-wrap" style="gap: 4px; margin-top: 6px">
            <span v-for="(e, j) in w.evidence" :key="j" class="tag-mono">{{ e }}</span>
          </div>
        </div>
      </section>

      <!-- 条款分析 -->
      <section v-if="report.clause_analysis" class="log-section">
        <h4 class="log-section-title">条款分析</h4>
        <p class="log-prose">{{ report.clause_analysis }}</p>
      </section>

      <!-- 播报文本 -->
      <section v-if="broadcast" class="log-section">
        <h4 class="log-section-title">播报文本</h4>
        <div class="log-broadcast">
          <span class="eyebrow">BROADCAST</span>
          <p>{{ broadcast }}</p>
        </div>
      </section>

      <!-- 证据核验 -->
      <section v-if="evidenceCheck" class="log-section">
        <h4 class="log-section-title">证据核验</h4>
        <div class="row row-wrap" style="gap: 6px">
          <span class="tag-mono" :class="checkTone === 'error' ? 'text-danger' : 'text-safe'">
            {{ evidenceCheck.valid === false ? '未通过' : '通过' }}
          </span>
          <span v-for="(r, i) in evidenceCheck.reasons || []" :key="i" class="tag-mono text-warn">{{ r }}</span>
        </div>
      </section>

      <!-- 人工结论 -->
      <section v-if="verdict" class="log-section">
        <h4 class="log-section-title">人工结论</h4>
        <p class="log-prose text-safe">
          {{ verdict.verdict }} · {{ verdict.risk_label || '未改判' }}
          <span v-if="verdict.notes" class="faint">—— {{ verdict.notes }}</span>
        </p>
      </section>
    </div>
  </article>
</template>

<style scoped>
.safety-log {
  overflow: hidden;
}
.safety-log-head {
  display: flex;
  gap: var(--sp-3);
  align-items: flex-start;
  padding: var(--sp-4);
  border-bottom: 1px solid var(--line);
  background: linear-gradient(180deg, color-mix(in oklab, var(--accent) 7%, transparent), transparent);
}
.safety-log-title {
  margin: 2px 0 0;
  font-size: var(--fs-title);
  letter-spacing: 0.01em;
}
.safety-log-verdict {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  font-weight: 600;
  border-bottom: 1px solid var(--line);
  background: color-mix(in oklab, var(--safe) 10%, transparent);
}
.safety-log-verdict.is-manual { background: color-mix(in oklab, var(--manual) 12%, transparent); }
.safety-log-verdict.is-running { background: color-mix(in oklab, var(--accent) 10%, transparent); }
.safety-log-verdict.warn { background: color-mix(in oklab, var(--warn) 12%, transparent); }
.safety-log-verdict.danger { background: color-mix(in oklab, var(--danger) 14%, transparent); }
.safety-log-verdict svg { flex: none; }
.safety-log-body {
  display: grid;
  gap: var(--sp-4);
  padding: var(--sp-4);
}
.log-section-title {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-small);
  font-weight: 700;
  letter-spacing: 0.06em;
  color: var(--text-dim);
}
.log-prose {
  margin: 0;
  line-height: 1.75;
  color: var(--text);
  white-space: pre-wrap;
}
.log-warning + .log-warning { margin-top: var(--sp-3); }
.log-warning-text { margin: 6px 0 0; line-height: 1.7; }
.log-broadcast {
  padding: var(--sp-3);
  border-left: 3px solid var(--accent);
  border-radius: 0 8px 8px 0;
  background: color-mix(in oklab, var(--accent) 8%, transparent);
}
.log-broadcast p { margin: 6px 0 0; line-height: 1.8; }
</style>
