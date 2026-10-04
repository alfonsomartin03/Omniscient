"""Offline RT-DETRv2 inference isolated from the always-on HTTPS process."""
from __future__ import annotations

import importlib.util
import io
import multiprocessing
import os
import threading
import time
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"

MODEL_DIR = Path(__file__).resolve().parent / "data" / "models" / "rtdetr-v2-r50vd"
MODEL_FILES = ("config.json", "preprocessor_config.json", "model.safetensors")


def bbox_iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    return intersection / max(1e-9, area_a + area_b - intersection)


class LocalDetector:
    """Created only inside the short-lived inference process."""

    def __init__(self):
        import torch
        from transformers import RTDetrImageProcessor, RTDetrV2ForObjectDetection

        threads = min(4, max(1, int(os.environ.get("OMNI_TORCH_THREADS", "2"))))
        torch.set_num_threads(threads)
        self.torch = torch
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.processor = RTDetrImageProcessor.from_pretrained(str(MODEL_DIR), local_files_only=True)
        self.model = RTDetrV2ForObjectDetection.from_pretrained(str(MODEL_DIR), local_files_only=True).to(self.device).eval()

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
            # Use local CPU execution when an operator is unavailable on MPS.
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
            detections.append({
                "label": str(self.model.config.id2label.get(label, "object")).lower(),
                "confidence": float(score),
                "box": [max(0, min(1, x1 / image.width)), max(0, min(1, y1 / image.height)),
                        max(0, min(1, x2 / image.width)), max(0, min(1, y2 / image.height))],
            })
        return detections


def _worker_main(connection):
    """Receive JPEG bytes from the local server; never open a network socket."""
    try:
        detector = LocalDetector()
        from PIL import Image
        connection.send(("ready", detector.device.upper()))
        while True:
            try:
                frame = connection.recv_bytes()
            except EOFError:
                break
            if not frame:
                break
            try:
                with Image.open(io.BytesIO(frame)) as source:
                    image = source.convert("RGB")
                connection.send(("result", detector.detect(image)))
            except Exception as exc:
                connection.send(("error", type(exc).__name__))
    except Exception as exc:
        try:
            connection.send(("error", type(exc).__name__))
        except (OSError, BrokenPipeError):
            pass
    finally:
        connection.close()


class ModelBusy(Exception):
    pass


class ModelFailure(Exception):
    pass


class ModelWorker:
    """Run one model process on demand; release its memory after idle time."""

    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._connection = None
        self.last_used = 0.0
        self.failed_reason = ""
        self.idle_seconds = min(3600, max(30, int(os.environ.get("OMNI_MODEL_IDLE_SECONDS", "300"))))

    @property
    def available(self):
        return (not self.failed_reason and all((MODEL_DIR / name).is_file() for name in MODEL_FILES)
                and all(importlib.util.find_spec(name) is not None for name in ("torch", "transformers", "PIL")))

    @property
    def status(self):
        if self.failed_reason:
            return self.failed_reason
        if not all((MODEL_DIR / name).is_file() for name in MODEL_FILES):
            return "Model files are not installed"
        if not all(importlib.util.find_spec(name) is not None for name in ("torch", "transformers", "PIL")):
            return "Local vision dependencies are not installed"
        if self._process is not None and self._process.is_alive():
            return "RT-DETRv2 R50 · local inference active"
        return "RT-DETRv2 R50 · installed · loads on demand"

    def _stop_locked(self):
        connection, process = self._connection, self._process
        self._connection = self._process = None
        if connection is not None:
            try:
                if process is not None and process.is_alive():
                    connection.send_bytes(b"")
            except (OSError, BrokenPipeError):
                pass
            connection.close()
        if process is not None:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)

    def _start_locked(self):
        if not self.available:
            raise ModelFailure(self.status)
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(target=_worker_main, args=(child,), daemon=True)
        try:
            process.start()
        except (OSError, RuntimeError) as exc:
            parent.close()
            child.close()
            raise ModelFailure("Local model process could not start") from exc
        child.close()
        self._connection, self._process = parent, process
        if not parent.poll(45):
            self.failed_reason = "Local model startup timed out"
            self._stop_locked()
            raise ModelFailure(self.failed_reason)
        try:
            state, detail = parent.recv()
        except (EOFError, OSError):
            state, detail = "error", "Local model process exited during startup"
        if state != "ready":
            self.failed_reason = f"Local model could not load ({detail})"
            self._stop_locked()
            raise ModelFailure(self.failed_reason)

    def detect(self, frame: bytes):
        if not self._lock.acquire(blocking=False):
            raise ModelBusy()
        try:
            if self._process is None or not self._process.is_alive():
                self._stop_locked()
                self._start_locked()
            try:
                self._connection.send_bytes(frame)
                if not self._connection.poll(30):
                    raise ModelFailure("Local model inference timed out")
                state, result = self._connection.recv()
            except (OSError, EOFError, BrokenPipeError) as exc:
                raise ModelFailure("Local model process stopped") from exc
            if state != "result":
                raise ModelFailure(f"Local model inference failed ({result})")
            self.last_used = time.monotonic()
            return result
        except ModelFailure as exc:
            self.failed_reason = str(exc)
            self._stop_locked()
            raise
        finally:
            self._lock.release()

    def unload_if_idle(self):
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._process is not None and time.monotonic() - self.last_used >= self.idle_seconds:
                self._stop_locked()
        finally:
            self._lock.release()

    def close(self):
        with self._lock:
            self._stop_locked()


MODEL_WORKER = ModelWorker()
