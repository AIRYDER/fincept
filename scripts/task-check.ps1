param(
    [Parameter(Mandatory = $true)]
    [string[]]$PackagePath,

    [Parameter(Mandatory = $true)]
    [string]$PytestPath,

    [switch]$Sync
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Command
}

if ($Sync) {
    Invoke-Step "Sync uv workspace" { uv sync --all-packages --all-groups }
}

$joinedPaths = $PackagePath -join ", "
Invoke-Step "Pytest ($PytestPath)" { uv run pytest $PytestPath }
Invoke-Step "Ruff ($joinedPaths)" { uv run ruff check @PackagePath }
Invoke-Step "Mypy ($joinedPaths)" { uv run mypy @PackagePath }

Write-Host "Task check passed." -ForegroundColor Green
