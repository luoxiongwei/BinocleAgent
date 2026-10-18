# -*- coding: utf-8 -*-
"""RGB/IR 单对标准化与几何配准。

原图只读。输出统一到 480x640 画布，并保证 fused、visible、IR 共用同一坐标系。
仿射配准仅在边缘对齐分数确有改善且变换参数合理时采用，否则退回尺寸归一化。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from tools.common import project_root

CANON_W, CANON_H = 480, 640
ALPHA_VIS = 0.43
EDGE_SIGMA = 3.0


def _read(path: Path) -> np.ndarray | None:
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def _write(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = ".png" if path.suffix.lower() == ".png" else ".jpg"
    ok, data = cv2.imencode(suffix, image)
    if not ok:
        raise IOError(f"图像编码失败: {path}")
    data.tofile(str(path))


def _edge_score(left: np.ndarray, right: np.ndarray) -> float:
    e_left = cv2.Canny(left, 50, 150)
    e_right = cv2.Canny(right, 50, 150)
    distance = cv2.distanceTransform(255 - e_left, cv2.DIST_L2, 3)
    mask = e_right > 0
    if int(mask.sum()) < 50:
        return 0.0
    d = distance[mask]
    return float(np.mean(np.exp(-(d ** 2) / (2.0 * EDGE_SIGMA ** 2))))


def _gradient(gray: np.ndarray) -> np.ndarray:
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    return cv2.normalize(mag, None, 0.0, 1.0, cv2.NORM_MINMAX)


def _sane_affine(matrix: np.ndarray) -> bool:
    linear = matrix[:, :2]
    determinant = float(np.linalg.det(linear))
    sx = float(np.linalg.norm(linear[:, 0]))
    sy = float(np.linalg.norm(linear[:, 1]))
    tx, ty = abs(float(matrix[0, 2])), abs(float(matrix[1, 2]))
    return (determinant > 0.0 and 0.70 <= sx <= 1.35 and 0.70 <= sy <= 1.35
            and tx <= CANON_W * 0.25 and ty <= CANON_H * 0.25)


def _find_pair(pair_id: str, source_dir: Path) -> tuple[Path, Path]:
    match = re.fullmatch(r"pair(\d+)", pair_id, flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"非法 pair_id: {pair_id}")
    number = int(match.group(1))
    ir_path = rgb_path = None
    for path in source_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        found = re.search(r"(\d+)", path.stem)
        if not found or int(found.group(1)) != number:
            continue
        stem = path.stem.lower()
        if stem.startswith("ir"):
            ir_path = path
        elif stem.startswith(("rgb", "vis")):
            rgb_path = path
    if ir_path is None or rgb_path is None:
        raise FileNotFoundError(f"{pair_id} 缺少 RGB/IR 原图: {source_dir}")
    return ir_path, rgb_path


def prepare_pair(pair_id: str, project: Path | None = None) -> dict[str, Any]:
    """标准化并尝试自动配准一对 RGB/IR，写入生产 fused/panels/QC 产物。"""
    root = Path(project or project_root())
    source_dir = root / "data" / "rgbt" / "output"
    ir_path, rgb_path = _find_pair(pair_id, source_dir)
    ir_native, rgb_native = _read(ir_path), _read(rgb_path)
    if ir_native is None or rgb_native is None:
        raise ValueError(f"{pair_id} 原图读取失败")

    ir = cv2.resize(ir_native, (CANON_W, CANON_H), interpolation=cv2.INTER_AREA)
    rgb = cv2.resize(rgb_native, (CANON_W, CANON_H), interpolation=cv2.INTER_AREA)
    ir_gray = cv2.cvtColor(ir, cv2.COLOR_BGR2GRAY)
    rgb_gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)
    before = _edge_score(ir_gray, rgb_gray)

    method = "resize_only"
    matrix = np.eye(2, 3, dtype=np.float32)
    aligned_rgb = rgb
    after = before
    error = ""
    try:
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 120, 1e-5)
        _, candidate = cv2.findTransformECC(
            _gradient(ir_gray), _gradient(rgb_gray), matrix.copy(), cv2.MOTION_AFFINE, criteria)
        warped = cv2.warpAffine(
            rgb, candidate, (CANON_W, CANON_H),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_REFLECT)
        warped_gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
        candidate_score = _edge_score(ir_gray, warped_gray)
        if _sane_affine(candidate) and candidate_score >= before + 0.01:
            aligned_rgb, rgb_gray, after = warped, warped_gray, candidate_score
            matrix, method = candidate, "ecc_affine"
    except cv2.error as exc:
        error = str(exc).splitlines()[0][:240]

    fused = (ALPHA_VIS * rgb_gray.astype(np.float32)
             + (1.0 - ALPHA_VIS) * ir_gray.astype(np.float32)).astype(np.uint8)
    fused_path = root / "results" / "fused" / f"{pair_id}.jpg"
    panels_dir = root / "results" / "panels"
    _write(fused_path, fused)
    _write(panels_dir / f"{pair_id}_vis.jpg", aligned_rgb)
    _write(panels_dir / f"{pair_id}_ir.jpg", ir)

    record = {
        "id": pair_id,
        "ir_src": ir_path.name,
        "rgb_src": rgb_path.name,
        "ir_native": f"{ir_native.shape[1]}x{ir_native.shape[0]}",
        "rgb_native": f"{rgb_native.shape[1]}x{rgb_native.shape[0]}",
        "canon": f"{CANON_W}x{CANON_H}",
        "registration": {
            "method": method,
            "edge_align_before": round(before, 4),
            "edge_align_after": round(after, 4),
            "affine_matrix": [[round(float(v), 6) for v in row] for row in matrix],
            "error": error or None,
        },
        "edge_align": round(after, 4),
    }
    qc_path = root / "results" / "fuse_qc.json"
    try:
        qc = json.loads(qc_path.read_text(encoding="utf-8")) if qc_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        qc = {}
    pairs = [item for item in (qc.get("pairs") or []) if item.get("id") != pair_id]
    pairs.append(record)
    pairs.sort(key=lambda item: int(re.search(r"\d+", item.get("id", "9999")).group()))
    scores = [float(item["edge_align"]) for item in pairs if item.get("edge_align") is not None]
    qc["pairs"] = pairs
    qc["summary"] = {
        **(qc.get("summary") or {}),
        "n_pairs": len(pairs),
        "alpha_vis": ALPHA_VIS,
        "alpha_ir": 1.0 - ALPHA_VIS,
        "canon": f"{CANON_W}x{CANON_H}",
        "edge_align_mean": round(float(np.mean(scores)), 4) if scores else None,
        "edge_align_min": round(float(np.min(scores)), 4) if scores else None,
        "registration_enabled": True,
    }
    qc_path.parent.mkdir(parents=True, exist_ok=True)
    qc_path.write_text(json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="标准化并配准单个 RGB/IR 图对")
    parser.add_argument("pair_id")
    args = parser.parse_args()
    print(json.dumps(prepare_pair(args.pair_id), ensure_ascii=False, indent=2))
