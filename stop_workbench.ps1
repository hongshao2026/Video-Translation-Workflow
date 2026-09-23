$ErrorActionPreference = "SilentlyContinue"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path (Join-Path $ProjectRoot "runtime") "workbench-pids.json"

if (Test-Path -LiteralPath $PidFile) {
    $saved = Get-Content -Raw -LiteralPath $PidFile | ConvertFrom-Json
    & taskkill.exe /PID ([int]$saved.frontend) /T /F | Out-Null
    & taskkill.exe /PID ([int]$saved.backend) /T /F | Out-Null
    Remove-Item -LiteralPath $PidFile -Force
}

Write-Host "Dub workbench stopped."
