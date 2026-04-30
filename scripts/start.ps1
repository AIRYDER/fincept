<#
.SYNOPSIS
  One-command launch for the Fincept stack (Redis + API + Dashboard).

.DESCRIPTION
  Verifies Memurai/Redis is reachable on :6379 (starts the Windows
  service if installed), launches the FastAPI server on :8000 in a new
  window, launches the Next.js dashboard on :3000 in a new window,
  polls /health until both are ready, optionally re-syncs Alpaca
  positions into Redis, and prints a JWT + URLs for the operator.

.PARAMETER Sync
  After the API is healthy, run scripts/sync_alpaca.py to refresh
  positions from the paper Alpaca account.

.PARAMETER NoDashboard
  Skip the Next.js dashboard (API-only mode).

.EXAMPLE
  ./scripts/start.ps1
  ./scripts/start.ps1 -Sync
#>
[CmdletBinding()]
param(
    [switch]$Sync,
    [switch]$NoDashboard,
    [switch]$NoServices,
    [switch]$WithGbm
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

function Write-Step {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host "==> $Message" -ForegroundColor $Color
}

function Test-TcpPort {
    param([string]$ComputerName = "127.0.0.1", [int]$Port, [int]$TimeoutMs = 500)
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $task = $tcp.ConnectAsync($ComputerName, $Port)
        if ($task.Wait($TimeoutMs)) {
            $ok = $tcp.Connected
            $tcp.Close()
            return $ok
        }
        $tcp.Close()
        return $false
    } catch {
        return $false
    }
}

function Wait-ForHttp {
    param(
        [string]$Url,
        [int]$TimeoutSec = 30,
        [string]$Label = "service"
    )
    $start = Get-Date
    while (((Get-Date) - $start).TotalSeconds -lt $TimeoutSec) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -NoProxy -UseBasicParsing `
                -TimeoutSec 2 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { return $true }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    Write-Host "    WARN: $Label did not respond to $Url within ${TimeoutSec}s" `
        -ForegroundColor Yellow
    return $false
}

