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

if (-not (Test-Path ".env")) {
    Invoke-Step "Create .env from .env.example" { Copy-Item ".env.example" ".env" }
} else {
    Write-Host "==> .env already present" -ForegroundColor DarkGray
}

Invoke-Step "Start docker services" { docker compose up -d }
Invoke-Step "Sync uv workspace" { uv sync --all-packages --all-groups }
Invoke-Step "Install pnpm workspace dependencies" { pnpm install --frozen-lockfile=false }
Invoke-Step "Install pre-commit hooks" { uv run pre-commit install }

Write-Host "Development setup complete." -ForegroundColor Green
