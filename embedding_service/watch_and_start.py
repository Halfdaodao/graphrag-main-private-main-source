"""Start Uvicorn automatically after the BGE-M3 weight download finishes."""

import os
import subprocess
import time
from pathlib import Path


root = Path(__file__).resolve().parents[1]
weight = root / "models" / "bge-m3" / "pytorch_model.bin"
while not weight.exists() or weight.stat().st_size < 2_000_000_000:
    time.sleep(10)

env = os.environ.copy()
env["EMBEDDING_MODEL"] = str(weight.parent)
env["HF_HOME"] = str(root / "models" / "huggingface")
subprocess.run(
    [str(root / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn", "server:app", "--app-dir", str(Path(__file__).parent), "--host", "127.0.0.1", "--port", "8001"],
    cwd=Path(__file__).parent,
    env=env,
    check=False,
)
