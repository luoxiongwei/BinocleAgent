# -*- coding: utf-8 -*-
"""L2 VLM 结构化判定（融合检出 + 图对输入）。

链路位置: L1 融合图检测 -> 检测框 -> 本脚本 -> 场景 JSON -> L3 判级

输入三图（同画布 480x640，框坐标通用）:
  1) 融合图（灰度，L1 实际推理的图，带框编号）—— VLM 看到的就是检测器看到的世界
  2) 可见光同视场图（真彩色）—— 判 PPE 颜色与场景语义的唯一证据来源
  3) 红外同视场图（灰度）—— 遮挡/夜间的人员存在性交叉确认

模式:
  默认 dry-run : 配对 + 画框 + 装配校验，不调 API，输出 annotated 图供抽查
  --run        : 真正调用 VLM API（key 从 config.json 经 ModelGateway 读取）

用法:
  python vlm_judge.py
  python vlm_judge.py --run
  python vlm_judge.py --run --pair-id pair02 --limit 1   # 单图对（Agent 调用入口）
"""
import argparse, base64, json, os, re, sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.api_config import load_api_config
from tools.common import validate_schema
from tools.model_gateway import ModelGateway
from tools.enhance import enhance_pair
from tools.reconcile import reconcile

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE / "data" / "rgbt"
FUSED_DIR = BASE / "results" / "fused"
PANEL_DIR = BASE / "results" / "panels"            # 与 fused 同源的配准面板（align_fuse 产出）
DET_JSON = BASE / "results" / "l1_smoke" / "l1_detections.json"
DEFAULT_OUT = BASE / "results" / "l2_scene"

# 统一配置：mode 等从 config.json 读取；API 的 model/base/key 由 ModelGateway 统一管理
try:
    _VLM_CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8")).get("vlm", {})
except (FileNotFoundError, json.JSONDecodeError):
    _VLM_CFG = {}

# 重查允许的可见光状态集合（17.2 边界条件 3）：夜间/过暗/过曝场景跳过重查，null 交由 P3
RECHECK_VISIBILITY = tuple(_VLM_CFG.get("recheck_visibility", ["清晰"]))

IDX2NAME = {0: "工人", 1: "机械", 2: "塔吊"}
CLS_COLOR = {0: (0, 255, 0), 1: (220, 120, 0), 2: (0, 160, 255)}  # BGR
CANVAS_W, CANVAS_H = 480, 640

SYSTEM_PROMPT = (
    "你是施工现场安全图像的研判员。同一视场的三张图同时输入：\n"
    "  图1 融合图（灰度，检测器实际输入，框已按 bbox_id 编号）\n"
    "  图2 可见光图（真彩色）\n"
    "  图3 红外图（灰度）\n"
    "三图同画布，框坐标通用。融合图提供几何轮廓与检测框基准，可见光图提供颜色纹理与场景语义，红外图提供热源分布。"
    "请你结合三张图综合分析：每个结论都必须是三图可用信息综合后的结果，并用 confirmed_by 标注证据来自哪些模态。\n"
    "硬性规则：\n"
    "1) 自行枚举画面中全部目标（工人/机械/塔吊），按从左到右、从上到下顺序编号 t1、t2…；"
    "每个目标输出 bbox_xyxy=[x1,y1,x2,y2]（画布坐标）。不得遗漏清晰可见目标，不得虚构目标；"
    "模糊/远处目标也需枚举并在 uncertainty 说明。\n"
    "2) 先识别每个工人目标作用的客体，写出动作描述 action，并识别场地类型 site_type，再由全部目标的 action + site_type + 相互位置关系综合推断 dominant_state"
        "（自由描述具体作业类型，如塔吊吊装、土方开挖、钢筋绑扎、模板支设、砌筑、焊接、高处安装、材料转运、场地清理等，不受固定类别限制，无法判断才用『无明确作业』），"
        "推断依据必须写入 dominant_state_basis。并根据目标的属性和位置关系推测实际距离。"
        "再判定场景语义：place（室内|室外：依据围护结构、屋顶、天空背景等环境线索）与 work_height（高空|平地：作业面是否临空/存在坠落高度——外脚手架作业面/临边/梯架之上为高空，地面/室内楼面为平地），各自依据写入 *_basis；无法判定填『不确定』，不得默认平地。"
        "每个机械/塔吊目标必须给出 equipment_state（运行中|静止|不确定）：依据吊物离地/吊索张紧/臂架回转/车体位移/料斗装载/红外排气热源/烟尘等线索判定；"
        "画面无法确认时填『不确定』且 confidence=0，不得默认静止——运行状态是风险分级的关键证据。\n"
    "3) 防护装备按『个体穿戴 → 个体外部装备 → 环境防护装置』三层逐项独立判定："
        "个体穿戴 ppe（helmet/vest/gloves/safety_shoes，头部、躯干、手部、足部）；"
        "判断为高空/悬空/临边作业时，需要注意个体外部装备 body_equipment（safety_belt/safety_rope 等防坠系挂装置，图中可见但未列出的装备写入 other 数组）；"
        "也要从可见光图像中识别环境防护装置并写入 scene.env_protections（安全网、临边护栏、洞口盖板、警示标志等，须注明完好/缺失/不可见状态）。不得只关注其中某一项；"
        "某项装备在画面中完全不可见时置 null，不得因看不清就默认未佩戴；"
        "穿戴与否及颜色以可见光图为准；可见光看不清但红外有热源时，可判人员在位、该项置 null。\n"
    "4) 属性/关系不确定时置 value=null、confidence=0，绝不猜测。\n"
    "5) 只输出合法 JSON，无任何附加文字。"
)

