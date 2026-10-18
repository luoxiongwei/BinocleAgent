# -*- coding: utf-8 -*-
"""L1 检测冒烟脚本：用 best.pt 对 RGB 可见光图跑检测，输出结构化 JSON 作 L2 底座。
工作区: upgrade_work/scripts/
用法: python l1_smoke.py --source <图目录> [--weights <best.pt>] [--conf 0.25] [--out <out_dir>]
"""
import argparse, json, os, sys, time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "libs"))   # 使用 upgrade_work 内置的 ultralytics 包，脱离 GDYOLO 原项目

DEFAULT_WEIGHTS = BASE / "data" / "weights" / "best.pt"
DEFAULT_SOURCE = BASE / "data" / "rgbt" / "visible"


def run(args):
    import torch as _torch
    _orig_load = _torch.load
    def _safe_load(*a, **k):
        k.setdefault("weights_only", False)  # 兼容旧版项目 torch.load（权重来源可信）
        return _orig_load(*a, **k)
    _torch.load = _safe_load

    from ultralytics import YOLO

    src = Path(args.source)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    model = YOLO(str(args.weights))
    names = model.names  # dict {idx:name}, 应反映 nc=3

    start = time.time()
    results = model.predict(source=str(src), imgsz=640, conf=args.conf, iou=args.iou,
                            device="cpu",  # 本机 GT710 算力 3.5 过老，CUDA 内核不可用，强制 CPU
                            save=False, save_txt=False, verbose=False)
    dt = time.time() - start

    records = {}
    for r in results:
        img_path = Path(r.path).name
        boxes = r.boxes
        objs = []
        if boxes is not None and len(boxes):
            xyxy = boxes.xyxy.tolist()
            confs = boxes.conf.tolist()
            clss = boxes.cls.long().tolist()
            for (x1, y1, x2, y2), conf, cls in zip(xyxy, confs, clss):
                x1, y1, x2, y2 = [round(v, 1) for v in (x1, y1, x2, y2)]
                objs.append({
                    "class_id": int(cls),
                    "class_name": names.get(int(cls), str(int(cls))),
                    "conf": round(float(conf), 3),
                    "bbox_xyxy": [x1, y1, x2, y2],
                    "w": round(x2 - x1, 1),
                    "h": round(y2 - y1, 1),
                })
        records[img_path] = objs

    meta = {
        "weights": str(args.weights),
        "nc": len(names),
        "names": {str(k): v for k, v in names.items()},
        "source": str(src),
        "n_images": len(records),
        "conf": args.conf,
        "iou": args.iou,
        "elapsed_s": round(dt, 1),
        "total_detections": sum(len(v) for v in records.values()),
    }

    json_path = out / "l1_detections.json"
    json_path.write_text(json.dumps({"meta": meta, "detections": records}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 控制台摘要
    print(f"[L1冒烟] 权重 nc={meta['nc']} names={meta['names']}")
    print(f"[L1冒烟] 图片数={meta['n_images']}  总检测框={meta['total_detections']}  耗时={meta['elapsed_s']}s")
    per_cls = {}
    for objs in records.values():
        for o in objs:
            per_cls[o["class_name"]] = per_cls.get(o["class_name"], 0) + 1
    print(f"[L1冒烟] 按类统计: {per_cls}")
    print(f"[L1冒烟] 结果已存: {json_path}")


def parse_opt():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=str, default=str(DEFAULT_SOURCE))
    p.add_argument("--weights", type=str, default=str(DEFAULT_WEIGHTS))
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--iou", type=float, default=0.45)
    p.add_argument("--out", type=str, default=str(BASE / "results" / "l1_smoke"))
    return p.parse_args()


if __name__ == "__main__":
    run(parse_opt())
