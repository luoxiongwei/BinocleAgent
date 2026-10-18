# AGENT × YOLO 融合设计

> 本文说明 Binocle Agent 的技术全貌：它是什么、与 GDYOLO（定制 YOLOv8 三分类检测器）
> 如何结合、相对于原始检测器的全部升级工作如何进行、关键设计决策与优化点。

---

## 1. 起点：GDYOLO 的能力与边界

GDYOLO 是本项目的感知基座——一个**定制版 Ultralytics YOLOv8**：

| 项 | 内容 |
|---|---|
| 定制点 | EMO 系列骨干等自定义 nn 模块（`libs/ultralytics/nn/modules/CoreV8/`） |
| 类别 | 3 类：`worker` 工人 / `construction machinery` 机械 / `tower crane` 塔吊 |
| 权重 | `data/weights/best.pt`（pickle 序列化，`libs/ultralytics` 引擎不可与权重分离） |
| 输入输出 | 单张图像 → 检测框（bbox + 类别 + 置信度） |

**能力边界**：它只能回答"图里有什么"，回答不了安全监管真正关心的四个问题——危不危险？违反哪条规范？该告警还是记录？证据链是否可追责？

本项目的全部工作，就是把这四个问题接上，同时不丢原有检测能力。

---

## 2. 升级总体思路

```text
GDYOLO（单图检测器）                     Binocle Agent（双光风险研判）
─────────────────────                    ────────────────────────────
RGB 图 ──► YOLO ──► 框                   双光图对 ──► 配准融合 ──► 双源检测（VLM+YOLO）──► 对账
                                            │                              │
                                            │                              ▼
                                            │                         定向重感知（分歧细查）
                                            ▼                              ▼
                                          无判级              规则引擎 + RAG 规范检索 + LLM 定级
                                                                          │
                                                                          ▼
                                                              三级处置 + 人工闸口 + 全程审计
```

四层扩展，每层都可独立验证：

| 层 | 升级内容 | 载体 |
|---|---|---|
| ① 感知扩展 | 一张双源融合图 → RGB-IR 一对图对，自动配准、融合 | `tools/image_registration.py` |
| ② 检测扩展 | GDYOLO → VLM 开放检测 + GDYOLO 校验，双源对账 | `tools/reconcile.py` + `scripts/vlm_judge.py` |
| ③ 判级扩展 | 无 → 规则引擎 + RAG 检索（9 部 JGJ/GB）+ LLM 定级 | `tools/rule_engine.py` `tools/rag_tool.py` `scripts/llm_judge.py` |
| ④ 形态扩展 | python脚本 → Agent（具体为LangGraph 13 节点状态机 + 服务 + 控制台 + 审计） | `agent/graph.py` `agent/service.py` `frontend/` |

---

## 3. 与 YOLO 的结合点（核心）

GDYOLO 没有被替换、也没有被神化——它从"唯一检测者"变成**双源校验体系中的事实底座**。

### 3.1 权重与引擎的绑定关系

`best.pt` 以 pickle 保存了完整模型对象，反序列化时必须能 import 到定制 nn 模块
（`ultralytics.nn.tasks.DetectionModel` + GDYOLO 骨干类）。因此：

- `libs/ultralytics/` 作为运行时必需依赖随仓分发，**不可删除、不可替换**；
- `scripts/l1_smoke.py` 入口用 `sys.path.insert(0, BASE/"libs")` 注入，脱离原项目独立运行；
- torch ≥ 2.6 起 `torch.load` 默认 `weights_only=True` 会拒绝反序列化自定义类，入口处打了兼容补丁（`l1_smoke.py:20`，权重来源可信的自有资产）。

### 3.2 YOLO 以工具身份被 Agent 调用

编排层（LangGraph）不直接 import torch/YOLO，而是通过 `agent/tools_registry.py`
以**子进程/工具**方式调用算法层脚本：

```text
graph 节点 detect ──► tools_registry ──subprocess──► scripts/l1_smoke.py
                                                        --source  <输入目录>
                                                        --weights data/weights/best.pt
                                                        --out     <run>/l1/
                                                        ──► l1_detections.json
```

子进程隔离的工程收益：torch/cv2 运行时与 Web 服务进程解耦（不会互相拖垮）；
算法可独立重跑（见 3.5）；算法层替换不影响编排层。

### 3.3 双源检测与对账：YOLO 的角色转变

这是本项目最重要的架构决策之一。

**问题**：单源检测有系统性盲区——YOLO 三类之外的风险源（未系安全绳、临边防护缺失、作业区闯入）它根本不检测；而纯 VLM 检测存在幻觉与计数不稳。

**方案**：VLM 先行开放检测，GDYOLO 后跑做**目标级校验**：

| 角色 | 承担者 | 职责 |
|---|---|---|
| 开放检测 | VLM | 自行枚举全部目标与场景要素，不受类别表限制 |
| 事实底座 | GDYOLO | 为每个检测框提供权威 bbox/类别/置信度，锚定 VLM 的枚举 |

