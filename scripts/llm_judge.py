# -*- coding: utf-8 -*-
"""L3 LLM 判级：场景 JSON + RAG 检索条文 -> 分级警示 JSON -> 警示文本。

链路位置: L2 场景 JSON -> 本脚本 -> 警示 JSON（疑似/确定 + 条款依据 + 证据链）

流程:
  1) 用 L2 的 dominant_state / site_type / candidate_violations / action 组装查询
  2) 混合检索：BGE 向量 + BM25 双路召回 -> RRF 融合 -> bge-reranker cross-encoder 精排 top-k
  3) 场景 JSON + 检索条文 -> LLM -> 警示 JSON
  4) 渲染人读警示文本

模式:
  默认 dry-run      : 只做检索 + 组装 prompt，落盘供人工核对，不调 API
  --retrieve-only   : 只跑检索，打印每张图召回的条文
  --run             : 真正调 LLM API（key 从 config.json 经 ModelGateway 读取）
  --tip             : 低风险 tip 模式——不加载 RAG、不检索条款，按推荐性提示 prompt 生成
                      （对应处置语义"低风险-可选提示"；--run 时由 Agent 路由调用）

用法:
  python llm_judge.py --retrieve-only
  python llm_judge.py                       # dry-run：检索 + 组装 prompt
  python llm_judge.py --run
  python llm_judge.py --run --limit 1 --scenes <l2_json> --out <dir>   # Agent 单图对入口
"""
import argparse, json, os, sys
from pathlib import Path

# Windows 控制台默认 GBK，条文中的特殊字符（如 ～）会导致 print 崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.api_config import load_api_config
from tools.model_gateway import ModelGateway
from tools.scene_tags import derive_scene_tags
from tools.rule_engine import evaluate_risk, SEVERITY as RULE_SEVERITY
from agent.case_memory import search_cases, summarize_memory_guidance

# 模型缓存定位（2026-10-18 交付适配）：GDYOLO_HF_HOME 环境变量优先；
# 否则本机有 F 盘则沿用用户规则落 F:\huggingface-cache；无 F 盘的设备落项目内
# .cache/huggingface，保证换机开箱可跑（须在 import sentence_transformers 之前设置）
_HF_CACHE = os.environ.get("GDYOLO_HF_HOME") or (
    r"F:\huggingface-cache" if os.path.isdir("F:/") else str(Path(__file__).resolve().parents[1] / ".cache" / "huggingface"))
os.environ.setdefault("HF_HOME", _HF_CACHE)
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", _HF_CACHE)
os.environ.setdefault("TRANSFORMERS_CACHE", _HF_CACHE)
os.environ.setdefault("HF_ENDPOINT", r"https://hf-mirror.com")

BASE = Path(__file__).resolve().parents[1]
RAG_DIR = BASE / "results" / "rag_store_v2"
SCENE_JSON = BASE / "results" / "l2_scene" / "l2_scene.json"
DEFAULT_OUT = BASE / "results" / "l3_warn"

# 统一配置：模型端点/key 由 ModelGateway 从 config.json 读取；此处只取检索参数
try:
    _CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    _CFG = {}

_LLM_CFG = _CFG.get("llm", {})
_RAG_CFG = _CFG.get("rag", {})

# ---------- 混合检索参数 ----------
RECALL_N = _RAG_CFG.get("recall_n", 20)        # 每路（向量 / BM25）粗排召回条数
RRF_K = _RAG_CFG.get("rrf_k", 60)              # Reciprocal Rank Fusion 平滑常数
RERANK_N = _RAG_CFG.get("rerank_n", 10)        # 送入 cross-encoder 精排的候选条数
RERANKER_ID = _RAG_CFG.get("reranker_id", "BAAI/bge-reranker-base")   # cross-encoder 精排模型
MIN_RERANK = _RAG_CFG.get("min_rerank", 0.20)  # 精排分下限（sigmoid 后 0~1），低于此值的条文不进 prompt

