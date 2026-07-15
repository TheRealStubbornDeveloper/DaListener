$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\pythonw.exe"
$LogDirectory = Join-Path $env:LOCALAPPDATA "DaListener\Logs"
$OutputLog = Join-Path $LogDirectory "dashboard.stdout.log"
$ErrorLog = Join-Path $LogDirectory "dashboard.stderr.log"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "DaListener is not installed. Run setup.bat first."
}

New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
$DataDirectory = Join-Path $env:LOCALAPPDATA "DaListener\DaListener"
$LaunchAuth = Join-Path $DataDirectory "dashboard-auth.json"
try {
    $Health = Invoke-RestMethod "http://127.0.0.1:8765/api/v1/health" -TimeoutSec 2
    if ($Health.app -eq "DaListener" -and $Health.status -eq "ready" -and (Test-Path -LiteralPath $LaunchAuth)) {
        $LaunchToken = (Get-Content -LiteralPath $LaunchAuth -Raw | ConvertFrom-Json).launch_token
        if ($LaunchToken) {
            $ExistingUrl = "http://127.0.0.1:8765/auth/exchange?token=$LaunchToken"
            Start-Process $ExistingUrl
            Write-Host "DaListener was already running and has been opened." -ForegroundColor Green
            exit 0
        }
    }
} catch {
    # No healthy DaListener instance owns the stable bridge port.
}
Remove-Item -LiteralPath $OutputLog, $ErrorLog -Force -ErrorAction SilentlyContinue

Write-Host "Starting DaListener..."
$Process = Start-Process `
    -FilePath $Python `
    -ArgumentList "-u", "-m", "dalistener.dashboard.server" `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $OutputLog `
    -RedirectStandardError $ErrorLog `
    -WindowStyle Hidden `
    -PassThru

$Deadline = [DateTime]::UtcNow.AddSeconds(15)
while ([DateTime]::UtcNow -lt $Deadline) {
    $Process.Refresh()
    if ($Process.HasExited) {
        $Output = Get-Content -LiteralPath $OutputLog -Raw -ErrorAction SilentlyContinue
        if ($Process.ExitCode -eq 0 -and $Output -match "DaListener already running: (http://\S+)") {
            Write-Host "DaListener was already running and has been opened." -ForegroundColor Green
            exit 0
        }
        Write-Host "DaListener exited during startup with code $($Process.ExitCode)." -ForegroundColor Red
        Get-Content -LiteralPath $OutputLog, $ErrorLog -ErrorAction SilentlyContinue
        Write-Host "Startup logs: $LogDirectory" -ForegroundColor Yellow
        exit 1
    }

    $Output = Get-Content -LiteralPath $OutputLog -Raw -ErrorAction SilentlyContinue
    $Errors = Get-Content -LiteralPath $ErrorLog -Raw -ErrorAction SilentlyContinue
    $DashboardUrl = if ($Output -match "DaListener dashboard: (http://\S+)") { $Matches[1] } else { $null }
    if ($DashboardUrl -and $Errors -match "Application startup complete") {
        Write-Host "DaListener is ready: $DashboardUrl" -ForegroundColor Green
        Write-Host "Live logs: $LogDirectory"
        exit 0
    }
    Start-Sleep -Milliseconds 200
}

Write-Host "DaListener did not become ready within 15 seconds." -ForegroundColor Red
Get-Content -LiteralPath $OutputLog, $ErrorLog -ErrorAction SilentlyContinue
Write-Host "Startup logs: $LogDirectory" -ForegroundColor Yellow
Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
exit 1