VLM 输入融合灰度图（检测同画布）+真彩色可见光图（PPE 颜色判定的唯一证据源）+ 红外图（遮挡/夜间人员存在性交叉确认）；
prompt 只要求描述事实与关系，不下违规结论。

**对账**（`tools/reconcile.py`）把两源结果逐目标比对：

- bbox_id 匹配与 IoU 阈值判定互相确认；
- `supplement`：VLM 报告而 YOLO 未检出的目标进入补漏流程；
- `unverified_by_yolo`：无 YOLO 支撑的 VLM 目标被显式标记，进入证据降级；
- 分歧枚举：`vlm_only` / `yolo_only` / `unadjudicated_conflict` 等；
- **硬下界规则**：最终风险等级不得低于规则引擎判定，只能抬不能降。

### 3.4 定向重感知 `recrop_verify`

对账发现分歧不等于终点。`recrop_verify`（`vlm_judge.py --recrop`）支持对
**指定目标裁剪放大 2.5× 重查指定字段**（ppe / body_equipment / equipment_state）：

```text
分歧目标 ──► 裁剪放大重查 ──► 重算对账（conflicts_after）──► recrop/l2_scene.json
                                                              （_meta.recrop 留 before/after 对比）
```

这让"感知不确定"有机会在不转人工的前提下自行解决；解决不了才进人工闸口。

### 3.5 单步重跑 `rerun_step`

`vlm / yolo / reconcile` 任一步可单独重跑，产物落 `_rerun/<step>/`，**不污染基线**。VLM 偶发解析失败、模型临时不可用时，系统自己恢复而不是甩锅给人工。

---

## 4. Agent工作流

生产路由唯一：`agent.service → agent.graph → tools/scripts`。
13 个节点与关键路由：

```text
START → qc →(ok)→ perceive →(ok)→ detect → reconcile ─┬─→ verify_equipment → judge
                        │                │            └─(reconcile_failed)→ plan ─┐
                        ▼                ▼                                     │
                   human_gate       human_gate                                  │
                        │                                                       │
                        └──────────────► finalize ◄─────────────────────────────┘
                                          ▲          ▲
   judge ─┬─ archive ────────────────────┘          │
         ├─ manual ──► plan（规划器启用时）─────────┤
         ├─ tip ────► plan ────────────────────────┤
         └─ full ───► plan ────────────────────────┘
```

| 节点 | 职责 |
|---|---|
| `qc` | 输入质检：图对存在性、格式、尺寸 |
| `perceive` | 配准 + 融合（产出 fused/panels 与 QC 指标） |
| `detect` | 调 GDYOLO（子进程，见 3.2） |
| `reconcile` | 双源对账（见 3.3） |
| `verify_equipment` | 设备状态/ PPE 定向核验 |
| `judge` | 规则引擎 + RAG + LLM 定级 |
| `archive` / `tip` / `full` | 三档处置的执行分支（安全归档 / 可选提示 / 强制告警） |
| `manual` | 判不了：原因可定位的失败出口 |
| `plan` | 有界 ReAct 规划器：调 `chain_health` / `inspect_reconcile` / `recrop_verify` / `rerun_step` 自救 |
| `human_gate` | 人工闸口：任何阶段感知异常一律进此，**不输出任何风险等级** |
| `finalize` | 写 `graph_state.json`、事件流、任务状态落盘 |

**规划器门禁**（`agent/planner.py`）：`finalize_plan` 在存在未裁决分歧（`_open_conflicts` 非空）且未调用过 `recrop_verify` 时会被拒绝——系统不能带着未裁决的证据分歧定级。

**状态持久化**：`checkpoints.sqlite`（LangGraph SqliteSaver）保证服务重启后运行中任务不丢，可回放（`agent.cli --mode replay`）。

---

## 5. 安全与风险判级

```text
对账后的场景 JSON ──┬─► rule_engine ──► 条款命中 + 硬下界等级
                    ├─► rag_tool ────► BGE 向量 + BM25 → RRF 融合 → cross-encoder 精排
                    │                     （results/rag_store_v2，9 部 JGJ/GB 规范切块）
                    └─► llm_judge ───► 汇总三源证据 → 符号化等级（安全/低风险/高风险）
```

- **规则引擎**是追责锚点：条款 ID 是判级的依据而非装饰；高危场景即使未命中条款
  也强制告警并记 `clause_support_missing`；
- **RAG 混合检索**让判级引用具体规范条文（向量语义 + 关键词双路召回，RRF 融合，
  cross-encoder 精排），不是黑盒打分；
- **LLM 定级**输出符号化结果（枚举等级 + 原因码），可统计、可审计。

### 终态三分类（`agent/outcome.py`）

