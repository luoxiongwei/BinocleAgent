import sys
import argparse
import os

sys.path.append(r'D:\Desktop\ultralyticsPro0312-YOLOv8\ultralytics') # Path

from ultralytics import YOLO

def main(opt):
    weights = opt.weights

    model = YOLO(weights)

    model.info()
    
    results = model.val(data='D:/Desktop/ultralyticsPro0312-YOLOv8/ultralytics/cfg/datasets/mydataset.yaml', imgsz=640, batch=8, split='val')

def parse_opt(known=False):
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', type=str, default=r'D:\Desktop\ultralyticsPro0312-YOLOv8\ultralytics\py\runs\detect\train3\weights\best.pt', help='initial weights path')
    # parser.add_argument('--artifact_alias', type=str, default='latest', help='W&B: Version of dataset artifact to use')

    opt = parser.parse_known_args()[0] if known else parser.parse_args()
    return opt

if __name__ == "__main__":
    opt = parse_opt()
    main(opt)