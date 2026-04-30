<#
.SYNOPSIS
  Stop the Fincept stack (API + Dashboard + trading services).

.DESCRIPTION
  Kills the listeners on :8000 and :3000 (with their child processes)
  and any pwsh windows opened by start.ps1 with titles matching
  "fincept-*" (the trading services: ingestor, features, gbm,
  orchestrator, oms, portfolio, jobs).

  Redis / Memurai is left running because other tools may depend on
  it; pass -IncludeRedis to stop that too.

.PARAMETER IncludeRedis
  Also stop the Memurai Windows service.

.PARAMETER NoServices
  Skip the trading-service window cleanup (only kill API + Dashboard).

.EXAMPLE
  ./scripts/stop.ps1
  ./scripts/stop.ps1 -IncludeRedis
  ./scripts/stop.ps1 -NoServices
#>
[CmdletBinding()]
param(
    [switch]$IncludeRedis,
    [switch]$NoServices
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

function Write-Step {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host "==> $Message" -ForegroundColor $Color
}

function Stop-Port {
    param(
        [int]$Port,
        [string]$Label
    )

    $conns = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Host "    :$Port  nothing listening" -ForegroundColor DarkGray
        return
    }

    $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $pids) {
        if (-not $procId -or $procId -eq 0) { continue }
        try {
            $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
            $name = if ($p) { $p.ProcessName } else { "?" }
            Write-Host "    :$Port  killing PID $procId ($name)"

            # Kill children first (webpack workers, reloader's child worker, etc.)
            Get-CimInstance Win32_Process -Filter "ParentProcessId=$procId" `
                -ErrorAction SilentlyContinue | ForEach-Object {
                try {
                    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                } catch {}
            }

            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        } catch {
            Write-Host "    :$Port  failed to stop PID ${procId}: $_" -ForegroundColor Yellow
        }
    }

    # Verify.
    Start-Sleep -Milliseconds 400
    $still = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    if ($still) {
        Write-Host "    :$Port  WARN some listeners remain" -ForegroundColor Yellow
    } else {
        Write-Host "    :$Port  clear" -ForegroundColor Green
    }
}

function Stop-FinceptServiceWindows {
    # Trading services run in pwsh windows with title "fincept-{name}".
    # They don't bind TCP ports - communication is via Redis Streams - so
    # we find them by MainWindowTitle and kill the process tree.
    $titles = @(
        "fincept-ingestor",
        "fincept-features",
        "fincept-gbm",
        "fincept-sentiment",
        "fincept-regime",
        "fincept-orchestrator",
        "fincept-oms",
        "fincept-portfolio",
        "fincept-jobs"
    )
    $procs = Get-Process -Name "pwsh" -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -in $titles }
    if (-not $procs) {
        Write-Host "    no service windows found" -ForegroundColor DarkGray
        return
    }
    foreach ($p in $procs) {
        Write-Host "    killing PID $($p.Id) ($($p.MainWindowTitle))"
        # Kill children (uv-spawned python).
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
}

if (-not $NoServices) {
    Write-Step "Trading services"
    Stop-FinceptServiceWindows
}

Write-Step "Dashboard (:3000)"
Stop-Port -Port 3000 -Label "dashboard"

Write-Step "API (:8000)"
Stop-Port -Port 8000 -Label "api"

if ($IncludeRedis) {
    Write-Step "Redis / Memurai (:6379)"
    $svc = Get-Service -Name Memurai -ErrorAction SilentlyContinue
    if ($null -ne $svc -and $svc.Status -eq 'Running') {
        Stop-Service -Name Memurai -Force -ErrorAction SilentlyContinue
        Write-Host "    service stopped" -ForegroundColor Green
    } else {
        Stop-Port -Port 6379 -Label "redis"
    }
}

Write-Host ""
Write-Host "Fincept stopped." -ForegroundColor Green
