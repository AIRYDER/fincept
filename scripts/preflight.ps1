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
Invoke-Step "Python lint" { uv run ruff check libs services }
Invoke-Step "Python format check" { uv run ruff format --check libs services }
Invoke-Step "Python typecheck" { uv run mypy libs services }
Invoke-Step "Alembic upgrade" { uv run alembic -c libs/fincept-db/alembic.ini upgrade head }
Invoke-Step "Pytest with coverage" { uv run pytest --cov --cov-report=xml --cov-report=term-missing }
Invoke-Step "JS lint" { pnpm -r --if-present lint }
Invoke-Step "JS typecheck" { pnpm -r --if-present typecheck }
Invoke-Step "JS test" { pnpm -r --if-present test }
Invoke-Step "JS build" { pnpm -r --if-present build }
Invoke-Step "Secret scan" { uv run pre-commit run gitleaks --all-files }

Write-Host "Preflight passed." -ForegroundColor Green
