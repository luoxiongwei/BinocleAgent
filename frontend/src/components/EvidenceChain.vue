<script setup>
// 证据链：从双光融合到规范条款的完整研判轨迹。
// 兼容两种数据源：8010 图对详情（扁平）与 8000 任务快照（evidence 子对象）。
import { computed } from 'vue'
import { toolCn } from '../api.js'

const props = defineProps({
  detail: { type: Object, default: () => ({}) },
})

// 统一取证据字段：任务快照在 .evidence 下，图对详情为扁平
const ev = computed(() => {
  const d = props.detail || {}
  return d.evidence && Object.keys(d.evidence).length ? { ...d, ...d.evidence } : d
})
// Task API may return graph_state's nested evidence shapes.
const normalizedScene = computed(() => ev.value.scene?.scene || ev.value.scene || null)
const normalizedDetections = computed(() => {
  const d = ev.value.detections || []
  return Array.isArray(d) ? d : (d.objects || [])
})
const normalizedWarnings = computed(() => ev.value.warnings || ev.value.report?.warnings || [])

const quality = computed(() => ev.value.quality || null)
const detections = normalizedDetections
const reconciliation = computed(() => ev.value.reconciliation || null)
const equipment = computed(() => ev.value.equipment_check || null)
const rule = computed(() => ev.value.rule || null)
const warnings = normalizedWarnings
const report = computed(() => ev.value.report || null)
const evidenceCheck = computed(() => ev.value.evidence_check || null)
const golden = computed(() => ev.value.golden || null)
const trace = computed(() => props.detail?.tool_calls || [])

const qualityTone = computed(() => {
  if (!quality.value) return 'skip'
  if (quality.value.flag === 'ok' || quality.value.passed) return 'ok'
  return 'review'
})

const recTone = computed(() => {
  const r = reconciliation.value
  if (!r) return 'skip'
  return r.vlm_only > 0 || r.yolo_only > 0 ? 'review' : 'ok'
})

const equipTone = computed(() => {
  const e = equipment.value
  if (!e) return 'skip'
  return e.hard_mismatch ? 'error' : 'ok'
})

const checkTone = computed(() => {
  const c = evidenceCheck.value
  if (!c) return 'skip'
  return c.valid === false ? 'error' : 'ok'
})

const clauseCount = computed(
  () => new Set(warnings.value.flatMap((w) => w.clause_ids || [])).size,
)

const riskLabel = computed(() => ev.value.risk_assessment?.overall_risk || ev.value.warning_overall_risk || '')
const disposition = computed(() => ev.value.disposition || '')
const verdict = computed(() => props.detail?.human_verdict || null)

const pct = (v) => (typeof v === 'number' ? `${Math.round(v * 100)}%` : v)
</script>

