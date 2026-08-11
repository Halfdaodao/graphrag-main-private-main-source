"""Download BGE-M3 to the project directory with resumable Hugging Face cache support."""

import os
from pathlib import Path

# The default Hugging Face large-file CDN is unreliable on this network.
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from huggingface_hub import snapshot_download


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = PROJECT_ROOT / "models" / "bge-m3"

snapshot_download(
    repo_id="BAAI/bge-m3",
    local_dir=MODEL_DIR,
    # ONNX weights are not used by SentenceTransformer and duplicate the model size.
    ignore_patterns=["onnx/*", "imgs/*", "*.jpg", "*.webp", ".gitattributes"],
)
print(f"BGE-M3 is ready at: {MODEL_DIR}")
