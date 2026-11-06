$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# Reuse the project's existing virtual environment and dependencies.
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "No existe .venv. Activa o crea el entorno habitual del proyecto antes de arrancar AI Importacion."
}

Write-Host ""
Write-Host "AI Importacion iniciando en http://127.0.0.1:9500"
Write-Host "Se reutiliza .venv y la configuración local existente."
Write-Host "Cierra esta ventana o pulsa Ctrl+C para detener el servicio."
Write-Host ""

& $Python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 9500
