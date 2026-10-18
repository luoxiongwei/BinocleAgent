# -*- coding: utf-8 -*-
"""规则引擎：以 作业类型 × 安全防护 × 监督陪护 三要素确定性判定安全状态。

设计立场（回应"依赖不稳定 confidence 臆测"的问题）：
  - overall_risk 的**下界**由本引擎基于 L2 结构化事实确定性推出，LLM 不得低于此边界；
  - 模型自报 confidence 只用作**证据充分性门控**（confidence<0.5 的字段视为"未知"，
    走证据缺口/转人工通道），不作为风险数值输入；
  - 每个判定都携带规则 ID（drivers），可审计、可回溯、可被 golden set 检验。

三要素：
  作业类型（work_hazard）: dominant_state/site_type 文本 → 高/中/低/未知 四级固有风险；
  安全防护（protection） : targets 的 ppe/body_equipment 显式缺失（value=false）与环境防护缺失；
  监督陪护（supervision） : 作业人数、单人作业、观察者/监护者在场（由 action 与空间关系推断）。

边界条件：
  - value=null 视为"未知"而非"缺失"——缺失必须有显式 false 证据；
  - state_attrs.value=true（confidence>=0.5）需结合设备运行状态判级（2026-09-29 golden set 反馈）：
    设备运行中→高风险；设备静止→低风险（距离提示）；不确定→转人工；
  - 固有风险匹配只看作业描述与目标动作，不看 site_type（位置词不升高危）；
  - 空间语义门控（2026-09-29 用户仲裁）：单人高危作业需"室外且高空"才升"高风险"，
    室内/平地保持提示级，空间未知保守升级并记证据缺口；
  - 无工人目标时整体判"安全"（仅机械在场不构成人员风险）；
  - 作业类型未知时给出 review 标记，由人工判级。
"""
from __future__ import annotations

import json

SEVERITY = {"安全": 0, "低风险": 1, "高风险": 2}

HAZARD_RULES: list[tuple[tuple[str, ...], str, str]] = [
    # (关键词, 固有风险等级, 规则 ID)
    (("吊装", "起重", "塔吊", "塔式起重机", "吊运", "吊具", "吊物"), "高", "R-WORK-LIFTING"),
    (("高处", "临边", "攀爬", "悬空", "上下梯架", "梯架", "脚手架"), "高", "R-WORK-HEIGHT"),
    (("基坑", "土方", "挖掘", "开挖", "边坡"), "高", "R-WORK-EXCAVATION"),
    (("操作机械", "驾驶", "挖掘机作业", "机械作业"), "高", "R-WORK-MACHINERY"),
    (("动火", "焊接", "焊割"), "高", "R-WORK-HOTWORK"),
    (("砌筑", "绑扎", "支设", "模板", "浇筑", "灌注", "安装作业", "砖墙", "砖块", "砖堆", "砂浆"), "中", "R-WORK-BUILD"),
    (("搬运", "转运", "堆放", "加工", "切割", "装卸", "运输"), "中", "R-WORK-MATERIAL"),
    (("行走", "站立观察", "巡查", "巡视", "场地清理"), "低", "R-WORK-ROUTINE"),
]

# 防护分级：
#   关键防护（critical）：缺失直接威胁生命 → 高危作业下缺失升级"高风险"
#   一般装备（soft）：反光衣/手套/鞋等 → 缺失升"低风险"并合并为一条警示，不主导最高等级
CRITICAL_ALWAYS = ["helmet"]
CRITICAL_CONTEXT = {"高处": ["safety_belt"], "临边": ["safety_belt"], "脚手架": ["safety_belt"],
                    "攀爬": ["safety_belt"], "悬空": ["safety_belt"], "梯架": ["safety_belt"]}
SOFT_ITEMS = ["vest", "gloves", "safety_shoes"]
SOFT_NOTE = "反光衣/手套/劳保鞋等一般装备缺失（可视性与基础防护问题，合并警示）"

