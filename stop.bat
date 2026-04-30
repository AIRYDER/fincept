@echo off
REM ---------------------------------------------------------------------
REM  Fincept Terminal - one-click stop.
REM
REM  Terminates whatever's on :8000 and :3000 (and their children).
REM  Pass "-IncludeRedis" to also stop Memurai.
REM ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title Fincept - stop
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1" %*
echo.
echo Press any key to close this window...
pause >nul
endlocal
