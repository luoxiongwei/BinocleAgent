# -*- coding: utf-8 -*-
"""RAG 索引构建：规范 Markdown(多级标题) -> 最细条文单元 chunk -> 中文 embedding -> 轻量向量库。

切块策略（参考 hierarchical / heading-based chunking）：
  文档用 Markdown 多级标题组织（# 章、## 节、### 条，层级可变）。
  以"最细的叶子标题单元"为 chunk：某标题之下不再有更深标题、且携带正文时，它就是一个条文单元。
  chunk 文本首行 = 从根到该叶子的完整层级 breadcrumb（编号.标题 ＞ 编号.标题 ＞ …），
  随后接条文正文 —— 召回的条目即形如：3.总则 ＞ 3.1一般规定 ＞ 3.1.7 内容。

- embedding 模型缓存 / HF 元数据 -> F 盘（用户规则）
- 输出: <out>/chunks.jsonl + <out>/vectors.npy + <out>/meta.json
用法: python rag_index.py [--docs RAG文档] [--out results/rag_store] [--model BAAI/bge-small-zh-v1.5]
"""
import argparse, json, os, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.scene_tags import tags_for_text

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
DEFAULT_DOCS = BASE / "data" / "rag_docs"
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"

RE_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
RE_NO = re.compile(r"^(\d+(?:\.\d+)*)\s+(.+)$")     # 编号 + 标题/内容，如 "4.1 分类"、"4.1.1 内容"
RE_IMG = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")  # 图片引用行，剔除

MAX_CHUNK_CHARS = 1600       # 单叶子正文超长时按行拆子块
MAX_SPLIT_JUNK = 4           # 子块序号占位长度


def iter_docs(root: Path):
    """扫描规范目录，产出 (doc_name, text)，排除图片/缓存。"""
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        mds = [p for p in sorted(d.glob("*.md")) if ".ipynb" not in p.name]
        if not mds:
            continue
        yield d.name, mds[0].read_text(encoding="utf-8", errors="ignore")


def split_no(title: str):
    """提取标题开头的编号与纯标题。返回 (no, label)，无编号时 no=None。"""
    m = RE_NO.match(title.strip())
    if m:
        return m.group(1), m.group(2).strip()
    return None, title.strip()


def parse_blocks(text: str):
    """把 Markdown 切成有序标题块列表。每块 = {"depth","title","lines"(正文行,不含空行)}。"""
    blocks = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        m = RE_HEADING.match(raw)
        if m:
            blocks.append({"depth": len(m.group(1)), "title": m.group(2).strip(), "lines": []})
        elif s and not RE_IMG.match(s):
            # 文档开头无标题的正文 -> 挂在隐式根块；否则挂在最后一块
            if blocks:
                blocks[-1]["lines"].append(s)
    return blocks


def build_chunks(doc_name, text):
    """标题树 -> 叶子 chunk（保留根到叶子 breadcrumb 路径）。"""
    std_m = re.match(r"([A-Z]+_\d+[^_]*|[A-Z]+_\d+)", doc_name)
    std_id = std_m.group(1) if std_m else doc_name
    doc_title = doc_name.split("_文档.md")[0].split("_文字提取.md")[0]

    chunks = build_leaf_chunks(parse_blocks(text))
    for c in chunks:
        c["doc_id"] = std_id
        c["doc_name"] = doc_title
    return chunks


def build_leaf_chunks(blocks):
    """单遍：叶子标题（其下不再有更深标题且带正文）产出一条 chunk。"""
    chunks = []
    stack = []   # 祖先链，元素 (depth, no, label, full)

    def breadcrumb():
        return " ＞ ".join(b[-1] for b in stack) if stack else ""

    for i, blk in enumerate(blocks):
        depth, title, lines = blk["depth"], blk["title"], blk["lines"]
        no, label = split_no(title)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        full = f"{no}.{label}" if no else label
        stack.append((depth, no, label, full))

        # 叶子条件：这是最后一个块，或下一个块的深度 <= 当前深度（即当前无更深子块）
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        is_leaf = (nxt is None) or (nxt["depth"] <= depth)
        if not is_leaf:
            continue
        body = "\n".join(ln for ln in lines if ln)

        prefix = breadcrumb()
        texts = split_body(body)
        for j, seg in enumerate(texts):
            head = prefix
            if len(texts) > 1:
                head = f"{prefix}（{j + 1}/{len(texts)}）"
            chunks.append({
                "chapter": seg_label(stack, 1),
                "section": seg_label(stack, 2),
                "clause_ids": [no] if no else [],
                "full_path": prefix,
                "text": head + ("\n" + seg if seg else ""),
            })
    return chunks


def seg_label(stack, depth):
    """取指定层级标题的纯标题；任意深度缺失时返回空串（兼容仅两级/仅一级文档）。"""
    for item in stack:
        if item[0] == depth:
            return item[2]
    return ""


def split_body(body: str, max_chars: int = MAX_CHUNK_CHARS):
    """正文超长时按行聚合切块，尽量静态各块 < max_chars。"""
    if len(body) <= max_chars:
        return [body]
    lines = body.split("\n")
    parts, cur = [], []
    cur_len = 0
    for ln in lines:
        if cur and cur_len + len(ln) > max_chars - MAX_SPLIT_JUNK:
            parts.append("\n".join(cur)); cur, cur_len = [], 0
        cur.append(ln); cur_len += len(ln) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts


def build(args):
    from sentence_transformers import SentenceTransformer
    import numpy as np

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    all_chunks = []
    for doc_name, text in iter_docs(Path(args.docs)):
        all_chunks.extend(build_chunks(doc_name, text))
    # 场景标签：按条文文本打 scene_tags，供检索侧按场景加成
    for c in all_chunks:
        c["scene_tags"] = tags_for_text(c.get("full_path", "") + "\n" + c.get("text", ""))
    tagged = sum(1 for c in all_chunks if c["scene_tags"])
    print(f"[rag_index] 解析 {len(all_chunks)} 个 chunk（{tagged} 个已打场景标签）")

    print(f"[rag_index] 加载模型 {args.model} ...")
    model = SentenceTransformer(args.model)
    dim = model.get_sentence_embedding_dimension()

    texts = [c["text"] for c in all_chunks]
    print(f"[rag_index] 编码 {len(texts)} 段文本 ...")
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=True)
    vecs = np.asarray(vecs, dtype="float32")

    (out / "chunks.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in all_chunks), encoding="utf-8")
    np.save(out / "vectors.npy", vecs)
    (out / "meta.json").write_text(json.dumps({
        "model": args.model, "dim": int(dim), "n_chunks": len(all_chunks),
        "norm": True,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    from collections import Counter
    per_doc = Counter(c["doc_id"] for c in all_chunks)
    print("[rag_index] 完成。各规范 chunk 数:", dict(per_doc))
    print(f"[rag_index] 已存 {out}")


def parse_opt():
    p = argparse.ArgumentParser()
    p.add_argument("--docs", type=str, default=str(DEFAULT_DOCS))
    p.add_argument("--out", type=str, default=str(BASE / "results" / "rag_store"))
    p.add_argument("--model", type=str, default=DEFAULT_MODEL)
    return p.parse_args()


if __name__ == "__main__":
    build(parse_opt())
