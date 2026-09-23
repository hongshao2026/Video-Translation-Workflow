param(
    [switch]$NoBrowser,
    [string]$ProjectConfig
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $ProjectRoot "runtime"
$PidFile = Join-Path $RuntimeDir "workbench-pids.json"
. (Join-Path $ProjectRoot "scripts\workbench-common.ps1")
$PythonExecutable = Resolve-WorkbenchPython -WorkbenchRoot $ProjectRoot
$NpmExecutable = Resolve-WorkbenchNpm

if ($ProjectConfig) {
    if (-not (Test-Path -LiteralPath $ProjectConfig -PathType Leaf)) {
        throw "The requested project configuration is unavailable."
    }
    $env:DUB_PROJECT_CONFIG = (Resolve-Path -LiteralPath $ProjectConfig).ProviderPath
}

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

$backend = Start-Process -FilePath $PythonExecutable `
    -ArgumentList @("-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8765") `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru

$frontend = Start-Process -FilePath $NpmExecutable `
    -ArgumentList @("run", "dev") `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru

@{
    backend = $backend.Id
    frontend = $frontend.Id
    started_at = (Get-Date).ToString("s")
} | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding UTF8

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 2
        $page = Invoke-WebRequest -Uri "http://localhost:3000/" -UseBasicParsing -TimeoutSec 2
        if ($health.ok -and $page.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 700
    }
}

if (-not $ready) {
    Stop-Process -Id $backend.Id, $frontend.Id -Force -ErrorAction SilentlyContinue
    throw "Dub workbench did not start in time."
}

if (-not $NoBrowser) {
    Start-Process "http://localhost:3000/"
}
Write-Host "Dub workbench started: http://localhost:3000/"
Write-Host "Run stop_workbench.cmd to stop it."
