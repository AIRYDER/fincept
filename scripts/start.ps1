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
    # --package fincept-core is required: scripts/wait_heartbeat.py imports
    # fincept_core.config and fincept_core.heartbeat, but the root project
    # (fincept-terminal) doesn't list fincept-core as a dependency, so a
    # bare `uv run` may not have it installed.  Same fix pattern as the
    # service launches above.
    $out = uv run --package fincept-core python scripts/wait_heartbeat.py $Name --timeout $TimeoutSec 2>&1
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

# Parallel-wait variant: spawn every service window, then poll Redis once
# for every expected heartbeat key.  Total startup = O(slowest service),
# not O(sum of services).  A service that never beats still doesn't block
# the others -- it just lands in the "WARN" bucket at the end.
function Wait-ForAllHeartbeats {
    param(
        [string[]]$Names,
        [int]$TimeoutSec = 30,
        [double]$PollIntervalSec = 0.5
    )
    if ($Names.Count -eq 0) { return @{} }
    $remaining = [System.Collections.Generic.HashSet[string]]::new($Names)
    $found = @{}
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline) {
        $script = @"
import asyncio, json, sys
from redis.asyncio import Redis
from fincept_core.heartbeat import read_all
from fincept_core.config import get_settings

async def go():
    r = Redis.from_url(get_settings().REDIS_URL)
    try:
        return await read_all(r)
    finally:
        await r.aclose()

print(json.dumps(asyncio.run(go())))
"@
        $raw = uv run --package fincept-core python -c $script 2>$null
        if ($LASTEXITCODE -eq 0 -and $raw) {
            try {
                $live = $raw | ConvertFrom-Json -AsHashtable
                foreach ($name in @($remaining)) {
                    if ($live.ContainsKey($name)) {
                        $found[$name] = $true
                        [void]$remaining.Remove($name)
                    }
                }
            } catch {
                # Bad JSON; just keep polling.
            }
        }
        if ($remaining.Count -gt 0) {
            Start-Sleep -Milliseconds ([int]($PollIntervalSec * 1000))
        }
    }
    foreach ($name in $remaining) { $found[$name] = $false }
    return $found
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
    Write-Step "Trading services (spawning all windows, then waiting in parallel)"

    # Build the list of services we expect to see beat.  Each block
    # spawns its window and, if the spawn happened, appends to
    # $expectedServices.  After every block runs we wait ONCE on all
    # of them concurrently -- worst case 30s total instead of 30s
    # per service.
    $expectedServices = New-Object System.Collections.Generic.List[string]

    # Ingestor: reads venue WebSocket, publishes md.trades + md.bars.1m.
    # Default is coinbase because binance returns HTTP 451 from US IPs
    # (geo-block).  Override with: $env:FINCEPT_INGESTOR_VENUE = "binance"
    # before running this script.  Supported: binance, coinbase, kraken.
    $ingestorVenue = if ($env:FINCEPT_INGESTOR_VENUE) { $env:FINCEPT_INGESTOR_VENUE } else { "coinbase" }
    Write-Host "    spawn: ingestor (venue=$ingestorVenue)" -ForegroundColor DarkGray
    Start-InNewWindow `
        -Title "fincept-ingestor" `
        -Command "uv run --package ingestor python -m ingestor.main --venue $ingestorVenue"
    $expectedServices.Add("ingestor")

    # Features: consumes bars, publishes online feature snapshots.
    Write-Host "    spawn: features" -ForegroundColor DarkGray
    Start-InNewWindow `
        -Title "fincept-features" `
        -Command "uv run --package features python -m features.main"
    $expectedServices.Add("features")

    # GBM predictor: consumes features, publishes Predictions.  Needs a
    # trained model; opt-in flag because most dev sessions don't have one.
    if ($WithGbm) {
        $modelDir = Join-Path $RepoRoot "models\gbm_predictor"
        if (Test-Path (Join-Path $modelDir "model.txt")) {
            Write-Host "    spawn: gbm_predictor" -ForegroundColor DarkGray
            Start-InNewWindow `
                -Title "fincept-gbm" `
                -Command "uv run --package agents python -m agents.gbm_predictor.main"
            $expectedServices.Add("gbm_predictor")
        } else {
            Write-Host "    SKIP: gbm_predictor (no model.txt at $modelDir)" `
                -ForegroundColor Yellow
            Write-Host "         Train with: uv run --package agents python -m agents.gbm_predictor.train --input <bars.parquet>" `
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
    $envText = ""
    if (Test-Path $envFile) {
        $envText = Get-Content $envFile -Raw
        if ($envText -match '(?m)^FINCEPT_NEWSAPI_API_KEY=\S') { $hasNewsAPI = $true }
        if ($envText -match '(?m)^FINCEPT_ANTHROPIC_API_KEY=\S') { $hasAnthropic = $true }
        if ($envText -match '(?m)^FINCEPT_OPENAI_API_KEY=\S') { $hasOpenAI = $true }
    }
    if ($hasNewsAPI -and ($hasAnthropic -or $hasOpenAI)) {
        Write-Host "    spawn: sentiment_agent" -ForegroundColor DarkGray
        Start-InNewWindow `
            -Title "fincept-sentiment" `
            -Command "uv run --package agents python -m agents.sentiment_agent.main"
        $expectedServices.Add("sentiment_agent")
    } else {
        Write-Host "    SKIP: sentiment_agent (need NEWSAPI_API_KEY plus ANTHROPIC_API_KEY or OPENAI_API_KEY in .env)" `
            -ForegroundColor Yellow
    }

    # Regime agent: polls FRED, classifies macro regime, publishes RegimeSignal
    # whenever the label changes.  Gated on FRED_API_KEY.
    $hasFred = $false
    if ($envText -match '(?m)^FINCEPT_FRED_API_KEY=\S') { $hasFred = $true }
    if ($hasFred) {
        Write-Host "    spawn: regime_agent" -ForegroundColor DarkGray
        Start-InNewWindow `
            -Title "fincept-regime" `
            -Command "uv run --package agents python -m agents.regime_agent.main"
        $expectedServices.Add("regime_agent")
    } else {
        Write-Host "    SKIP: regime_agent (missing FRED_API_KEY in .env)" `
            -ForegroundColor Yellow
    }

    # Orchestrator: consumes Predictions + sentiment + regime + price feed, publishes OrderIntents.
    Write-Host "    spawn: orchestrator" -ForegroundColor DarkGray
    Start-InNewWindow `
        -Title "fincept-orchestrator" `
        -Command "uv run --package orchestrator python -m orchestrator.main"
    $expectedServices.Add("orchestrator")

    # OMS: consumes OrderIntents, applies risk gate, fills via sim or Alpaca.
    Write-Host "    spawn: oms" -ForegroundColor DarkGray
    Start-InNewWindow `
        -Title "fincept-oms" `
        -Command "uv run --package oms python -m oms.main"
    $expectedServices.Add("oms")

    # Portfolio: consumes Fills, updates PositionStore.
    Write-Host "    spawn: portfolio" -ForegroundColor DarkGray
    Start-InNewWindow `
        -Title "fincept-portfolio" `
        -Command "uv run --package portfolio python -m portfolio.main"
    $expectedServices.Add("portfolio")

    # Jobs: APScheduler for cron tasks (EOD load).
    Write-Host "    spawn: jobs" -ForegroundColor DarkGray
    Start-InNewWindow `
        -Title "fincept-jobs" `
        -Command "uv run --package jobs python -m jobs.main"
    $expectedServices.Add("jobs")

    # ------ wait for all heartbeats in parallel ------
    if ($expectedServices.Count -gt 0) {
        Write-Host ""
        Write-Host "    waiting up to 30s for heartbeats from: $($expectedServices -join ', ')"
        $results = Wait-ForAllHeartbeats -Names $expectedServices.ToArray() -TimeoutSec 30
        foreach ($name in $expectedServices) {
            if ($results[$name]) {
                Write-Host "    OK   $name" -ForegroundColor Green
            } else {
                Write-Host "    WARN $name (no heartbeat in 30s; check 'fincept-$name' window)" `
                    -ForegroundColor Yellow
            }
        }
    }
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
