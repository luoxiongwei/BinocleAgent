# -*- coding: utf-8 -*-
"""场景标签体系：知识库 chunk 打标与检索查询打标共用。

设计目的：
  rag_index 切块时为每个条文 chunk 计算 scene_tags（按条文文本关键词）；
  llm_judge 检索时对 L2 场景 JSON 计算 query 侧 scene_tags，
  命中相同标签的条文在精排分上获得小幅加成，提升"场景-条文"贴合度。
标签体系与 L2 schema 对应：
  照明/临时用电 ↔ scene.lighting 与夜间场景；
  高处作业/防坠/脚手架 ↔ body_equipment 与临边高处场景；
  吊装/机械作业 ↔ state_attrs 与塔吊/机械目标；
  个体防护各标签 ↔ ppe/body_equipment 字段；
  环境防护各标签 ↔ scene.env_protections。
"""
from __future__ import annotations

# (关键词元组, 标签名) —— 命中任一关键词即打该标签
TAG_RULES: list[tuple[tuple[str, ...], str]] = [
    (("照明", "夜间", "采光", "能见度"), "照明"),
    (("用电", "漏电", "配电", "电缆", "电气"), "临时用电"),
    (("高处", "临边", "洞口", "攀登", "悬空", "坠落"), "高处作业"),
    (("安全带", "安全绳", "防坠", "系挂"), "防坠装备"),
    (("脚手架", "作业架", "梯架", "马道"), "脚手架"),
    (("起重", "吊装", "塔式起重机", "塔吊", "吊运", "吊具"), "吊装"),
    (("机械", "挖掘", "桩工", "卷扬", "钢筋机械", "混凝土机械"), "机械作业"),
    (("基坑", "土方", "降水", "边坡"), "基坑土方"),
    (("安全帽", "头部防护"), "安全帽"),
    (("反光", "警示服", "防护服", "高可视"), "反光衣"),
    (("手套", "手部防护"), "手套"),
    (("鞋", "足部防护", "劳保鞋"), "劳保鞋"),
    (("安全网", "密目"), "安全网"),
    (("护栏", "防护栏杆", "盖板", "防护棚"), "环境防护设施"),
    (("动火", "焊接", "焊割", "防火"), "动火焊接"),
    (("有限空间", "密闭", "通风"), "有限空间"),
]


def tags_for_text(text: str) -> list[str]:
    """按关键词规则为条文/查询文本打标签，返回排序后的标签列表。"""
    if not text:
        return []
    return sorted({tag for keywords, tag in TAG_RULES if any(k in text for k in keywords)})


def derive_scene_tags(scene: dict) -> list[str]:
    """从 L2 场景 JSON 推导查询侧标签（结构化与开放字段双兼容）。"""
    texts: list[str] = []
    s = scene.get("scene", {}) or {}
    texts.extend(str(v) for v in s.values() if isinstance(v, str))
    texts.extend(str(s.get(k, "") or "") for k in ("dominant_state", "site_type"))
    texts.append(str(scene.get("scene_summary", "") or ""))
    texts.append(str(scene.get("uncertainty", "") or ""))
    for v in scene.get("env_protections", []) or []:
        texts.append(str(v))
    for v in scene.get("candidate_violations", []) or []:
        texts.append(str(v))
    for t in scene.get("targets", []) or []:
        if not isinstance(t, dict):
            continue
        texts.append(str(t.get("action", "") or ""))
        texts.append(str(t.get("type", "") or ""))
        for group in ("ppe", "body_equipment"):
            for v in (t.get(group) or {}).values():
                if isinstance(v, dict) and v.get("value"):
                    texts.append(str(group))
        for obs in t.get("observations", []) or []:
            if isinstance(obs, dict):
                texts.extend(str(obs.get(k, "") or "") for k in ("item", "state", "evidence"))
    for r in scene.get("spatial_relations", []) or []:
        if isinstance(r, dict) and r.get("relation"):
            texts.append(str(r["relation"]))
    return tags_for_text(" ".join(texts))
