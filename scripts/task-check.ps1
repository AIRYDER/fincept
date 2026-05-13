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
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Get-WorkspacePackageName {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $pyproject = Join-Path $RepoRoot (Join-Path $Path "pyproject.toml")
    if (-not (Test-Path $pyproject)) {
        return $null
    }

    $nameLine = Select-String -Path $pyproject -Pattern '^\s*name\s*=' | Select-Object -First 1
    if (-not $nameLine) {
        return $null
    }

    return ($nameLine.Line -replace '^\s*name\s*=\s*"', '' -replace '"\s*$', '')
}

if ($Sync) {
    Invoke-Step "Sync uv workspace" { uv sync --all-packages --all-groups }
}

$joinedPaths = $PackagePath -join ", "
$pytestPackage = Get-WorkspacePackageName -Path $PackagePath[0]
if ($pytestPackage) {
    Invoke-Step "Pytest ($PytestPath)" { uv run --package $pytestPackage pytest $PytestPath }
} else {
    Invoke-Step "Pytest ($PytestPath)" { uv run pytest $PytestPath }
}
Invoke-Step "Ruff ($joinedPaths)" { uv run ruff check @PackagePath }
Invoke-Step "Mypy ($joinedPaths)" { uv run mypy @PackagePath }

Write-Host "Task check passed." -ForegroundColor Green
