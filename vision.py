"""Offline, local-only RT-DETRv2 object detection and persistent tracking."""
from __future__ import annotations

import io
import os
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"

MODEL_DIR = Path(__file__).resolve().parent / "data" / "models" / "rtdetr-v2-r50vd"


class LocalDetector:
    def __init__(self):
        self.ready = False
        self.reason = "Model files are not installed"
        self.model = None
        self.processor = None
        self.torch = None
        if not MODEL_DIR.is_dir():
            return
        try:
            import torch
            from PIL import Image
            from transformers import RTDetrImageProcessor, RTDetrV2ForObjectDetection
            self.torch, self.Image = torch, Image
            self.device = "mps" if torch.backends.mps.is_available() else "cpu"
            self.processor = RTDetrImageProcessor.from_pretrained(str(MODEL_DIR), local_files_only=True)
            self.model = RTDetrV2ForObjectDetection.from_pretrained(str(MODEL_DIR), local_files_only=True).to(self.device).eval()
            self.ready, self.reason = True, "RT-DETRv2 R50 · local inference · " + self.device.upper()
        except Exception as exc:
            self.reason = f"Local model could not load ({type(exc).__name__})"

    def decode(self, frame_data: bytes, max_size: tuple[int, int], grid: tuple[int, int]):
        try:
            image = self.Image.open(io.BytesIO(frame_data)).convert("RGB")
            if image.width < 16 or image.height < 16 or image.width > max_size[0] or image.height > max_size[1]:
                raise ValueError("Camera frame dimensions are outside the permitted range")
            gray_image = image.convert("L").resize(grid)
            return image, bytes(gray_image.getdata())
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Invalid JPEG camera frame") from exc

    def detect(self, image, threshold: float = 0.38):
        torch = self.torch
        try:
            inputs = self.processor(images=image, return_tensors="pt").to(self.device)
            with torch.inference_mode():
                outputs = self.model(**inputs)
            targets = torch.tensor([(image.height, image.width)], device=self.device)
            result = self.processor.post_process_object_detection(outputs, target_sizes=targets, threshold=threshold)[0]
        except Exception:
            if self.device != "mps":
                raise
            # Some PyTorch/MPS versions lack a particular operator. Fall back
            # on-device to CPU; neither path can make a network request.
            self.device = "cpu"
            self.model = self.model.to("cpu")
            inputs = self.processor(images=image, return_tensors="pt")
            with torch.inference_mode():
                outputs = self.model(**inputs)
            targets = torch.tensor([(image.height, image.width)])
            result = self.processor.post_process_object_detection(outputs, target_sizes=targets, threshold=threshold)[0]
        detections = []
        for score, label, box in zip(result["scores"].tolist(), result["labels"].tolist(), result["boxes"].tolist()):
            x1, y1, x2, y2 = box
            if x2 <= x1 or y2 <= y1:
                continue
            detections.append({"label": str(self.model.config.id2label.get(label, "object")).lower(), "confidence": float(score), "box": [max(0, min(1, x1 / image.width)), max(0, min(1, y1 / image.height)), max(0, min(1, x2 / image.width)), max(0, min(1, y2 / image.height))]})
        return detections

    @staticmethod
    def iou(a, b):
        x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
        intersection = max(0, x2 - x1) * max(0, y2 - y1)
        area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
        area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
        return intersection / max(1e-9, area_a + area_b - intersection)


LOCAL_DETECTOR = LocalDetector()
