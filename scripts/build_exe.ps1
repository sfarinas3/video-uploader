# Builds a standalone Windows desktop .exe (video-uploader.exe) with a
# custom icon, using PyInstaller. Run from the repo root:
#   .venv\Scripts\Activate.ps1
#   pip install -e ".[build]"
#   .\scripts\build_exe.ps1
#
# Output: dist\video-uploader.exe (plus config.example.yaml copied next to
# it -- config.py resolves config.yaml/data/ relative to the exe's own
# folder when frozen, not the source tree).

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

pyinstaller `
  --name video-uploader `
  --onefile `
  --windowed `
  --icon assets\icon.ico `
  --paths src `
  --add-data "src\video_uploader\web\templates;video_uploader\web\templates" `
  --add-data "src\video_uploader\web\static;video_uploader\web\static" `
  --hidden-import uvicorn.logging `
  --hidden-import uvicorn.loops `
  --hidden-import uvicorn.loops.auto `
  --hidden-import uvicorn.protocols `
  --hidden-import uvicorn.protocols.http `
  --hidden-import uvicorn.protocols.http.auto `
  --hidden-import uvicorn.protocols.websockets `
  --hidden-import uvicorn.protocols.websockets.auto `
  --hidden-import uvicorn.lifespan `
  --hidden-import uvicorn.lifespan.on `
  run.py

Copy-Item -Force config.example.yaml dist\config.example.yaml

Write-Host ""
Write-Host "Built dist\video-uploader.exe"
Write-Host "First run: copy config.example.yaml to config.yaml next to the exe and fill in credentials,"
Write-Host "or right-click the exe -> Create shortcut, then pin/move the shortcut to your desktop/taskbar."