USER_SCHEMA = (
    "请严格按以下 JSON schema 输出（枚举值不得越界，不确定用 null 且 confidence=0）：\n"
    '{"image_id":"<时间戳>",'
    '"scene":{"lighting":"白天|黄昏|夜间","rgb_visibility":"清晰|过暗|过曝",'
    '"ir_quality":"良好|一般|差",'
    '"dominant_state":"<具体作业/工序的自由描述，如塔吊吊装|土方开挖|钢筋绑扎|模板支设|砌筑|焊接|高处安装|材料转运|场地清理等；无法判断才填无明确作业>",'
    '"dominant_state_basis":"<一句话：由哪些 bbox 的 action 与位置关系推出该作业类型>",'
    '"site_type":"<具体场地/区域的自由描述，如深基坑周边|外脚手架作业面|塔吊回转覆盖区|钢筋加工棚|材料堆场|临边通道等>",'
    '"site_basis":"<一句话：依据哪些环境要素判定该场地类型>",'
    '"place":"室内|室外|不确定","place_basis":"<一句话依据>",'
    '"work_height":"高空|平地|不确定","work_height_basis":"<一句话依据>",'
    '"env_protections":["<环境防护装置及状态，如临边护栏已设置|安全网完好|洞口未加盖|警示标志缺失等；无则空数组>"]},'
    '"targets":[{"bbox_id":"t1","type":"工人|机械|塔吊",'
    '"bbox_xyxy":[x1,y1,x2,y2],'
    '"action":"<动作+作用的客体，如:在砖墙前砌筑|搬运袋装水泥|操作挖掘机|攀爬金属梯架；工人必填，机械/塔吊填运行状态如静止/回转/带载>",'
    '"equipment_state":{"value":"运行中|静止|不确定","confidence":0.0},'
    '"ppe":{"helmet":{"value":true|false|null,"confidence":0.0},'
    '"vest":{"value":true|false|null,"confidence":0.0},'
    '"gloves":{"value":true|false|null,"confidence":0.0},'
    '"safety_shoes":{"value":true|false|null,"confidence":0.0}},'
    '"body_equipment":{"safety_belt":{"value":true|false|null,"confidence":0.0},'
    '"safety_rope":{"value":true|false|null,"confidence":0.0},'
    '"other":["<画面中可见但未列出的其他防护装备，如防坠器、绝缘手套、防尘口罩；无则空数组>"]},'
    '"state_attrs":{"in_crane_load_zone":{"value":true|false|null,"confidence":0.0},'
    '"in_machinery_radius":{"value":true|false|null,"confidence":0.0}},'
    '"occluded":true|false,"confirmed_by":"rgb|ir|both"}],'
    '"spatial_relations":[{"subject":"t2","object":"t1","relation":"<定性描述>","confidence":0.0}],'
    '"candidate_violations":["<描述性短语，不带等级/条款>"],'
    '"uncertainty":"<不确定性说明，无则空串>"}\n'
)


def imread_u(path, color=True):
    flag = cv2.IMREAD_COLOR if color else cv2.IMREAD_GRAYSCALE
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), flag)


def imwrite_u(path, img, quality=95):
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise IOError(f"编码失败: {path}")
    buf.tofile(str(path))


def ts_of(name: str) -> str:
    m = re.search(r"(HM\d{14})", name)
    return m.group(1) if m else Path(name).stem


def load_manifest(det_path=None, fused_dir=None, panel_dir=None):
    """遍历融合图，挂上检测框与同源配准面板（align_fuse 产出，保证 L1/L2 几何一致）。"""
    det_path = Path(det_path or DET_JSON)
    fused_dir = Path(fused_dir or FUSED_DIR)
    panel_dir = Path(panel_dir or PANEL_DIR)
    det = json.loads(det_path.read_text(encoding="utf-8"))["detections"] if det_path.exists() else {}

    items = []
    for f in sorted(fused_dir.glob("*.jpg")):
        ts = ts_of(f.name)
        vis_p = panel_dir / f"{ts}_vis.jpg"
        ir_p = panel_dir / f"{ts}_ir.jpg"
        if not (vis_p.exists() and ir_p.exists()):
            print(f"[warn] {ts} 缺配准面板，跳过")
            continue
        boxes = det.get(f.name, [])
        items.append({"ts": ts, "fused": f,
                      "vis": imread_u(vis_p, color=True),
                      "ir": imread_u(ir_p, color=True),
                      "boxes": boxes})
    return items


