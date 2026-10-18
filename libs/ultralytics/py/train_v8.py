import sys
import argparse
import os

project_root = r'D:\Desktop\ultralyticsPro0312-YOLOv8\ultralytics'
'''
python train_v8.py --22ultralyticsPro0312-YOLOv8/ultralyticsPro0312-YOLOv8/ultralytics/cfg/models/cfg2025/YOLOv8-Backbone/Ghost/YOLOv8-C2f_Ghost.yaml 

'''
from ultralytics import YOLO
def main(opt):
    yaml = r"D:/Desktop/ultralyticsPro0312-YOLOv8/ultralytics/cfg/models/cfg2025/YOLOv8-Backbone/Ghost/YOLOv8-C3_Ghost.yaml"
    weights = opt.weights
    model = YOLO(yaml).load(weights)
    #model = YOLO(weights)
    #model = YOLO(yaml)


    # print(model)

    model.info()

    results = model.train(data='D:/Desktop/ultralyticsPro0312-YOLOv8/ultralytics/cfg/datasets/mydataset.yaml',
                        epochs=100, 
                        imgsz=640, 
                        workers=8,
                        batch=16,
                        patience=10,
                        )

def parse_opt(known=False):
    parser = argparse.ArgumentParser()
    parser.add_argument('--cfg', type=str, default='D:/Desktop/ultralyticsPro0312-YOLOv8/ultralytics/cfg/models/cfg2025/YOLOv8-Backbone/Ghost/YOLOv8-C3_Ghost.yaml', help='')
    parser.add_argument('--weights', type=str, default='yolov8s.pt', help='')

    opt = parser.parse_known_args()[0] if known else parser.parse_args()
    return opt

if __name__ == "__main__":
    opt = parse_opt()
    main(opt)