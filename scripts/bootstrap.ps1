param(
    [ValidateSet("core", "dev", "media")]
    [string]$Profile = "dev",
    [switch]$SkipNode
)

$ErrorActionPreference = "Stop"
$WorkbenchRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
. (Join-Path $WorkbenchRoot "scripts\workbench-common.ps1")

$BootstrapPython = Resolve-WorkbenchPython -WorkbenchRoot $WorkbenchRoot
$PythonVersion = & $BootstrapPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($PythonVersion -ne "3.12") {
    throw "Python 3.12 is required; resolved interpreter is $PythonVersion."
}

$VirtualEnvironment = Join-Path $WorkbenchRoot ".venv"
$VirtualPython = Join-Path $VirtualEnvironment "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VirtualPython -PathType Leaf)) {
    & $BootstrapPython -m venv $VirtualEnvironment
}
if (-not (Test-Path -LiteralPath $VirtualPython -PathType Leaf)) {
    $VirtualPython = Join-Path $VirtualEnvironment "bin\python"
}

$Requirements = switch ($Profile) {
    "core" { "requirements.lock" }
    "dev" { "requirements-dev.lock" }
    "media" { "requirements-media.lock" }
}
& $VirtualPython -m pip install --disable-pip-version-check -r (Join-Path $WorkbenchRoot $Requirements)
& $VirtualPython -m pip install --disable-pip-version-check --no-build-isolation --no-deps -e $WorkbenchRoot

if (-not $SkipNode) {
    $Npm = Resolve-WorkbenchNpm
    & $Npm ci --prefix $WorkbenchRoot
}

$ExitCode = 0
Push-Location $WorkbenchRoot
try {
    & $VirtualPython -m backend.portability.cli diagnose --workbench-root $WorkbenchRoot
    $ExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($ExitCode -ne 0) {
    throw "Environment installation completed, but diagnostics could not run."
}
Write-Host "Workbench environment installed. Device-local bindings still need to be configured."
