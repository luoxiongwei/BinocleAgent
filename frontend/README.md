# Binocle 安全日志 Agent · 控制台前端

Vue 3 + Vite 单页应用，系统的**主控制台**（非调试台）：安全日志生成 / 工作总览 / 人工复核 / 档案管理四页。
完整项目说明（架构、部署、交付）见根目录 `../README.md`。

## 启动

```bash
cd frontend
npm install
npm run dev        # http://127.0.0.1:5173
```

后端未启动时前端可打开但无数据；先按根 README 2.4 起 8000/8010 两个服务。

Vite 代理（`vite.config.js`，本地自动分流）：

- `/api/*` → `http://127.0.0.1:8010`（只读验收控制台：总览、图对、人工队列、归档）
- `/tasks/*`、`/health`、`/inputs`、`/qa`、`/chat` → `http://127.0.0.1:8000`（任务执行服务）

## 页面能力（当前）

- **落地页**：入口页，跳转安全日志
- **安全日志生成**：双 Tab（`TaskWorkspace.vue` 承载，与档案管理同款 Tab 手势），Tab 组在视口内垂直居中、内容超一屏时自动从顶部续排
  - **智能问答**（默认）：多轮对话（session 绑定任务）；对话里发起的研判任务会自动切到「研判任务」并加载该任务
  - **研判任务**：上传双光图对（支持中文命名/替换上传）→ 运行流水线 → 实时事件流 → 安全日志 + 证据链
  - 待机与运行态共用同一容器，工作态可通过标题栏「发起新任务」一键回待机
- **工作总览**：KPI 4 卡（待人工复核/强制告警/研判完成/在线研判）、处置分布、**经验记忆**（累计反馈 / 判定准确率 / 已沉淀案例 / 校准候选 + 最近验收案例，数据来自 `:8000` 的 `/feedback/summary`、`/feedback/candidates`、`/memory/cases`）、最近研判任务（点击在安全日志页打开）、离线人工归档；
  **不展示验收批次**（金标集只覆盖 30 对实验数据，对交付受众不可达——2026-10-19 决策）
- **人工复核**：待审队列按日期分组，队列头右侧的日期按钮打开隐藏式月历（与档案管理同款 `CalendarPicker`）：选日只显示当天条目、再点一次取消；**选中无条目的日期显式提示「该日无档案记录」**并提供「查看全部日期」退回。可接受 / 修改 / 驳回并回写风险等级
- **档案管理**：双 Tab（实时任务流 / 历史日志），统一时间轴（批次 / 任务 / 待审按本地日期归并），日期头隐藏式日历选日，双栏（当日列表 + 详情）

## 构建

```bash
npm run build     # 产物在 frontend/dist/（约 150 KB JS + 34 KB CSS）
```

## 技术栈

Vue 3.5.43 + Vite 8.3.4 + @vitejs/plugin-vue 6.0.10（版本已在 `package.json` 锁定）。
零依赖只读控制台见 `../web/console.html`（8010 `/console` 挂载，无需 Node）。

## 视觉系统（2026-10-23 重构）

方向：**夜视监视器 / 安全监督台** —— 近黑底 + 薄荷绿信号色 + 图纸细网格 + 液体玻璃铭牌。

| 令牌 | 值 | 用途 |
|---|---|---|
| `--bg` / `--bg-deep` | `#070b0a` / `#050807` | 画布 / 输入框底 |
| `--surface` 系列 | `#0c1211` → `#17201d` | 面板、悬停、内嵌块 |
| `--line` / `--line-strong` | `rgba(255,255,255,.09)` / `.18` | 全部发丝线（不用实色灰） |
| `--accent` | `#5ed29c` | 主色：按钮、激活态、批注 |
| `--danger/-warn/-safe/-manual` | `#f4736a` / `#e9b55c` / `#5ed29c` / `#86b6f0` | 四通道处置语义色 |
| `--font-display` | Instrument Serif → Noto Serif SC | 区块标题、KPI 数字 |
| `--font-body` | Inter → PingFang SC / 微软雅黑 | 正文与界面 |

