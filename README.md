# Binocle · 双光工地安全风险研判 Agent

> 一对 RGB-IR 双光图像进，结构化安全风险结论出：
> **像素级配准融合 → VLM + YOLO 双源检测交叉对账 → RAG 检索安全规范条款 → 风险定级与处置分流**。
> 全程结构化留痕、可回放、可人工复核——像一位 7×24 在岗、永不疲劳的安全总监。

**跨模态融合 · 双源交叉验证 · 规范条款可解释判级 · 三级处置分流 · 全链路审计 · 纯 CPU 可跑 · 开箱即用 Web 控制台**

- 语言/框架：Python 3.10+（FastAPI + LangGraph）+ Vue 3（Vite）
- 算力要求：**无需 GPU**，检测与检索均为 CPU 推理，普通办公机即可部署
- 外部依赖：任一 OpenAI 兼容模型 API（默认 DeepSeek，key 自备）

---

## 1. 核心能力

| 能力 | 说明 |
|---|---|
| **RGB-IR 像素级配准融合** | 可见光与红外自动对齐、融合成图，夜间/烟尘/逆光场景不漏检 |
| **双源检测交叉对账** | VLM 语义理解与 YOLO 三类检测（工人/机械/塔吊）独立跑、交叉验证，单源误报/漏报被对账环节拦截 |
| **规范条款可解释判级** | RAG 混合检索（BGE 向量 + BM25 + 精排）内置 9 部 JGJ/GB 国家/行业规范，每条风险都带条款依据，不是黑盒打分 |
| **三级处置分流** | 高风险强制告警 / 低风险可选提示 / 安全归档；系统判不了的自动进**人工待审队列**——宁可转人工，不出假安全 |
| **规范问答与任务对话** | 内置规范知识问答（RAG 检索条款原文）与多轮对话工作区（可绑定当前任务），研判过程中随时追问依据 |
| **全链路审计留痕** | 每个任务独立状态快照（配准质量、双源分歧、条款命中、判级依据），可回放可追责 |
| **双形态控制台** | Vue3 控制台（落地页 + 安全日志生成 / 工作总览 / 人工复核 / 档案管理四页 + 对话工作区）+ 零依赖只读控制台（批次评估 / 真值集 / 记忆沉淀），后者无需 Node 即可部署 |

## 2. 系统架构

```text
                    ┌──────────────────────────────────────────────┐
                    │  Vue3 控制台（frontend/，:5173 dev / dist 静态）│
                    │  落地页 · 安全日志 · 工作总览 · 复核 · 档案 · 对话 │
                    └───────┬───────────────────────┬──────────────┘
                            │ /tasks /health /inputs │ /api
                            │ /qa /chat              │
                            ▼                       ▼
              ┌─────────────────────────┐ ┌──────────────────────────┐
              │ agent.service:app :8000 │ │ agent.api_web:standalone │
              │ 生产任务 API（唯一入口）  │ │ :8010 只读控制台          │
              └────────────┬────────────┘ │ （另挂 /console 零依赖页） │
                           │              └──────────────────────────┘
                           ▼
              ┌─────────────────────────┐
              │  agent.graph（LangGraph │
              │  13 节点状态机，唯一编排）│
              └───────┬─────────────────┘
                      ▼
        ┌─────────────────────────────┐
        │ tools/（能力层，无状态）      │
        │ scripts/（算法层，子进程）    │
        │  libs/ultralytics（内置引擎） │
        └─────────────────────────────┘
                      ▼
        results/agent/<run>/<pair>/graph_state.json（每任务独立产物）
```

- 生产路由唯一：`agent.service → agent.graph → tools/scripts`，职责分层清晰
- 只读控制台 API（8010）不触发任何模型调用——**无需 key 也能查历史与人工队列**，适合部署给安全督查岗
- 每任务独立 `graph_state.json`，审计与回放天然隔离

