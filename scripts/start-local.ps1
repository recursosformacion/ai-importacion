$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } else {
        & python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw "No se pudo crear el entorno virtual de Python." }
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
& $Python -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "No se pudieron instalar las dependencias." }

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "downloads") | Out-Null

$env:AI_IMPORTACION_DB = Join-Path $ProjectRoot "data\ai-importacion.sqlite3"
$env:AI_IMPORTACION_DOWNLOAD_DIR = Join-Path $ProjectRoot "downloads"
if (-not $env:AI_IMPORTACION_OCR_LANG) { $env:AI_IMPORTACION_OCR_LANG = "eng+spa" }

Write-Host ""
Write-Host "AI Importacion iniciando en http://127.0.0.1:8900"
Write-Host "Base SQLite: $env:AI_IMPORTACION_DB"
Write-Host "Descargas:   $env:AI_IMPORTACION_DOWNLOAD_DIR"
Write-Host "Cierra esta ventana o pulsa Ctrl+C para detener el servicio."
Write-Host ""

& $Python -m uvicorn app.main:app --host 127.0.0.1 --port 8900 --proxy-headers --forwarded-allow-ips=127.0.0.1
