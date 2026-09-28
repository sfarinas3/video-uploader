# Builds dist\video-uploader.exe (via build_exe.ps1) and then wraps it in a
# proper Windows installer: dist_installer\VideoUploaderSetup.exe. Requires
# Inno Setup (https://jrsoftware.org/isinfo.php) -- installs itself, no
# admin rights needed to run this script.
#
# Run from the repo root:
#   .venv\Scripts\Activate.ps1
#   pip install -e ".[build]"
#   .\scripts\build_installer.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

& "$PSScriptRoot\build_exe.ps1"

$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $found) {
        throw "ISCC.exe (Inno Setup compiler) not found. Install it from https://jrsoftware.org/isinfo.php and re-run."
    }
    $iscc = $found
} else {
    $iscc = $iscc.Source
}

& $iscc "installer\video_uploader.iss"

Write-Host ""
Write-Host "Built dist_installer\VideoUploaderSetup.exe"
Write-Host "Hand that single file to anyone to install -- it's a per-user install, no admin rights needed."