> 功能明细（端到端链路、API 清单、判定语义、已知坑位）见 [`docs/项目功能清单.md`](docs/项目功能清单.md)；
> 架构权威文档见 [`docs/当前架构与入口.md`](docs/当前架构与入口.md)；
> 深入设计（AGENT × YOLO 如何结合、双源对账、判级链路、全部升级点）见 [`AGENT与YOLO融合设计.md`](AGENT与YOLO融合设计.md)。

## 3. 快速开始

### 3.1 前置要求

| 项 | 要求 |
|---|---|
| Python | ≥ 3.10 |
| Node.js | ≥ 20.19 或 ≥ 22.12（仅 Vue 前端需要；零依赖控制台可跳过） |
| 模型 API | OpenAI 兼容端点（默认 DeepSeek `api.deepseek.com`），key 自备 |
| GPU | **不需要** |
| 外网（可选） | 落地页背景影像走公网 HLS（`hls.js` 已随 `package.json` 安装）、字体走 Google Fonts、首次建 RAG 库需下载 BGE 模型；**内网/离线时自动降级为静态深绿底色与系统字体，功能不受影响** |

### 3.2 安装

```bash
git clone https://github.com/luoxiongwei/BinocleAgent.git
cd BinocleAgent
pip install -r requirements.txt
python scripts/rag_index.py   # 重建 RAG 向量库（依据 data/rag_docs/；首次自动下载 BGE 模型，约 1 GB，需联网）
cd frontend && npm install   # 可选；只用零依赖控制台可跳过
```

### 3.3 配置 API key

```bash
copy config.example.json config.json   # Windows
cp config.example.json config.json     # Linux / macOS
```

编辑 `config.json`，填入 `vlm` / `llm` 两段的 `model_id`、`api_base`、`api_key`。
`config.json` 属个人配置，已被 `.gitignore` 排除。

### 3.4 启动

Windows 一键三进程（任务服务 + 只读控制台 + Vue dev server）：

```bat
scripts\start_debug_stack.bat
```

也可以直接双击仓库根目录的 `启动Binocle安全日志Agent.bat`，停止用同目录的
`关闭Binocle安全日志Agent.bat`。启动器会自动探测可用
Python，等待 8000/8010/5173 三个服务健康后再返回；日志和 PID 记录在
`results/debug_stack/`。常用管理命令：

```bat
scripts\start_debug_stack.bat status
scripts\start_debug_stack.bat restart
scripts\start_debug_stack.bat stop
```

其他平台手动启动（三条命令，三个终端）：

```bash
python -m uvicorn agent.service:app --host 127.0.0.1 --port 8000    # 生产任务服务
python -m uvicorn agent.api_web:standalone --host 127.0.0.1 --port 8010  # 只读控制台
cd frontend && npm run dev                                          # Vue 前端
```

打开 <http://127.0.0.1:5173> 使用完整控制台；
或打开 <http://127.0.0.1:8010/console> 使用零依赖只读控制台（无需 Node）。

### 3.5 部署自检

```bash
python scripts/doctor.py
```

28 项一键体检：Python 版本、14 个依赖模块、权重/规范库/向量库等 9 类关键资产、API key 配置、端口占用。全部 PASS 即可放心启动。

> Docker 部署思路：`python:3.12-slim` 基础镜像 + `pip install -r requirements.txt`（torch 用 CPU 轮子）+
> 全量 `COPY . .`，`config.json` 挂载注入勿打进镜像，RAG 模型缓存挂卷复用。

---

## 4. 判定原则（产品承诺）

1. **有等级必有处置**：判出风险等级就落到明确处置，不存在"判了又不管"
2. **处置三档 + 人工闸口**：`强制告警` / `可选提示` / `安全归档`；系统无法自信判断的一律转人工，**绝不输出"假安全"**
3. **有危险必须告警**：规范条款是追责依据、不是告警门槛；高危场景即使未命中条款也强制告警并标注条款缺失
4. **感知链异常不装正常**：VLM 降级、零目标等感知异常自动触发人工闸口，异常状态如实上报

