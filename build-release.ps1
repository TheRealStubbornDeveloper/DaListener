$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Run setup.bat before building a release."
}

& ".venv\Scripts\python.exe" -m pip install -e ".[build,local-fallback]"
if ($LASTEXITCODE -ne 0) { throw "Build dependencies could not be installed." }
Push-Location "frontend"
& npm.cmd install
if ($LASTEXITCODE -ne 0) { throw "Frontend dependencies could not be installed." }
& npm.cmd run build
if ($LASTEXITCODE -ne 0) { throw "Dashboard frontend build failed." }
Pop-Location
& ".venv\Scripts\pyinstaller.exe" --noconfirm --clean "packaging\dalistener.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$Archive = Join-Path $Root "dist\DaListener-0.3.0-alpha.2-windows-x64.zip"
if (Test-Path $Archive) { Remove-Item -LiteralPath $Archive }
Compress-Archive -Path "dist\DaListener\*" -DestinationPath $Archive -CompressionLevel Optimal
Write-Host "Created $Archive"