SYSTEM_PROMPT = (
    "你是施工现场的安全员。你需要根据 L2 视觉判定给出的【场景 JSON】（客观事实，不含结论）"
    "与从安全规范中检索出的【候选条文】输出警示。硬性规则：\n"
        "1) 事实只能来自场景 JSON，禁止臆造未出现的目标、属性或关系。\n"
        "2) 条款只能引用【候选条文】中出现的条号，禁止臆测、捏造。\n"
        "3) 每条警示带两个独立维度："
            "level 表示证据确定性，只有『确定』（事实明确、有直接条文支撑）与『疑似』（事实不完整或条文仅部分对应）；"
            "risk_level 表示风险后果等级，只有『高风险』『低风险』『安全』三档，两者互不替代。\n"
        "4) risk_level 判定标准："
            "人员处于即时致命风险组合（吊装载荷下方或起重作业区内、机械作业半径内、临边或高处作业且未佩戴安全带、夜间叠加多重防护缺失）→『高风险』；"
            "存在防护缺失或进入风险区、但暂无致命组合 →『低风险』；无违规事实或风险可忽略 →『安全』。\n"
        "5) PPE 警示须平等对待全部装备项（安全帽/反光衣/安全带/手套/劳保鞋等），同一目标的多项 PPE 缺失必须合并为一条警示并列明全部缺失项，进入机械作业半径、吊装区、临边等位置与行为类违规风险更高，必须优先、完整输出。\n"
        "6) 只评估场景 JSON 实际记录、且有证据支持的观察和风险线索。不得因某装备字段存在、未提及或不可见，就推断其不适用、缺失或违规；装备风险必须同时有场景事实和适用条款依据。不得把未知或不可观察改写成未佩戴。\n"
        "7) 若关键属性 value=null 或 confidence<0.5，不得给出『确定』，最多『疑似』；若连事实都不足以支撑任何违规，warnings 置空、overall_risk 取『安全』并令 need_human_review=true。\n"
        "8) 四个描述字段必须完整撰写："
        "work_description（作业场景与人员行为，引用 bbox_id）、situation_assessment（逐项说明合规点与风险点，引用具体属性值）、clause_analysis（逐条说明所引条款的要求与本场景事实的对应关系，无适用条款时说明原因）、warning_text（面向现场人员的正式警示：违规事项+可能后果+整改要求，无违规时给出安全提示）；"
        "凡 risk_level=『高风险』的警示，warning_text 必须明确要求人员立即远离危险源"
        "（如立即撤出吊装区/机械作业半径），高危场景不使用劝导式措辞；"
        "四个字段合计不少于 500 字，以实际证据自然展开，内容只能来自场景 JSON 与候选条文，不虚构、不为凑字数补写推测。\n"
        "9) 每条警示必须给出证据链（引用 bbox_id 与具体属性值）；overall_risk 必须取全部警示中最高的 risk_level。\n"
        "10) 只输出合法 JSON，无任何附加文字。"
)

MEMORY_INSTRUCTION = (
    "\n记忆使用规则：输入中的【历史验收记忆】来自专家或人工验收，"
    "只能作为相似案例对照。当前【场景 JSON】是在线事实，【规则引擎判定】是契约边界，"
    "三者必须分栏分析。先列出当前事实与历史案例的相同点和差异点，再决定历史结论是否适用。"
    "历史案例不能证明当前图像中存在未观察到的事实；历史低风险结论不能降低规则引擎的高风险下界。"
    "若当前事实与历史结论冲突，保留当前证据驱动的风险等级，并在 situation_assessment 中说明冲突。"
)

USER_SCHEMA = (
    "请严格按此 JSON schema 输出：\n"
    '{"image_id":"<时间戳>",'
    '"overall_risk":"安全|低风险|高风险",'
    '"work_description":"<作业场景与人员行为描述：作业/工序类型、在场人员与机械及各自 action、位置关系，引用 bbox_id>",'
    '"situation_assessment":"<状态判断：逐项说明合规点与风险点，引用具体属性值与 confidence>",'
    '"clause_analysis":"<逐条说明所引条款原文要求与本场景事实的对应关系；无适用条款时说明原因>",'
    '"warnings":[{"level":"疑似|确定","risk_level":"安全|低风险|高风险",'
    '"violation":"<具体违规行为>",'
    '"clause_ids":["<条号>"],"clause_sources":["<规范名>"],'
    '"evidence":["<bbox_id + 属性值>"],"confidence":0.0}],'
    '"warning_text":"<面向现场人员的正式警示：违规事项+可能后果+整改要求；无违规时给出安全提示>",'
    '"need_human_review":true|false}\n'
)