---

## 5. 常用命令

| 目的 | 命令 |
|---|---|
| 生产后端 | `python -m uvicorn agent.service:app --port 8000` |
| 只读控制台 | `python -m uvicorn agent.api_web:standalone --port 8010` |
| Vue 前端 | `cd frontend && npm run dev` |
| 一键三进程（Win） | `scripts\start_debug_stack.bat` |
| 部署自检 | `python scripts/doctor.py` |
| 单任务（CLI） | `python -m agent.cli --mode live --pair-id pair01` |
| 全量批跑 | `python -m agent.run_all --out-root results/agent/<run> --pairs all` |
| 重建 RAG 库 | `python scripts/rag_index.py`（更新 `data/rag_docs/` 后执行） |

---

## 6. 导入自己的图对

系统输入是**成对的 RGB 可见光 + 红外图**。两种导入方式，落盘位置都是 `data/rgbt/output/`：

**方式 1：前端上传（推荐）**——安全日志生成页 → 上传区选 RGB 与红外两张图 → 填名称
（支持中文，如「东侧作业面」）→ 点「上传图像对」。系统自动分配规范 ID（`pairNN`）、
以规范名存盘并登记中文名别名。同一编号重复上传为替换语义。

**方式 2：手动放入目录**——把文件拷到 `data/rgbt/output/`，命名规则：

- 文件名**包含编号数字**，前缀区分模态：`ir` 开头 = 红外，`rgb` 或 `vis` 开头 = 可见光
- 例：`rgb31.jpg` + `ir31.jpg` → 规范 ID 即 `pair31`
- 编号避开已占用（看目录内现有文件，或调 `GET /inputs`）
- 中文名可选：登记到 `data/pair_aliases.json` 的 `aliases` 段，之后即可用中文名指代

---

## 7. 目录结构

| 目录 | 用途 |
|---|---|
| `agent/` | 编排层：service（8000）/ api_web（8010）/ graph（LangGraph 13 节点）/ outcome / planner |
| `tools/` | 能力层（无状态）：检测、配准、融合、RAG、规则引擎、模型网关、API 配置 |
| `scripts/` | 算法层（子进程执行）：L1 检测 / VLM 评审 / LLM 定级 / RAG 建库 / 部署自检 |
| `libs/ultralytics/` | 内置 YOLOv8 引擎（定制版），L1 检测专用，开箱即用 |
| `frontend/` | Vue3 + Vite 控制台（落地页 + 安全日志/总览/复核/档案四页 + 对话工作区） |
| `web/console.html` | 零依赖只读控制台（8010 `/console` 挂载） |
| `docs/` | 项目文档：功能清单 / 架构与入口 |
| `data/` | 输入资产：L1 权重、双光图对（用户自有）、9 部规范原文、图对别名表 |
| `results/` | 运行产物（**不随仓库/交付包分发**，首次运行自动生成；RAG 库用 `scripts/rag_index.py` 重建） |
| `config/` `schemas/` | 数据配置 / JSON Schema |

---

## 8. Roadmap

- **判级汇总报表**：任务级审计快照已全量落盘，跨任务统计报表与趋势看板持续增强
- **更大规模感知模型**：L1 检测权重与 VLM 评审模型支持换装升级
- **视频流实时接入**：从静态图对走向多路摄像头实时研判
- **容器化一键部署**：官方 Docker 镜像与 docker-compose 编排

---

## 9. 适用场景

- 施工现场安全督查：夜间/烟尘场景红外补盲，高危作业（吊装、临边、用电）自动巡检
- 安全生产责任追溯：每条风险带规范条款依据与完整审计链
- 多项目集中监管：零依赖只读控制台部署于督查岗，无需模型 key
