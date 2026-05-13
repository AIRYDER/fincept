<#
.SYNOPSIS
  One-command launch for the Fincept stack.

.DESCRIPTION
  Verifies Memurai/Redis is reachable on :6379 (starts the Windows
  service if installed), launches the local OpenBB API if available,
  launches the FastAPI server on :8010 in a new window, launches the
  Next.js dashboard on :3000 in a new window, launches core trading
  services, news enrichment, news outcome labeling, optional GBM and
  news-alpha predictors, optional sentiment and regime agents, polls
  service health, optionally re-syncs Alpaca positions into Redis, and
  prints a JWT + URLs for the operator.

.PARAMETER Sync
  After the API is healthy, run scripts/sync_alpaca.py to refresh
  positions from the paper Alpaca account.

.PARAMETER NoDashboard
  Skip the Next.js dashboard (API-only mode).

.PARAMETER NoOpenBB
  Skip starting the local OpenBB API backend.

.PARAMETER NoServices
  Skip trading/news/agent service windows.

.PARAMETER WithGbm
  Start the GBM predictor when a trained model exists.

.PARAMETER ReloadApi
  Start FastAPI with uvicorn --reload. Disabled by default to reduce
  process count and Windows commit/pagefile pressure during full-stack
  startup.

.EXAMPLE
  ./scripts/start.ps1
  ./scripts/start.ps1 -Sync
