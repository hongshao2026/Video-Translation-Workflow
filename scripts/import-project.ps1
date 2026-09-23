param(
    [Parameter(Mandatory = $true)]
    [string]$Archive,
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$WorkbenchRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
. (Join-Path $WorkbenchRoot "scripts\workbench-common.ps1")
$PythonExecutable = Resolve-WorkbenchPython -WorkbenchRoot $WorkbenchRoot

$ExitCode = 0
Push-Location $WorkbenchRoot
try {
    & $PythonExecutable -m backend.portability.cli import $Archive $Destination
    $ExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $ExitCode
