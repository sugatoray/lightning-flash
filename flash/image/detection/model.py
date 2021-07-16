# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Type, Union

import torch
from torch import nn, tensor
from torch.optim import Optimizer
from torch.utils.data import DataLoader, Sampler

from flash.core.data.auto_dataset import BaseAutoDataset
from flash.core.data.process import Serializer
from flash.core.model import Task
from flash.core.registry import FlashRegistry
from flash.core.utilities.imports import _ICEVISION_AVAILABLE, _TORCHVISION_AVAILABLE
from flash.image.detection.heads import OBJECT_DETECTION_HEADS
from flash.image.detection.serialization import DetectionLabels

if _TORCHVISION_AVAILABLE:
    import torchvision
    from torchvision.models.detection.rpn import AnchorGenerator
    from torchvision.ops import box_iou

    _models = {
        "fasterrcnn": torchvision.models.detection.fasterrcnn_resnet50_fpn,
        "retinanet": torchvision.models.detection.retinanet_resnet50_fpn,
    }

else:
    AnchorGenerator = None

if _ICEVISION_AVAILABLE:
    from icevision.core import BaseRecord
    from icevision.data import Dataset
    from icevision.metrics import COCOMetric, COCOMetricType


def _evaluate_iou(target, pred):
    """
    Evaluate intersection over union (IOU) for target from dataset and output prediction from model
    """
    if pred["boxes"].shape[0] == 0:
        # no box detected, 0 IOU
        return tensor(0.0, device=pred["boxes"].device)
    return box_iou(target["boxes"], pred["boxes"]).diag().mean()