function Start-InNewWindow {
    param(
        [string]$Title,
        [string]$Command,
        [string]$WorkingDirectory = $RepoRoot
    )
    $escaped = $Command.Replace("'", "''")
    $launch = @"
`$Host.UI.RawUI.WindowTitle = '$Title'
Set-Location '$WorkingDirectory'
Write-Host '[$Title] starting...' -ForegroundColor Green
$escaped
"@
    Start-Process pwsh -ArgumentList @(
        '-NoExit',
        '-NoProfile',
        '-Command',
        $launch
    ) | Out-Null
}

function Wait-ForHeartbeat {
    param(
        [string]$Name,
        [int]$TimeoutSec = 30
    )
    $out = uv run python scripts/wait_heartbeat.py $Name --timeout $TimeoutSec 2>&1
    return ($LASTEXITCODE -eq 0)
}

function Start-Service-WithHeartbeat {
    param(
        [string]$WindowTitle,
        [string]$ServiceName,
        [string]$Command,
        [int]$TimeoutSec = 30
    )
    Write-Step "Service: $ServiceName"
    Start-InNewWindow -Title $WindowTitle -Command $Command
    if (Wait-ForHeartbeat -Name $ServiceName -TimeoutSec $TimeoutSec) {
        Write-Host "    OK  heartbeat received" -ForegroundColor Green
    } else {
        Write-Host "    WARN: $ServiceName never reported a heartbeat (check '$WindowTitle' window)" `
            -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------
# 1. Redis / Memurai
# ---------------------------------------------------------------------

Write-Step "Redis / Memurai on :6379"
if (Test-TcpPort -Port 6379) {
    Write-Host "    already running" -ForegroundColor DarkGray
} else {
    $svc = Get-Service -Name Memurai -ErrorAction SilentlyContinue
    if ($null -ne $svc) {
        Write-Host "    starting Memurai service..."
        Start-Service -Name Memurai
        Start-Sleep -Seconds 1
    }
    if (-not (Test-TcpPort -Port 6379)) {
        Write-Host "    ERROR: Redis not reachable on :6379." -ForegroundColor Red
        Write-Host "    Install Memurai (https://www.memurai.com) or start redis-server manually."
        exit 1
    }
    Write-Host "    OK" -ForegroundColor Green
}

# ---------------------------------------------------------------------
# 2. API (FastAPI / uvicorn on :8000)
# ---------------------------------------------------------------------

Write-Step "API on :8000"
if (Test-TcpPort -Port 8000) {
    Write-Host "    already running (leaving it alone)" -ForegroundColor DarkGray
} else {
    Start-InNewWindow `
        -Title "fincept-api" `
        -Command "uv run --package api uvicorn api.main:app --reload --port 8000"
    $ok = Wait-ForHttp -Url "http://127.0.0.1:8000/health" -TimeoutSec 30 -Label "API"
    if (-not $ok) {
        Write-Host "    API window opened but /health never responded." -ForegroundColor Yellow
        Write-Host "    Check the 'fincept-api' window for a traceback."
    } else {
        Write-Host "    OK  http://127.0.0.1:8000" -ForegroundColor Green
    }
}

# ---------------------------------------------------------------------
# 3. Trading services (ingestor + features + orchestrator + OMS + portfolio + jobs)
#
# Each runs in its own window so a crash is visible.  Heartbeats are
# written to Redis (`service:heartbeat:{name}`) and surfaced in the
# dashboard /risk page Services Health panel.
# ---------------------------------------------------------------------

if (-not $NoServices) {
    Write-Step "Trading services"

    # Ingestor: reads venue WebSocket, publishes md.trades + md.bars.1m.
    # Default is coinbase because binance returns HTTP 451 from US IPs
    # (geo-block).  Override with: $env:FINCEPT_INGESTOR_VENUE = "binance"
    # before running this script.  Supported: binance, coinbase, kraken.
    $ingestorVenue = if ($env:FINCEPT_INGESTOR_VENUE) { $env:FINCEPT_INGESTOR_VENUE } else { "coinbase" }
    Write-Host "    venue: $ingestorVenue (override with `$env:FINCEPT_INGESTOR_VENUE)" -ForegroundColor DarkGray
    Start-Service-WithHeartbeat `
        -WindowTitle "fincept-ingestor" `
        -ServiceName "ingestor" `
        -Command "uv run --package ingestor python -m ingestor.main --venue $ingestorVenue"

    # Features: consumes bars, publishes online feature snapshots.
    Start-Service-WithHeartbeat `
        -WindowTitle "fincept-features" `
        -ServiceName "features" `
        -Command "uv run --package features python -m features.main"

    # GBM predictor: consumes features, publishes Predictions.  Needs a
    # trained model; opt-in flag because most dev sessions don't have one.
    if ($WithGbm) {
        $modelDir = Join-Path $RepoRoot "models\gbm_predictor"
        if (Test-Path (Join-Path $modelDir "model.txt")) {
            Start-Service-WithHeartbeat `
                -WindowTitle "fincept-gbm" `
                -ServiceName "gbm_predictor" `
                -Command "uv run --package agents python -m agents.gbm_predictor.main"
        } else {
            Write-Host "    SKIP: gbm_predictor (no model.txt at $modelDir)" `
                -ForegroundColor Yellow
            Write-Host "         Train with: uv run python -m agents.gbm_predictor.train --input <bars.parquet>" `
                -ForegroundColor DarkGray
        }
    }

    # Sentiment agent: polls NewsAPI + (Anthropic OR OpenAI), publishes
    # SentimentSignal.  NewsAPI is required; the agent picks a usable
    # LLM provider via fincept_core.config.LLM_PROVIDER (default "auto"
    # tries Anthropic, falls back to OpenAI).  We probe .env here to
    # avoid spawning a window that closes immediately.
    $envFile = Join-Path $RepoRoot ".env"
    $hasNewsAPI = $false
    $hasAnthropic = $false
    $hasOpenAI = $false
    if (Test-Path $envFile) {
        $envText = Get-Content $envFile -Raw
        if ($envText -match '(?m)^FINCEPT_NEWSAPI_API_KEY=\S') { $hasNewsAPI = $true }
        if ($envText -match '(?m)^FINCEPT_ANTHROPIC_API_KEY=\S') { $hasAnthropic = $true }
        if ($envText -match '(?m)^FINCEPT_OPENAI_API_KEY=\S') { $hasOpenAI = $true }
    }
    if ($hasNewsAPI -and ($hasAnthropic -or $hasOpenAI)) {
        Start-Service-WithHeartbeat `
            -WindowTitle "fincept-sentiment" `
            -ServiceName "sentiment_agent" `
            -Command "uv run --package agents python -m agents.sentiment_agent.main"
    } else {
        Write-Host "    SKIP: sentiment_agent (need NEWSAPI_API_KEY plus ANTHROPIC_API_KEY or OPENAI_API_KEY in .env)" `
            -ForegroundColor Yellow
    }

    # Regime agent: polls FRED, classifies macro regime, publishes RegimeSignal
    # whenever the label changes.  Gated on FRED_API_KEY.
    $hasFred = $false
    if (Test-Path $envFile) {
        if ($envText -match '(?m)^FINCEPT_FRED_API_KEY=\S') { $hasFred = $true }
    }
    if ($hasFred) {
        Start-Service-WithHeartbeat `
            -WindowTitle "fincept-regime" `
            -ServiceName "regime_agent" `
            -Command "uv run --package agents python -m agents.regime_agent.main"
    } else {
        Write-Host "    SKIP: regime_agent (missing FRED_API_KEY in .env)" `
            -ForegroundColor Yellow
    }

    # Orchestrator: consumes Predictions + sentiment + regime + price feed, publishes OrderIntents.
    Start-Service-WithHeartbeat `
        -WindowTitle "fincept-orchestrator" `
        -ServiceName "orchestrator" `
        -Command "uv run --package orchestrator python -m orchestrator.main"

    # OMS: consumes OrderIntents, applies risk gate, fills via sim or Alpaca.
    Start-Service-WithHeartbeat `
        -WindowTitle "fincept-oms" `
        -ServiceName "oms" `
        -Command "uv run --package oms python -m oms.main"

    # Portfolio: consumes Fills, updates PositionStore.
    Start-Service-WithHeartbeat `
        -WindowTitle "fincept-portfolio" `
        -ServiceName "portfolio" `
        -Command "uv run --package portfolio python -m portfolio.main"

    # Jobs: APScheduler for cron tasks (EOD load).
    Start-Service-WithHeartbeat `
        -WindowTitle "fincept-jobs" `
        -ServiceName "jobs" `
        -Command "uv run --package jobs python -m jobs.main"
}

# ---------------------------------------------------------------------
# 4. Optional Alpaca sync
# ---------------------------------------------------------------------

if ($Sync) {
    Write-Step "Sync Alpaca positions"
    if (-not (Test-Path (Join-Path $RepoRoot ".env"))) {
        if (Test-Path (Join-Path $RepoRoot ".env.example")) {
            Copy-Item (Join-Path $RepoRoot ".env.example") (Join-Path $RepoRoot ".env")
            Write-Host "    created .env from .env.example" -ForegroundColor DarkGray
        }
    }
    $hasKey = $false
    if (Test-Path (Join-Path $RepoRoot ".env")) {
        $env_text = Get-Content (Join-Path $RepoRoot ".env") -Raw
        if ($env_text -match '(?m)^FINCEPT_ALPACA_API_KEY=\S') { $hasKey = $true }
    }
    if (-not $hasKey) {
        Write-Host "    skipped - Alpaca keys not set in .env" -ForegroundColor Yellow
        Write-Host "    Edit .env at the repo root and fill in:" -ForegroundColor Yellow
        Write-Host "      FINCEPT_ALPACA_API_KEY=..."
        Write-Host "      FINCEPT_ALPACA_API_SECRET=..."
        Write-Host "    Then re-run ./scripts/start.ps1 -Sync (or ./start.bat)."
    } else {
        try {
            uv run python scripts/sync_alpaca.py
        } catch {
            Write-Host "    Sync failed: $_" -ForegroundColor Yellow
        }
    }
}

# ---------------------------------------------------------------------
# 4. Dashboard (Next.js on :3000)
# ---------------------------------------------------------------------

if (-not $NoDashboard) {
    Write-Step "Dashboard on :3000"
    if (Test-TcpPort -Port 3000) {
        Write-Host "    already running (leaving it alone)" -ForegroundColor DarkGray
    } else {
        Start-InNewWindow `
            -Title "fincept-dashboard" `
            -Command "pnpm dev" `
            -WorkingDirectory (Join-Path $RepoRoot "apps\dashboard")
        $ok = Wait-ForHttp -Url "http://127.0.0.1:3000" -TimeoutSec 60 -Label "Dashboard"
        if ($ok) {
            Write-Host "    OK  http://localhost:3000" -ForegroundColor Green
        }
    }
}

# ---------------------------------------------------------------------
# 6. Mint a dev JWT for the login screen
# ---------------------------------------------------------------------

Write-Step "Dev JWT"
$token = $null
try {
    $out = uv run --package api python -W ignore -c "import warnings; warnings.filterwarnings('ignore', category=UserWarning, module='cryptography'); from api.auth import encode_token; print(encode_token({'sub':'operator'}))" 2>$null
    # Keep only the JWT-shaped line (3 base64url segments separated by dots).
    $token = ($out | Where-Object { $_ -match '^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$' } |
        Select-Object -First 1)
    if ($token) { $token = $token.Trim() }
} catch {
    $token = ""
}

# ---------------------------------------------------------------------
# Summary banner
# ---------------------------------------------------------------------

Write-Host ""
Write-Host "---------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "  Fincept Terminal is up" -ForegroundColor Green
Write-Host "---------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "  Dashboard : http://localhost:3000"
Write-Host "  API       : http://127.0.0.1:8000  (docs: /docs)"
Write-Host "  Redis     : 127.0.0.1:6379"
if ($token) {
    Write-Host ""
    Write-Host "  Dev JWT (paste into the login screen):"
    Write-Host "  $token" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  Stop everything : ./scripts/stop.ps1"
Write-Host "  Status          : ./scripts/status.ps1"
Write-Host "---------------------------------------------------------------" -ForegroundColor DarkGray
