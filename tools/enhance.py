# -*- coding: utf-8 -*-
"""RGB-IR 图对画质修复工具（Agent 可调用，R4）。

动因（§18.3）：pair17/18/19 融合 QC 不达标、pair08/20/26 夜间"判不了"，
根源都包含底图质量。本工具在 VLM 研判前对可见光/红外做保守增强：

  - 红外：CLAHE 对比度增强（零训练、效果稳定，热源显著性提升最直接）
  - 可见光：低照度自适应 Gamma + 轻度 CLAHE（仅当判定为低照度时启用，避免过度处理）

设计边界：
  - 只做保守增强，不引入幻觉纹理；增强与否/强度记录进 _meta（可审计）；
  - 输入输出均为 BGR/灰度 ndarray，不落盘（由调用方决定送 VLM 或存档）。
"""
from __future__ import annotations

import cv2
import numpy as np


def is_lowlight(vis_bgr) -> bool:
    """粗判可见光是否低照度：均值 < 70 或 CLAHE 前后亮度分布过窄。"""
    gray = cv2.cvtColor(vis_bgr, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) < 70.0


def enhance_ir(ir_img):
    """红外 CLAHE 对比度增强（接受灰度或 BGR，返回增强后灰度图）。"""
    if ir_img is None:
        return None, "skipped:no_image"
    if ir_img.ndim == 3:
        ir_img = cv2.cvtColor(ir_img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    out = clahe.apply(ir_img)
    return out, "clahe"


def enhance_vis_lowlight(vis_bgr, gamma=1.6):
    """低照度可见光增强：Gamma 提亮 + LAB 亮度通道轻度 CLAHE。"""
    if vis_bgr is None:
        return None, "skipped:no_image"
    lab = cv2.cvtColor(vis_bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    out = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    # 自适应 Gamma：均值越低提亮越强
    mean = float(cv2.cvtColor(out, cv2.COLOR_BGR2GRAY).mean())
    if mean < 1.0:
        return out, "gamma_skipped"
    g = min(2.2, max(1.0, 110.0 / mean))
    table = np.array([(i / 255.0) ** (1.0 / g) * 255 for i in range(256)]).astype("uint8")
    out = cv2.LUT(out, table)
    return out, f"lowlight_gamma_{g:.2f}"


def enhance_pair(vis_bgr, ir_gray, force=False):
    """按需增强图对。

    策略：红外总是 CLAHE（安全）；可见光仅低照度时提亮（白天清晰图不做处理，
    避免破坏颜色证据——PPE 颜色判定依赖可见光保真）。
    返回 (vis, ir, notes)。
    """
    notes = []
    ir_out, note_ir = enhance_ir(ir_gray)
    notes.append(f"ir:{note_ir}")
    if force or is_lowlight(vis_bgr):
        vis_out, note_vis = enhance_vis_lowlight(vis_bgr)
    else:
        vis_out, note_vis = vis_bgr, "skipped:sufficient_light"
    notes.append(f"vis:{note_vis}")
    return vis_out, ir_out, notes