#>
[CmdletBinding()]
param(
    [switch]$Sync,
    [switch]$NoDashboard,
    [switch]$NoOpenBB,
    [switch]$NoServices,
    [switch]$Full,
    [switch]$WithMarketData,
    [switch]$WithNewsLearning,
    [switch]$WithJobs,
    [switch]$WithGbm,
    [switch]$WithNewsAlpha,
    [switch]$WithSentiment,
    [switch]$WithRegime,
    [switch]$WithOpenBB,
    [switch]$ReloadApi,
    [int]$ApiPort = 8010,
    [int]$SpawnDelayMs = 250
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
$StartOpenBB = ($Full -or $WithOpenBB) -and -not $NoOpenBB
$StartMarketData = $Full -or $WithMarketData
$StartNewsLearning = $Full -or $WithNewsLearning
$StartJobs = $Full -or $WithJobs
$StartGbm = $Full -or $WithGbm
$StartNewsAlpha = $Full -or $WithNewsAlpha
$StartSentiment = $Full -or $WithSentiment
$StartRegime = $Full -or $WithRegime

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

function Write-Step {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host "==> $Message" -ForegroundColor $Color
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Start-MemuraiWithElevation {
    try {
        $command = "Start-Service -Name Memurai"
        Start-Process pwsh -Verb RunAs -Wait -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            $command
        ) | Out-Null
        Start-Sleep -Seconds 1
        return $true
    } catch {
        Write-Host "    WARN: elevated Memurai start failed or was canceled: $($_.Exception.Message)" `
            -ForegroundColor Yellow
        return $false
    }
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

function Test-FinceptApi {
    param([string]$Url)
    try {
        $resp = Invoke-WebRequest -Uri $Url -NoProxy -UseBasicParsing `
            -TimeoutSec 2 -ErrorAction Stop
        if ($resp.StatusCode -ne 200) { return $false }
        $json = $resp.Content | ConvertFrom-Json -ErrorAction Stop
        return [bool]($json.ok -eq $true -and $json.version)
    } catch {
        return $false
    }
}

function Get-DotEnvValue {
    param([string]$Name)
    $live = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if (-not [string]::IsNullOrWhiteSpace($live)) {
        return $live.Trim()
    }
    $envFile = Join-Path $RepoRoot ".env"
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.+?)\s*$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

function Get-FinceptSettingValue {
    param([string]$Name)
    $prefixed = Get-DotEnvValue -Name "FINCEPT_$Name"
    if (-not [string]::IsNullOrWhiteSpace($prefixed)) {
        return $prefixed
    }
    return Get-DotEnvValue -Name $Name
}

function Get-OpenBBApiUrl {
    $configured = Get-DotEnvValue -Name "OPENBB_API_URL"
    if ([string]::IsNullOrWhiteSpace($configured)) {
        return "http://127.0.0.1:6900"
    }
    return $configured.TrimEnd("/")
}

function Get-OpenBBApiCommand {
    $cmd = Get-Command openbb-api -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd -and $cmd.Source) { return $cmd.Source }
    $candidates = @(
        "C:\Python310\Scripts\openbb-api.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\Scripts\openbb-api.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\Scripts\openbb-api.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\Scripts\openbb-api.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    return $null
}

function Test-OpenBBApi {
    param([string]$BaseUrl)
    try {
        $resp = Invoke-WebRequest -Uri "$BaseUrl/openapi.json" -NoProxy -UseBasicParsing `
            -TimeoutSec 2 -ErrorAction Stop
        return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300)
    } catch {
        return $false
    }
}

function Find-OpenBBApiUrl {
    param(
        [string]$BaseUrl,
        [int]$MaxPortOffset = 10
    )
    try {
        $uri = [Uri]$BaseUrl
    } catch {
        return $null
    }
    $hostName = if ($uri.Host) { $uri.Host } else { "127.0.0.1" }
    $startPort = if ($uri.Port -gt 0) { $uri.Port } else { 6900 }
    for ($port = $startPort; $port -le ($startPort + $MaxPortOffset); $port++) {
        $candidate = "{0}://{1}:{2}" -f $uri.Scheme, $hostName, $port
        if (Test-OpenBBApi -BaseUrl $candidate) {
            return $candidate
        }
    }
    return $null
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
    if ($SpawnDelayMs -gt 0) {
        Start-Sleep -Milliseconds $SpawnDelayMs
    }
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
    uv run --package fincept-core python scripts/wait_heartbeat.py $Name --timeout $TimeoutSec 2>&1 | Out-Null
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

# Parallel-wait variant: spawn every service window, then run ONE Python
# process that polls Redis in a loop and prints results as services come
# online.  Total startup = O(slowest service), not O(sum of services).
#
# Why a single process instead of polling from PowerShell:
#   * Each `uv run` spawn costs ~1-2s, so polling every 500ms from the
#     shell is silly -- you'd get maybe 10 polls in a 30s window.
#   * If REDIS_URL points at 'localhost' on Windows, every spawn opens
#     a fresh Redis client and hits the IPv6 tarpit (::1 -> 60s OS
#     timeout).  A 30s shell-side deadline turns into hours.
#   * One long-lived Python process with socket_connect_timeout=2 fails
#     fast and gives PowerShell a deterministic completion time.
function Wait-ForAllHeartbeats {
    param(
        [string[]]$Names,
        [int]$TimeoutSec = 30
    )
    if ($Names.Count -eq 0) { return @{} }
    $namesJson = ($Names | ConvertTo-Json -Compress)
    if ($Names.Count -eq 1) {
        # ConvertTo-Json on a 1-element array yields a scalar string;
        # force array shape so the Python side can json.loads() it as
        # a list.
        $namesJson = "[$namesJson]"
    }
    $py = @"
import asyncio, json, sys, time
from redis.asyncio import Redis
from fincept_core.config import get_settings
from fincept_core.heartbeat import HEARTBEAT_PREFIX

NAMES = json.loads('''$namesJson''')
DEADLINE = time.monotonic() + $TimeoutSec

async def main():
    settings = get_settings()
    # socket_connect_timeout fails the connect attempt fast if the URL
    # is unreachable (e.g. IPv6 tarpit, wrong port).  Without it the
    # async resolver can hang the whole process indefinitely.
    redis = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2.0)
    found = {}
    try:
        while time.monotonic() < DEADLINE and len(found) < len(NAMES):
            for name in NAMES:
                if name in found:
                    continue
                try:
                    val = await redis.get(f"{HEARTBEAT_PREFIX}{name}")
                except Exception:
                    val = None
                if val is not None:
                    found[name] = True
            if len(found) == len(NAMES):
                break
            await asyncio.sleep(0.5)
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass
    print(json.dumps({n: (n in found) for n in NAMES}))

asyncio.run(main())
"@
    $raw = uv run --package fincept-core python -c $py 2>$null
    $result = @{}
    foreach ($n in $Names) { $result[$n] = $false }
    if ($LASTEXITCODE -eq 0 -and $raw) {
        try {
            $parsed = $raw | ConvertFrom-Json -AsHashtable
            foreach ($k in $parsed.Keys) { $result[$k] = [bool]$parsed[$k] }
        } catch {
            # Bad JSON; leave $result all-false.
        }
    }
    return $result
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
        try {
            Start-Service -Name Memurai -ErrorAction Stop
            Start-Sleep -Seconds 1
        } catch {
            Write-Host "    WARN: could not start Memurai service: $($_.Exception.Message)" `
                -ForegroundColor Yellow
            if (-not (Test-IsAdministrator)) {
                Write-Host "    This shell is not elevated. Re-run start.ps1 as Administrator, or start Memurai from Services."
                Write-Host "    Requesting elevation to start Memurai..."
                [void](Start-MemuraiWithElevation)
            } else {
                Write-Host "    Check service status with: Get-Service Memurai"
            }
        }
    }
    if (-not (Test-TcpPort -Port 6379)) {
        Write-Host "    ERROR: Redis not reachable on :6379." -ForegroundColor Red
        Write-Host "    Install Memurai (https://www.memurai.com) or start redis-server manually."
        exit 1
    }
    Write-Host "    OK" -ForegroundColor Green
}