class ObjectDetector(Task):
    """The ``ObjectDetector`` is a :class:`~flash.Task` for detecting objects in images. For more details, see
    :ref:`object_detection`.

    Args:
        num_classes: the number of classes for detection, including background
        model: a string of :attr`_models`. Defaults to 'fasterrcnn'.
        backbone: Pretained backbone CNN architecture. Constructs a model with a
            ResNet-50-FPN backbone when no backbone is specified.
        fpn: If True, creates a Feature Pyramind Network on top of Resnet based CNNs.
        pretrained: if true, returns a model pre-trained on COCO train2017
        pretrained_backbone: if true, returns a model with backbone pre-trained on Imagenet
        trainable_backbone_layers: number of trainable resnet layers starting from final block.
            Only applicable for `fasterrcnn`.
        loss: the function(s) to update the model with. Has no effect for torchvision detection models.
        metrics: The provided metrics. All metrics here will be logged to progress bar and the respective logger.
            Changing this argument currently has no effect.
        optimizer: The optimizer to use for training. Can either be the actual class or the class name.
        pretrained: Whether the model from torchvision should be loaded with it's pretrained weights.
            Has no effect for custom models.
        learning_rate: The learning rate to use for training

    """

    # backbones: FlashRegistry = OBJ_DETECTION_BACKBONES

    heads: FlashRegistry = OBJECT_DETECTION_HEADS

    required_extras: str = "image"

    def __init__(
        self,
        num_classes: int,
        backbone: Optional[str] = "resnet18_fpn",
        head: Optional[str] = "retinanet",
        pretrained: bool = True,
        pretrained_backbone: bool = True,
        trainable_backbone_layers: int = 3,
        anchor_generator: Optional[Type['AnchorGenerator']] = None,
        loss=None,
        metrics: Union[Callable, nn.Module, Mapping, Sequence, None] = None,
        optimizer: Type[Optimizer] = torch.optim.AdamW,
        learning_rate: float = 1e-3,
        serializer: Optional[Union[Serializer, Mapping[str, Serializer]]] = None,
        **kwargs: Any,
    ):
        self.save_hyperparameters()

        # if model in _models:
        #     model = ObjectDetector.get_model(
        #         model, num_classes, backbone, fpn, pretrained, pretrained_backbone, trainable_backbone_layers,
        #         anchor_generator, **kwargs
        #     )
        # else:
        #     ValueError(f"{model} is not supported yet.")

        super().__init__(
            model=None,
            loss_fn=loss,
            metrics=None,
            learning_rate=learning_rate,
            optimizer=optimizer,
            serializer=serializer or DetectionLabels(),
        )

        metadata = self.heads.get(head, with_metadata=True)
        backbones = metadata["metadata"]["backbones"]
        backbone_config = backbones.get(backbone)(pretrained)
        self.model_type, self.model, adapter, self.backbone = metadata["fn"](backbone_config, num_classes, **kwargs)
        self.adapter = adapter(model=self.model, metrics=metrics or [COCOMetric(metric_type=COCOMetricType.bbox)])

    @classmethod
    def available_backbones(cls, head: str) -> List[str]:
        metadata = cls.heads.get(head, with_metadata=True)
        backbones = metadata["metadata"]["backbones"]
        return backbones.available_keys()

    # @staticmethod
    # def get_model(
    #     model_name,
    #     num_classes,
    #     backbone,
    #     fpn,
    #     pretrained,
    #     pretrained_backbone,
    #     trainable_backbone_layers,
    #     anchor_generator,
    #     **kwargs,
    # ):
    #     if backbone is None:
    #         # Constructs a model with a ResNet-50-FPN backbone when no backbone is specified.
    #         if model_name == "fasterrcnn":
    #             model = _models[model_name](
    #                 pretrained=pretrained,
    #                 pretrained_backbone=pretrained_backbone,
    #                 trainable_backbone_layers=trainable_backbone_layers,
    #             )
    #             in_features = model.roi_heads.box_predictor.cls_score.in_features
    #             head = FastRCNNPredictor(in_features, num_classes)
    #             model.roi_heads.box_predictor = head
    #         else:
    #             model = _models[model_name](pretrained=pretrained, pretrained_backbone=pretrained_backbone)
    #             model.head = RetinaNetHead(
    #                 in_channels=model.backbone.out_channels,
    #                 num_anchors=model.head.classification_head.num_anchors,
    #                 num_classes=num_classes,
    #                 **kwargs
    #             )
    #     else:
    #         backbone_model, num_features = ObjectDetector.backbones.get(backbone)(
    #             pretrained=pretrained_backbone,
    #             trainable_layers=trainable_backbone_layers,
    #             **kwargs,
    #         )
    #         backbone_model.out_channels = num_features
    #         if anchor_generator is None:
    #             anchor_generator = AnchorGenerator(
    #                 sizes=((32, 64, 128, 256, 512), ), aspect_ratios=((0.5, 1.0, 2.0), )
    #             ) if not hasattr(backbone_model, "fpn") else None
    #
    #         if model_name == "fasterrcnn":
    #             model = FasterRCNN(backbone_model, num_classes=num_classes, rpn_anchor_generator=anchor_generator)
    #         else:
    #             model = RetinaNet(backbone_model, num_classes=num_classes, anchor_generator=anchor_generator)
    #     return model

    def on_save_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        if self._data_pipeline_state is not None and '_data_pipeline_state' not in checkpoint:
            checkpoint['_data_pipeline_state'] = self._data_pipeline_state

    def process_train_dataset(
        self,
        dataset: BaseAutoDataset,
        batch_size: int,
        num_workers: int,
        pin_memory: bool,
        collate_fn: Callable,
        shuffle: bool = False,
        drop_last: bool = False,
        sampler: Optional[Sampler] = None
    ) -> DataLoader:
        return self.model_type.train_dl(
            dataset,
            batch_size=batch_size,
            num_workers=num_workers,
            pin_memory=pin_memory,
            shuffle=shuffle,
            drop_last=drop_last,
            sampler=sampler,
        )

    def process_val_dataset(
        self,
        dataset: BaseAutoDataset,
        batch_size: int,
        num_workers: int,
        pin_memory: bool,
        collate_fn: Callable,
        shuffle: bool = False,
        drop_last: bool = False,
        sampler: Optional[Sampler] = None
    ) -> DataLoader:
        return self.model_type.valid_dl(
            dataset,
            batch_size=batch_size,
            num_workers=num_workers,
            pin_memory=pin_memory,
            shuffle=shuffle,
            drop_last=drop_last,
            sampler=sampler,
        )

    def process_test_dataset(
        self,
        dataset: BaseAutoDataset,
        batch_size: int,
        num_workers: int,
        pin_memory: bool,
        collate_fn: Callable,
        shuffle: bool = False,
        drop_last: bool = False,
        sampler: Optional[Sampler] = None
    ) -> DataLoader:
        return self.model_type.valid_dl(
            dataset,
            batch_size=batch_size,
            num_workers=num_workers,
            pin_memory=pin_memory,
            shuffle=shuffle,
            drop_last=drop_last,
            sampler=sampler,
        )

    def process_predict_dataset(
        self,
        dataset: BaseAutoDataset,
        batch_size: int = 1,
        num_workers: int = 0,
        pin_memory: bool = False,
        collate_fn: Callable = lambda x: x,
        shuffle: bool = False,
        drop_last: bool = True,
        sampler: Optional[Sampler] = None,
        convert_to_dataloader: bool = True
    ) -> Union[DataLoader, BaseAutoDataset]:
        if convert_to_dataloader:
            return self.model_type.infer_dl(
                dataset,
                batch_size=batch_size,
                num_workers=num_workers,
                pin_memory=pin_memory,
                shuffle=shuffle,
                drop_last=drop_last,
                sampler=sampler,
            )
        return dataset

    def training_step(self, batch, batch_idx) -> Any:
        return self.adapter.training_step(batch, batch_idx)

    def validation_step(self, batch, batch_idx):
        return self.adapter.validation_step(batch, batch_idx)

    def test_step(self, batch, batch_idx):
        return self.adapter.validation_step(batch, batch_idx)

    def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: int = 0) -> Any:
        if isinstance(batch, list) and isinstance(batch[0], BaseRecord):
            data = Dataset(batch)
            return self.model_type.predict(self.model, data)
        return self.model_type.predict_from_dl(self.model, [batch], show_pbar=False)

    def training_epoch_end(self, outputs) -> None:
        return self.adapter.training_epoch_end(outputs)

    def validation_epoch_end(self, outputs) -> None:
        return self.adapter.validation_epoch_end(outputs)

    def test_epoch_end(self, outputs) -> None:
        return self.adapter.validation_epoch_end(outputs)

    # def configure_finetune_callback(self):
    #     return [ObjectDetectionFineTuning(train_bn=True)]

    def _ci_benchmark_fn(self, history: List[Dict[str, Any]]) -> None:
        """
        This function is used only for debugging usage with CI
        """
        # todo (tchaton) Improve convergence
        # history[-1]["val_iou"]
