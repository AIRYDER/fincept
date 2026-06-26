<#
.SYNOPSIS
  Lightweight verification receipt runner for Fincept Terminal.

.DESCRIPTION
  Runs the safe, focused checks that every task should leave green, and
  writes a timestamped Markdown + JSON receipt under reports/verification/.
  Heavy checks that need operator environment (Docker Compose, browser
  smoke, live provider/broker/RunPod) are recorded as explicitly skipped
  with a reason, never silently dropped.

  This is the receipt harness referenced by AAAAAAAAAA_BIG_PLAN.md
  TASK-0101.  It intentionally does NOT run preflight.ps1's heavy steps
  (docker compose, mypy, full pytest with coverage, builds) so it stays
  fast and safe to run before any PR.

  Exit code is non-zero if any REQUIRED check fails.  Skipped checks do
  not fail the run.

.PARAMETER OutDir
  Override the receipt output directory (default reports/verification).

.PARAMETER SkipDashboard
  Skip the dashboard focused checks (useful when node/pnpm unavailable).

.PARAMETER SkipPython
  Skip the Python focused checks (useful when uv unavailable).

.EXAMPLE
  pwsh ./scripts/verification-receipt.ps1
#>

[CmdletBinding()]
param(
    [string]$OutDir = "",
    [switch]$SkipDashboard,
    [switch]$SkipPython
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"  # capture per-step exit codes instead of aborting

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if ([string]::IsNullOrEmpty($OutDir)) {
    $OutDir = Join-Path $RepoRoot "reports" "verification"
}
if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

$StartedAt = Get-Date
$Stamp = $StartedAt.ToString("yyyyMMddTHHmmss")
$MdPath = Join-Path $OutDir "receipt-$Stamp.md"
$JsonPath = Join-Path $OutDir "receipt-$Stamp.json"

# Results collected as ordered hashtables; converted to JSON at the end.
$Results = [System.Collections.Generic.List[hashtable]]::new()
$RequiredFailures = 0

function Add-Result {
    param(
        [string]$Name,
        [string]$Command,
        [string]$Status,      # pass | fail | skipped
        [int]$ExitCode,
        [double]$DurationMs,
        [string]$Reason = "",
        [bool]$Required = $true
    )
    $Results.Add(@{
        name = $Name
        command = $Command
        status = $Status
        exit_code = $ExitCode
        duration_ms = [int]$DurationMs
        reason = $Reason
        required = $Required
    })
    if ($Status -eq "fail" -and $Required) {
        $script:RequiredFailures += 1
    }
}

function Invoke-Check {
    param(
        [string]$Name,
        [string]$Command,        # display string
        [scriptblock]$Block,
        [bool]$Required = $true
    )
    Write-Host "==> $Name" -ForegroundColor Cyan
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $output = (& $Block 2>&1 | Out-String)
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    $sw.Stop()
    $status = if ($code -eq 0) { "pass" } else { "fail" }
    $color = if ($status -eq "pass") { "Green" } else { "Red" }
    Write-Host "   -> $status (exit $code, $($sw.ElapsedMilliseconds)ms)" -ForegroundColor $color
    Add-Result -Name $Name -Command $Command -Status $status -ExitCode $code `
        -DurationMs $sw.ElapsedMilliseconds -Required $Required
}

function Add-Skipped {
    param([string]$Name, [string]$Reason)
    Write-Host "==> $Name [skipped]" -ForegroundColor DarkYellow
    Write-Host "   -> reason: $Reason" -ForegroundColor DarkGray
    Add-Result -Name $Name -Command "" -Status "skipped" -ExitCode 0 `
        -DurationMs 0 -Reason $Reason -Required $false
}

# --------------------------------------------------------------------------- #
# Required dashboard focused checks
# --------------------------------------------------------------------------- #
if ($SkipDashboard) {
    Add-Skipped "dashboard:shadow-news-impact" "-SkipDashboard"
    Add-Skipped "dashboard:source-health" "-SkipDashboard"
    Add-Skipped "dashboard:strategy-readiness" "-SkipDashboard"
    Add-Skipped "dashboard:tsc-noEmit" "-SkipDashboard"
} else {
    Invoke-Check "dashboard:shadow-news-impact" `
        "npm run test:shadow-news-impact" `
        { Set-Location (Join-Path $RepoRoot "apps" "dashboard"); npm run test:shadow-news-impact; Set-Location $RepoRoot }
    Invoke-Check "dashboard:source-health" `
        "npm run test:source-health" `
        { Set-Location (Join-Path $RepoRoot "apps" "dashboard"); npm run test:source-health; Set-Location $RepoRoot }
    Invoke-Check "dashboard:strategy-readiness" `
        "npm run test:strategy-readiness" `
        { Set-Location (Join-Path $RepoRoot "apps" "dashboard"); npm run test:strategy-readiness; Set-Location $RepoRoot }
    Invoke-Check "dashboard:tsc-noEmit" `
        "pnpm --dir apps/dashboard exec tsc --noEmit --pretty false" `
        { pnpm --dir apps/dashboard exec tsc --noEmit --pretty false }
}

# --------------------------------------------------------------------------- #
# Required Python focused checks
# --------------------------------------------------------------------------- #
if ($SkipPython) {
    Add-Skipped "python:news-impact-api" "-SkipPython"
    Add-Skipped "python:core-lib" "-SkipPython"
} else {
    Invoke-Check "python:news-impact-api" `
        "uv run pytest services/api/tests/test_news_impact.py -q" `
        { uv run pytest services/api/tests/test_news_impact.py -q }
    Invoke-Check "python:core-lib" `
        "uv run pytest libs/fincept-core/tests -q" `
        { uv run pytest libs/fincept-core/tests -q }
}

# --------------------------------------------------------------------------- #
# Explicitly skipped heavy checks (with reasons)
# --------------------------------------------------------------------------- #
Add-Skipped "docker:compose-boot" "needs operator environment + Docker daemon"
Add-Skipped "browser:smoke" "needs Playwright session + running dashboard"
Add-Skipped "provider:live-checks" "needs provider API keys; never run by default"
Add-Skipped "broker:checks" "needs broker credentials; never run by default"
Add-Skipped "runpod:checks" "RunPod work not yet implemented (later phases)"
Add-Skipped "ci:mypy-full" "heavy; run in preflight.ps1 or CI, not the light receipt"
Add-Skipped "ci:full-pytest-cov" "heavy; run in preflight.ps1 or CI, not the light receipt"

$EndedAt = Get-Date
$DurationS = [math]::Round(($EndedAt - $StartedAt).TotalSeconds, 2)

# --------------------------------------------------------------------------- #
# Write receipts
# --------------------------------------------------------------------------- #
$passCount = @($Results | Where-Object { $_.status -eq "pass" }).Count
$failCount = @($Results | Where-Object { $_.status -eq "fail" }).Count
$skipCount = @($Results | Where-Object { $_.status -eq "skipped" }).Count
$overall = if ($RequiredFailures -eq 0) { "PASS" } else { "FAIL" }

$md = New-Object System.Text.StringBuilder
[void]$md.AppendLine("# Verification Receipt — $($StartedAt.ToString('o'))")
[void]$md.AppendLine("")
[void]$md.AppendLine("> Generated by ``scripts/verification-receipt.ps1`` (TASK-0101).")
[void]$md.AppendLine("> Receipts never include secrets, tokens, or credentials.")
[void]$md.AppendLine("")
[void]$md.AppendLine("## Summary")
[void]$md.AppendLine("")
[void]$md.AppendLine("- **Overall:** $overall")
[void]$md.AppendLine("- **Started:** $($StartedAt.ToString('o'))")
[void]$md.AppendLine("- **Duration:** ${DurationS}s")
[void]$md.AppendLine("- **Pass:** $passCount  **Fail:** $failCount  **Skipped:** $skipCount")
[void]$md.AppendLine("- **Required failures:** $RequiredFailures")
[void]$md.AppendLine("")
[void]$md.AppendLine("## Checks")
[void]$md.AppendLine("")
[void]$md.AppendLine("| Name | Command | Status | Exit | Duration (ms) | Required | Reason |")
[void]$md.AppendLine("|---|---|---|---|---|---|---|")
foreach ($r in $Results) {
    $req = if ($r.required) { "yes" } else { "no" }
    $reason = if ([string]::IsNullOrEmpty($r.reason)) { "" } else { $r.reason }
    [void]$md.AppendLine("| $($r.name) | $($r.command) | $($r.status) | $($r.exit_code) | $($r.duration_ms) | $req | $reason |")
}
[void]$md.AppendLine("")
[void]$md.AppendLine("## Skipped checks (explicit)")
[void]$md.AppendLine("")
foreach ($r in @($Results | Where-Object { $_.status -eq "skipped" })) {
    [void]$md.AppendLine("- **$($r.name):** $($r.reason)")
}
[void]$md.AppendLine("")

[System.IO.File]::WriteAllText($MdPath, $md.ToString())

$receipt = [ordered]@{
    schema = "fincept.verification-receipt/v1"
    started_at = $StartedAt.ToString("o")
    ended_at = $EndedAt.ToString("o")
    duration_s = $DurationS
    overall = $overall
    summary = [ordered]@{
        pass = $passCount
        fail = $failCount
        skipped = $skipCount
        required_failures = $RequiredFailures
    }
    checks = $Results
}
$receipt | ConvertTo-Json -Depth 6 | Out-File -FilePath $JsonPath -Encoding utf8

Write-Host ""
Write-Host "Receipt written:" -ForegroundColor Green
Write-Host "  $MdPath" -ForegroundColor DarkGray
Write-Host "  $JsonPath" -ForegroundColor DarkGray
Write-Host "Overall: $overall (pass=$passCount fail=$failCount skipped=$skipCount)" -ForegroundColor $(if ($overall -eq "PASS") { "Green" } else { "Red" })

if ($RequiredFailures -gt 0) {
    exit 1
}
exit 0