# 低风险-可选提示（2026-09-29 用户语义）：知识库无强制条款、作业简单 → 推荐性提示，不检索不引用条款
SYSTEM_TIP_PROMPT = (
    "你是施工现场的安全员。当前场景属于低风险简单作业，知识库中没有针对性的强制性安全规范条款，"
    "因此不需要检索或引用条款，只需基于【场景 JSON】的客观事实生成推荐性安全提示。硬性规则：\n"
    "1) 事实只能来自场景 JSON，禁止臆造未出现的目标、属性或关系。\n"
    "2) 提示为推荐性质：提醒人员远离危险源（静止机械/塔吊回转覆盖区等）、与机械保持安全距离、"
    "加强注意、补齐无法确认的防护装备；语气平和、面向现场人员，不使用强制或处罚表述。\n"
    "3) warnings 中每条的 clause_ids 与 clause_sources 一律为空数组（无条款依据），"
    "level 固定填『疑似』；risk_level 按后果严重度取『安全|低风险|高风险』。\n"
    "4) 若你基于场景事实认为已达到必须强制告警的程度（如设备运行中人员处于危险区），"
    "将该条 risk_level 置『高风险』并令 need_human_review=true——交由人工确认后再走条款检索链路。\n"
    "5) overall_risk 不得低于【规则引擎判定】的 overall_risk；"
    "overall_risk 必须取全部警示中最高的 risk_level。\n"
    "6) work_description/situation_assessment 照常撰写并引用 bbox_id；"
    "clause_analysis 填写『低风险作业，无适用强制性条款，输出推荐性提示』；"
    "warning_text 为面向现场人员的完整提示文本。\n"
    "7) 只输出合法 JSON，无任何附加文字。"
)

# ---------- L4 自主检索：LLM 通过 function-calling 自主决定检索内容 ----------

AGENTIC_MAX_ROUNDS = 3   # 工具调用轮数上限（有界，防成本失控）

TOOLS_SPEC = [{
    "type": "function",
    "function": {
        "name": "search_clauses",
        "description": "在施工安全规范知识库中检索条款。输入自然语言检索查询与返回条数，"
                       "返回相关条文原文、条号与规范名。只在现有候选条文不足以支撑判定时调用。",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string",
                                                "description": "检索查询短语，如:高处作业 安全带 系挂"},
                                      "topk": {"type": "integer",
                                               "description": "返回条数，默认6，最大10"}},
                       "required": ["query"]}}}]


