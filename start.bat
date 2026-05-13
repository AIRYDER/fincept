@echo off
REM ---------------------------------------------------------------------
REM  Fincept Terminal - one-click start.
REM
REM  Launches the lean core setup: Redis (if needed), API (:8010),
REM  Dashboard (:3000), strategy host, orchestrator, OMS, portfolio, and
REM  re-syncs Alpaca positions into Redis. Optional heavier lanes are opt-in
REM  via PowerShell flags (e.g. "start.bat -WithMarketData", "-WithGbm",
REM  "-WithOpenBB", or "-Full"). Pass any extra flags after start.bat.
REM ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title Fincept - start
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" -Sync %*
if not "%~1"=="" goto end
echo.
echo Press any key to close this window...
pause >nul
:end
endlocal
