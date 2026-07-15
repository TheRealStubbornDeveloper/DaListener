param(
    [string]$PreparedModels = "$env:LOCALAPPDATA\DaListener\DaListener\Models\LocalFallback",
    [switch]$SkipApplicationBuild
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Version = "0.3.0-alpha.2"
$Output = Join-Path $Root "dist\DaListener-$Version-windows-x64-full"
$Assets = Join-Path $Output "offline-assets\LocalFallback"
$DistRoot = [IO.Path]::GetFullPath((Join-Path $Root "dist"))
$ResolvedOutput = [IO.Path]::GetFullPath($Output)
if (-not $ResolvedOutput.StartsWith($DistRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to replace an output directory outside the workspace dist directory."
}

Set-Location $Root
if (-not $SkipApplicationBuild) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\build-release.ps1"
    if ($LASTEXITCODE -ne 0) { throw "The DaListener application build failed." }
}

$Model = Get-ChildItem -LiteralPath $PreparedModels -Filter "*.gguf" -File -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $Model) {
    throw "No prepared LFM GGUF exists in '$PreparedModels'. Prepare Local mode once in DaListener, then rerun this script."
}
if (-not (Test-Path (Join-Path $PreparedModels "Moonshine"))) {
    throw "Prepared Moonshine models are missing. Prepare Local mode once in DaListener, then rerun this script."
}

if (Test-Path $Output) { Remove-Item -LiteralPath $Output -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Output,$Assets | Out-Null
Copy-Item -Path "$Root\dist\DaListener\*" -Destination $Output -Recurse -Force
Copy-Item -LiteralPath $Model.FullName -Destination $Assets
Copy-Item -LiteralPath (Join-Path $PreparedModels "Moonshine") -Destination $Assets -Recurse

$RuntimeTarget = Join-Path $Assets "LlamaCpp"
if (Test-Path (Join-Path $PreparedModels "LlamaCpp")) {
    Copy-Item -LiteralPath (Join-Path $PreparedModels "LlamaCpp") -Destination $Assets -Recurse
}

# Add the official CPU runtime too, so the same full package works without NVIDIA CUDA.
$Release = Invoke-RestMethod -Headers @{ "User-Agent" = "DaListener/$Version" } -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
$CpuAsset = $Release.assets | Where-Object { $_.name -match '^llama-.*-bin-win-cpu-x64\.zip$' } | Select-Object -First 1
if (-not $CpuAsset) { throw "The latest llama.cpp release has no Windows x64 CPU runtime." }
$CpuArchive = Join-Path $env:TEMP $CpuAsset.name
Invoke-WebRequest -Headers @{ "User-Agent" = "DaListener/$Version" } -Uri $CpuAsset.browser_download_url -OutFile $CpuArchive
if ($CpuAsset.digest -and $CpuAsset.digest.StartsWith("sha256:")) {
    $Expected = $CpuAsset.digest.Substring(7).ToLowerInvariant()
    $Actual = (Get-FileHash $CpuArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $Expected) { throw "llama.cpp CPU archive checksum verification failed." }
}
$CpuTarget = Join-Path $RuntimeTarget "$($Release.tag_name)\cpu"
New-Item -ItemType Directory -Force -Path $CpuTarget | Out-Null
Expand-Archive -LiteralPath $CpuArchive -DestinationPath $CpuTarget -Force
Remove-Item -LiteralPath $CpuArchive -Force

$Notices = Join-Path $Output "licenses"
New-Item -ItemType Directory -Force -Path $Notices | Out-Null
Invoke-WebRequest "https://huggingface.co/LiquidAI/LFM2.5-8B-A1B-GGUF/raw/main/LICENSE" -OutFile (Join-Path $Notices "LFM1.0-LICENSE.txt")
Invoke-WebRequest "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/LICENSE" -OutFile (Join-Path $Notices "llama.cpp-LICENSE.txt")
Copy-Item -LiteralPath "$Root\THIRD_PARTY_NOTICES.md" -Destination $Notices

$Manifest = @{
    format = 1
    created_utc = [DateTime]::UtcNow.ToString("o")
    lfm_model = $Model.Name
    lfm_sha256 = (Get-FileHash (Join-Path $Assets $Model.Name) -Algorithm SHA256).Hash.ToLowerInvariant()
    llama_cpp_release = $Release.tag_name
    includes = @("DaListener", "Moonshine streaming model", "LFM2.5-8B-A1B GGUF Q4", "llama.cpp CUDA runtime", "llama.cpp CPU runtime")
} | ConvertTo-Json -Depth 4
Set-Content -LiteralPath (Join-Path $Output "offline-assets\manifest.json") -Value $Manifest -Encoding utf8

$Instructions = @"
DaListener Full Offline Windows Package

1. Review licenses\THIRD_PARTY_NOTICES.md and the included license files.
2. Run DaListener.exe. No Python, Node.js, llama.cpp, or LFM download is required.
3. Choose Local mode and share a Chromium tab with Share tab audio enabled.
4. Stop from the dashboard or run: DaListener.exe --stop

Keep this folder together. DaListener selects CUDA when supported and CPU otherwise.
"@
Set-Content -LiteralPath (Join-Path $Output "START-HERE.txt") -Value $Instructions -Encoding utf8

Write-Host "Created full offline package directory: $Output"
Write-Host "Run: $Output\DaListener.exe"