def agentic_judge(sc, rule, gateway, model_id, rag, topk, stats,
                  max_rounds=None, memory_block=""):
    """L4 自主检索循环：LLM 通过 search_clauses 工具自主决定检索什么（有界）。

    - 种子检索：先按 build_query 自动检一轮作为证据底座（LLM 零调用也有依据）；
    - 有界：工具调用最多 max_rounds 轮，超限后模型必须给出最终判定；
    - 全审计：每轮工具调用的 query/命中数记录在 meta.tool_calls；
    - 返回 (final_content, 累计去重命中, meta)。命中集合即越界校验的 allowed 范围。
    """
    max_rounds = max_rounds or AGENTIC_MAX_ROUNDS
    tags = derive_scene_tags(sc)
    retrieved, seen = [], set()

    def remember(hits):
        for h in hits:
            key = (h.get("doc_id"), tuple(h.get("clause_ids", [])), h.get("text", "")[:60])
            if key not in seen:
                seen.add(key)
                retrieved.append(h)

    remember(rag.search(build_query(sc), topk, tags=tags))

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + MEMORY_INSTRUCTION},
        {"role": "user", "content": (
            f"【场景 JSON】\n{json.dumps(sc, ensure_ascii=False, indent=2)}\n\n"
            f"【规则引擎判定】（客观边界，overall_risk 不得低于它）\n"
            f"{json.dumps(rule, ensure_ascii=False, indent=1)}\n\n"
            f"{memory_block}\n\n"
            f"【种子检索条文】（已自动检索，可直接引用；不足以支撑判定时调用 search_clauses 补充，"
            f"最多 {max_rounds} 轮；证据足够后不再调用工具）\n"
            f"{fmt_clauses(list(retrieved))}\n\n"
            f"image_id: {sc.get('image_id', '')}\n"
            "证据足够后，停止调用工具，按以下 JSON schema 输出最终判定：\n" + USER_SCHEMA)},
    ]
    rounds, final_content, audit = 0, None, []
    while rounds <= max_rounds:
        rounds += 1
        response = gateway.complete(model=model_id, messages=messages, tools=TOOLS_SPEC)
        if response.status != "ok":
            raise RuntimeError(response.error or "LLM 调用失败")
        usage = response.data.get("usage", {})
        stats["tokens_in"] += usage.get("prompt_tokens", 0) or 0
        stats["tokens_out"] += usage.get("completion_tokens", 0) or 0
        # ReAct 之 Thought：模型本轮的显式推理链，随 Action 一起入审计
        thought = str(response.data.get("reasoning") or "")
        tcs = response.data.get("tool_calls")
        if not tcs:
            final_content = response.data.get("content")
            audit.append({"round": rounds, "tool": "final_answer",
                          "thought": thought[:800]})
            break
        messages.append({"role": "assistant",
                         "content": response.data.get("content") or None,
                         "tool_calls": [{"id": t["id"], "type": "function",
                                         "function": {"name": t["function"]["name"],
                                                      "arguments": t["function"]["arguments"]}}
                                        for t in tcs]})
        for t in tcs:
            try:
                args_ = json.loads(t["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args_ = {}
            if t["function"]["name"] == "search_clauses":
                q = str(args_.get("query", ""))[:200]
                tk = min(int(args_.get("topk") or topk), 10)
                hits = rag.search(q, tk, tags=tags)
                remember(hits)
                audit.append({"round": rounds, "tool": "search_clauses",
                              "query": q, "topk": tk, "n_hits": len(hits),
                              "thought": thought[:600]})
                result_text = fmt_clauses(hits) or "（无命中）"
            else:
                audit.append({"round": rounds, "tool": t["function"]["name"], "error": "未知工具"})
                result_text = f"未知工具: {t['function']['name']}"
            messages.append({"role": "tool", "tool_call_id": t["id"], "content": result_text})
    meta = {"rounds": rounds, "tool_calls": audit, "n_unique_clauses": len(retrieved)}
    return final_content, retrieved, meta


# ---------- 作业类型记忆消费端（L3，仅先验参考） ----------

def work_type_prior() -> str | None:
    """读取作业类型记忆，生成先验提示。仅作统计参考，不作为本图事实。"""
    p = BASE / "results" / "agent" / "work_type_memory.json"
    try:
        records = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not records:
        return None
    from collections import Counter
    ds = Counter(str(r.get("dominant_state")) for r in records if r.get("dominant_state"))
    st = Counter(str(r.get("site_type")) for r in records if r.get("site_type"))
    top = lambda c: "、".join(f"{k}({v})" for k, v in c.most_common(3))
    return (f"近 {len(records)} 次研判的作业类型分布：{top(ds)}；"
            f"场地分布：{top(st)}。（统计先验，仅辅助判断，不作为本图事实）")


# ---------- RAG 检索 ----------

class RagStore:
    """混合检索：向量(BGE) + BM25(jieba) 双路召回 -> RRF 融合 -> cross-encoder 精排。

    任一模型源不可用时自动降级（BM25-only / RRF-only），保证检索链路不因环境问题中断。
    """

    def __init__(self, rag_dir: Path):
        import numpy as np
        self.np = np
        self.vecs = np.load(rag_dir / "vectors.npy")
        self.chunks = [json.loads(l) for l in (rag_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        meta = json.loads((rag_dir / "meta.json").read_text(encoding="utf-8"))
        self.model_name = meta["model"]
        self._model = None
        self._reranker = None
        self._bm25 = None
        self._jieba = None
        self.last_search_mode = "uninitialized"

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            # 强制 CPU：本机 GT 710 算力 3.5 过老，torch 自动选 cuda 会 RuntimeError 导致向量路降级
            self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model

    @property
    def reranker(self):
        if self._reranker is None:
            from sentence_transformers import CrossEncoder
            self._reranker = CrossEncoder(RERANKER_ID, max_length=512, device="cpu")
        return self._reranker

    @property
    def bm25(self):
        if self._bm25 is None:
            import jieba
            from rank_bm25 import BM25Okapi
            jieba.setLogLevel(60)
            self._jieba = jieba
            corpus = [self._tokenize(c["text"]) for c in self.chunks]
            self._bm25 = BM25Okapi(corpus)
        return self._bm25

    def _tokenize(self, text: str):
        return [t for t in self._jieba.lcut(text) if t.strip()]

    def search(self, query: str, topk: int = 6, tags: list[str] | None = None):
        # 1) BM25 粗排（词项层，始终可用）
        bm_scores = self.bm25.get_scores(self._tokenize(query))
        bm_idx = [int(i) for i in self.np.argsort(-bm_scores)[:RECALL_N]]

        # BGE 权重不可用时，退回 BM25-only 词项检索
        try:
            q = self.model.encode([query], normalize_embeddings=True)[0]
            sims = self.vecs @ q
            vec_idx = [int(i) for i in self.np.argsort(-sims)[:RECALL_N]]
        except Exception as exc:
            self.last_search_mode = f"bm25_only:model_unavailable:{type(exc).__name__}"
            max_score = float(max(bm_scores)) if len(bm_scores) else 0.0
            return [dict(self.chunks[int(i)],
                         score=float(bm_scores[int(i)] / max_score) if max_score > 0 else 0.0,
                         rrf=0.0, retrieval_mode=self.last_search_mode)
                    for i in bm_idx[:topk]]

        # 2) RRF 融合（只看排名不看原始分，规避两路分数量纲不可比）
        rrf = {}
        for rank, i in enumerate(vec_idx):
            rrf[i] = rrf.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
        for rank, i in enumerate(bm_idx):
            rrf[i] = rrf.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
        cand = sorted(rrf, key=lambda i: -rrf[i])[:RERANK_N]

        # 3) cross-encoder 精排；模型不可用时保留 RRF 排名
        try:
            pairs = [(query, self.chunks[i]["text"]) for i in cand]
            scores = self.np.asarray(self.reranker.predict(pairs), dtype="float32").ravel()
            if scores.min() < 0 or scores.max() > 1:
                scores = 1.0 / (1.0 + self.np.exp(-scores))
            # 场景标签加成：条文 scene_tags 与查询侧标签交集越大，小幅提分（上限 1.0）
            if tags:
                tag_set = set(tags)
                for k, i in enumerate(cand):
                    overlap = len(tag_set & set(self.chunks[i].get("scene_tags", []) or []))
                    if overlap:
                        scores[k] = min(1.0, float(scores[k]) + 0.05 * overlap)
            order = self.np.argsort(-scores)[:topk]
            hits = [dict(self.chunks[cand[int(j)]], score=float(scores[int(j)]),
                         rrf=float(rrf[cand[int(j)]]), retrieval_mode="hybrid_reranked")
                    for j in order]
            strong = [h for h in hits if h["score"] >= MIN_RERANK]
            self.last_search_mode = "hybrid_reranked"
            return strong or hits
        except Exception as exc:
            self.last_search_mode = f"hybrid_rrf:model_unavailable:{type(exc).__name__}"
            return [dict(self.chunks[i], score=float(rrf[i]), rrf=float(rrf[i]),
                         retrieval_mode=self.last_search_mode)
                    for i in cand[:topk]]


def build_query(scene: dict) -> str:
    """由场景 JSON 组装检索查询串（同时兼容结构化字段与开放观察字段）。"""
    parts = []
    s = scene.get("scene", {}) or {}
    if s.get("dominant_state"):
        parts.append(s["dominant_state"])
    if s.get("site_type"):
        parts.append(s["site_type"])
    if scene.get("scene_summary"):
        parts.append(scene["scene_summary"])
    context = scene.get("work_context", {}) or {}
    if isinstance(context, dict):
        for key in ("activity", "site", "summary"):
            if context.get(key):
                parts.append(str(context[key]))
    for item in scene.get("risk_candidates", []) or []:
        if isinstance(item, dict) and item.get("description"):
            parts.append(item["description"])
    for item in scene.get("scene_observations", []) or []:
        if isinstance(item, dict):
            parts.extend(str(item[k]) for k in ("item", "state", "description") if item.get(k))
    for v in scene.get("candidate_violations", []) or []:
        parts.append(v)
    for t in scene.get("targets", []) or []:
        if t.get("type"):
            parts.append(t["type"])
        if t.get("action"):
            parts.append(t["action"])
        for obs in t.get("observations", []) or []:
            if isinstance(obs, dict):
                parts.extend(str(obs[k]) for k in ("item", "state", "evidence") if obs.get(k))
    for r in scene.get("spatial_relations", []) or []:
        if r.get("relation"):
            parts.append(r["relation"])
    if not parts:
        parts.append("施工现场 安全隐患")
    return " ".join(parts)


def build_memory_context(scene: dict, task_id: str | None = None,
                        current_risk: str | None = None) -> tuple[str, dict]:
    """把人工验收案例转成提示词中的对照材料。

    历史案例只描述过去的人工结论和适用条件；在线场景 JSON 与规则引擎
    仍是当前事实和风险下界。模型必须先找差异，再决定是否把历史建议当作参考。
    """
    summary = scene.get("scene", {}) if isinstance(scene.get("scene"), dict) else {}
    work = str(summary.get("dominant_state") or "")
    site = str(summary.get("site_type") or "")
    cases = search_cases(build_query(scene), work_type=work, site_type=site,
                         limit=3, exclude_task_id=task_id)
    current = None
    guidance = summarize_memory_guidance(cases, current_risk)
    if not cases:
        return "【历史验收记忆】无可用的已验收同类案例。", {"count": 0, "guidance": guidance}
    lines = ["【历史验收记忆】以下是相似作业的人工验收结果，仅作对照，不是当前事实："]
    for i, item in enumerate(cases, 1):
        try:
            facts = json.loads(item.get("facts_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            facts = {}
        lines.append(json.dumps({
            "case": i,
            "memory_id": item.get("memory_id"),
            "work_type": item.get("work_type"),
            "site_type": item.get("site_type"),
            "human_risk": item.get("human_risk"),
            "human_disposition": item.get("human_disposition"),
            "error_type": item.get("error_type"),
            "notes": item.get("notes"),
            "risk_facts": facts,
            "matched_terms": item.get("matched_terms", []),
        }, ensure_ascii=False))
    lines.append("【记忆建议】\n" + json.dumps(guidance, ensure_ascii=False))
    lines.append("历史案例使用规则：先比较当前设备状态、人员危险区关系、" 
                 "防护事实与历史案例的差异；只有关键条件一致时才能作为建议。"
                 "历史低风险结论不得覆盖当前规则引擎的高风险下界。")
    return "\n".join(lines), {"count": len(cases), "guidance": guidance,
                                "memory_ids": [x.get("memory_id") for x in cases]}


def fmt_clauses(hits):
    lines = []
    for h in hits:
        lines.append(f"[{h['doc_id']} | {h.get('chapter','')} {h.get('section','')} | 条号 {','.join(h.get('clause_ids',[]))} | 相似度 {h['score']:.3f}]\n{h['text']}")
    return "\n\n".join(lines)


# ---------- 主流程 ----------

def load_scenes(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("scenes", data)


def run(args):
    cfg = load_api_config(BASE)
    topk = args.topk or int(_LLM_CFG.get("topk", 6))
    scenes = load_scenes(Path(args.scenes))
    if args.limit > 0:
        scenes = dict(list(scenes.items())[:args.limit])
    print(f"[l3] 载入场景 {len(scenes)} 条")
    if args.tip and args.retrieve_only:
        raise SystemExit("[l3] --tip 与 --retrieve-only 互斥：tip 模式不检索条款")
    rag = None if args.tip else RagStore(Path(args.rag))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    todo, skipped = {}, {}
    for ts, sc in scenes.items():
        meta = sc.get("_meta", {}) if isinstance(sc, dict) else {}
        if meta.get("skipped") or meta.get("parsing_error"):
            skipped[ts] = {"_meta": {"skipped": True,
                                     "reason": meta.get("reason") or meta.get("validate_error") or "L2 未产出可用场景 JSON"}}
            continue
        todo[ts] = sc

    if args.retrieve_only:
        for ts, sc in todo.items():
            q = build_query(sc)
            print(f"\n=== {ts} ===\n查询: {q}")
            for h in rag.search(q, topk):
                print(f"  [{h['doc_id']} {','.join(h.get('clause_ids',[]))}] {h['score']:.3f}  {h['text'][:70]}...")
        print(f"\n[l3] 检索完成（未调 API）。跳过 {len(skipped)} 条")
        return

    if args.run:
        gateway, model_id = ModelGateway.from_project_config(
            BASE, "llm", timeout_seconds=int(cfg.get("api", {}).get("timeout_seconds", 120)),
            temperature=0)

    prompts_dir = out / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    warnings, stats = dict(skipped), {"ok": 0, "bad": 0, "tokens_in": 0, "tokens_out": 0}

    agentic = (args.agentic or bool(_LLM_CFG.get("agentic", False))) and not args.tip
    prior = work_type_prior()
    prior_block = f"【作业类型记忆】（统计先验，仅辅助判断）\n{prior}\n\n" if prior else ""
    for ts, sc in todo.items():
        rule = evaluate_risk(sc)
        memory_block, memory_meta = build_memory_context(
            sc, task_id=ts, current_risk=rule.get("overall_risk"))
        rule_block = ("【规则引擎判定】（基于场景 JSON 三要素的确定性输出，属客观边界：\n"
                      "你的 overall_risk 不得低于其中的 overall_risk；如需上调必须引用场景 JSON 中的事实；"
                      "drivers 是已触发的规则，可直接作为警示依据引用）\n"
                      + json.dumps(rule, ensure_ascii=False, indent=1))

        if args.run and args.tip:
            # 低风险-可选提示：无条款检索，直接推荐性提示（LLM 升级到高风险时转人工走完整链路）
            hits = []
            user_text = (f"【场景 JSON】\n{json.dumps(sc, ensure_ascii=False, indent=2)}\n\n"
                         f"{rule_block}\n\n"
                         f"{memory_block}\n\n"
                         f"image_id: {ts}\n" + USER_SCHEMA)
            (prompts_dir / f"{ts}.txt").write_text(
                "=== SYSTEM(tip) ===\n" + SYSTEM_TIP_PROMPT + MEMORY_INSTRUCTION + "\n\n=== USER ===\n" + user_text,
                encoding="utf-8")
            response = gateway.complete(
                model=model_id,
                messages=[{"role": "system", "content": SYSTEM_TIP_PROMPT + MEMORY_INSTRUCTION},
                          {"role": "user", "content": user_text}],
                response_format={"type": "json_object"})
            if response.status != "ok":
                raise RuntimeError(response.error or "LLM API 调用失败")
            raw = response.data["content"]
            usage = response.data.get("usage", {})
            stats["tokens_in"] += usage.get("prompt_tokens", 0) or 0
            stats["tokens_out"] += usage.get("completion_tokens", 0) or 0
        elif args.run and agentic:
            # L4 自主检索：LLM 通过 search_clauses 工具自主决定补充检索
            final_content, hits, agentic_meta = agentic_judge(
                sc, rule, gateway, model_id, rag, topk, stats,
                memory_block=memory_block,
                max_rounds=args.max_tool_rounds)
            raw = final_content
            (prompts_dir / f"{ts}.agentic.json").write_text(json.dumps(
                {"rule_engine": rule, "memory": memory_meta, "agentic": agentic_meta}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        else:
            sys_prompt = SYSTEM_TIP_PROMPT if args.tip else SYSTEM_PROMPT
            hits = [] if args.tip else rag.search(build_query(sc), topk, tags=derive_scene_tags(sc))
            user_text = (f"【场景 JSON】\n{json.dumps(sc, ensure_ascii=False, indent=2)}\n\n"
                         f"【候选条文】\n{fmt_clauses(hits)}\n\n"
                         f"{rule_block}\n\n"
                         f"{prior_block}"
                         f"{memory_block}\n\n"
                         f"image_id: {ts}\n" + USER_SCHEMA)
            (prompts_dir / f"{ts}.txt").write_text(
                "=== SYSTEM" + ("(tip)" if args.tip else "") + " ===\n" + sys_prompt
                + "\n\n=== USER ===\n" + user_text, encoding="utf-8")
            if not args.run:
                warnings[ts] = {"_meta": {"dry_run": True, "n_clauses": len(hits),
                                          "retrieval_mode": ("tip_mode" if args.tip
                                                             else rag.last_search_mode),
                                          "retrieved_clause_ids": [c for h in hits for c in h.get("clause_ids", [])]}}
                continue
            response = gateway.complete(
                model=model_id,
                messages=[{"role": "system", "content": sys_prompt + MEMORY_INSTRUCTION},
                          {"role": "user", "content": user_text}],
                response_format={"type": "json_object"})
            if response.status != "ok":
                raise RuntimeError(response.error or "LLM API 调用失败")
            raw = response.data["content"]
            usage = response.data.get("usage", {})
            stats["tokens_in"] += usage.get("prompt_tokens", 0) or 0
            stats["tokens_out"] += usage.get("completion_tokens", 0) or 0

        parse_error = None
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            obj = {"warnings": []}
            parse_error = f"LLM JSON 解析失败: {exc}"
        if not isinstance(obj, dict):
            obj = {"warnings": []}
            parse_error = "LLM 输出根节点必须为 JSON 对象"
        if not isinstance(obj.get("warnings", []), list):
            obj["warnings"] = []
            parse_error = "LLM 输出 warnings 必须为列表"
        # 条款越界校验：引用条号必须在检索命中集合内；
        # 无编号条文（如 GB 表1 表格行）无法按条号匹配，只要其来源规范在命中集合内即视为可溯
        allowed = {c for h in hits for c in h.get("clause_ids", []) if c}
        unnumbered = {h["doc_id"] for h in hits if not h.get("clause_ids")}
        bad_clause = []
        for w in obj.get("warnings", []) or []:
            srcs = set(w.get("clause_sources") or [])
            for c in (w.get("clause_ids") or []):
                if not c or c in allowed:
                    continue
                if any(s.startswith(d) or d.startswith(s) or d in c
                       for d in unnumbered for s in srcs):
                    continue
                bad_clause.append(c)
        # 规则引擎下界：LLM 的 overall_risk 不得低于规则判定；LLM 上调则强制转人工
        rule_risk = rule.get("overall_risk")
        llm_risk = obj.get("overall_risk") or "安全"
        risk_floor_applied = False
        manual_gate_applied = False
        if rule_risk is None and rule.get("need_human_review"):
            # 规则引擎明确判不了时，LLM 不能自行补出安全/低风险/高风险。
            obj["overall_risk"] = None
            obj["need_human_review"] = True
            manual_gate_applied = True
        elif RULE_SEVERITY.get(llm_risk, 0) < RULE_SEVERITY.get(rule_risk, 0):
            obj["overall_risk"] = rule_risk
            risk_floor_applied = True
        llm_escalated = RULE_SEVERITY.get(llm_risk, 0) > RULE_SEVERITY.get(rule_risk, 0)
        obj["_meta"] = {"clause_out_of_scope": bad_clause, "n_clauses": len(hits),
                        "retrieval_mode": "tip_mode" if args.tip else rag.last_search_mode,
                        "retrieved_clause_ids": [c for h in hits for c in h.get("clause_ids", [])],
                        "parsing_error": bool(parse_error), "validate_error": parse_error,
                        "rule_engine": {"overall_risk": rule_risk,
                                        "drivers": rule.get("drivers", []),
                                        "evidence_gaps": rule.get("evidence_gaps", []),
                                        "need_human_review": rule.get("need_human_review", False),
                        "risk_floor_applied": risk_floor_applied,
                        "manual_gate_applied": manual_gate_applied,
                                        "llm_escalated": llm_escalated}}
        if llm_escalated or rule.get("need_human_review"):
            obj["need_human_review"] = True
        stats["bad" if bad_clause or parse_error else "ok"] += 1
        warnings[ts] = obj
        print(f"  {ts} 警示={len(obj.get('warnings', []) or [])} 越界条款={bad_clause}")

    result = {"stats": stats, "warnings": warnings}
    (out / "l3_warnings.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.run:
        (out / "warn_text.md").write_text(render_text(warnings), encoding="utf-8")
        print(f"[l3] 完成 {len(todo)} 条；正常 {stats['ok']}，条款越界/解析异常 {stats['bad']}，跳过 {len(skipped)}")
        print(f"[l3] token 输入={stats['tokens_in']} 输出={stats['tokens_out']}")
    else:
        print(f"[l3] dry-run 完成：{len(todo)} 条 prompt 已落盘 {prompts_dir}")
    print(f"[l3] 结果 {out / 'l3_warnings.json'}")


def render_text(warnings: dict) -> str:
    RISK_ORDER = {"高风险": 0, "低风险": 1, "安全": 2}
    def worst(items):
        rs = [x.get("risk_level", "") for x in items]
        return max(rs, key=lambda r: RISK_ORDER.get(r, 3)) if rs else "安全"
    lines = ["# 施工现场安全警示清单", ""]
    for ts, w in warnings.items():
        lines.append(f"## {ts}")
        if w.get("_meta", {}).get("skipped"):
            lines.append(f"- 转人工：{w['_meta'].get('reason','')}")
            lines.append("")
            continue
        ws = w.get("warnings", []) or []
        ws = sorted(ws, key=lambda x: RISK_ORDER.get(x.get("risk_level", ""), 3))
        overall = w.get("overall_risk") or worst(ws)
        lines.append(f"**整体风险等级：{overall}**")
        if w.get("work_description"):
            lines.append(f"- 作业描述：{w['work_description']}")
        if w.get("situation_assessment"):
            lines.append(f"- 状态判断：{w['situation_assessment']}")
        if w.get("clause_analysis"):
            lines.append(f"- 条款分析：{w['clause_analysis']}")
        if not ws:
            lines.append("- 未发现需告警的违规（或事实不足）")
        for i, it in enumerate(ws, 1):
            lines.append(f"{i}. 【{it.get('level','')}|{it.get('risk_level','')}】{it.get('violation','')}")
            lines.append(f"   - 依据：{','.join(it.get('clause_sources') or [])} {'、'.join(it.get('clause_ids') or [])}")
            lines.append(f"   - 证据：{'；'.join(it.get('evidence') or [])}")
            lines.append(f"   - 置信度：{it.get('confidence','')}")
        if w.get("warning_text"):
            lines.append(f"- 警示：{w['warning_text']}")
        if w.get("need_human_review"):
            lines.append("- 需人工复核")
        lines.append("")
    return "\n".join(lines)


def parse_opt():
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    p.add_argument("--retrieve-only", action="store_true", help="只跑 RAG 检索，不组装 prompt 之外的事")
    p.add_argument("--scenes", type=str, default=str(SCENE_JSON))
    p.add_argument("--rag", type=str,
                   default=str(BASE / _RAG_CFG["dir"]) if _RAG_CFG.get("dir") else str(RAG_DIR))
    p.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    p.add_argument("--topk", type=int, default=None, help="可选覆盖；默认读取 config.json")
    p.add_argument("--limit", type=int, default=0, help="最多处理多少条，0 表示全部")
    p.add_argument("--agentic", action=argparse.BooleanOptionalAction,
                   default=bool(_LLM_CFG.get("agentic", False)),
                   help="L4 自主检索：LLM 通过 search_clauses 工具自主决定检索（默认取 config.json）")
    p.add_argument("--tip", action="store_true",
                   help="低风险 tip 模式：不加载 RAG、不检索条款，生成推荐性提示（低风险-可选提示）")
    p.add_argument("--max-tool-rounds", type=int, default=None,
                   help="agentic 模式工具调用轮数上限，默认取代码常量 3")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_opt())
