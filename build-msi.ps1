$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\build-release.ps1"
if ($LASTEXITCODE -ne 0) { throw "The portable application build failed." }

$Tools = Join-Path $Root "build\tools"
$DotnetDir = Join-Path $Tools "dotnet"
$WixDir = Join-Path $Tools "wix"
$Dotnet = Join-Path $DotnetDir "dotnet.exe"
$Wix = Join-Path $WixDir "wix.exe"
New-Item -ItemType Directory -Force -Path $Tools | Out-Null

if (-not (Test-Path $Dotnet)) {
    $Installer = Join-Path $Tools "dotnet-install.ps1"
    Write-Host "Downloading the official local .NET SDK bootstrapper..."
    Invoke-WebRequest "https://dot.net/v1/dotnet-install.ps1" -OutFile $Installer
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Installer -Channel 8.0 -InstallDir $DotnetDir
    if ($LASTEXITCODE -ne 0) { throw "The local .NET SDK could not be installed." }
}

if (-not (Test-Path $Wix)) {
    Write-Host "Installing WiX Toolset 5 into the local build directory..."
    & $Dotnet tool install wix --tool-path $WixDir --version "5.*"
    if ($LASTEXITCODE -ne 0) { throw "WiX Toolset could not be installed." }
}

$env:DOTNET_ROOT = $DotnetDir
$env:PATH = "$DotnetDir;$env:PATH"

$Exe = Join-Path $Root "dist\DaListener\DaListener.exe"
$Msi = Join-Path $Root "dist\DaListener-0.3.0-alpha.2-windows-x64.msi"
$SignTool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
$Pfx = $env:DALISTENER_WINDOWS_SIGN_PFX
$PfxPassword = $env:DALISTENER_WINDOWS_SIGN_PASSWORD

if ($Pfx -and $PfxPassword -and $SignTool) {
    & $SignTool.Source sign /fd SHA256 /td SHA256 /tr "http://timestamp.digicert.com" /f $Pfx /p $PfxPassword $Exe
    if ($LASTEXITCODE -ne 0) { throw "DaListener.exe signing failed." }
} else {
    Write-Host "Building an unsigned beta MSI. Configure DALISTENER_WINDOWS_SIGN_PFX and DALISTENER_WINDOWS_SIGN_PASSWORD to sign it."
}

if (Test-Path $Msi) { Remove-Item -LiteralPath $Msi }
& $Wix build ".\packaging\windows\Package.wxs" -arch x64 -bindpath "Payload=$Root\dist\DaListener" -out $Msi
if ($LASTEXITCODE -ne 0) { throw "WiX MSI build failed." }

if ($Pfx -and $PfxPassword -and $SignTool) {
    & $SignTool.Source sign /fd SHA256 /td SHA256 /tr "http://timestamp.digicert.com" /f $Pfx /p $PfxPassword $Msi
    if ($LASTEXITCODE -ne 0) { throw "MSI signing failed." }
}

$Hash = (Get-FileHash $Msi -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$Msi.sha256" -Value "$Hash  $(Split-Path $Msi -Leaf)" -Encoding ascii
Write-Host "Created $Msi"
Write-Host "SHA-256: $Hash"