<template>
  <div class="chain">
    <!-- 执行轨迹（工具调用） -->
    <template v-if="trace.length">
      <div class="chain-node ok">
        <div class="chain-title"><span class="chain-step">TRACE</span>执行轨迹 · {{ trace.length }} 次工具调用</div>
        <div class="chain-facts">
          <span v-for="(call, i) in trace" :key="i" class="tag-mono">
            {{ toolCn(call.tool) }}<template v-if="call.status"> · {{ call.status }}</template>
          </span>
        </div>
      </div>
    </template>

    <!-- 1 双光融合配准 -->
    <div v-if="quality" class="chain-node" :class="qualityTone">
      <div class="chain-title"><span class="chain-step">01</span>双光融合配准</div>
      <div class="chain-facts">
        <span class="tag-mono">边缘重合度 {{ quality.edge_align ?? '—' }}</span>
        <span class="tag-mono">阈值 {{ quality.threshold ?? '—' }}</span>
        <span class="tag-mono" :class="qualityTone === 'ok' ? 'text-safe' : 'text-warn'">
          {{ qualityTone === 'ok' ? '通过' : '待复核' }}
        </span>
      </div>
    </div>

    <!-- 2 VLM 开放检测 -->
    <div v-if="detections.length || report" class="chain-node ok">
      <div class="chain-title"><span class="chain-step">02</span>VLM 开放检测</div>
      <div class="chain-facts">
        <span v-if="detections.length" class="tag-mono">检出目标 {{ detections.length }} 个</span>
        <span v-if="ev.scene?.site_type" class="tag-mono">{{ ev.scene.site_type }}</span>
      </div>
      <div v-if="detections.length" class="chain-detail">
        <p v-for="d in detections" :key="d.bbox_id">
          <b class="mono">{{ d.bbox_id }}</b>
          <span class="muted">（{{ d.category || '未分类' }}）</span>
          {{ d.action || d.description || '' }}
          <span v-if="d.equipment_state" class="text-warn">· 设备状态：{{ d.equipment_state }}</span>
          <span v-if="d.occluded" class="faint">· 有遮挡</span>
        </p>
      </div>
    </div>

    <!-- 3 双源对账 -->
    <div v-if="reconciliation" class="chain-node" :class="recTone">
      <div class="chain-title"><span class="chain-step">03</span>双源对账（VLM × YOLO）</div>
      <div class="chain-facts">
        <span class="tag-mono">VLM {{ reconciliation.n_vlm }}</span>
        <span class="tag-mono">YOLO {{ reconciliation.n_yolo }}</span>
        <span class="tag-mono">匹配 {{ reconciliation.matched }}</span>
        <span v-if="reconciliation.vlm_only" class="tag-mono text-warn">仅 VLM {{ reconciliation.vlm_only }}</span>
        <span v-if="reconciliation.yolo_only" class="tag-mono text-warn">仅 YOLO {{ reconciliation.yolo_only }}</span>
      </div>
      <div v-if="reconciliation.vlm_only || reconciliation.yolo_only" class="chain-detail">
        <p class="text-warn">存在单侧检出目标 —— 系统要求对未匹配目标做位置对账，不得直接采信单侧结论。</p>
      </div>
    </div>

    <!-- 4 设备状态校验 -->
    <div v-if="equipment" class="chain-node" :class="equipTone">
      <div class="chain-title"><span class="chain-step">04</span>设备状态校验</div>
      <template v-if="equipment.equipments?.length">
        <div class="chain-facts">
          <span
            v-for="eq in equipment.equipments"
            :key="eq.bbox_id"
            class="tag-mono"
            :class="eq.consistent === false ? 'text-danger' : ''"
          >
            {{ eq.bbox_id }} · VLM「{{ eq.vlm_state || '不确定' }}」{{
              eq.yolo_matched ? ' · YOLO 佐证' : ' · 无 YOLO 佐证'
            }}
          </span>
        </div>
      </template>
      <div v-else class="chain-facts"><span class="tag-mono">未记录在册设备</span></div>
      <div v-if="equipment.hard_mismatch" class="chain-detail text-danger">
        <p>存在硬性不一致：VLM 判定设备运行但无任何几何佐证 —— 按规则不得据此定级。</p>
      </div>
    </div>

    <!-- 5 规则引擎研判 -->
    <div v-if="rule" class="chain-node ok">
      <div class="chain-title"><span class="chain-step">05</span>规则引擎研判（确定性下界）</div>
      <div class="chain-facts">
        <span class="tag-mono">作业危险度 {{ rule.work_hazard?.level || '—' }}</span>
        <span class="tag-mono">监护 {{ rule.supervision?.solo ? '单人作业' : '有监护' }}</span>
        <span class="tag-mono">人员 {{ rule.protection?.n_workers ?? rule.supervision?.n_workers ?? '—' }} 人</span>
        <span v-if="rule.protection?.missing_critical?.length" class="tag-mono text-danger">
          缺失关键防护：{{ rule.protection.missing_critical.join('、') }}
        </span>
        <span v-if="rule.protection?.unknown_fields" class="tag-mono">未知字段 {{ rule.protection.unknown_fields }}</span>
      </div>
      <div v-if="rule.drivers?.length" class="chain-detail">
        <p>命中规则：</p>
        <p v-for="(drv, i) in rule.drivers" :key="i" class="mono" style="margin: 0 0 2px">{{ drv }}</p>
      </div>
      <div v-if="rule.evidence_gaps?.length" class="chain-detail">
        <p class="text-warn">证据缺口：{{ rule.evidence_gaps.join('；') }}</p>
      </div>
    </div>

    <!-- 6 规范条款（RAG） -->
    <div v-if="warnings.length" class="chain-node ok">
      <div class="chain-title"><span class="chain-step">06</span>规范条款 · RAG 检索命中 {{ clauseCount }} 条</div>
      <div v-for="(w, i) in warnings" :key="i" style="margin-top: 8px">
        <div class="row row-wrap" style="gap: 6px">
          <span class="pill" :class="w.risk_level === '高风险' ? 'pill-danger' : 'pill-warn'">
            <span class="dot" />{{ w.level || '疑似' }} · {{ w.risk_level }}
          </span>
        </div>
        <div class="clause-quote">
          {{ w.violation }}
          <span v-if="w.clause_sources?.length" class="clause-src">
            <span class="clause-ids">{{ (w.clause_ids || []).join(' / ') }}</span>
            《{{ w.clause_sources.join('》《') }}》
          </span>
          <div v-if="w.evidence?.length" style="margin-top: 6px">
            <span v-for="(e, j) in w.evidence" :key="j" class="tag-mono" style="margin: 0 4px 4px 0">{{ e }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 7 判级报告 -->
    <div v-if="report" class="chain-node ok">
      <div class="chain-title"><span class="chain-step">07</span>判级报告（LLM + 规范）</div>
      <details v-if="report.work_description" class="json-fold" style="border-top: 0">
        <summary>场景描述</summary>
        <pre style="padding: 0 0 8px; white-space: pre-wrap">{{ report.work_description }}</pre>
      </details>
      <details v-if="report.situation_assessment" class="json-fold" style="border-top: 0">
        <summary>态势评估</summary>
        <pre style="padding: 0 0 8px; white-space: pre-wrap">{{ report.situation_assessment }}</pre>
      </details>
      <details v-if="report.clause_analysis" class="json-fold" style="border-top: 0">
        <summary>条款分析</summary>
        <pre style="padding: 0 0 8px; white-space: pre-wrap">{{ report.clause_analysis }}</pre>
      </details>
    </div>

    <!-- 8 证据核验 -->
    <div v-if="evidenceCheck" class="chain-node" :class="checkTone">
      <div class="chain-title"><span class="chain-step">08</span>证据核验</div>
      <div class="chain-facts">
        <span class="tag-mono" :class="checkTone === 'error' ? 'text-danger' : 'text-safe'">
          {{ evidenceCheck.valid === false ? '未通过' : '通过' }}
        </span>
        <span v-for="(r, i) in evidenceCheck.reasons || []" :key="i" class="tag-mono text-warn">{{ r }}</span>
      </div>
    </div>

    <!-- 9 结论与处置（仅在已有判级结论时渲染，避免中间态出现空壳节点） -->
    <div v-if="disposition || riskLabel || verdict" class="chain-node" :class="disposition === 'P3_manual_judge' ? 'review' : 'ok'">
      <div class="chain-title"><span class="chain-step">09</span>结论与处置</div>
      <div class="chain-facts">
        <span v-if="riskLabel" class="tag-mono">风险 {{ riskLabel }}</span>
        <span v-if="disposition" class="tag-mono">{{ disposition }}</span>
        <span v-if="ev.need_human_review" class="tag-mono text-manual">需人工裁定</span>
      </div>
      <div v-if="verdict" class="chain-detail">
        <p class="text-safe">
          人工结论：{{ verdict.verdict }} · {{ verdict.risk_label || '未改判' }}
          <span v-if="verdict.notes" class="faint">—— {{ verdict.notes }}</span>
        </p>
      </div>
    </div>

    <!-- 专家真值对照 -->
    <div v-if="golden" class="chain-node ok">
      <div class="chain-title"><span class="chain-step">REF</span>专家真值（金标）</div>
      <div class="chain-facts">
        <span class="tag-mono">场景：{{ golden.scene_label || '—' }}</span>
        <span class="pill pill-gold">专家：{{ golden.expert_risk || '—' }} · {{ golden.action || '—' }}</span>
      </div>
      <div v-if="golden.notes" class="chain-detail"><p>{{ golden.notes }}</p></div>
    </div>
  </div>
</template>
