import sys
import argparse
import os

os.environ['HTTP_PROXY'] = ''
os.environ['HTTPS_PROXY'] = ''
# ---------------------------------------------------------------------------

sys.path.append(r'D:\Desktop\ultralyticsPro0312-YOLOv8\ultralytics')  # 仅在需要自定义模块时保留

from ultralytics import YOLO

def main(opt):
    model = YOLO(r'D:\Desktop\ultralyticsPro0312-YOLOv8\ultralytics\py\runs\detect\run\train3\weights\best.pt')  # ✅ 加载你训练好的模型权重

    model.predict(
        source=r'D:\Desktop\ultralyticsPro0312-YOLOv8\ultralytics\data\test\images',  # ✅ 测试集图片文件夹
        save=True,                   # ✅ 保存预测后的图片
        imgsz=640, 
        conf=0.5, 
        save_txt=True,              # ✅ 保存预测框标签
        save_conf=True              # ✅ 保存预测置信度
    )

def parse_opt(known=False):
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact_alias', type=str, default='latest', help='W&B: Version of dataset artifact to use')
    opt = parser.parse_known_args()[0] if known else parser.parse_args()
    return opt

if __name__ == "__main__":
    opt = parse_opt()
    main(opt)