| 终态 | 判据 | 处置 |
|---|---|---|
| `judgement_success` | 得到确定等级 | `P1_auto_alert` / `TIP_optional` / `P4_archive` |
| `judgement_failed` | 判不了但**原因可定位** | `P3_failed_closed`（自主结案，进抽样复核） |
| `undecidable` | 既无等级又定位不到原因 | `P3_manual_judge`（转人工） |

`FORCE_MANUAL = {"state_save_failed"}`：结果完整性受损时，即便能归因也不得自主结案。
可定位失败的原因码（`REASON_TABLE`）覆盖感知链失败、证据缺口/分歧未裁决、
模型自评三类共 20+ 项——**"判不了"本身是被设计和枚举的状态，不是异常**。

---

## 6. 处置与审计

- 处置三档 + 人工闸口（上文）；人工队列 `pending_reviews.json`，复核回写 `human_archives.json`；
- 每任务独立 `results/agent/<run>/<pair>/graph_state.json` 状态快照；
- 运行期事件流 `events.jsonl`（前端实时时间线消费）；
- 审计链完整：输入 → 配准 QC → 双源原始结果 → 对账分歧 → recrop before/after →
  条款命中 → 判级依据 → 处置动作，可回放可追责。

---

## 7. 工程优化清单（相对原始检测器的全部优化）

| 类别 | 优化 |
|---|---|
| 架构 | 三层分工（`scripts/` 算法子进程 → `tools/` 无状态能力 → `agent/` 编排）；单一生产路由，`runner` 仅 replay 兼容 |
| 感知 | 双光配准融合（替代单图输入）；三图 VLM 输入原则；融合配方按训练域反查标定 |
| 检测 | 双源交叉对账替代单源直出；分歧定向重感知；单步重跑自愈 |
| 判级 | 从"无判级"到 规则硬下界 + RAG 条款可解释 + LLM 符号化输出 |
| 可靠性 | 终态三分类 + 原因码枚举 + 人工闸口一等化 + `state_save_failed` 强制人工 |
| 工程 | CPU 推理（无 GPU 依赖）；HF 模型缓存路径可移植（`GDYOLO_HF_HOME`）；`requirements.txt` 按实测核定；`doctor.py` 28 项部署自检 |
| 服务 | FastAPI 双服务（8000 任务 / 8010 只读）；SqliteSaver 持久化；断线重连、任务流式事件 |
| 前端 | Vue3 四页控制台（生成/总览/复核/档案）+ 零依赖只读控制台（`web/console.html`，无 Node 可部署）；待机态舞台、实时时间线、隐藏式日历 |
| 数据层 | 图对规范 ID `pairNN`（管道基石）+ 中文别名层 + 上传替换语义 |
| 交付 | 交付边界收窄为"代码 + 引擎 + 权重 + 规范"，其余接收方自备（见 README） |

---

## 8. 一次任务的完整旅程

```text
① 上传双光图对（data/rgbt/output/，规范名 rgbNN/irNN）
② POST /tasks → service 创建 run，LangGraph START
③ qc          图对完整性检查（失败 → human_gate）
④ perceive    配准 + 融合 → results/fused, results/panels, fuse_qc.json
⑤ detect      GDYOLO 子进程 → l1_detections.json
               VLM 三图开放检测 → l2_scene.json（对账前）
⑥ reconcile   双源对账 → 分歧清单（supplement / vlm_only / yolo_only…）
               reconcile_failed → plan（规划器自救）
⑦ verify_equipment  设备状态核验
⑧ judge       规则引擎 + RAG + LLM → 风险等级 + 条款依据
               manual → plan：recrop_verify 定向重感知后再判
⑨ archive/tip/full/manual/human_gate → finalize
⑩ finalize    graph_state.json + events.jsonl + 任务状态落盘
              前端实时时间线展示，人工队列可复核回写
```

---

## 9. 代码地图

| 功能 | 位置 |
|---|---|
| 编排状态机 | `agent/graph.py`（13 节点） |
| 规划器与门禁 | `agent/planner.py` |
| 终态分类与处置映射 | `agent/outcome.py` |
| 工具注册与子进程编排 | `agent/tools_registry.py` |
| 任务 API / 只读 API | `agent/service.py` / `agent/api_web.py` |
| GDYOLO 调用入口 | `scripts/l1_smoke.py`（`libs/` 注入 + torch 兼容补丁） |
| VLM 检测 / 重感知 | `scripts/vlm_judge.py` |
| LLM 定级 | `scripts/llm_judge.py` |
| RAG 建库 | `scripts/rag_index.py` |
| 配准融合 | `tools/image_registration.py` `tools/fusion_tool.py` |
| 双源对账 | `tools/reconcile.py` |
| 规则引擎 / RAG 检索 | `tools/rule_engine.py` `tools/rag_tool.py` |
| 检测能力封装 | `tools/detection_tool.py` |
| 模型网关（key 管理） | `tools/model_gateway.py` |