ENV_NEGATIVE_KEYWORDS = ("缺失", "未加盖", "未设置", "无护栏", "无防护", "损坏", "拆除")


def _match_hazard(text: str) -> tuple[str, list[str]]:
    """文本 → (固有风险等级, 命中的规则 ID)。多规则命中取最高等级。"""
    rank = {"高": 3, "中": 2, "低": 1}
    matched: list[str] = []
    best, best_rank = "未知", 0
    for keywords, lv, rid in HAZARD_RULES:
        if any(k in text for k in keywords):
            matched.append(rid)
            if rank[lv] > best_rank:
                best, best_rank = lv, rank[lv]
    return best, matched


def _field_flag(v) -> str:
    """字段三态：missing（显式缺失）/ unknown（null 或低置信）/ ok。

    2026-09-29 用户修订：conf>=0.7 才采信"显式缺失"——0.5~0.7 之间的 false
    视为 unknown（低确信缺失走证据缺口通道，不直接判违规）。

    2026-09-29 加固：`value` **必须是布尔**；非布尔（如模型把 value 写成
    「黄色安全帽」这类描述串）一律按 unknown 处理 —— 否则它会被当成"已就位"，
    而「未戴安全帽」这类负面描述串同样会落进 ok 分支，等于把违规读成合规。
    """
    if not isinstance(v, dict):
        return "unknown"
    conf = float(v.get("confidence") or 0)
    val = v.get("value")
    if not isinstance(val, bool):
        return "unknown"           # null 或非布尔字符串 → 未知，不得视为已就位
    if val is False:
        return "missing" if conf >= 0.7 else "unknown"
    return "unknown" if conf < 0.7 else "ok"


def _equipment_state(scene: dict) -> str:
    """机械/塔吊运行状态聚合（2026-09-29 新增，golden set 反馈的核心判别维度）。

    返回 running / idle / uncertain / none：
      running   任一设备 value=运行中 且 conf>=0.5；
      idle      设备在场且全部 value=静止；
      uncertain 设备在场但存在不确定/低置信/缺字段（含旧版 scene JSON 无 equipment_state）；
      none      画面无机械/塔吊目标。
    """
    vals = []
    for t in scene.get("targets", []) or []:
        if not isinstance(t, dict) or t.get("type") not in ("机械", "塔吊"):
            continue
        v = t.get("equipment_state")
        val = v.get("value") if isinstance(v, dict) else None
        conf = float(v.get("confidence") or 0) if isinstance(v, dict) else 0.0
        vals.append(str(val) if (val in ("运行中", "静止") and conf >= 0.5) else "不确定")
    if not vals:
        return "none"
    if "运行中" in vals:
        return "running"
    if "不确定" in vals:
        return "uncertain"
    return "idle"


def _space_context(scene: dict) -> dict:
    """空间语义（2026-09-29 用户修订）：室内/室外 × 高空/平地。

    VLM 字段（scene.place / scene.work_height）优先；缺失时以 site_type/site_basis/
    dominant_state_basis 文本关键词兜底推断；仍无法判定返回 None（由调用方保守处理）。
    """
    s = scene.get("scene", {}) or {}
    place = str(s.get("place", "") or "")
    height = str(s.get("work_height", "") or "")
    indoor = True if place == "室内" else False if place == "室外" else None
    high = True if height == "高空" else False if height == "平地" else None
    if indoor is None or high is None:
        text = " ".join(str(s.get(k, "") or "") for k in
                        ("site_type", "site_basis", "dominant_state_basis"))
        if indoor is None:
            if "室内" in text:
                indoor = True
            elif "室外" in text:
                indoor = False
        if high is None:
            if any(k in text for k in ("临边", "悬空", "外脚手架", "高处", "临空", "攀登", "梯架")):
                high = True
            elif any(k in text for k in ("平地", "地面", "室内楼面")):
                high = False
    return {"indoor": indoor, "high": high}


