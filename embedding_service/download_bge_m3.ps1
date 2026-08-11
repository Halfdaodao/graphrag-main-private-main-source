$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$modelDir = Join-Path $projectRoot "models\bge-m3"
$target = Join-Path $modelDir "pytorch_model.bin"
$part = Join-Path $modelDir "pytorch_model.bin.part"
$url = "https://hf-mirror.com/BAAI/bge-m3/resolve/main/pytorch_model.bin"

New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
if ((Test-Path $target) -and ((Get-Item $target).Length -ge 2000000000)) {
    Write-Host "BGE-M3 weights already downloaded."
    exit 0
}

Write-Host "Downloading BGE-M3 weights (resumable)..."
& curl.exe -L --fail --retry 20 --retry-delay 5 --connect-timeout 30 -C - --output $part $url
if ((Test-Path $part) -and ((Get-Item $part).Length -ge 2000000000)) {
    Move-Item -Force $part $target
    Write-Host "BGE-M3 weights are ready at $target"
} else {
    Write-Host "Download paused; run this script again to resume."
}
