$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}
& $python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\blr_installer.spec