def draw_boxes(img, boxes):
    """在图上按 bbox_id 编号画框（ASCII 编号，规避 OpenCV 无中文字形）。"""
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    else:
        img = img.copy()
    for i, b in enumerate(boxes, 1):
        x1, y1, x2, y2 = [int(v) for v in b["bbox_xyxy"]]
        color = CLS_COLOR.get(b["class_id"], (200, 200, 200))
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        cv2.putText(img, f"t{i}", (x1, max(16, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    return img


def build_user_content(ts, img_paths):
    """组装开放检测多模态 user content（无检测框清单注入，VLM 自行枚举目标）。"""
    def b64(path):
        return base64.b64encode(Path(path).read_bytes()).decode()

    content = []
    for tag, p in img_paths:
        content.append({"type": "text", "text": f"{tag}："})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64(p)}"}})
    content.append({"type": "text", "text": f"image_id: {ts}\n" + USER_SCHEMA})
    return content


def sanitize_targets(obj, canvas_w=CANVAS_W, canvas_h=CANVAS_H):
    """坐标毛刺修正：将越界 bbox 夹取到画布内（VLM 坐标输出常见小越界）。返回夹取数。"""
    clamped = 0
    for t in obj.get("targets", []) or []:
        if not isinstance(t, dict):
            continue
        b = t.get("bbox_xyxy")
        if isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b):
            nb = [int(min(max(v, 0), lim)) for v, lim in zip(b, (canvas_w, canvas_h, canvas_w, canvas_h))]
            if nb != [int(v) for v in b]:
                t["bbox_xyxy"] = nb
                clamped += 1
    return clamped


_FLAG_GROUPS = ("ppe", "body_equipment", "state_attrs")
_EQUIP_VALUES = ("运行中", "静止", "不确定")


def sanitize_field_values(obj) -> int:
    """把越界的字段值规整回契约，并在入口拦截（2026-09-29）。

    `value` 只允许 true/false/null；`equipment_state.value` 只允许 运行中/静止/不确定/null。
    实测模型偶尔写成描述串（如 `"黄色安全帽"`）——**后果有安全含义**：
    下游 `_field_flag` 曾把非布尔值当"已就位"，若模型写的是「未戴安全帽」就会被读成合规。
    这里就地转为 null/不确定 并计数（写入 `_meta.coerced_values`），
    使落盘产物始终合规，同时把这次越界记录在案。
    """
    coerced = 0
    for t in obj.get("targets", []) or []:
        if not isinstance(t, dict):
            continue
        for group in _FLAG_GROUPS:
            for v in (t.get(group) or {}).values():
                if isinstance(v, dict) and v.get("value") is not None \
                        and not isinstance(v.get("value"), bool):
                    v["value"] = None
                    v["confidence"] = 0
                    coerced += 1
        es = t.get("equipment_state")
        if isinstance(es, dict) and es.get("value") not in (*_EQUIP_VALUES, None):
            es["value"] = "不确定"
            es["confidence"] = 0
            coerced += 1
    return coerced


def drop_degenerate_targets(obj, min_size=4):
    """剔除夹取后退化的目标框（宽或高 < min_size，如坐标大部分越界的坏框）。

    被剔除的目标若真实存在，会由 reconcile 的 yolo_only 补漏环节找回。
    返回剔除数。
    """
    dropped = 0
    targets = obj.get("targets", []) or []
    kept = []
    for t in targets:
        b = t.get("bbox_xyxy") or []
        if (isinstance(b, list) and len(b) == 4
                and all(isinstance(v, (int, float)) for v in b)
                and (b[2] - b[0]) >= min_size and (b[3] - b[1]) >= min_size):
            kept.append(t)
        else:
            dropped += 1
    obj["targets"] = kept
    return dropped


def validate_scene(obj, canvas_w=CANVAS_W, canvas_h=CANVAS_H):
    """开放检测校验：bbox_id 自洽（t1…tN 连续唯一）、bbox 坐标合法、工人带装备字段。

    2026-09-28 修订：不再与 YOLO 框数比对（VLM 先行自行枚举目标）；
    空 targets 合法（画面确无目标）。
    """
    if not isinstance(obj, dict):
        return False, "非 JSON 对象"
    targets = obj.get("targets", [])
    if not isinstance(targets, list):
        return False, "targets 非列表"
    if not targets:
        return True, ""
    ids = [t.get("bbox_id") for t in targets if isinstance(t, dict)]
    expect = [f"t{i}" for i in range(1, len(ids) + 1)]
    if sorted(ids) != sorted(expect):
        return False, f"bbox_id 必须为连续的 t1…t{len(ids)}，实际: {ids}"
    if len(set(ids)) != len(ids):
        return False, "bbox_id 存在重复"
    for t in targets:
        if not isinstance(t, dict):
            return False, "targets 含非对象元素"
        b = t.get("bbox_xyxy")
        if (not isinstance(b, list) or len(b) != 4
                or not all(isinstance(v, (int, float)) for v in b)):
            return False, f"{t.get('bbox_id')} bbox_xyxy 非法"
        x1, y1, x2, y2 = b
        if not (0 <= x1 < x2 <= canvas_w + 5 and 0 <= y1 < y2 <= canvas_h + 5):
            return False, f"{t.get('bbox_id')} bbox 越界: {b}"
        if t.get("type") == "工人":
            for g in ("ppe", "body_equipment"):
                if not isinstance(t.get(g), dict):
                    return False, f"{t.get('bbox_id')} 缺少 {g} 字段"
    return True, ""


def recheck_eligible(scene, allowed=("清晰",)):
    """重查场景过滤（L2 判定标准 17.2 边界条件 3 的实现）。

    局部裁剪只在可见光可辨时才有新证据价值：夜间/过暗/过曝场景的 null
    是"底图信息不足"而非"模型没仔细看"，裁剪放大无法凭空恢复像素，
    强行重查只会烧 token（此前 98 次尝试 0 更新的主因之一）。
    这类不确定性直接交给分级处置（P3 常规复核）。返回 (是否重查, 原因)。
    """
    s = (scene.get("scene") or {}) if isinstance(scene, dict) else {}
    vis = s.get("rgb_visibility") or ""
    if allowed and vis not in allowed:
        return False, f"rgb_visibility={vis or '未知'} 不在重查允许集 {sorted(allowed)}"
    return True, ""


def collect_uncertain_fields(scene, conf_threshold=0.5,
                             groups=("ppe", "body_equipment"), include_equipment=False):
    """收集 value=null 或 confidence<阈值 的字段，按 bbox_id 聚合。

    边界条件：默认只收集近距离可观察的个体装备字段（ppe/body_equipment），且仅限工人目标——
    机械/塔吊没有个体装备，其 ppe 全 null 是正常语义而非待重查的不确定性。
    state_attrs（作业半径/吊装区等空间关系）与 scene 级字段需要全画面上下文，
    局部裁剪重查不适用，不进入重查范围。

    include_equipment=True（定向重感知复核 recrop 使用）：额外收集机械/塔吊的
    equipment_state（value 非『运行中/静止』或低置信/缺字段），用于裁决
    "设备运行状态 vs 几何证据"的冲突。
    """
    out = {}
    for t in scene.get("targets", []) or []:
        if not isinstance(t, dict):
            continue
        tid = t.get("bbox_id")
        if not tid:
            continue
        fields = {}
        if t.get("type") == "工人":
            for group in groups:
                for k, v in (t.get(group) or {}).items():
                    if isinstance(v, dict) and (
                            v.get("value") is None
                            or float(v.get("confidence") or 0) < conf_threshold):
                        fields[f"{group}.{k}"] = v
        if include_equipment and t.get("type") in ("机械", "塔吊"):
            es = t.get("equipment_state")
            if not isinstance(es, dict) or (
                    es.get("value") not in ("运行中", "静止")
                    or float(es.get("confidence") or 0) < conf_threshold):
                fields["equipment_state"] = es if isinstance(es, dict) else {}
        if fields:
            out[tid] = fields
    return out


def equipment_uncertain(obj) -> list:
    """列出设备运行状态未定的机械/塔吊 bbox_id（value 非『运行中/静止』或低置信）。

    equipment_state 决定规则引擎的 R-ZONE-RUNNING / IDLE / UNCERTAIN 三岔路，
    是风险分级的关键证据；但实测它在同一张图上也会摇摆（2026-09-29 观察到
    同图两次分别输出『静止/0.7』与『不确定/0.0』）。因此对未定项强制做一次局部放大复核。
    """
    out = []
    for t in obj.get("targets", []) or []:
        if not isinstance(t, dict) or t.get("type") not in ("机械", "塔吊"):
            continue
        es = t.get("equipment_state")
        if not isinstance(es, dict) or (
                es.get("value") not in ("运行中", "静止")
                or float(es.get("confidence") or 0) < 0.5):
            if t.get("bbox_id"):
                out.append(t["bbox_id"])
    return out


def crop_zoom(img, box, label="", factor=2.5, margin=0.15):
    """按 bbox 裁剪局部并放大，返回带框标注的 BGR 图。"""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in box["bbox_xyxy"]]
    mx, my = int((x2 - x1) * margin), int((y2 - y1) * margin)
    cx1, cy1 = max(0, x1 - mx), max(0, y1 - my)
    cx2, cy2 = min(w, x2 + mx), min(h, y2 + my)
    crop = img[cy1:cy2, cx1:cx2]
    if crop.size == 0:
        return img
    zoom = cv2.resize(crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
    if zoom.ndim == 2:
        zoom = cv2.cvtColor(zoom, cv2.COLOR_GRAY2BGR)
    bx1, by1 = int((x1 - cx1) * factor), int((y1 - cy1) * factor)
    bx2, by2 = int((x2 - cx1) * factor), int((y2 - cy1) * factor)
    color = CLS_COLOR.get(box.get("class_id"), (200, 200, 200))
    cv2.rectangle(zoom, (bx1, by1), (bx2, by2), color, 2)
    cv2.putText(zoom, label, (bx1, max(16, by1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    return zoom


def load_raw_rgb(pair_ts: str, raw_dir):
    """加载原始全分辨率可见光图（面板是 480x640 降采样，重查需要更高分辨率）。"""
    m = re.search(r"(\d+)", pair_ts or "")
    if not m:
        return None, None
    idx = m.group(1).lstrip("0") or "0"   # pair01 -> 1（文件名 rgb图片1.png 无前导零）
    raw_dir = Path(raw_dir)
    for cand in sorted(raw_dir.glob(f"rgb图片{idx}.*")):
        img = imread_u(cand, color=True)
        if img is not None:
            return img, cand.name
    return None, None


def recheck_pair(scene, item, gateway, model_id, stats, max_crops=3, raw_rgb=None,
                 only_targets=None, only_fields=None, include_equipment=False):
    """主动感知回路：对 null/低置信的个体装备字段裁剪放大定向重查（就地更新 scene）。

    覆盖范围：默认 ppe / body_equipment；`include_equipment=True` 时含机械/塔吊的
    equipment_state（供定向重感知复核裁决"状态 vs 几何"冲突）。
    分辨率：优先使用原始全分辨率可见光图（坐标按画布 480x640 → 原图尺寸换算），
    红外仍用配准面板（红外原生分辨率不高于面板）。
    只重查、只更新列出的不确定字段；新值 confidence>=0.5 且非 null 才采纳。
    每图对最多一次重查调用、最多 max_crops 个目标局部图，控制成本。

    `only_targets` / `only_fields`（2026-09-29 新增，默认 None = 原行为）：把本次复核
    限定到指定 bbox_id 与字段——这是"针对分歧点做细粒度重感知"的基础。
    """
    uncertain = collect_uncertain_fields(scene, include_equipment=include_equipment)
    if only_targets:                      # 限定复核目标（定向重感知的基础）
        keep = {str(t) for t in only_targets}
        uncertain = {k: v for k, v in uncertain.items() if k in keep}
    if only_fields:                       # 限定复核字段，接受 'helmet' 或 'ppe.helmet'
        raw_f = {str(f) for f in only_fields}
        short_f = {f.split(".")[-1] for f in raw_f}
        narrowed: dict = {}
        for tid, fields in uncertain.items():
            kept = {fk: fv for fk, fv in fields.items()
                    if fk in raw_f or fk.split(".")[-1] in short_f}
            if kept:
                narrowed[tid] = kept
        uncertain = narrowed
    attempted = sum(len(v) for v in uncertain.values())
    if not attempted:
        return {"attempted": 0, "updated": 0, "skipped": "no_uncertain_fields"}
    tid_list = sorted(uncertain, key=lambda t: -len(uncertain[t]))[:max_crops]
    boxes_by_id = {t.get("bbox_id"): {"bbox_xyxy": t.get("bbox_xyxy")}
                   for t in scene.get("targets", []) if isinstance(t, dict)}
    raw_meta = None
    if raw_rgb is not None:
        raw_meta = {"size": list(raw_rgb.shape[:2])[::-1]}

    content = [{"type": "text",
                "text": "以下目标存在无法确认的防护/状态字段。附图为这些目标的可见光（原始分辨率）"
                        "与红外局部放大图，请仅针对列出的字段重新判断；视觉证据支持明确结论才给 "
                        "true/false（confidence>=0.5），仍无法判断则 value=null、confidence=0，不得猜测。"}]
    for tid in tid_list:
        box = dict(boxes_by_id.get(tid, {}))
        box["label"] = tid
        if not box.get("bbox_xyxy"):
            continue
        # 可见光：优先原始分辨率（坐标换算）；红外：配准面板
        if raw_rgb is not None:
            h, w = raw_rgb.shape[:2]
            sx, sy = w / 480.0, h / 640.0
            scaled = dict(box, bbox_xyxy=[v * s for v, s in zip(box["bbox_xyxy"], (sx, sy, sx, sy))])
            vis_img, vis_tag = raw_rgb, "可见光局部(原始分辨率)"
        else:
            scaled, vis_img, vis_tag = box, item["vis"], "可见光局部"
        crop = crop_zoom(vis_img, scaled, label=tid)
        ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if ok:
            content.append({"type": "text", "text": f"{vis_tag}（{tid}）："})
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64,"
                                                + base64.b64encode(buf.tobytes()).decode()}})
        crop_ir = crop_zoom(item["ir"], box, label=tid)
        ok, buf = cv2.imencode(".jpg", crop_ir, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if ok:
            content.append({"type": "text", "text": f"红外局部（{tid}）："})
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64,"
                                                + base64.b64encode(buf.tobytes()).decode()}})
    field_lines = [f"{tid}: " + ", ".join(sorted(uncertain[tid])) for tid in tid_list]
    content.append({"type": "text", "text": "待更新字段清单：\n" + "\n".join(field_lines)
                    + "\n只输出这些目标的更新 JSON："
                      '{"targets":[{"bbox_id":"tX","ppe":{...},"body_equipment":{...},'
                      '"equipment_state":{"value":"运行中|静止|不确定","confidence":0.0}}],'
                      '"notes":"<判断依据>"}'
                      "（仅输出清单里列出的字段，其余字段省略；equipment_state 仅对机械/塔吊需要）"})

    response = gateway.complete(
        model=model_id,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": content}],
        response_format={"type": "json_object"})
    if response.status != "ok":
        return {"attempted": attempted, "updated": 0, "error": response.error or "recheck 调用失败"}
    usage = response.data.get("usage", {})
    stats["tokens_in"] += usage.get("prompt_tokens", 0) or 0
    stats["tokens_out"] += usage.get("completion_tokens", 0) or 0

    updated = 0
    try:
        patch = json.loads(response.data["content"])
    except (json.JSONDecodeError, TypeError):
        patch = {}
    by_id = {t.get("bbox_id"): t for t in scene.get("targets", []) if isinstance(t, dict)}
    for pt in patch.get("targets", []) or []:
        if not isinstance(pt, dict):
            continue
        tgt = by_id.get(pt.get("bbox_id"))
        if not tgt:
            continue
        uncertain_fields = uncertain.get(pt.get("bbox_id"), {})   # 键格式为 "ppe.helmet" 等
        for group in ("ppe", "body_equipment"):
            for k, v in (pt.get(group) or {}).items():
                # 修复：此前用裸字段名 k 去匹配 "group.k" 格式的键，永远失配 → 采纳率恒 0
                if not isinstance(v, dict) or f"{group}.{k}" not in uncertain_fields:
                    continue
                # 形状守卫（2026-09-29）：value 只接受 true/false/null。
                # 实测模型有时把 value 写成描述串（如「黄色安全帽」），若直接采纳会破坏
                # 下游三态语义（非布尔值曾被当作"已就位"）。
                if v.get("value") is not None and not isinstance(v.get("value"), bool):
                    continue
                if v.get("value") is not None and float(v.get("confidence") or 0) >= 0.5:
                    tgt.setdefault(group, {})[k] = v
                    updated += 1
        # equipment_state（2026-09-29：定向复核"设备运行状态 vs 几何证据"冲突同样重要）
        if "equipment_state" in uncertain_fields:
            es = pt.get("equipment_state")
            if (isinstance(es, dict) and es.get("value") in ("运行中", "静止")
                    and float(es.get("confidence") or 0) >= 0.5):
                tgt["equipment_state"] = es
                updated += 1
    meta = {"attempted": attempted, "updated": updated, "targets": tid_list,
            "patch": (patch.get("targets") or [])[:6], "notes": str(patch.get("notes", ""))[:300],
            # 重查的 Thought：模型基于局部放大图的推理链，与 notes 互证
            "thought": str(response.data.get("reasoning") or "")[:400]}
    if raw_meta:
        meta["raw_rgb"] = raw_meta
    return meta


