# -*- coding: utf-8 -*-
"""reconcile 对账器：VLM 开放检测 targets 与 GDYOLO 独立检测框的几何对账（R2）。

分类（**位置匹配** + 类别一致 才算 matched；框大小不参与判定）：
  matched   双方一致 → confirmed_by 补充标注双源
  vlm_only  VLM 有/YOLO 无 → 保留目标 + unverified_by_yolo 标记
             （携带违规 → 强制转人工，防幻觉直通自动告警，决策 A）
  yolo_only YOLO 有/VLM 漏 → 触发追问补充（由 vlm_judge 裁剪该区域追问 VLM）

纯几何逻辑，不调任何模型；调用方（vlm_judge/runner）负责追问与状态更新。
"""
from __future__ import annotations


def iou(a: list, b: list) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


CLASS_NAMES = {0: "工人", 1: "机械", 2: "塔吊"}


def center_of(b: list) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def center_distance(a: list, b: list) -> float:
    ax, ay = center_of(a)
    bx, by = center_of(b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def position_match(a: list, b: list, tol: float = 0.5) -> bool:
    """位置匹配 —— **与框大小无关**（2026-09-29 用户口径：只要框的数量和位置对了就行，框大小不重要）。

    判据（任一成立即视为同一目标）：
      1) 任一框的中心点落在对方框内；
      2) 两中心点距离 ≤ 较小框对角线 × tol（默认 0.5，即半条对角线内）。

    改用位置匹配的原因：VLM 自由枚举的框普遍偏大，用 IoU 会把同一目标误判成双源分歧。
    实测 pair01：VLM 框与 YOLO 框 IoU 仅 0.368 / 0.454（卡在 0.5 阈值下方），
    但中心点完全落在对方框内 —— 明明是同一目标，却被记成 `vlm_only_all + yolo_only_unresolved`。
    """
    if len(a) != 4 or len(b) != 4:
        return False
    ax, ay = center_of(a)
    bx, by = center_of(b)
    if a[0] <= bx <= a[2] and a[1] <= by <= a[3]:
        return True
    if b[0] <= ax <= b[2] and b[1] <= ay <= b[3]:
        return True
    w_a, h_a = a[2] - a[0], a[3] - a[1]
    w_b, h_b = b[2] - b[0], b[3] - b[1]
    ref = min((w_a ** 2 + h_a ** 2) ** 0.5, (w_b ** 2 + h_b ** 2) ** 0.5)
    return center_distance(a, b) <= ref * tol


def reconcile(vlm_targets: list, yolo_boxes: list, match_tol: float = 0.5) -> dict:
    """对账 VLM targets 与 YOLO 检测框（位置匹配，不看框大小）。

    vlm_targets: [{bbox_id, type, bbox_xyxy, ...}]（VLM 开放检测输出）
    yolo_boxes:  [{class_id, bbox_xyxy, conf}]（GDYOLO 输出，canvas 坐标）
    返回 {matched, vlm_only, yolo_only, summary}：
      matched  : [{"bbox_id", "type", "yolo_conf", "iou", "center_dist"}]
      vlm_only : [{"bbox_id", "type", "bbox_xyxy"}]
      yolo_only: [{"class_id", "class_name", "bbox_xyxy", "conf"}]

    类别必须一致（工人/机械/塔吊）才可匹配；多个候选时取中心距最近者。
    """
    yolo_left = []
    for idx, b in enumerate(yolo_boxes):
        yolo_left.append({
            "idx": idx, "class_id": b.get("class_id"),
            "class_name": CLASS_NAMES.get(b.get("class_id"), str(b.get("class_id"))),
            "bbox_xyxy": [float(x) for x in b.get("bbox_xyxy", [])],
            "conf": float(b.get("conf") or 0),
        })

    matched, vlm_only = [], []
    used_yolo = set()
    for t in vlm_targets or []:
        if not isinstance(t, dict):
            continue
        tb = t.get("bbox_xyxy")
        ttype = t.get("type")
        if not tb or len(tb) != 4:
            vlm_only.append({"bbox_id": t.get("bbox_id"), "type": ttype,
                             "bbox_xyxy": None, "reason": "no_bbox"})
            continue
        tb = [float(x) for x in tb]
        best, best_dist = None, None
        for yb in yolo_left:
            if yb["idx"] in used_yolo:
                continue
            if yb["class_name"] != ttype:
                continue                         # 类别必须一致才可匹配
            if not position_match(tb, yb["bbox_xyxy"], match_tol):
                continue
            dist = center_distance(tb, yb["bbox_xyxy"])
            if best_dist is None or dist < best_dist:
                best, best_dist = yb, dist
        if best is not None:
            used_yolo.add(best["idx"])
            matched.append({"bbox_id": t.get("bbox_id"), "type": ttype,
                            "yolo_conf": best["conf"],
                            "iou": round(iou(tb, best["bbox_xyxy"]), 3),
                            "center_dist": round(best_dist or 0.0, 1)})
        else:
            vlm_only.append({"bbox_id": t.get("bbox_id"), "type": ttype,
                             "bbox_xyxy": tb})

    yolo_only = [yb for yb in yolo_left if yb["idx"] not in used_yolo]

    summary = {"n_vlm": len(vlm_targets or []), "n_yolo": len(yolo_left),
               "matched": len(matched), "vlm_only": len(vlm_only),
               "yolo_only": len(yolo_only), "match_tol": match_tol}
    return {"matched": matched, "vlm_only": vlm_only, "yolo_only": yolo_only,
            "summary": summary}