# ---------------------------------------------------------------------
# 2. OpenBB API (optional local research backend on :6900 by default)
# ---------------------------------------------------------------------

$OpenBBBaseUrl = Get-OpenBBApiUrl
if ($StartOpenBB) {
    Write-Step "OpenBB API at $OpenBBBaseUrl"
    $openbbUri = $null
    try {
        $openbbUri = [Uri]$OpenBBBaseUrl
    } catch {
        Write-Host "    WARN: OPENBB_API_URL is not a valid URL: $OpenBBBaseUrl" `
            -ForegroundColor Yellow
    }
    $openbbHost = if ($openbbUri -and $openbbUri.Host) { $openbbUri.Host } else { "127.0.0.1" }
    $openbbPort = if ($openbbUri -and $openbbUri.Port -gt 0) { $openbbUri.Port } else { 6900 }
    if (Test-OpenBBApi -BaseUrl $OpenBBBaseUrl) {
        Write-Host "    already running" -ForegroundColor DarkGray
        [Environment]::SetEnvironmentVariable("OPENBB_API_URL", $OpenBBBaseUrl, "Process")
    } elseif (Test-TcpPort -ComputerName $openbbHost -Port $openbbPort) {
        $actualOpenBBUrl = Find-OpenBBApiUrl -BaseUrl $OpenBBBaseUrl
        if ($actualOpenBBUrl) {
            $OpenBBBaseUrl = $actualOpenBBUrl
            [Environment]::SetEnvironmentVariable("OPENBB_API_URL", $OpenBBBaseUrl, "Process")
            Write-Host "    OK  OpenBB detected at $OpenBBBaseUrl" -ForegroundColor Green
        } else {
            Write-Host "    WARN: :$openbbPort is listening, but OpenBB /openapi.json did not respond" `
                -ForegroundColor Yellow
            Write-Host "          Open Data Platform may still be starting, or another process owns the port." `
                -ForegroundColor Yellow
        }
    } else {
        $openbbCommand = Get-OpenBBApiCommand
        if ($openbbCommand) {
            Write-Host "    spawn: OpenBB API ($openbbCommand)" -ForegroundColor DarkGray
            Start-InNewWindow `
                -Title "fincept-openbb" `
                -Command "& `"$openbbCommand`" --host $openbbHost --port $openbbPort"
            if (Wait-ForHttp -Url "$OpenBBBaseUrl/openapi.json" -TimeoutSec 45 -Label "OpenBB API") {
                [Environment]::SetEnvironmentVariable("OPENBB_API_URL", $OpenBBBaseUrl, "Process")
                Write-Host "    OK  $OpenBBBaseUrl" -ForegroundColor Green
            } else {
                $actualOpenBBUrl = Find-OpenBBApiUrl -BaseUrl $OpenBBBaseUrl
                if ($actualOpenBBUrl) {
                    $OpenBBBaseUrl = $actualOpenBBUrl
                    [Environment]::SetEnvironmentVariable("OPENBB_API_URL", $OpenBBBaseUrl, "Process")
                    Write-Host "    OK  OpenBB moved to $OpenBBBaseUrl" -ForegroundColor Green
                } else {
                    Write-Host "    WARN: OpenBB API did not start. Check the 'fincept-openbb' window." `
                        -ForegroundColor Yellow
                    Write-Host "          Prior setup also supports Open Data Platform Desktop -> Backends -> OpenBB API -> Start." `
                        -ForegroundColor Yellow
                }
            }
        } else {
            Write-Host "    SKIP: openbb-api command not found" -ForegroundColor Yellow
            Write-Host "          Start Open Data Platform Desktop -> Backends -> OpenBB API -> Start." `
                -ForegroundColor Yellow
        }
    }
} else {
    Write-Step "OpenBB API skipped (use -WithOpenBB or -Full)"
}

# ---------------------------------------------------------------------
# 3. API (FastAPI / uvicorn on configured API port)
# ---------------------------------------------------------------------

Write-Step "API on :$ApiPort"
$ApiBaseUrl = "http://127.0.0.1:$ApiPort"
if (Test-TcpPort -Port $ApiPort) {
    if (Test-FinceptApi -Url "$ApiBaseUrl/health") {
        Write-Host "    already running (leaving it alone)" -ForegroundColor DarkGray
    } else {
        $owner = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty OwningProcess
        $cmd = $null
        if ($owner) {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$owner" `
                -ErrorAction SilentlyContinue).CommandLine
        }
        Write-Host "    ERROR: :$ApiPort is occupied, but it is not Fincept API." -ForegroundColor Red
        if ($cmd) {
            Write-Host "    Owner: $cmd" -ForegroundColor Yellow
        }
        Write-Host "    Stop that process or choose another -ApiPort, then rerun ./start.bat." `
            -ForegroundColor Yellow
        exit 1
    }
} else {
    $apiCommand = "uv run --package api uvicorn api.main:app --host 127.0.0.1 --port $ApiPort"
    if ($ReloadApi) {
        $apiCommand = "$apiCommand --reload"
    }
    Start-InNewWindow `
        -Title "fincept-api" `
        -Command $apiCommand
    $ok = Wait-ForHttp -Url "$ApiBaseUrl/health" -TimeoutSec 120 -Label "API"
    if (-not $ok) {
        Write-Host "    API window opened but /health never responded." -ForegroundColor Yellow
        Write-Host "    Check the 'fincept-api' window for a traceback."
        Write-Host "    Dashboard startup is aborted so the UI does not load against an offline API." `
            -ForegroundColor Yellow
        exit 1
    } else {
        Write-Host "    OK  $ApiBaseUrl" -ForegroundColor Green
    }
}

