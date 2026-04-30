<#
.SYNOPSIS
  Show the current state of the Fincept stack.

.DESCRIPTION
  Probes Redis (:6379), API (:8000 + /health), and Dashboard (:3000),
  and reports the process id, owning executable, and health status of
  each in a single terminal-friendly table.  Also reports the number
  of positions stored per strategy in Redis.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

function Get-PortOwner {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    $p = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    [pscustomobject]@{
        Pid     = $conn.OwningProcess
        Name    = if ($p) { $p.ProcessName } else { "?" }
        Address = $conn.LocalAddress
    }
}

function Test-HttpOk {
    param([string]$Url, [int]$TimeoutSec = 2)
    try {
        $r = Invoke-WebRequest -Uri $Url -NoProxy -UseBasicParsing `
            -TimeoutSec $TimeoutSec -ErrorAction Stop
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Show-Service {
    param(
        [string]$Name,
        [int]$Port,
        [string]$HealthUrl = $null
    )
    $owner = Get-PortOwner -Port $Port
    if (-not $owner) {
        "{0,-12} :{1,-5} {2,-8} {3}" -f $Name, $Port, "DOWN", "-" |
            ForEach-Object { Write-Host $_ -ForegroundColor Red }
        return
    }

    $healthy = $true
    if ($HealthUrl) { $healthy = Test-HttpOk -Url $HealthUrl }

    $state = if ($healthy) { "UP" } else { "LISTENING" }
    $color = if ($healthy) { "Green" } else { "Yellow" }
    "{0,-12} :{1,-5} {2,-8} PID {3} ({4})" -f $Name, $Port, $state, $owner.Pid, $owner.Name |
        ForEach-Object { Write-Host $_ -ForegroundColor $color }
}

Write-Host ""
Write-Host "Fincept stack status" -ForegroundColor Cyan
Write-Host "--------------------" -ForegroundColor DarkGray

Show-Service -Name "Redis"     -Port 6379
Show-Service -Name "API"       -Port 8000 -HealthUrl "http://127.0.0.1:8000/health"
Show-Service -Name "Dashboard" -Port 3000 -HealthUrl "http://127.0.0.1:3000"

# Positions snapshot from Redis (requires python + redis client).
try {
    $py = @'
from fincept_core.config import get_settings
from redis import Redis
r = Redis.from_url(get_settings().REDIS_URL)
strategies = sorted(s.decode() for s in r.smembers("portfolio:strategies"))
if not strategies:
    print("no strategies")
else:
    for sid in strategies:
        n = r.hlen(f"positions:{sid}")
        print(f"{sid:30s} {n:>4d} positions")
'@
    Write-Host ""
    Write-Host "Positions (Redis)" -ForegroundColor Cyan
    Write-Host "-----------------" -ForegroundColor DarkGray
    uv run python -c $py 2>$null
} catch {
    # Silent — non-critical.
}

Write-Host ""
