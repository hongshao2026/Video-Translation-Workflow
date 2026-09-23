$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeFile = Join-Path (Join-Path $ProjectRoot "runtime") "workbench-pids.json"
. (Join-Path $ProjectRoot "scripts\workbench-common.ps1")
$PythonExecutable = Resolve-WorkbenchPython -WorkbenchRoot $ProjectRoot

if (-not (Test-Path -LiteralPath $RuntimeFile -PathType Leaf)) {
    throw "No running workbench state was found."
}

$saved = Get-Content -Raw -LiteralPath $RuntimeFile | ConvertFrom-Json
$backendId = [int]$saved.backend
$backendProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $backendId"
if ($backendProcess -and $backendProcess.CommandLine -notmatch "uvicorn backend\.app:app.+8765") {
    throw "Saved backend PID does not belong to this workbench."
}

if ($backendProcess) {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $backendId"
    foreach ($child in $children) {
        Stop-Process -Id ([int]$child.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $backendId -Force -ErrorAction SilentlyContinue
}

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue
    if (-not $listener) { break }
    Start-Sleep -Milliseconds 250
}
if (Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue) {
    throw "Port 8765 is still occupied after stopping the saved backend."
}

$backend = Start-Process -FilePath $PythonExecutable `
    -ArgumentList @("-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8765") `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru

$saved.backend = $backend.Id
$saved.started_at = (Get-Date).ToString("s")
$saved | ConvertTo-Json | Set-Content -LiteralPath $RuntimeFile -Encoding UTF8

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 2
        if ($health.ok) {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $ready) {
    throw "Dub workbench backend did not restart in time."
}

Write-Host "Dub workbench backend restarted: PID $($backend.Id)"
