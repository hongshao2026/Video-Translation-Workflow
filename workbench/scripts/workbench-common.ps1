Set-StrictMode -Version Latest

function Resolve-WorkbenchPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$WorkbenchRoot
    )

    if ($env:DUB_WORKBENCH_PYTHON) {
        if (-not (Test-Path -LiteralPath $env:DUB_WORKBENCH_PYTHON -PathType Leaf)) {
            throw "DUB_WORKBENCH_PYTHON points to an unavailable executable."
        }
        return (Resolve-Path -LiteralPath $env:DUB_WORKBENCH_PYTHON).ProviderPath
    }

    $candidates = @(
        (Join-Path $WorkbenchRoot ".venv\Scripts\python.exe"),
        (Join-Path $WorkbenchRoot ".venv\bin\python")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).ProviderPath
        }
    }

    $command = Get-Command python -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Python was not found. Run scripts/bootstrap.ps1 or set DUB_WORKBENCH_PYTHON."
    }
    return $command.Source
}

function Resolve-WorkbenchNpm {
    $command = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $command) {
        $command = Get-Command npm -ErrorAction SilentlyContinue
    }
    if (-not $command) {
        throw "npm was not found. Install the Node.js version declared in package.json."
    }
    return $command.Source
}