def _protection(scene: dict, hazard: str) -> dict:
    """按 作业类型上下文 计算关键防护与一般装备的缺失情况。"""
    text = (scene.get("scene", {}) or {})
    context = " ".join(str(text.get(k, "") or "") for k in ("dominant_state", "site_type",
                                                            "dominant_state_basis"))
    critical = list(CRITICAL_ALWAYS)
    for kw, items in CRITICAL_CONTEXT.items():
        if kw in context:
            critical += [i for i in items if i not in critical]
    soft = list(SOFT_ITEMS)
    if hazard == "未知":                      # 作业类型未知时不设必备项
        critical, soft = [], []
    missing_critical, missing_soft, unknown_fields = [], [], 0
    workers = 0
    for t in scene.get("targets", []) or []:
        if not isinstance(t, dict) or t.get("type") != "工人":
            continue
        workers += 1
        for item in critical:
            v = (t.get("ppe", {}) or {}).get(item) or (t.get("body_equipment", {}) or {}).get(item)
            if _field_flag(v) == "missing":
                missing_critical.append({"bbox_id": t.get("bbox_id"), "item": item})
        for item in soft:
            v = (t.get("ppe", {}) or {}).get(item) or (t.get("body_equipment", {}) or {}).get(item)
            if _field_flag(v) == "missing":
                missing_soft.append({"bbox_id": t.get("bbox_id"), "item": item})
        # unknown_fields 统计全部装备字段（含非必备项），用于证据缺口评估
        for group in ("ppe", "body_equipment"):
            for v in (t.get(group, {}) or {}).values():
                if _field_flag(v) == "unknown":
                    unknown_fields += 1
    env_issues = []
    for e in scene.get("env_protections", []) or []:
        if any(k in str(e) for k in ENV_NEGATIVE_KEYWORDS):
            env_issues.append(str(e))
    return {"critical_required": critical, "soft_required": soft,
            "missing_critical": missing_critical, "missing_soft": missing_soft,
            "unknown_fields": unknown_fields, "env_issues": env_issues, "n_workers": workers}


def _supervision(scene: dict) -> dict:
    workers = [t for t in scene.get("targets", []) or []
               if isinstance(t, dict) and t.get("type") == "工人"]
    n = len(workers)
    actions = [str(t.get("action", "") or "") for t in workers]
    observer = any(("观察" in a or "监护" in a or "看护" in a) for a in actions) and n >= 2
    solo = n == 1
    relations = len(scene.get("spatial_relations", []) or [])
    return {"n_workers": n, "solo": solo, "observer_present": observer,
            "relations_recorded": relations}


