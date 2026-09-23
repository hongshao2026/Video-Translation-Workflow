param(
    [switch]$RequireProject,
    [ValidateSet("none", "any", "minimax", "openai")]
    [string]$Provider = "none",
    [switch]$Strict
)

$ErrorActionPreference = "Stop"
$WorkbenchRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
. (Join-Path $WorkbenchRoot "scripts\workbench-common.ps1")
$PythonExecutable = Resolve-WorkbenchPython -WorkbenchRoot $WorkbenchRoot

$Arguments = @(
    "-m", "backend.portability.cli", "diagnose",
    "--workbench-root", $WorkbenchRoot,
    "--provider", $Provider
)
if ($RequireProject) { $Arguments += "--require-project" }
if ($Strict) { $Arguments += "--strict" }

$ExitCode = 0
Push-Location $WorkbenchRoot
try {
    & $PythonExecutable @Arguments
    $ExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $ExitCode