落地页（`LandingView.vue` + `landing.css`）：Mux HLS 视频背景（`hls.js`，`enableWorker:false`，
懒加载独立 chunk，离线自动退回静态深绿辉光底）+ 顶部椭圆辉光（`feGaussianBlur 25`）+
owl 品牌标（浅色底衬，原图深色主体）+ 主标语衬线排版 + 底部四张能力卡 + 移动端全屏菜单。
导航项直接进入工作区（`navigate` 事件），无需回退到按钮。

排版分层（同一字号只对应一个字族）：11.5/12 mono · 13 衬线斜体 · 14 无衬线 · 15–16 无衬线 ·
18 衬线 700 · 26 无衬线 · 44–78 衬线 900。详见 `src/landing.css` 顶部注释。

粒子层 `SwarmCursor.vue`：React Bits 的 SwarmCursor 移植为 Vue 3（WebGL2 流体场 + 实例化绘制，
逻辑逐行保留，仅把 `useEffect([])` 改为 `onMounted`、`settingsRef` 改为 `computed`）。
作为落地页背景层挂载（`.hero-swarm`，z-index −1，位于影像之上、内容之下）：跟随光标聚散、
悬停链接/按钮时绕行、点击爆散；粒子数按视口降载（520 / 330 / 190），`prefers-reduced-motion`
下只静态绘制一帧。仅支持 WebGL2，不支持时静默空转，不影响页面其它部分。

能力块交互层 `BentoGrid.vue`：移植 React Bits「Magic Bento」的交互部分（**不含**其 6 套内置
artwork 与 @hugeicons 依赖）。四张能力卡共享一个全局光斑坐标，换算到每张卡后分别点亮：
① 跨卡接力的 1px 描边光（`mask-composite` 只留边框）② 卡内聚光 ③ 点击波纹环扫过全网格
④ 错峰入场 + 5px 磁吸（不做 3D 倾斜，避免中文正文发虚）。`prefers-reduced-motion` 下全部关闭。

**进场时序**（`LandingView` 的两级闸门）：影像首帧 `playing`（或 2400ms 兜底 / HLS 致命错误）→
影像淡入 0→60%（700ms）→ 辉光 + 文案上浮 → **再等 320ms 才放行粒子**（`swarmOn`）→ 能力卡 80ms/张错峰。
两级是为了避免「背景还没出、光晕粒子先冒出来」的破相；等待期间由 `.hero-media` 的深绿玻璃底盘 +
基础辉光（opacity .28）顶着，不会显示成纯黑。HLS 致命错误时立即认定降级底盘，不必干等 2.4s。

工作区背景层 `GhostFibers.vue`：React Bits 的 GhostFibers 移植为 Vue 3（真三维丝带 + 5 级 bloom 降/升采样 +
景深散景 + 浮尘，WebGL2）。**全站只挂一层**（`App.vue` 的 `.app-backdrop`，`sticky` 定在视口高），
四个页面共用——KeepAlive 下若按页挂载会同时存在 4 个 WebGL 上下文。
调参取向：`scale 2.6 / rotation 38 / waves 2.2` 让它成为一条斜向扫光而非居中光斑；
`thickness .4 + depthOfField 1.25` 让光点小而亮（景深会放大点径，两者必须同步收）；
`hoverEffect none` 不抢指针、`pulseOnClick false` 不干扰点击、`:dpr="1"` 降一半渲染像素。

面板为毛玻璃：`rgba(12,18,16,.63)` + `backdrop-filter: blur(12px)`（KPI 用 .58）。
**不要给外层容器再叠一层 `backdrop-filter`** —— 嵌套模糊会成倍放大开销，弱机直接卡死。
`prefers-reduced-motion` 下只静态渲染一帧。

改主题只改 `style.css` 的 `:root`；组件内联样式已全部改为 token 引用，新增组件请同样只用 token。
