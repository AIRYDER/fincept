@echo off
REM ---------------------------------------------------------------------
REM  Fincept Terminal - one-click start.
REM
REM  Launches Redis (if needed), API (:8000), Dashboard (:3000), and
REM  re-syncs Alpaca positions into Redis.  Pass any extra PowerShell
REM  flags after the double-click (e.g. "start.bat -NoDashboard").
REM ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title Fincept - start
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" -Sync %*
echo.
echo Press any key to close this window...
pause >nul
endlocal
