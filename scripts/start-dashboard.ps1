# Start AERP: REST API (8000) + control API (8712) + dashboard (8501), all on 127.0.0.1.
# Usage (from the repo root, PowerShell 7+ or Windows PowerShell 5.1):
#   .\scripts\start-dashboard.ps1            # then open http://127.0.0.1:8501
# Press Ctrl+C in this window to stop everything.
param([int]$ApiPort = 8000, [int]$ControlPort = 8712, [int]$DashboardPort = 8501)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
$py = if (Test-Path $venvPy) { $venvPy } else { "python" }

# One-time: dashboard dependency
& $py -c "import streamlit" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Installing dashboard requirements (one time)..." -ForegroundColor Yellow
  & $py -m pip install -r dashboard/requirements.txt
}

# Local-only settings for this session. The control token is random per start and
# never shown; the dashboard gets it through its environment (no password to type).
$env:APP_ENV = "development"
$env:LOG_FORMAT = "text"
$env:AUTH_ENABLED = "false"
$env:EQUITY_RESEARCH_CONTROL_TOKEN = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }) -as [byte[]])
$env:AERP_API_URL = "http://127.0.0.1:$ApiPort"
$env:AERP_CONTROL_URL = "http://127.0.0.1:$ControlPort"

$logDir = Join-Path $root "logs"; New-Item -ItemType Directory -Force $logDir | Out-Null
$hide = @{}; if ($PSVersionTable.PSEdition -eq "Desktop" -or $IsWindows) { $hide.WindowStyle = "Hidden" }
$api = Start-Process $py -ArgumentList "-m financial_intelligence.dashboard.launch api --port $ApiPort" `
  -RedirectStandardOutput "$logDir/api.out.log" -RedirectStandardError "$logDir/api.log" -PassThru @hide
$ctl = Start-Process $py -ArgumentList "-m financial_intelligence.dashboard.launch control --port $ControlPort" `
  -RedirectStandardOutput "$logDir/control.out.log" -RedirectStandardError "$logDir/control.log" -PassThru @hide

Write-Host "Waiting for the API..." -NoNewline
foreach ($i in 1..40) {
  try { Invoke-RestMethod "http://127.0.0.1:$ApiPort/health" -TimeoutSec 2 | Out-Null; Write-Host " ready" -ForegroundColor Green; break }
  catch { Start-Sleep -Milliseconds 500; Write-Host "." -NoNewline }
}
Write-Host "API:       http://127.0.0.1:$ApiPort/docs   (logs: logs\api.log)"
$url = "http://127.0.0.1:$DashboardPort"
Write-Host "Dashboard: $url  (Ctrl+C to stop all)" -ForegroundColor Cyan
if ($PSVersionTable.PSEdition -eq "Desktop" -or $IsWindows) { Start-Job { Start-Sleep 4; Start-Process $using:url } | Out-Null }
try {
  & $py -m streamlit run dashboard/app.py --server.port $DashboardPort --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false
}
finally {
  foreach ($p in @($api, $ctl)) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force } }
  Write-Host "Stopped API and control API."
}
