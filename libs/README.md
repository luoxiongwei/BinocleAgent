# libs/ —— 内置推理引擎（vendor）

## 身份与作用

`ultralytics/` 为**定制版 Ultralytics YOLOv8 框架**（含 EMO 系列骨干等自定义模块，
见 `nn/modules/CoreV8/Backbone/emo.py`）。

**它是运行时必需依赖，不可删除**：`data/weights/best.pt` 的检查点按 pickle 序列化，
反序列化必须能 import 到这些自定义 nn 模块（`ultralytics.nn.tasks.DetectionModel`
及定制骨干类），缺失即加载失败。

## 相对上游的修改（仅 1 处）

- `nn/modules/CoreV8/Backbone/emo.py`：timm 新旧版本兼容补丁——
  `from timm.models.efficientnet_builder import _parse_ksize` 外包了
  try/except 回退到 `timm.models._efficientnet_builder`（适配 timm 0.6.13）。

## 权重说明

- 本仓库只随分发推理权重 `data/weights/best.pt`（3 类：工人/机械/塔吊）；
- `py/yolov8n.pt`、`py/yolov8s.pt` 原为 COCO 预训练权重，仅重训练用，
  纯推理不需要，**未随仓库分发**；确需重训练时由 ultralytics 自动下载即可；
- 训练数据集不随仓库存储，重训练时的数据布局见 `config/l1_data_3cls.yaml` 注释。

## 维护边界

`libs/` 只做"让 `best.pt` 能加载推理"这一件事；框架代码如需进一步改动，
应在此文件记录变更。