def evaluate_risk(scene: dict) -> dict:
    """三要素确定性判定。返回可 JSON 序列化的判定对象（含 drivers 与证据缺口）。"""
    s = scene.get("scene", {}) or {}
    # 固有风险只看"正在做什么"（作业描述+目标动作），不看 site_type——
    # 否则"基坑周边/塔吊回转覆盖区"等位置描述会让全部在场作业误升高危（golden set pair25 反馈）
    work_text = " ".join(str(s.get(k, "") or "") for k in ("dominant_state", "dominant_state_basis"))
    for t in scene.get("targets", []) or []:
        if isinstance(t, dict) and t.get("action"):
            work_text += " " + str(t["action"])
    hazard, matched = _match_hazard(work_text)

    prot = _protection(scene, hazard)
    sup = _supervision(scene)

    drivers: list[str] = []
    evidence_gaps: list[str] = []
    risk: str | None = None        # 仅由确定性事实 escalate；判不了时保持 None（未判定）
    review = False                 # 存在关键未知项（判不了）

    def escalate(level: str, reason: str):
        nonlocal risk
        if risk is None or SEVERITY[level] > SEVERITY[risk]:
            risk = level
        drivers.append(reason)

    if prot["n_workers"] == 0:
        # 感知链健康前置（2026-09-29 修复）："空 targets" ≠ "现场无人"。
        # VLM 降级/解析失败时场景被置空，n_workers 必为 0；此时判"安全"会把感知失效
        # 误判成现场安全（pair28 案例：GDYOLO 实检 1 名 conf=0.926 的工人，系统却判
        # "安全-记录"归档）。感知链不健康时不得输出任何风险等级，一律转人工。
        meta = scene.get("_meta") or {}
        if meta.get("degraded") or meta.get("parsing_error"):
            drivers.append("R-PERCEPT-DEGRADED-感知链失效（VLM 降级/解析失败），"
                           "零目标不可作为安全依据")
            return {"work_hazard": {"level": hazard, "matched": matched},
                    "protection": prot, "supervision": sup,
                    "overall_risk": None, "drivers": drivers,
                    "evidence_gaps": ["感知链失效：VLM 降级或解析失败，目标清单不可信"],
                    "need_human_review": True}
        # 区分"目标清单为空"与"仅机械/塔吊在场"，避免审计串文案与事实不符
        if not (scene.get("targets") or []):
            drivers.append("R-NOWORKER-目标清单为空，未检出人员或机械")
        else:
            drivers.append("R-NOWORKER-仅机械/塔吊在场，无人员暴露")
        return {"work_hazard": {"level": hazard, "matched": matched},
                "protection": prot, "supervision": sup,
                "overall_risk": "安全", "drivers": drivers,
                "evidence_gaps": [], "need_human_review": False}

    if hazard == "未知":
        review = True
        drivers.append("R-WORK-UNKNOWN-作业类型无法判定，人工判级")
        if prot["missing_critical"]:
            escalate("高风险", "R-PPE-CRITICAL-关键防护（安全帽/安全带）显式缺失:"
                     + json.dumps(prot["missing_critical"], ensure_ascii=False))
    elif hazard == "高":
        escalate("低风险", f"R-WORK-HIGH-{','.join(matched) or '高危作业'}-固有高风险作业")
        if prot["missing_critical"]:
            escalate("高风险", "R-PPE-CRITICAL-关键防护（安全帽/安全带）显式缺失:"
                     + json.dumps(prot["missing_critical"], ensure_ascii=False))
        if prot["missing_soft"]:
            drivers.append("R-PPE-SOFT-" + SOFT_NOTE + ":" + json.dumps(prot["missing_soft"], ensure_ascii=False))
        if sup["solo"]:
            # 空间语义门控（2026-09-29 用户仲裁）：单人高危作业，室外且高空才升"高风险"；
            # 室内/平地保持提示级；空间未知保守升级并记证据缺口
            ctx = _space_context(scene)
            if ctx["high"] is True and ctx["indoor"] is False:
                escalate("高风险", "R-SUP-SOLO-HIGH-单人室外高空高危作业，无监护陪护")
            elif ctx["indoor"] is True or ctx["high"] is False:
                drivers.append("R-SUP-SOLO-SHELTERED-单人室内/平地高危作业（无临空坠落增幅），保持提示级")
            else:
                escalate("高风险", "R-SUP-SOLO-HIGH-单人高危作业且空间语义未知（保守升级），无监护陪护")
                evidence_gaps.append("空间语义缺失（室内/室外、高空/平地）")
        if sup["observer_present"]:
            drivers.append("R-SUP-OBSERVER-现场存在观察者/监护者，陪护要素在位")
    elif hazard == "中":
        escalate("低风险", f"R-WORK-MID-{','.join(matched) or '中等风险作业'}-作业暴露")
        if prot["missing_critical"]:
            escalate("高风险", "R-PPE-CRITICAL-关键防护（安全帽/安全带）显式缺失:"
                     + json.dumps(prot["missing_critical"], ensure_ascii=False))
        if prot["missing_soft"]:
            drivers.append("R-PPE-SOFT-" + SOFT_NOTE + ":" + json.dumps(prot["missing_soft"], ensure_ascii=False))
        if sup["solo"]:
            drivers.append("R-SUP-SOLO-MID-单人中等风险作业")
    else:  # 低
        if prot["missing_critical"] or prot["missing_soft"]:
            escalate("低风险", "R-PPE-LOW-防护缺失:" + json.dumps(
                prot["missing_critical"] + prot["missing_soft"], ensure_ascii=False))
        else:
            drivers.append("R-LOW-CLEAN-低风险作业且防护在位")

    # 进入危险区的直接事实（置信>=0.5）× 设备运行状态门控（2026-09-29 golden set 反馈）：
    #   设备运行中 → 高风险；设备静止 → 低风险（距离提示）；状态不确定/缺字段 → 转人工。
    # "人在危险区"不再是单独的充分条件——静止机械旁的距离问题是提示项而非告警项（pair13 反馈）
    equip = _equipment_state(scene)
    zone_labels = {"in_crane_load_zone": "吊装区", "in_machinery_radius": "机械作业半径"}
    for t in scene.get("targets", []) or []:
        if not isinstance(t, dict) or t.get("type") != "工人":
            continue
        for k, label in zone_labels.items():
            v = (t.get("state_attrs", {}) or {}).get(k)
            if not (isinstance(v, dict) and v.get("value") is True
                    and float(v.get("confidence") or 0) >= 0.5):
                continue
            if equip == "running":
                escalate("高风险",
                         f"R-ZONE-RUNNING-{k}-{t.get('bbox_id')}-设备运行中人员位于{label}")
            elif equip == "idle":
                escalate("低风险",
                         f"R-ZONE-IDLE-{k}-{t.get('bbox_id')}-设备静止人员位于{label}（距离提示）")
            else:  # uncertain / none（含旧版无 equipment_state 的场景 JSON）
                review = True
                drivers.append(f"R-ZONE-UNCERTAIN-{k}-{t.get('bbox_id')}"
                               f"-人员位于{label}但设备运行状态不确定（关键未知项，不输出等级）")
    # 夜间低风险作业的防护未知提示（golden set pair26/29：夜间巡查未配 PPE 应至少"提示"级）
    if (hazard == "低" and str(s.get("lighting", "") or "") == "夜间"
            and prot["unknown_fields"] > 0 and not prot["missing_critical"]):
        escalate("低风险", f"R-PPE-NIGHT-夜间低风险作业且{prot['unknown_fields']}个防护字段无法确认，提示核查")
    # 环境防护缺失 + 临边/洞口类场景
    if prot["env_issues"] and any(k in work_text for k in ("临边", "洞口", "基坑", "脚手架")):
        escalate("低风险", "R-ENV-环境防护缺失:" + ";".join(prot["env_issues"]))

    if prot["unknown_fields"]:
        evidence_gaps.append(f"{prot['unknown_fields']} 个个体防护字段为未知(null/低置信)")
    if hazard == "未知":
        evidence_gaps.append("作业类型未知")

    # 结果语义（2026-09-29 修订）：
    #   已确认高风险 → 输出等级（已是最高级，未决项不影响处置，仅记证据缺口）；
    #   存在关键未知项且未达高风险 → 不输出等级（None），纯转人工；
    #   禁止"安全/低风险 + need_human_review=True"并存（"判了又没判"的矛盾态）。
    if risk == "高风险":
        overall: str | None = "高风险"
        need_review = False
    elif review:
        overall = None
        need_review = True
    else:
        overall = risk or "安全"
        need_review = False

    return {"work_hazard": {"level": hazard, "matched": matched},
            "protection": prot, "supervision": sup,
            "overall_risk": overall, "drivers": drivers,
            "evidence_gaps": evidence_gaps,
            "need_human_review": need_review}
