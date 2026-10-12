# Build the Windows rpmap.exe locally. Mirrors the CI job.
#   Right-click -> Run with PowerShell, or:  powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot   # scripts/ -> repo root
Set-Location $root

python -m pip install --upgrade pip pyinstaller
python -m PyInstaller --onefile --console --name rpmap --add-data "VERSION;." scripts/rpmap.py --noconfirm

Write-Host "Built: $root\dist\rpmap.exe"
