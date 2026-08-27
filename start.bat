@echo off
REM ---------------------------------------------------------------------
REM  YinTu Medical Patent Agent - double-click launcher.
REM  Runs start.ps1 with ExecutionPolicy Bypass so that the default
REM  Windows "scripts are disabled on this system" error cannot happen.
REM  All user-facing text (in Chinese) is printed by start.ps1 itself.
REM  Extra arguments are forwarded, e.g.:  start.bat -Port 8080
REM ---------------------------------------------------------------------
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
  echo.
  echo Startup failed. See the messages above.
  pause
)
