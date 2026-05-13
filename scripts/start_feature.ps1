[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("market_data", "news_learning", "jobs", "gbm_predictor", "news_alpha_predictor", "sentiment", "regime", "openbb")]
    [string]$FeatureId,
    [int]$SpawnDelayMs = 250
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

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
        $resp = Invoke-WebRequest -Uri "$BaseUrl/openapi.json" -NoProxy -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300)
    } catch {
        return $false
    }
}

function Start-OpenBB {
    $baseUrl = Get-OpenBBApiUrl
    if (Test-OpenBBApi -BaseUrl $baseUrl) {
        Write-Host "openbb already running at $baseUrl"
        return
    }
    try {
        $uri = [Uri]$baseUrl
    } catch {
        throw "OPENBB_API_URL is not valid: $baseUrl"
    }
    $hostName = if ($uri.Host) { $uri.Host } else { "127.0.0.1" }
    $port = if ($uri.Port -gt 0) { $uri.Port } else { 6900 }
    if (Test-TcpPort -ComputerName $hostName -Port $port) {
        Write-Host "port $port is already listening"
        return
    }
    $openbbCommand = Get-OpenBBApiCommand
    if (-not $openbbCommand) {
        throw "openbb-api command not found"
    }
    Start-InNewWindow -Title "fincept-openbb" -Command "& `"$openbbCommand`" --host $hostName --port $port"
    Write-Host "openbb launch requested"
}

function Start-NewsAlphaPredictor {
    $activePointer = Join-Path $RepoRoot "models\active\news_alpha_predictor.v1.json"
    $modelDir = if ($env:NEWS_ALPHA_MODEL_DIR) { $env:NEWS_ALPHA_MODEL_DIR } else { Join-Path $RepoRoot "models\news_alpha_predictor" }
    if ((Test-Path $activePointer) -and -not $env:NEWS_ALPHA_MODEL_DIR) {
        try {
            $pointer = Get-Content $activePointer -Raw | ConvertFrom-Json
            if ($pointer.model_name) {
                $modelDir = Join-Path (Join-Path $RepoRoot "models") $pointer.model_name
            }
        } catch {}
    }
    if (-not (Test-Path (Join-Path $modelDir "model.txt"))) {
        throw "news_alpha_predictor model.txt not found at $modelDir"
    }
    Start-InNewWindow -Title "fincept-news-alpha" -Command "uv run --package agents python -m agents.news_alpha_predictor.main"
}

switch ($FeatureId) {
    "market_data" {
        $venue = if ($env:FINCEPT_INGESTOR_VENUE) { $env:FINCEPT_INGESTOR_VENUE } else { "coinbase" }
        Start-InNewWindow -Title "fincept-ingestor" -Command "uv run --package ingestor python -m ingestor.main --venue $venue"
        Start-InNewWindow -Title "fincept-features" -Command "uv run --package features python -m features.main"
    }
    "news_learning" {
        Start-InNewWindow -Title "fincept-information-enricher" -Command "uv run --package agents python -m agents.information_enricher.main"
        Start-InNewWindow -Title "fincept-news-outcome-labeler" -Command "uv run --package agents python -m agents.news_outcome_labeler.main"
    }
    "jobs" {
        Start-InNewWindow -Title "fincept-jobs" -Command "uv run --package jobs python -m jobs.main"
    }
    "gbm_predictor" {
        $modelDir = Join-Path $RepoRoot "models\gbm_predictor"
        if (-not (Test-Path (Join-Path $modelDir "model.txt"))) {
            throw "gbm_predictor model.txt not found at $modelDir"
        }
        Start-InNewWindow -Title "fincept-gbm" -Command "uv run --package agents python -m agents.gbm_predictor.main"
    }
    "news_alpha_predictor" {
        Start-NewsAlphaPredictor
    }
    "sentiment" {
        $hasAnthropic = -not [string]::IsNullOrWhiteSpace((Get-FinceptSettingValue -Name "ANTHROPIC_API_KEY"))
        $hasOpenAI = -not [string]::IsNullOrWhiteSpace((Get-FinceptSettingValue -Name "OPENAI_API_KEY"))
        if (-not ($hasAnthropic -or $hasOpenAI)) {
            throw "sentiment requires ANTHROPIC_API_KEY or OPENAI_API_KEY"
        }
        Start-InNewWindow -Title "fincept-sentiment" -Command "uv run --package agents python -m agents.sentiment_agent.main"
        Start-InNewWindow -Title "fincept-sentiment-features" -Command "uv run --package agents python -m agents.sentiment_features.main"
    }
    "regime" {
        $hasFred = -not [string]::IsNullOrWhiteSpace((Get-FinceptSettingValue -Name "FRED_API_KEY"))
        if (-not $hasFred) {
            throw "regime requires FRED_API_KEY"
        }
        Start-InNewWindow -Title "fincept-regime" -Command "uv run --package agents python -m agents.regime_agent.main"
    }
    "openbb" {
        Start-OpenBB
    }
}

Write-Host "feature launch requested: $FeatureId"
