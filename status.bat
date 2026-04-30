@echo off
REM ---------------------------------------------------------------------
REM  Fincept Terminal - status snapshot.
REM
REM  Shows whether Redis / API / Dashboard are running, which PID owns
REM  each port, and how many positions are stored per strategy.
REM ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title Fincept - status
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\status.ps1" %*
echo.
echo Press any key to close this window...
pause >nul
endlocal
