$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path "$PSScriptRoot\..\.."
Set-Location $repoRoot

$venv = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $venv)) { throw "Run from repo root with .venv set up." }

& $venv -m pip install --quiet pyinstaller

$outDir = Join-Path $repoRoot "desktop\bin"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

& $venv -m PyInstaller `
  --noconfirm --clean --onefile `
  --name taxlens-backend `
  --paths src `
  --add-data "src/taxlens/tax_rules;taxlens/tax_rules" `
  --add-data "src/taxlens/web;taxlens/web" `
  --add-data "src/taxlens/demo;taxlens/demo" `
  --collect-submodules taxlens `
  --hidden-import uvicorn.loops.auto `
  --hidden-import uvicorn.protocols.http.auto `
  --hidden-import uvicorn.protocols.websockets.auto `
  --hidden-import uvicorn.lifespan.on `
  src/taxlens/__main__.py

Copy-Item -Force "dist\taxlens-backend.exe" $outDir
Write-Host "Wrote $outDir\taxlens-backend.exe"