def render_images(item, out_dir, mode):
    """按 mode 产出画框图，返回 [(标签, 路径)]。"""
    ts = item["ts"]
    paths = []
    if mode in ("all", "fused"):
        p = out_dir / f"{ts}_1fused.jpg"
        imwrite_u(p, draw_boxes(imread_u(item["fused"], color=False), item["boxes"]))
        paths.append(("图1 融合图(灰度,含检测框)", p))
    if mode in ("all", "vis"):
        p = out_dir / f"{ts}_2visible.jpg"
        imwrite_u(p, draw_boxes(item["vis"], item["boxes"]))
        paths.append(("图2 可见光图(彩色,含检测框)", p))
    if mode in ("all", "ir"):
        p = out_dir / f"{ts}_3infrared.jpg"
        imwrite_u(p, draw_boxes(item["ir"], item["boxes"]))
        paths.append(("图3 红外图(灰度,含检测框)", p))
    return paths


def dryrun(args, items):
    out = Path(args.out) / "dryrun"
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for it in items:
        paths = render_images(it, out, args.mode)
        summary.append({"ts": it["ts"], "n_boxes": len(it["boxes"]),
                        "images": [p.name for _, p in paths]})
    (out / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    n = len(summary)
    n_empty = sum(1 for s in summary if s["n_boxes"] == 0)
    print(f"[dry-run] 图 {n} 张，含框 {n - n_empty} 张，无框 {n_empty} 张，送图模式={args.mode}")
    print(f"[dry-run] annotated 已存 {out}")


def run_vlm(args, items):
    cfg = load_api_config(BASE)
    gateway, model_id = ModelGateway.from_project_config(
        BASE, "vlm", timeout_seconds=int(cfg.get("api", {}).get("timeout_seconds", 120)),
        temperature=0)
    out = Path(args.out)
    tmp = out / "annotated"
    tmp.mkdir(parents=True, exist_ok=True)

    records, stats = {}, {"ok": 0, "bad": 0, "skipped": 0, "tokens_in": 0, "tokens_out": 0}
    for it in items:
        if args.enhance:
            it["vis"], it["ir"], notes = enhance_pair(it["vis"], it["ir"])
            it["enhance"] = notes
        paths = render_images(it, tmp, args.mode)
        payload = build_user_content(it["ts"], paths)
        response = gateway.complete(
            model=model_id,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": payload}],
            response_format={"type": "json_object"})
        if response.status != "ok":
            raise RuntimeError(response.error or "VLM API 调用失败")
        raw = response.data["content"]
        usage = response.data.get("usage", {})
        stats["tokens_in"] += usage.get("prompt_tokens", 0) or 0
        stats["tokens_out"] += usage.get("completion_tokens", 0) or 0
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = {"raw": raw[:800]}
        clamped = sanitize_targets(obj)
        dropped = drop_degenerate_targets(obj)
        coerced = sanitize_field_values(obj)      # 入口强制字段契约（越界值 → null/不确定）
        ok, err = validate_scene(obj)
        obj["_meta"] = {"parsing_error": not ok, "validate_error": err,
                        "n_targets": len(obj.get("targets", []) or []),
                        "coords_clamped": clamped, "targets_dropped": dropped,
                        "coerced_values": coerced,
                        "need_human_review": not ok, "enhance": it.get("enhance")}
        if not ok:
            obj["_meta"]["raw_response"] = raw[:12000]
        # 契约校验（schemas/scene.schema.json，2026-09-29 接入）：只作审计报告，
        # 不并入 parsing_error。理由：该 schema 校验的是模型自由生成的文本，任何 prompt
        # 措辞微调都可能触发形状差异；若让它决定 parsing_error，就是以"格式问题"
        # 伪造"判不了 → 转人工"，违反"判断失败必须是真判不了"的语义。
        # 漂移的硬拦截放在 tests/test_scene_schema.py（对真实产物做断言）。
        schema_ok, schema_err = validate_schema(obj, "scene")
        obj["_meta"]["schema_ok"] = schema_ok
        obj["_meta"]["schema_error"] = schema_err
        if ok and args.recheck:
            # 主动感知回路：null/低置信字段先定向重查自救，失败才留给下游转人工；
            # 场景过滤：可见光不清晰（夜间/过暗/过曝）时跳过，null 交由 P3 处置
            eligible, why = recheck_eligible(obj, RECHECK_VISIBILITY)
            if eligible:
                raw_rgb, raw_name = load_raw_rgb(it["ts"], args.raw_rgb_dir)
                obj["_meta"]["recheck"] = recheck_pair(obj, it, gateway, model_id, stats,
                                                       raw_rgb=raw_rgb)
            else:
                obj["_meta"]["recheck"] = {"attempted": 0, "updated": 0, "skipped": why}
            # 设备运行状态定向复核（2026-09-29）：仍有机械/塔吊状态未定时，
            # 用局部放大图再做一次**仅该字段**的复核，把不确定性尽量在 L2 消化掉，
            # 而不是让它一路传到规则引擎变成"判不了"。每次图对最多一次。
            pending_eq = equipment_uncertain(obj)
            if eligible and pending_eq:
                raw_rgb2, _ = load_raw_rgb(it["ts"], args.raw_rgb_dir)
                obj["_meta"]["equipment_recheck"] = recheck_pair(
                    obj, it, gateway, model_id, stats, raw_rgb=raw_rgb2, max_crops=2,
                    only_fields=["equipment_state"], include_equipment=True)
        stats["ok" if ok else "bad"] += 1
        records[it["ts"]] = obj
        print(f"  {it['ts']} 目标={len(obj.get('targets', []) or [])} {'OK' if ok else 'BAD:' + err}")

    (out / "l2_scene.json").write_text(
        json.dumps({"stats": stats, "scenes": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[run] 完成 {len(records)} 张；合法 {stats['ok']}，异常 {stats['bad']}，跳过 {stats['skipped']}")
    print(f"[run] token 输入={stats['tokens_in']} 输出={stats['tokens_out']}；结果 {out / 'l2_scene.json'}")


# ---------- 阶段2：对账补充（R2） ----------

def supplement_yolo_only(yolo_only, item, gateway, model_id, stats, scene, max_crops=3):
    """对 yolo_only 区域裁剪追问 VLM：确认漏检 → 补入 targets；确认误检 → 剔除记录。"""
    confirmed = []
    boxes = yolo_only[:max_crops]
    if not boxes:
        return {"confirmed": 0, "rejected": 0, "skipped": "none"}
    content = [{"type": "text",
                "text": "以下区域来自独立检测器 GDYOLO（另一观察者），可能被你遗漏。"
                        "逐个裁剪图判断：区域内是否存在工人/机械/塔吊？"
                        "若存在：给出类型、完整属性（按 schema 的 targets 元素，bbox 用画布坐标）；"
                        '若确认无目标：输出 {"targets":[]}。不要迎合提示而虚构目标。'}]
    for i, yb in enumerate(boxes, 1):
        for tag, img in (("可见光局部", item["vis"]), ("红外局部", item["ir"])):
            crop = crop_zoom(img, {"bbox_xyxy": yb["bbox_xyxy"]}, label=f"R{i}")
            ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            if ok:
                content.append({"type": "text",
                                "text": f"检测器区域 R{i}（{yb['class_name']}，conf={yb['conf']:.2f}）{tag}："})
                content.append({"type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64,"
                                                    + base64.b64encode(buf.tobytes()).decode()}})
    content.append({"type": "text",
                    "text": '只输出确认的目标 JSON：{"targets":[{"bbox_id":"NEW1",'
                            '"type":"工人|机械|塔吊","bbox_xyxy":[x1,y1,x2,y2],'
                            '"ppe":{...},"body_equipment":{...},"action":"..."}],"notes":"..."}'})

    response = gateway.complete(
        model=model_id,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": content}],
        response_format={"type": "json_object"})
    if response.status != "ok":
        return {"confirmed": 0, "rejected": 0, "error": response.error or "supplement 调用失败"}
    usage = response.data.get("usage", {})
    stats["tokens_in"] += usage.get("prompt_tokens", 0) or 0
    stats["tokens_out"] += usage.get("completion_tokens", 0) or 0
    try:
        patch = json.loads(response.data["content"])
    except (json.JSONDecodeError, TypeError):
        patch = {}

    n_existing = len(scene.get("targets", []) or [])
    for j, t in enumerate(patch.get("targets", []) or []):
        if not isinstance(t, dict) or t.get("type") not in IDX2NAME.values():
            continue
        t["bbox_id"] = f"t{n_existing + j + 1}"
        # Supplement targets must satisfy the scene contract.  ``setdefault``
        # does not replace an explicit ``null`` emitted by the model, which
        # previously left invalid ``bbox_xyxy`` values in persisted L2 JSON.
        if (not isinstance(t.get("bbox_xyxy"), list)
                or len(t.get("bbox_xyxy") or []) != 4):
            if boxes:
                t["bbox_xyxy"] = boxes[min(j, len(boxes) - 1)]["bbox_xyxy"]
            else:
                # No geometric support means this target cannot be safely
                # added as a supplement; omit it rather than writing a
                # contract-invalid record.
                continue
        t.setdefault("_meta_src", "yolo_only_supplement")
        scene.setdefault("targets", []).append(t)
        confirmed.append(t["bbox_id"])
    return {"confirmed": len(confirmed), "rejected": len(boxes) - len(confirmed),
            "confirmed_ids": confirmed}


def run_reconcile(args, items):
    """阶段2：加载 GDYOLO 独立检测 → 对账 → yolo_only 追问补充 → 定向重查。"""
    cfg = load_api_config(BASE)
    gateway, model_id = ModelGateway.from_project_config(
        BASE, "vlm", timeout_seconds=int(cfg.get("api", {}).get("timeout_seconds", 120)),
        temperature=0)
    scene_path = Path(args.scene_json)
    data = json.loads(scene_path.read_text(encoding="utf-8"))
    yolo = json.loads(Path(args.yolo_det).read_text(encoding="utf-8")).get("detections", {})

    stats = {"tokens_in": 0, "tokens_out": 0}
    for it in items:
        ts = it["ts"]
        sc = data.get("scenes", {}).get(ts, {})
        if not sc or sc.get("_meta", {}).get("parsing_error"):
            continue
        yolo_boxes = yolo.get(f"{ts}.jpg", [])
        rec = reconcile(sc.get("targets", []) or [], yolo_boxes, match_tol=args.match_tol)
        sup = supplement_yolo_only(rec["yolo_only"], it, gateway, model_id, stats, sc)
        for v in rec["vlm_only"]:
            t = next((x for x in sc.get("targets", []) if isinstance(x, dict)
                      and x.get("bbox_id") == v.get("bbox_id")), None)
            if t is not None:
                t["unverified_by_yolo"] = True
        rc_eligible, rc_why = recheck_eligible(sc, RECHECK_VISIBILITY) if args.recheck else (False, "recheck disabled")
        rc = (recheck_pair(sc, it, gateway, model_id, stats,
                           raw_rgb=load_raw_rgb(ts, args.raw_rgb_dir)[0])
              if rc_eligible else {"attempted": 0, "updated": 0, "skipped": rc_why})
        # 补漏会新增目标 → **必须重算对账**，否则摘要与实际 targets 自相矛盾
        # （实测 pair01：targets 有 4 个、摘要却说 n_vlm=2/matched=0）
        rec_final = reconcile(sc.get("targets", []) or [], yolo_boxes, match_tol=args.match_tol)
        sc.setdefault("_meta", {})["reconciliation"] = rec_final["summary"]
        sc["_meta"]["reconciliation_before_supplement"] = rec["summary"]
        sc["_meta"]["supplement"] = sup
        sc["_meta"]["recheck"] = rc
        data["scenes"][ts] = sc
        print(f"  {ts} 对账={rec['summary']} 补充={sup} 重查={rc}")

    scene_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[reconcile] 完成；结果 {scene_path}")


def run_recrop(args, items):
    """定向重感知复核（2026-09-29）：针对感知-校验分歧点做一次细粒度重感知 + 重校验。

    与 --run 的区别：不重跑整图开放检测，只对指定目标（或全部分歧目标）裁剪放大重查
    ppe/body_equipment/equipment_state，必要时对 GDYOLO 未确认区域追问补漏，最后**重算对账**。
    产物写 `--out/l2_scene.json`（与输入同构，含 `_meta.recrop` 审计），不改动原始 l2 基线。
    """
    cfg = load_api_config(BASE)
    gateway, model_id = ModelGateway.from_project_config(
        BASE, "vlm", timeout_seconds=int(cfg.get("api", {}).get("timeout_seconds", 120)),
        temperature=0)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data = json.loads(Path(args.scene_json).read_text(encoding="utf-8"))
    yolo = {}
    if args.yolo_det and Path(args.yolo_det).is_file():
        yolo = json.loads(Path(args.yolo_det).read_text(encoding="utf-8")).get("detections", {})

    want_ids = [x.strip() for x in (args.targets or "").split(",") if x.strip()]
    want_fields = [x.strip() for x in (args.fields or "").split(",") if x.strip()]
    stats = {"tokens_in": 0, "tokens_out": 0}

    for it in items:
        ts = it["ts"]
        sc = (data.get("scenes") or {}).get(ts)
        if not isinstance(sc, dict):
            continue
        yolo_boxes = yolo.get(f"{ts}.jpg", [])
        before = reconcile(sc.get("targets", []) or [], yolo_boxes, match_tol=args.match_tol)
        eligible, why = recheck_eligible(sc, RECHECK_VISIBILITY)
        rc = (recheck_pair(sc, it, gateway, model_id, stats,
                           raw_rgb=load_raw_rgb(ts, args.raw_rgb_dir)[0],
                           only_targets=want_ids or None, only_fields=want_fields or None,
                           include_equipment=True)
              if eligible else {"attempted": 0, "updated": 0, "skipped": why})
        # GDYOLO 未确认区域追问补漏（对账分歧的另一个方向）
        rec = reconcile(sc.get("targets", []) or [], yolo_boxes, match_tol=args.match_tol)
        sup = (supplement_yolo_only(rec["yolo_only"], it, gateway, model_id, stats, sc)
               if rec["yolo_only"] else {"confirmed": 0, "rejected": 0, "skipped": "none"})
        for v in rec["vlm_only"]:
            tgt = next((x for x in sc.get("targets", []) if isinstance(x, dict)
                        and x.get("bbox_id") == v.get("bbox_id")), None)
            if tgt is not None:
                tgt["unverified_by_yolo"] = True
        # 重校验：据复核后的目标重算对账，并给出剩余分歧
        rec2 = reconcile(sc.get("targets", []) or [], yolo_boxes, match_tol=args.match_tol)
        summary = rec2["summary"]
        conflicts: list[str] = []
        if int(summary.get("vlm_only") or 0) and int(summary.get("n_yolo") or 0) == 0:
            conflicts.append("vlm_only_no_yolo_support")
        if int(summary.get("yolo_only") or 0):
            conflicts.append("yolo_only_unresolved")
        if sup.get("rejected"):
            conflicts.append("supplement_rejected")
        meta = sc.setdefault("_meta", {})
        meta["recrop"] = {"reason": args.reason, "targets": want_ids or "all",
                          "fields": want_fields or "all_uncertain", "recheck": rc,
                          "supplement": sup, "reconciliation_before": before["summary"],
                          "reconciliation_after": summary, "conflicts_after": conflicts}
        meta["recheck"] = rc
        meta["supplement"] = sup
        meta["reconciliation"] = dict(summary, conflicts=conflicts) if conflicts else summary
        data["scenes"][ts] = sc
        print(f"  {ts} 复核目标={want_ids or 'all'} 更新={rc.get('updated')} "
              f"对账后={summary} 剩余分歧={conflicts}")

    (out / "l2_scene.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[recrop] 完成；结果 {out / 'l2_scene.json'}")


def parse_opt():
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true", help="真正调用 VLM API（否则 dry-run）")
    p.add_argument("--enhance", action=argparse.BooleanOptionalAction,
                   default=bool(_VLM_CFG.get("enhance", True)),
                   help="画质修复：红外 CLAHE 始终；可见光低照度提亮（默认开）")
    p.add_argument("--reconcile", action="store_true",
                   help="阶段2：对已有 scene JSON 执行对账补充+定向重查（需 --scene-json --yolo-det）")
    p.add_argument("--scene-json", type=str, default="", help="阶段2/recrop 输入：已有 l2_scene.json")
    p.add_argument("--yolo-det", type=str, default="", help="阶段2/recrop 输入：GDYOLO 检测 JSON")
    p.add_argument("--recrop", action="store_true",
                   help="定向重感知复核：对分歧点/不确定字段局部放大重查并重算对账"
                        "（需 --scene-json，会调用 VLM）")
    p.add_argument("--targets", type=str, default="", help="recrop 限定目标 bbox_id，逗号分隔")
    p.add_argument("--fields", type=str, default="",
                   help="recrop 限定字段，逗号分隔，如 ppe.helmet,equipment_state")
    p.add_argument("--reason", type=str, default="", help="recrop 缘由（写入 _meta.recrop 审计）")
    p.add_argument("--match-tol", type=float, default=0.5,
                   help="对账位置匹配容差（中心距 ≤ 较小框对角线×tol；框大小不参与判定）")
    p.add_argument("--iou-thr", type=float, default=None,
                   help="[已弃用] 历史 IoU 阈值，等价于 --match-tol")
    p.add_argument("--recheck", action=argparse.BooleanOptionalAction, default=True,
                   help="--run 时对 null/低置信字段做裁剪放大定向重查（默认开启）")
    p.add_argument("--mode", choices=["all", "fused", "vis", "ir"],
                   default=_VLM_CFG.get("mode", "all"),
                   help="送哪些图给 VLM：all=融合+可见光+红外（默认）")
    p.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    p.add_argument("--limit", type=int, default=0, help="最多处理多少张，0 表示全部")
    p.add_argument("--pair-id", type=str, default="", help="只处理指定图对，例如 pair02")
    p.add_argument("--det-json", type=str, default=str(DET_JSON))
    p.add_argument("--fused-dir", type=str, default=str(FUSED_DIR))
    p.add_argument("--panel-dir", type=str, default=str(PANEL_DIR))
    p.add_argument("--raw-rgb-dir", type=str, default=str(BASE / "data" / "rgbt" / "output"),
                   help="原始全分辨率可见光目录（重查裁剪源，坐标按画布比例换算）")
    args = p.parse_args()
    if args.iou_thr is not None:          # 兼容旧调用：--iou-thr 等价于 --match-tol
        args.match_tol = args.iou_thr
    return args


if __name__ == "__main__":
    args = parse_opt()
    items = load_manifest(args.det_json, args.fused_dir, args.panel_dir)
    if args.pair_id:
        items = [item for item in items if item["ts"] == args.pair_id]
        if not items:
            raise SystemExit(f"未找到图对: {args.pair_id}")
    if args.limit > 0:
        items = items[:args.limit]
    if args.recrop:
        if not args.scene_json:
            raise SystemExit("--recrop 需要 --scene-json")
        run_recrop(args, items)
    elif args.reconcile:
        if not (args.scene_json and args.yolo_det):
            raise SystemExit("--reconcile 需要 --scene-json 与 --yolo-det")
        if args.run:
            run_reconcile(args, items)
        else:
            print("[reconcile] dry-run 不支持，请加 --run")
    elif args.run:
        run_vlm(args, items)
    else:
        dryrun(args, items)