# ---------------------------------------------------------------------
# 4. Trading services (ingestor + features + orchestrator + OMS + portfolio + jobs)
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
    if ($StartMarketData) {
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
    } else {
        Write-Host "    SKIP: market_data (use -WithMarketData or dashboard Start feature)" `
            -ForegroundColor DarkGray
    }

    if ($StartNewsLearning) {
        Write-Host "    spawn: information_enricher" -ForegroundColor DarkGray
        Start-InNewWindow `
            -Title "fincept-information-enricher" `
            -Command "uv run --package agents python -m agents.information_enricher.main"
        $expectedServices.Add("information_enricher")

        Write-Host "    spawn: news_outcome_labeler" -ForegroundColor DarkGray
        Start-InNewWindow `
            -Title "fincept-news-outcome-labeler" `
            -Command "uv run --package agents python -m agents.news_outcome_labeler.main"
        $expectedServices.Add("news_outcome_labeler")
    } else {
        Write-Host "    SKIP: news_learning (use -WithNewsLearning or dashboard Start feature)" `
            -ForegroundColor DarkGray
    }

    # GBM predictor: consumes features, publishes Predictions.  Needs a
    # trained model; opt-in flag because most dev sessions don't have one.
    if ($StartGbm) {
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

    $newsAlphaActivePointer = Join-Path $RepoRoot "models\active\news_alpha_predictor.v1.json"
    $newsAlphaModelDir = if ($env:NEWS_ALPHA_MODEL_DIR) { $env:NEWS_ALPHA_MODEL_DIR } else { Join-Path $RepoRoot "models\news_alpha_predictor" }
    if ((Test-Path $newsAlphaActivePointer) -and -not $env:NEWS_ALPHA_MODEL_DIR) {
        try {
            $newsAlphaPointer = Get-Content $newsAlphaActivePointer -Raw | ConvertFrom-Json
            if ($newsAlphaPointer.model_name) {
                $newsAlphaModelDir = Join-Path (Join-Path $RepoRoot "models") $newsAlphaPointer.model_name
            }
        } catch {}
    }
    if ($StartNewsAlpha -and (Test-Path (Join-Path $newsAlphaModelDir "model.txt"))) {
        Write-Host "    spawn: news_alpha_predictor" -ForegroundColor DarkGray
        Start-InNewWindow `
            -Title "fincept-news-alpha" `
            -Command "uv run --package agents python -m agents.news_alpha_predictor.main"
        $expectedServices.Add("news_alpha_predictor")
    } elseif ($StartNewsAlpha) {
        Write-Host "    SKIP: news_alpha_predictor (no model.txt at $newsAlphaModelDir)" `
            -ForegroundColor Yellow
    } else {
        Write-Host "    SKIP: news_alpha_predictor (use -WithNewsAlpha or dashboard Start feature)" `
            -ForegroundColor DarkGray
    }

    $hasAnthropic = -not [string]::IsNullOrWhiteSpace((Get-FinceptSettingValue -Name "ANTHROPIC_API_KEY"))
    $hasOpenAI = -not [string]::IsNullOrWhiteSpace((Get-FinceptSettingValue -Name "OPENAI_API_KEY"))
    if ($StartSentiment -and ($hasAnthropic -or $hasOpenAI)) {
        Write-Host "    spawn: sentiment_agent" -ForegroundColor DarkGray
        Start-InNewWindow `
            -Title "fincept-sentiment" `
            -Command "uv run --package agents python -m agents.sentiment_agent.main"
        $expectedServices.Add("sentiment_agent")

        Write-Host "    spawn: sentiment_features" -ForegroundColor DarkGray
        Start-InNewWindow `
            -Title "fincept-sentiment-features" `
            -Command "uv run --package agents python -m agents.sentiment_features.main"
        $expectedServices.Add("sentiment_features")
    } elseif ($StartSentiment) {
        Write-Host "    SKIP: sentiment_agent (need ANTHROPIC_API_KEY or OPENAI_API_KEY in .env)" `
            -ForegroundColor Yellow
        Write-Host "    SKIP: sentiment_features (depends on sentiment_agent LLM configuration)" `
            -ForegroundColor Yellow
    } else {
        Write-Host "    SKIP: sentiment (use -WithSentiment or dashboard Start feature)" `
            -ForegroundColor DarkGray
    }

    $hasFred = -not [string]::IsNullOrWhiteSpace((Get-FinceptSettingValue -Name "FRED_API_KEY"))
    if ($StartRegime -and $hasFred) {
        Write-Host "    spawn: regime_agent" -ForegroundColor DarkGray
        Start-InNewWindow `
            -Title "fincept-regime" `
            -Command "uv run --package agents python -m agents.regime_agent.main"
        $expectedServices.Add("regime_agent")
    } elseif ($StartRegime) {
        Write-Host "    SKIP: regime_agent (missing FRED_API_KEY in .env)" `
            -ForegroundColor Yellow
    } else {
        Write-Host "    SKIP: regime_agent (use -WithRegime or dashboard Start feature)" `
            -ForegroundColor DarkGray
    }

    Write-Host "    spawn: strategy_host" -ForegroundColor DarkGray
    Start-InNewWindow `
        -Title "fincept-strategy-host" `
        -Command "uv run --package strategy_host python -m strategy_host.main"
    $expectedServices.Add("strategy_host")

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

    if ($StartJobs) {
        # Jobs: APScheduler for cron tasks (EOD load).
        Write-Host "    spawn: jobs" -ForegroundColor DarkGray
        Start-InNewWindow `
            -Title "fincept-jobs" `
            -Command "uv run --package jobs python -m jobs.main"
        $expectedServices.Add("jobs")
    } else {
        Write-Host "    SKIP: jobs (use -WithJobs or dashboard Start feature)" `
            -ForegroundColor DarkGray
    }

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
# 5. Optional Alpaca sync
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
# 6. Dashboard (Next.js on :3000)
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
# 7. Mint a dev JWT for the login screen
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
Write-Host "  API       : $ApiBaseUrl  (docs: /docs)"
Write-Host "  OpenBB    : $OpenBBBaseUrl"
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
