[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("market_data", "news_learning", "jobs", "gbm_predictor", "news_alpha_predictor", "sentiment", "regime", "openbb")]
    [string]$FeatureId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$FeatureWindows = @{
    market_data = @("fincept-ingestor", "fincept-features")
    news_learning = @("fincept-information-enricher", "fincept-news-outcome-labeler")
    jobs = @("fincept-jobs")
    gbm_predictor = @("fincept-gbm")
    news_alpha_predictor = @("fincept-news-alpha")
    sentiment = @("fincept-sentiment", "fincept-sentiment-features")
    regime = @("fincept-regime")
    openbb = @("fincept-openbb")
}

$titles = $FeatureWindows[$FeatureId]
if (-not $titles -or $titles.Count -eq 0) {
    throw "unknown feature: $FeatureId"
}

$procs = Get-Process -Name "pwsh" -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle -in $titles }

if (-not $procs) {
    Write-Host "feature already stopped: $FeatureId"
    exit 0
}

foreach ($p in $procs) {
    Write-Host "stopping $($p.MainWindowTitle) pid=$($p.Id)"
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$($p.Id)" `
        -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {}
    }
    try {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    } catch {}
}

Start-Sleep -Milliseconds 400
Write-Host "feature stop requested: $FeatureId"
