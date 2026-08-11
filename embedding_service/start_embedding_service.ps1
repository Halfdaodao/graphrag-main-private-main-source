$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "GraphRAG virtual environment was not found: $python"
}

# Keep the multi-gigabyte Hugging Face model cache on D: with this project.
$env:HF_HOME = Join-Path $projectRoot "models\huggingface"
$env:EMBEDDING_MODEL = Join-Path $projectRoot "models\bge-m3"
# Some corporate networks reset Hugging Face's Xet transfer connections.
$env:HF_HUB_DISABLE_XET = "1"

$weightFile = Join-Path $env:EMBEDDING_MODEL "pytorch_model.bin"
if (-not (Test-Path $weightFile)) {
    & (Join-Path $PSScriptRoot "download_bge_m3.ps1")
}

& $python -m uvicorn server:app --app-dir $PSScriptRoot --host 127.0.0.1 --port 8001
