param(
    [Parameter(Mandatory = $true)]
    [string]$Root,
    [Parameter(Mandatory = $true)]
    [string]$Manifest,
    [string]$Destination = "成片",
    [switch]$RemoveSource
)

$ErrorActionPreference = "Stop"

$ResolvedRoot = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Root).ProviderPath).TrimEnd('\')
$ResolvedManifest = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Manifest).ProviderPath)
$ResolvedDestination = [System.IO.Path]::GetFullPath((Join-Path $ResolvedRoot $Destination)).TrimEnd('\')

if (-not ($ResolvedDestination + '\').StartsWith($ResolvedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination must stay inside the authorized root."
}

$RawItems = @(Get-Content -Raw -LiteralPath $ResolvedManifest | ConvertFrom-Json)
if ($RawItems.Count -eq 0) { throw "Manifest contains no files." }

$Items = foreach ($RawItem in $RawItems) {
    $RelativeSource = [string]$RawItem.source
    $Name = [string]$RawItem.name
    if ([System.IO.Path]::IsPathRooted($RelativeSource)) {
        throw "Manifest source paths must be relative to the authorized root."
    }
    if ([string]::IsNullOrWhiteSpace($Name) -or $Name -ne [System.IO.Path]::GetFileName($Name)) {
        throw "Each destination name must be a plain filename."
    }
    $Source = [System.IO.Path]::GetFullPath((Join-Path $ResolvedRoot $RelativeSource))
    if (-not ($Source + '\').StartsWith($ResolvedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Source escaped the authorized root."
    }
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "A manifest source file is missing."
    }
    [pscustomobject]@{
        Source = $Source
        Destination = Join-Path $ResolvedDestination $Name
        Name = $Name
        Bytes = [int64](Get-Item -LiteralPath $Source).Length
        SourceSha256 = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

if ((@($Items.Name | Sort-Object -Unique)).Count -ne $Items.Count) {
    throw "Manifest contains duplicate destination filenames."
}
if (Test-Path -LiteralPath $ResolvedDestination) {
    if (-not (Get-Item -LiteralPath $ResolvedDestination).PSIsContainer) {
        throw "Destination exists but is not a directory."
    }
} else {
    New-Item -ItemType Directory -Path $ResolvedDestination | Out-Null
}
foreach ($Item in $Items) {
    if (Test-Path -LiteralPath $Item.Destination) {
        throw "A destination file already exists; existing files are never overwritten."
    }
}

foreach ($Item in $Items) {
    Copy-Item -LiteralPath $Item.Source -Destination $Item.Destination -ErrorAction Stop
    $DestinationItem = Get-Item -LiteralPath $Item.Destination
    if ($DestinationItem.Length -ne $Item.Bytes) { throw "Copied file size does not match." }
    $DestinationHash = (Get-FileHash -LiteralPath $Item.Destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($DestinationHash -ne $Item.SourceSha256) { throw "Copied file hash does not match." }
    $Item | Add-Member -NotePropertyName DestinationSha256 -NotePropertyValue $DestinationHash
}

if ($RemoveSource) {
    foreach ($Item in $Items) {
        # Every source was resolved and checked inside $ResolvedRoot above, and
        # every destination has already passed byte-size and SHA-256 checks.
        Remove-Item -LiteralPath $Item.Source -Force -ErrorAction Stop
    }
}

[pscustomobject]@{
    Status = "completed"
    Destination = $ResolvedDestination
    FileCount = $Items.Count
    TotalBytes = [int64](($Items | Measure-Object -Property Bytes -Sum).Sum)
    SourcesRemoved = [bool]$RemoveSource
    Files = @($Items | ForEach-Object {
        [pscustomobject]@{
            Name = $_.Name
            Bytes = $_.Bytes
            Sha256 = $_.DestinationSha256
        }
    })
} | ConvertTo-Json -Depth 4
