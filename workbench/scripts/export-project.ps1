param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [Parameter(Mandatory = $true)]
    [string]$Archive,
    [switch]$IncludeMedia,
    [switch]$Overwrite
)

$ErrorActionPreference = "Stop"
$WorkbenchRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
. (Join-Path $WorkbenchRoot "scripts\workbench-common.ps1")
$PythonExecutable = Resolve-WorkbenchPython -WorkbenchRoot $WorkbenchRoot

$Arguments = @("-m", "backend.portability.cli", "export", $ProjectRoot, $Archive)
if ($IncludeMedia) { $Arguments += "--include-media" }
if ($Overwrite) { $Arguments += "--overwrite" }
$ExitCode = 0
Push-Location $WorkbenchRoot
try {
    & $PythonExecutable @Arguments
    $ExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $ExitCode
