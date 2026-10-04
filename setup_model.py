#!/usr/bin/env python3
"""One-time online provisioning for the pinned local RT-DETRv2 model."""
import hashlib
import os
from pathlib import Path

os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"

from huggingface_hub import snapshot_download

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
MODELS_DIR = DATA_DIR / "models"
DESTINATION = MODELS_DIR / "rtdetr-v2-r50vd"
REPOSITORY = "PekingU/rtdetr_v2_r50vd"
REVISION = "282494075698cab9faa1096ae26856890030c817"
WEIGHTS_SHA256 = "3331d977dbc0c7a6cdae9ec0b0b6ad156eb6720d65b7cf0fa710dcc541d88d71"

DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
MODELS_DIR.mkdir(mode=0o700, exist_ok=True)
DESTINATION.mkdir(mode=0o700, exist_ok=True)
for private_dir in (DATA_DIR, MODELS_DIR, DESTINATION):
    private_dir.chmod(0o700)
snapshot_download(
    repo_id=REPOSITORY,
    revision=REVISION,
    local_dir=str(DESTINATION),
    allow_patterns=["config.json", "model.safetensors", "preprocessor_config.json"],
)
weights = DESTINATION / "model.safetensors"
checksum = hashlib.sha256()
with weights.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        checksum.update(chunk)
digest = checksum.hexdigest()
if digest != WEIGHTS_SHA256:
    weights.unlink(missing_ok=True)
    raise SystemExit("Model weights failed the published SHA-256 integrity check; the file was removed.")
for path in DESTINATION.rglob("*"):
    if path.is_file():
        path.chmod(0o600)
print(f"Pinned model installed at {DESTINATION}")
print(f"Verified model.safetensors SHA-256: {digest}")
print("The server loads only these local files. It will not download or update model files at runtime.")
